"""Mirror the signed catalog to a local directory.

The output reproduces the CDN layout, so that serving the directory over HTTP gives a drop-in
replacement for images.openimages.cloud:

    <dest>/index.json                       rewritten: URLs point to the mirror base URL
    <dest>/<image>/latest.json
    <dest>/<image>/versions.json            only the versions present in the mirror
    <dest>/<image>/<version>/<file>.qcow2   plus .bundle, .sha256..., MANIFEST*.json, SHA256SUMS,
                                            index.html when the CDN has them

Every qcow2 is streamed to a hidden temporary file, checked (size, sha256, sha512 when known
and the cosign bundle against the sha256) and only then renamed into place. Files already
present with the right digests are kept, and an interrupted download resumes from its
temporary file. Signatures and bundles are copied untouched: they cover the digests, not the
URLs, so clients verify the mirror exactly as they verify the CDN.
"""

from __future__ import annotations

import contextlib
import copy
import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx

from oic_sync.hashing import HashMismatch
from oic_sync.index import Artifact, CatalogIndex, Image, Target
from oic_sync.signature import MAX_BUNDLE_BYTES, SignatureError

log = logging.getLogger(__name__)

CHUNK = 1024 * 1024
# Sidecars (MANIFEST, SHA256SUMS, index.html, checksum files) are a few KiB.
MAX_SIDECAR_BYTES = 4 * 1024 * 1024
MAX_VERSIONS_BYTES = 16 * 1024 * 1024
# Checksum files published next to each qcow2 on the CDN (missing ones are skipped).
CHECKSUM_SUFFIXES = (".sha256", ".sha512", ".sha1", ".md5")
VERSION_SIDECARS = ("SHA256SUMS", "index.html")
LOCK_NAME = ".oic-sync-mirror.lock"

# Image ids, versions and file names become path components: no separators, no dot files.
_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")


class MirrorError(Exception):
    """The mirror cannot run at all (destination not usable, another run holds the lock)."""


class _BundleVerifier(Protocol):
    def verify_bundle(self, artifact: Artifact, raw: bytes) -> str: ...


def _safe(value: str, what: str) -> str:
    if not _SAFE.match(value) or ".." in value:
        raise MirrorError(f"unsafe {what} {value!r}")
    return value


def _basename(url: str) -> str:
    return urlsplit(url).path.rsplit("/", 1)[-1]


def _part(path: Path) -> Path:
    return path.with_name(f".{path.name}.part")


def _dirname(url: str) -> str:
    return url.rsplit("/", 1)[0]


@dataclass
class _File:
    """One qcow2 of one version, with its upstream URLs."""

    artifact: Artifact
    filename: str
    manifest: str | None
    entry: dict[str, Any]  # versions.json artifact entry


@dataclass
class _Version:
    version: str
    built_at: str | None
    files: list[_File]


@dataclass
class ImageResult:
    image_id: str
    ok: bool = True
    entry: dict[str, Any] | None = None  # rewritten index entry, when the latest is mirrored
    messages: list[str] = field(default_factory=list)


@dataclass
class _Digests:
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


def _hash_file(path: Path, digests: _Digests | None = None) -> _Digests:
    digests = digests or _Digests()
    with path.open("rb") as f:
        while block := f.read(CHUNK):
            digests.update(block)
    return digests


def _write_atomic(path: Path, data: bytes) -> bool:
    """Write data to path through a temporary file; return False when it was already there."""
    with contextlib.suppress(OSError):
        if path.read_bytes() == data:
            return False
    tmp = path.with_name(f".{path.name}.tmp")
    with tmp.open("wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return True


def _json_bytes(data: Any) -> bytes:
    return (json.dumps(data, indent=2) + "\n").encode()


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_bytes())
    except (OSError, ValueError):
        return None


def _human(size: int | None) -> str:
    return f"{size / 2**20:.0f} MiB" if size is not None else "unknown size"


