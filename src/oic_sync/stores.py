"""Where `oic-sync mirror` writes: a local directory or an S3 bucket.

Both stores hold the same layout, addressed by relative keys ("debian-13/latest.json"). A
qcow2 is always downloaded to a local staging file first, verified there, then committed:
renamed into place in a directory, uploaded with its digests as object metadata in a bucket.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import logging
import os
import shutil
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from oic_sync.hashing import HashMismatch
from oic_sync.index import Artifact

log = logging.getLogger(__name__)

CHUNK = 1024 * 1024
LOCK_NAME = ".oic-sync-mirror.lock"
# An S3 lock older than this is left over by a run that died; it is taken over.
S3_LOCK_STALE = timedelta(hours=12)


class MirrorError(Exception):
    """The mirror cannot run at all (destination not usable, another run holds the lock)."""


@dataclass
class Digests:
    size: int = 0
    sha256: Any = field(default_factory=hashlib.sha256)
    sha512: Any = field(default_factory=hashlib.sha512)

    def update(self, data: bytes) -> None:
        self.size += len(data)
        self.sha256.update(data)
        self.sha512.update(data)

    def check(self, artifact: Artifact) -> None:
        if artifact.size is not None and self.size != artifact.size:
            raise HashMismatch(f"size mismatch: got {self.size} bytes, index says {artifact.size}")
        got = self.sha256.hexdigest()
        if got != artifact.sha256:
            raise HashMismatch(f"sha256 mismatch: computed {got}, index says {artifact.sha256}")
        if artifact.sha512 is not None and self.sha512.hexdigest() != artifact.sha512:
            raise HashMismatch("sha512 mismatch with the index")


def hash_file(path: Path, digests: Digests | None = None) -> Digests:
    digests = digests or Digests()
    with path.open("rb") as f:
        while block := f.read(CHUNK):
            digests.update(block)
    return digests


class Store(Protocol):
    def describe(self) -> str: ...
    def check(self) -> None: ...
    def prepare(self) -> None: ...
    def lock(self) -> contextlib.AbstractContextManager[None]: ...
    def read(self, key: str) -> bytes | None: ...
    def write(self, key: str, data: bytes) -> bool: ...
    def has_file(self, key: str) -> bool: ...
    def has_dir(self, prefix: str) -> bool: ...
    def present(self, key: str, artifact: Artifact) -> bool: ...
    def staging(self, key: str) -> Path: ...
    def commit(self, staged: Path, key: str, artifact: Artifact) -> None: ...
    def remove_dir(self, prefix: str) -> None: ...


def _write_atomic(path: Path, data: bytes) -> bool:
    """Write data to path through a temporary file; return False when it was already there."""
    with contextlib.suppress(OSError):
        if path.read_bytes() == data:
            return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    with tmp.open("wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return True


class LocalStore:
    """A directory, served by any web server."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def describe(self) -> str:
        return str(self.root)

    def check(self) -> None:
        if self.root.exists() and not self.root.is_dir():
            raise MirrorError(f"{self.root} is not a directory")

    def prepare(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    @contextlib.contextmanager
    def lock(self) -> Iterator[None]:
        with (self.root / LOCK_NAME).open("a") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as e:
                raise MirrorError(f"another oic-sync mirror is running on {self.root}") from e
            yield

    def read(self, key: str) -> bytes | None:
        try:
            return (self.root / key).read_bytes()
        except OSError:
            return None

    def write(self, key: str, data: bytes) -> bool:
        return _write_atomic(self.root / key, data)

    def has_file(self, key: str) -> bool:
        return (self.root / key).is_file()

    def has_dir(self, prefix: str) -> bool:
        return (self.root / prefix).is_dir()

    def present(self, key: str, artifact: Artifact) -> bool:
        path = self.root / key
        if not path.is_file():
            return False
        if artifact.size is not None and path.stat().st_size != artifact.size:
            return False
        try:
            hash_file(path).check(artifact)
        except HashMismatch:
            return False
        return True

    def staging(self, key: str) -> Path:
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        return path.with_name(f".{path.name}.part")

    def commit(self, staged: Path, key: str, artifact: Artifact) -> None:
        os.replace(staged, self.root / key)

    def remove_dir(self, prefix: str) -> None:
        shutil.rmtree(self.root / prefix)


# Cache headers of the CDN: aliases and listings move, version directories never change.
_SHORT_CACHE = "public, max-age=300"
_IMMUTABLE = "public, max-age=31536000, immutable"


def _content_type(key: str) -> str:
    if key.endswith(".json"):
        return "application/json"
    if key.endswith(".html"):
        return "text/html; charset=utf-8"
    if key.endswith(".qcow2"):
        return "application/octet-stream"
    return "text/plain; charset=utf-8"


def _cache_control(key: str) -> str:
    # <image>/<version>/<file>: immutable; index.json, <image>/latest.json, versions.json: short.
    return _IMMUTABLE if key.count("/") >= 2 else _SHORT_CACHE


def _error_code(exc: Exception) -> str:
    return str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))


class S3Store:
    """An S3 bucket (AWS or any S3-compatible object storage), served by the bucket itself.

    Credentials and endpoint come from the usual AWS settings (environment, ~/.aws/config,
    AWS_ENDPOINT_URL). The digests of each qcow2 are stored as object metadata at upload and
    compared on later runs, so present files are not downloaded back to be hashed.
    """

    def __init__(
        self,
        url: str,
        *,
        client: Any = None,
        endpoint_url: str | None = None,
        staging_dir: Path | None = None,
        now: Any = None,
    ) -> None:
        parts = urlsplit(url)
        if parts.scheme != "s3" or not parts.netloc:
            raise MirrorError(f"invalid S3 destination {url!r}, expected s3://bucket[/prefix]")
        self.url = url.rstrip("/")
        self.bucket = parts.netloc
        self.prefix = parts.path.strip("/")
        if client is None:
            try:
                import boto3
            except ImportError as e:
                raise MirrorError(
                    "an S3 destination needs boto3: pip install 'oic-sync[s3]'"
                ) from e
            client = boto3.client("s3", endpoint_url=endpoint_url)
        self.client = client
        digest = hashlib.sha256(self.url.encode()).hexdigest()[:16]
        self.staging_root = staging_dir or Path(tempfile.gettempdir()) / f"oic-sync-mirror-{digest}"
        self.now = now or (lambda: datetime.now(timezone.utc))

    def _key(self, key: str) -> str:
        return f"{self.prefix}/{key}" if self.prefix else key

    def describe(self) -> str:
        return self.url

    def check(self) -> None:
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except Exception as e:
            raise MirrorError(f"bucket {self.bucket} is not reachable: {e}") from e

    def prepare(self) -> None:
        self.staging_root.mkdir(parents=True, exist_ok=True)

    @contextlib.contextmanager
    def lock(self) -> Iterator[None]:
        """Lock object created only if absent (conditional write); stale locks are taken over."""
        key = self._key(LOCK_NAME)
        body = json.dumps({"locked_at": self.now().strftime("%Y-%m-%dT%H:%M:%SZ")}).encode()
        try:
            self.client.put_object(Bucket=self.bucket, Key=key, Body=body, IfNoneMatch="*")
        except Exception as e:
            code = _error_code(e)
            if code in ("PreconditionFailed", "ConditionalRequestConflict"):
                head = self._head(LOCK_NAME)
            elif code in ("NotImplemented", "InvalidArgument", "InvalidRequest", "501"):
                # No conditional writes on this storage: check, then write (best effort).
                log.warning("%s: no conditional writes, the lock is best effort", self.url)
                head = self._head(LOCK_NAME)
            else:
                raise MirrorError(f"cannot create the lock {self.url}/{LOCK_NAME}: {e}") from e
            if head is not None and self.now() - head["LastModified"] < S3_LOCK_STALE:
                raise MirrorError(f"another oic-sync mirror is running on {self.url}") from e
            self.client.put_object(Bucket=self.bucket, Key=key, Body=body)
        try:
            yield
        finally:
            self.client.delete_object(Bucket=self.bucket, Key=key)

    def read(self, key: str) -> bytes | None:
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=self._key(key))
        except Exception as e:
            if _error_code(e) in ("NoSuchKey", "404"):
                return None
            raise
        return response["Body"].read()

    def write(self, key: str, data: bytes) -> bool:
        if self.read(key) == data:
            return False
        self.client.put_object(
            Bucket=self.bucket,
            Key=self._key(key),
            Body=data,
            ContentType=_content_type(key),
            CacheControl=_cache_control(key),
        )
        return True

    def _head(self, key: str) -> dict[str, Any] | None:
        try:
            return self.client.head_object(Bucket=self.bucket, Key=self._key(key))
        except Exception as e:
            if _error_code(e) in ("NoSuchKey", "404", "NotFound"):
                return None
            raise

    def has_file(self, key: str) -> bool:
        return self._head(key) is not None

    def has_dir(self, prefix: str) -> bool:
        listing = self.client.list_objects_v2(
            Bucket=self.bucket, Prefix=self._key(prefix) + "/", MaxKeys=1
        )
        return bool(listing.get("Contents"))

    def present(self, key: str, artifact: Artifact) -> bool:
        head = self._head(key)
        if head is None:
            return False
        if artifact.size is not None and head.get("ContentLength") != artifact.size:
            return False
        meta = head.get("Metadata") or {}
        if meta.get("sha256") != artifact.sha256:
            return False
        return artifact.sha512 is None or meta.get("sha512") == artifact.sha512

    def staging(self, key: str) -> Path:
        path = self.staging_root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        return path.with_name(f".{path.name}.part")

    def commit(self, staged: Path, key: str, artifact: Artifact) -> None:
        metadata = {"sha256": artifact.sha256}
        if artifact.sha512:
            metadata["sha512"] = artifact.sha512
        self.client.upload_file(
            str(staged),
            self.bucket,
            self._key(key),
            ExtraArgs={
                "ContentType": _content_type(key),
                "CacheControl": _cache_control(key),
                "Metadata": metadata,
            },
        )
        staged.unlink(missing_ok=True)
        for parent in staged.parents:  # drop the emptied staging directories
            if parent == self.staging_root or self.staging_root not in parent.parents:
                break
            try:
                parent.rmdir()
            except OSError:
                break

    def remove_dir(self, prefix: str) -> None:
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=self._key(prefix) + "/"):
            keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
            if keys:
                self.client.delete_objects(Bucket=self.bucket, Delete={"Objects": keys})


def open_store(
    dest: str | Path,
    *,
    endpoint_url: str | None = None,
    staging_dir: Path | None = None,
) -> Store:
    if isinstance(dest, str) and dest.startswith("s3://"):
        return S3Store(dest, endpoint_url=endpoint_url, staging_dir=staging_dir)
    return LocalStore(Path(dest))