class Mirror:
    def __init__(
        self,
        client: httpx.Client,
        verifier: _BundleVerifier,
        dest: Path,
        base_url: str,
        *,
        index_url: str,
        keep: int = 1,
        prune: bool = False,
        dry_run: bool = False,
        out: Callable[[str], None] = print,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if keep < 1:
            raise ValueError("keep must be at least 1")
        if not base_url.startswith(("https://", "http://")):
            raise ValueError(f"--base-url must be an http or https URL, got {base_url!r}")
        self.client = client
        self.verifier = verifier
        self.dest = dest
        self.base_url = base_url.rstrip("/")
        self.index_url = index_url
        self.source_base = _dirname(index_url)
        self.keep = keep
        self.prune = prune
        self.dry_run = dry_run
        self.out = out
        self.now = now

    # -- entry point --------------------------------------------------------------------

    def run(
        self, raw_index: Mapping[str, Any], index: CatalogIndex, targets: Sequence[Target]
    ) -> list[ImageResult]:
        if self.dest.exists() and not self.dest.is_dir():
            raise MirrorError(f"{self.dest} is not a directory")
        if self.dry_run:
            return self._run(raw_index, index, targets)
        self.dest.mkdir(parents=True, exist_ok=True)
        with self._lock():
            return self._run(raw_index, index, targets)

    @contextlib.contextmanager
    def _lock(self) -> Iterator[None]:
        with (self.dest / LOCK_NAME).open("a") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as e:
                raise MirrorError(f"another oic-sync mirror is running on {self.dest}") from e
            yield

    def _run(
        self, raw_index: Mapping[str, Any], index: CatalogIndex, targets: Sequence[Target]
    ) -> list[ImageResult]:
        raw_images = {str(i.get("id")): i for i in raw_index.get("images", [])}
        by_image: dict[str, list[Target]] = {}
        for t in targets:
            by_image.setdefault(t.image.id, []).append(t)

        results = []
        for image in index.images:
            if image.id not in by_image:
                continue
            result = ImageResult(image.id)
            try:
                self._mirror_image(image, raw_images[image.id], by_image[image.id], result)
            except (MirrorError, httpx.HTTPError, OSError, ValueError) as e:
                result.ok = False
                self._say(result, f"ERROR: {e}")
            except Exception as e:  # keep going with the other images
                log.debug("unexpected error", exc_info=True)
                result.ok = False
                self._say(result, f"ERROR: {type(e).__name__}: {e}")
            results.append(result)

        if not self.dry_run:
            self._write_index(raw_index, index, results)
        return results

    def _timestamp(self) -> str:
        return self.now().strftime("%Y-%m-%dT%H:%M:%SZ")

    def _say(self, result: ImageResult, message: str) -> None:
        result.messages.append(message)
        self.out(f"{result.image_id}: {message}")

    # -- one image ----------------------------------------------------------------------

    def _fetch(self, url: str, limit: int, *, optional: bool = False) -> bytes | None:
        """GET a small file; None when optional and absent (404)."""
        with self.client.stream("GET", url) as response:
            if optional and response.status_code == 404:
                return None
            response.raise_for_status()
            data = b""
            for block in response.iter_bytes(CHUNK):
                data += block
                if len(data) > limit:
                    raise MirrorError(f"{url} is larger than {limit} bytes")
        return data

    def _upstream_versions(self, image: Image, raw_image: Mapping[str, Any]) -> dict[str, Any]:
        url = raw_image.get("versions_url") or f"{self.source_base}/{image.id}/versions.json"
        data = self._fetch(str(url), MAX_VERSIONS_BYTES, optional=True)
        if data is None:
            return {}
        doc = json.loads(data)
        if not isinstance(doc, dict) or not isinstance(doc.get("versions"), list):
            raise MirrorError(f"{url}: not a versions document")
        doc["_url"] = str(url)
        return doc

    def _plan_versions(
        self, image: Image, targets: Sequence[Target], upstream: Mapping[str, Any]
    ) -> list[_Version]:
        """The `keep` newest versions: the index one first, then the older ones of versions.json."""
        variants = {t.artifact.variant for t in targets}
        latest = targets[0].artifact.version
        entries = {
            str(v.get("version")): v for v in upstream.get("versions", []) if isinstance(v, dict)
        }
        versions = [
            _Version(
                latest,
                targets[0].artifact.built_at,
                [self._file_from_index(t.artifact, entries.get(latest)) for t in targets],
            )
        ]
        versions_url = upstream.get("_url") or f"{self.source_base}/{image.id}/versions.json"
        for name, entry in entries.items():
            if len(versions) >= self.keep:
                break
            if name == latest:
                continue
            files = [
                self._file_from_versions(name, a, _dirname(versions_url))
                for a in entry.get("artifacts", [])
                if isinstance(a, dict) and a.get("variant") in variants
            ]
            if files:
                versions.append(_Version(name, entry.get("built_at"), files))
        for v in versions:
            _safe(v.version, "version")
            for f in v.files:
                _safe(f.filename, "file name")
                if f.manifest:
                    _safe(f.manifest, "file name")
        return versions

    @staticmethod
    def _file_from_index(artifact: Artifact, entry: Mapping[str, Any] | None) -> _File:
        filename = _basename(artifact.url)
        manifest = _basename(artifact.manifest_url) if artifact.manifest_url else None
        raw = next(
            (
                a
                for a in (entry or {}).get("artifacts", [])
                if isinstance(a, dict) and a.get("filename") == filename
            ),
            None,
        )
        if raw is None:
            raw = {
                "variant": artifact.variant,
                "filename": filename,
                "sha256": artifact.sha256,
                "sha512": artifact.sha512,
                "size": artifact.size,
                "virtual_size": artifact.virtual_size,
                "firmware": artifact.firmware,
                "arch": artifact.arch,
                "path": f"{artifact.version}/{filename}",
                "manifest": f"{artifact.version}/{manifest}" if manifest else None,
            }
        return _File(artifact, filename, manifest, dict(raw))

    @staticmethod
    def _file_from_versions(version: str, raw: Mapping[str, Any], image_base: str) -> _File:
        filename = str(raw.get("filename") or "")
        path = str(raw.get("path") or "")
        if path != f"{version}/{filename}":
            raise MirrorError(f"versions.json: unexpected path {path!r} for {version}")
        sha256 = str(raw.get("sha256") or "").lower()
        if not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise MirrorError(f"versions.json: invalid sha256 for {path}")
        sha512 = raw.get("sha512")
        if sha512 is not None and not re.fullmatch(r"[0-9a-f]{128}", str(sha512).lower()):
            raise MirrorError(f"versions.json: invalid sha512 for {path}")
        size = raw.get("size")
        if size is not None and (isinstance(size, bool) or not isinstance(size, int) or size < 0):
            raise MirrorError(f"versions.json: invalid size for {path}")
        manifest = raw.get("manifest")
        manifest_name = None
        if manifest:
            manifest_name = str(manifest).rsplit("/", 1)[-1]
            if str(manifest) != f"{version}/{manifest_name}":
                raise MirrorError(f"versions.json: unexpected manifest path {manifest!r}")
        url = f"{image_base}/{path}"
        artifact = Artifact(
            variant=raw.get("variant"),
            version=version,
            arch=str(raw.get("arch") or "x86_64"),
            format="qcow2",
            firmware=str(raw.get("firmware") or "bios"),
            url=url,
            sha256=sha256,
            sha512=str(sha512).lower() if sha512 else None,
            size=size,
            virtual_size=raw.get("virtual_size"),
            bundle_url=url + ".bundle",
            manifest_url=f"{image_base}/{manifest}" if manifest else None,
            signed=True,
            built_at=None,
        )
        return _File(artifact, filename, manifest_name, dict(raw))

    def _mirror_image(
        self,
        image: Image,
        raw_image: Mapping[str, Any],
        targets: Sequence[Target],
        result: ImageResult,
    ) -> None:
        image_dir = self.dest / _safe(image.id, "image id")
        upstream = self._upstream_versions(image, raw_image)
        if not upstream:
            self._say(result, "note: no versions.json upstream, only the index version")
        versions = self._plan_versions(image, targets, upstream)

        done: list[_Version] = []
        for v in versions:
            if self._mirror_version(image_dir, v, result):
                done.append(v)
            else:
                result.ok = False
        latest_ok = bool(done) and done[0] is versions[0]

        previous = _read_json(image_dir / "versions.json") or {}
        previous_entries = [
            e for e in previous.get("versions", []) if isinstance(e, dict) and e.get("version")
        ]
        kept_names = {v.version for v in versions}
        extra = [e for e in previous_entries if e["version"] not in kept_names]

        if self.prune:
            if latest_ok and len(done) == len(versions):
                extra = self._prune(image_dir, kept_names, extra, upstream, result)
            else:
                self._say(result, "not pruning: a kept version failed")
        elif extra:
            self._say(result, f"{len(extra)} older version(s) kept on disk (use --prune)")

        if self.dry_run:
            if latest_ok:
                result.entry = {}
            return
        listed = [self._versions_entry(v) for v in done] + [
            e for e in extra if (image_dir / str(e["version"])).is_dir()
        ]
        if listed:
            doc = {
                "schema": upstream.get("schema") or previous.get("schema") or "oic-versions/v1",
                "name": upstream.get("name") or image.id,
                "generated_at": upstream.get("generated_at") or self._timestamp(),
                "versions": listed,
            }
            _write_atomic(image_dir / "versions.json", _json_bytes(doc))
        if latest_ok:
            _write_atomic(image_dir / "latest.json", _json_bytes({"version": versions[0].version}))
            result.entry = self._index_entry(raw_image, image, versions[0])

    @staticmethod
    def _versions_entry(v: _Version) -> dict[str, Any]:
        entry: dict[str, Any] = {"version": v.version}
        if v.built_at:
            entry["built_at"] = v.built_at
        entry["artifacts"] = [f.entry for f in v.files]
        return entry

    def _index_entry(
        self, raw_image: Mapping[str, Any], image: Image, latest: _Version
    ) -> dict[str, Any]:
        entry = copy.deepcopy(dict(raw_image))
        image_base = f"{self.base_url}/{image.id}"
        version_base = f"{image_base}/{latest.version}"
        entry["versions_url"] = f"{image_base}/versions.json"
        mirrored = {f.filename: f for f in latest.files}
        artifacts = []
        for raw in entry.get("artifacts", []):
            if not isinstance(raw, dict) or not raw.get("url"):
                continue
            f = mirrored.get(_basename(str(raw["url"])))
            if f is None:  # variant not selected
                continue
            raw["url"] = f"{version_base}/{f.filename}"
            raw["bundle_url"] = f"{version_base}/{f.filename}.bundle"
            raw["manifest_url"] = f"{version_base}/{f.manifest}" if f.manifest else None
            artifacts.append(raw)
        entry["artifacts"] = artifacts
        return entry

    def _prune(
        self,
        image_dir: Path,
        kept: set[str],
        extra: list[dict[str, Any]],
        upstream: Mapping[str, Any],
        result: ImageResult,
    ) -> list[dict[str, Any]]:
        """Remove the versions beyond --keep: only directories this tool knows as versions."""
        known = {str(e["version"]) for e in extra} | {
            str(v.get("version")) for v in upstream.get("versions", []) if isinstance(v, dict)
        }
        for name in sorted(known - kept):
            if not _SAFE.match(name) or ".." in name:
                continue
            path = image_dir / name
            if not path.is_dir():
                continue
            self._say(result, f"{'would prune' if self.dry_run else 'pruned'} {name}")
            if not self.dry_run:
                shutil.rmtree(path)
        return [] if not self.dry_run else extra

    # -- one version --------------------------------------------------------------------

    def _mirror_version(self, image_dir: Path, v: _Version, result: ImageResult) -> bool:
        vdir = image_dir / v.version
        try:
            for f in v.files:
                self._mirror_file(vdir, v, f, result)
            self._mirror_sidecars(vdir, v)
        except (SignatureError, HashMismatch, MirrorError, httpx.HTTPError, OSError) as e:
            self._say(result, f"ERROR {v.version}: {e}")
            return False
        return True

    def _mirror_file(self, vdir: Path, v: _Version, f: _File, result: ImageResult) -> None:
        a = f.artifact
        if not a.bundle_url:
            raise SignatureError(f"{f.filename}: the index lists no cosign bundle")
        bundle = self._fetch(a.bundle_url, MAX_BUNDLE_BYTES)
        assert bundle is not None
        identity = self.verifier.verify_bundle(a, bundle)
        path = vdir / f.filename
        exists = path.is_file()
        if exists and self._present(path, a):
            self._say(result, f"{v.version}: {f.filename} present, digests and signature verified")
        elif self.dry_run:
            action = "would replace (local copy does not match)" if exists else "would download"
            self._say(result, f"{v.version}: {action} {f.filename} ({_human(a.size)})")
        else:
            vdir.mkdir(parents=True, exist_ok=True)
            action = "local copy does not match, downloading" if exists else "downloading"
            part = _part(path)
            resume = f", resuming after {_human(part.stat().st_size)}" if part.is_file() else ""
            self._say(result, f"{v.version}: {action} {f.filename} ({_human(a.size)}{resume})")
            self._download(a, path)
            self._say(result, f"{v.version}: {f.filename} digests verified, signed by {identity}")
        if not self.dry_run:
            vdir.mkdir(parents=True, exist_ok=True)
            _write_atomic(vdir / f"{f.filename}.bundle", bundle)

    @staticmethod
    def _present(path: Path, artifact: Artifact) -> bool:
        if artifact.size is not None and path.stat().st_size != artifact.size:
            return False
        try:
            _hash_file(path).check(artifact)
        except HashMismatch:
            return False
        return True

    def _download(self, artifact: Artifact, path: Path) -> None:
        """Stream to a hidden .part file (resuming it when possible), verify, then rename."""
        part = _part(path)
        offset = part.stat().st_size if part.is_file() else 0
        if offset and artifact.size is not None and offset > artifact.size:
            part.unlink()
            offset = 0
        digests = _hash_file(part) if offset else _Digests()
        if not (offset and offset == artifact.size):
            headers = {"Range": f"bytes={offset}-"} if offset else {}
            with self.client.stream("GET", artifact.url, headers=headers) as response:
                if offset and response.status_code == 206:
                    mode = "ab"
                else:
                    response.raise_for_status()
                    mode, digests = "wb", _Digests()
                with part.open(mode) as out:
                    for block in response.iter_bytes(CHUNK):
                        digests.update(block)
                        if artifact.size is not None and digests.size > artifact.size:
                            break
                        out.write(block)
                    out.flush()
                    os.fsync(out.fileno())
        try:
            digests.check(artifact)
        except HashMismatch:
            part.unlink(missing_ok=True)
            raise
        os.replace(part, path)

    def _mirror_sidecars(self, vdir: Path, v: _Version) -> None:
        """MANIFEST, SHA256SUMS, index.html and checksum files, when the CDN has them."""
        source = _dirname(v.files[0].artifact.url)
        names = list(dict.fromkeys([*(f.manifest for f in v.files if f.manifest)]))
        names += [f"{f.filename}{s}" for f in v.files for s in CHECKSUM_SUFFIXES]
        names += list(VERSION_SIDECARS)
        if self.dry_run:
            return
        for name in names:
            data = self._fetch(f"{source}/{name}", MAX_SIDECAR_BYTES, optional=True)
            if data is not None:
                _write_atomic(vdir / name, data)

    # -- index --------------------------------------------------------------------------

    def _write_index(
        self, raw_index: Mapping[str, Any], index: CatalogIndex, results: list[ImageResult]
    ) -> None:
        """Rewrite index.json; an image whose new version failed keeps its previous entry."""
        previous = _read_json(self.dest / "index.json") or {}
        previous_entries = {
            str(i.get("id")): i for i in previous.get("images", []) if isinstance(i, dict)
        }
        new_entries = {r.image_id: r.entry for r in results if r.entry}
        images = []
        for image in index.images:
            entry = new_entries.get(image.id) or previous_entries.get(image.id)
            if entry and (self.dest / image.id).is_dir():
                images.append(entry)
        known = {i.id for i in index.images}
        for image_id, entry in previous_entries.items():  # no longer in the upstream index
            if image_id not in known and (self.dest / image_id).is_dir():
                images.append(entry)
        if not images:
            return
        data = {key: copy.deepcopy(value) for key, value in raw_index.items() if key != "images"}
        data["base_url"] = self.base_url
        data["mirror"] = {
            "source": self.index_url,
            "mirrored_at": self._timestamp(),
        }
        data["images"] = images
        _write_atomic(self.dest / "index.json", _json_bytes(data))
