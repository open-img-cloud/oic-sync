"""Fakes for Glance, Nova, Cinder and the CDN. No test talks to a real service."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

PROJECT = "p-123"
BASE = "https://images.example.test"
IDENTITY_RE = (
    r"^https://github.com/open-img-cloud/\.github/\.github/workflows/"
    r"build-(libguestfs|dib|nix-flake|freebsd)-image\.yml@"
)
ISSUER = "https://token.actions.githubusercontent.com"

DEBIAN_DATA = b"debian-13 qcow2 payload " * 1000
ALPINE_BIOS_DATA = b"alpine bios payload " * 500
ALPINE_UEFI_DATA = b"alpine uefi payload " * 500
OCTAVIA_DATA = b"octavia payload " * 300


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha512(data: bytes) -> str:
    return hashlib.sha512(data).hexdigest()


def artifact(
    image_id: str,
    version: str,
    data: bytes,
    *,
    variant: str | None = None,
    firmware: str = "bios",
    with_sha512: bool = True,
    extra_props: dict[str, str] | None = None,
) -> dict[str, Any]:
    suffix = f"-{variant}" if variant else ""
    name = f"{image_id}-{version}{suffix}-x86_64.qcow2"
    url = f"{BASE}/{image_id}/{version}/{name}"
    props = {
        "os_type": "linux",
        "architecture": "x86_64",
        "hw_disk_bus": "scsi",
        "hw_scsi_model": "virtio-scsi",
        **(extra_props or {}),
        "oic_id": image_id,
        "oic_version": version,
        "oic_sha256": sha256(data),
    }
    if firmware == "uefi":
        props.update(hw_firmware_type="uefi", hw_machine_type="q35")
    return {
        "variant": variant,
        "version": version,
        "arch": "x86_64",
        "format": "qcow2",
        "firmware": firmware,
        "url": url,
        "sha256": sha256(data),
        "sha512": sha512(data) if with_sha512 else None,
        "size": len(data),
        "virtual_size": 3 * 2**30 + 1,
        "bundle_url": url + ".bundle",
        "manifest_url": f"{BASE}/{image_id}/{version}/MANIFEST{suffix}.json",
        "built_at": "2026-11-11T13:39:56Z",
        "build_url": None,
        "commit": None,
        "signed": True,
        "glance_properties": props,
    }


def image(image_id: str, title: str, artifacts: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": image_id,
        "title": title,
        "status": "stable",
        "status_note": None,
        "repo": f"https://github.com/open-img-cloud/{image_id}",
        "login_user": "debian",
        "os_distro": "debian",
        "os_version": "13",
        "latest_version": artifacts[0]["version"],
        "versions_url": None,
        "tags": [],
        "glance_properties": {},
        "optional_glance_properties": {"hw_rng_model": "virtio"},
        "artifacts": artifacts,
    }


def index_dict() -> dict[str, Any]:
    return {
        "schema": "oic-index/v1",
        "generated_at": "2026-11-12T09:42:27Z",
        "base_url": BASE,
        "signing": {
            "type": "cosign-keyless",
            "bundle_format": "sigstore-bundle",
            "certificate_identity_regexp": IDENTITY_RE,
            "certificate_oidc_issuer": ISSUER,
        },
        "images": [
            image(
                "debian-13", "Debian 13", [artifact("debian-13", "13-20261103-2651", DEBIAN_DATA)]
            ),
            image(
                "alpine-linux",
                "Alpine Linux",
                [
                    artifact("alpine-linux", "3.24.1", ALPINE_BIOS_DATA, variant="bios"),
                    artifact(
                        "alpine-linux", "3.24.1", ALPINE_UEFI_DATA, variant="uefi", firmware="uefi"
                    ),
                ],
            ),
            image(
                "octavia-amphora",
                "Octavia Amphora",
                [artifact("octavia-amphora", "2026.1", OCTAVIA_DATA, with_sha512=False)],
            ),
        ],
    }


def cdn_files(index: dict[str, Any] | None = None) -> dict[str, bytes]:
    """URL -> body for the fake CDN: index, bundles and images."""
    index = index or index_dict()
    payloads = {
        "debian-13": [DEBIAN_DATA],
        "alpine-linux": [ALPINE_BIOS_DATA, ALPINE_UEFI_DATA],
        "octavia-amphora": [OCTAVIA_DATA],
    }
    files = {f"{BASE}/index.json": json.dumps(index).encode()}
    for img in index["images"]:
        for a, data in zip(img["artifacts"], payloads.get(img["id"], []), strict=False):
            files[a["url"]] = data
            files[a["bundle_url"]] = json.dumps({"fake-bundle-for": a["sha256"]}).encode()
    return files


def cdn_transport(files: dict[str, bytes], log: list[str] | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if log is not None:
            log.append(url)
        assert request.headers["User-Agent"].startswith("oic-sync/")
        if url not in files:
            return httpx.Response(404, text="not found")
        return httpx.Response(200, content=files[url])

    return httpx.MockTransport(handler)


class FakeVerifier:
    """Stands in for sigstore's Verifier; records the digests it was asked about."""

    def __init__(self, fail_for: set[str] | None = None) -> None:
        self.fail_for = fail_for or set()
        self.calls: list[str] = []

    def verify(self, artifact: Any) -> str:  # SignatureVerifier interface
        from oic_sync.signature import SignatureError

        self.calls.append(artifact.sha256)
        if artifact.sha256 in self.fail_for:
            raise SignatureError("Bundle message digest mismatch")
        return (
            "https://github.com/open-img-cloud/.github/.github/workflows/build.yml@refs/heads/main"
        )


@dataclass
class FakeResponse:
    status_code: int
    body: Any = None
    text: str = ""

    def json(self) -> Any:
        return self.body


def _read_all(data: Any) -> bytes:
    if isinstance(data, bytes):
        return data
    chunks = []
    while True:
        block = data.read(65536)
        if not block:
            return b"".join(chunks)
        chunks.append(block)


@dataclass
class FakeGlance:
    """In-memory Glance v2 API behind the RestAdapter interface."""

    import_methods: list[str] = field(default_factory=lambda: ["glance-direct"])
    remote: dict[str, bytes] = field(default_factory=dict)  # what web-download fetches
    hash_algo: str = "sha512"
    corrupt_store: bool = False  # store different bytes than received
    web_download_fails: bool = False
    page_size: int | None = None
    images: dict[str, dict[str, Any]] = field(default_factory=dict)
    staged: dict[str, bytes] = field(default_factory=dict)
    calls: list[tuple[str, str]] = field(default_factory=list)
    clock: datetime = field(default_factory=lambda: datetime(2026, 11, 12, tzinfo=timezone.utc))
    _n: int = 0

    # -- helpers for tests ---------------------------------------------------------------

    def add(self, **fields: Any) -> dict[str, Any]:
        self._n += 1
        img = {
            "id": fields.pop("id", f"img-{self._n}"),
            "name": "",
            "status": "active",
            "visibility": "private",
            "os_hidden": False,
            "owner": PROJECT,
            "created_at": (self.clock + timedelta(minutes=self._n)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            **fields,
        }
        self.images[img["id"]] = img
        return img

    def writes(self) -> list[tuple[str, str]]:
        return [c for c in self.calls if c[0] != "GET"]

    def _store(self, img: dict[str, Any], data: bytes) -> None:
        if self.corrupt_store:
            data = data + b"x"
        digest = hashlib.new(self.hash_algo, data).hexdigest()
        img.update(
            status="active", size=len(data), os_hash_algo=self.hash_algo, os_hash_value=digest
        )

    # -- RestAdapter -----------------------------------------------------------------------

    def get(self, url: str, params: dict[str, Any] | None = None, **_: Any) -> FakeResponse:
        self.calls.append(("GET", url))
        if url == "/info/import":
            return FakeResponse(200, {"import-methods": {"value": self.import_methods}})
        if url == "/images":
            params = dict(params or {})
            limit = int(params.pop("limit", 25))
            if self.page_size:
                limit = min(limit, self.page_size)
            marker = params.pop("marker", None)
            hidden = params.pop("os_hidden", "false") == "true"
            found = [
                copy.deepcopy(i)
                for i in sorted(self.images.values(), key=lambda i: i["id"])
                if bool(i.get("os_hidden")) == hidden
                and all(str(i.get(k)) == str(v) for k, v in params.items())
            ]
            if marker:
                ids = [i["id"] for i in found]
                found = found[ids.index(marker) + 1 :]
            page = found[:limit]
            body: dict[str, Any] = {"images": page}
            if len(found) > limit:
                body["next"] = f"/v2/images?marker={page[-1]['id']}"
            return FakeResponse(200, body)
        image_id = url.removeprefix("/images/")
        if image_id in self.images:
            return FakeResponse(200, copy.deepcopy(self.images[image_id]))
        return FakeResponse(404, text="not found")

    def post(self, url: str, json: Any = None, **_: Any) -> FakeResponse:
        self.calls.append(("POST", url))
        if url == "/images":
            body = dict(json)
            img = self.add(**body, status="queued")
            return FakeResponse(201, copy.deepcopy(img))
        image_id = url.removeprefix("/images/").removesuffix("/import")
        img = self.images[image_id]
        method = json["method"]["name"]
        if method not in self.import_methods:
            return FakeResponse(409, text="method not allowed")
        if method == "web-download":
            if self.web_download_fails or json["method"]["uri"] not in self.remote:
                img["os_glance_failed_import"] = "store1"
                img["status"] = "queued"
            else:
                self._store(img, self.remote[json["method"]["uri"]])
        else:
            self._store(img, self.staged.pop(image_id))
        return FakeResponse(202)

    def put(self, url: str, data: Any = None, **_: Any) -> FakeResponse:
        self.calls.append(("PUT", url))
        image_id = url.split("/")[2]
        payload = _read_all(data)
        if url.endswith("/stage"):
            self.staged[image_id] = payload
            self.images[image_id]["status"] = "uploading"
        else:
            self._store(self.images[image_id], payload)
        return FakeResponse(204)

    def patch(self, url: str, data: str = "", **_: Any) -> FakeResponse:
        self.calls.append(("PATCH", url))
        img = self.images[url.removeprefix("/images/")]
        for op in json.loads(data):
            assert op["op"] == "replace"
            img[op["path"].lstrip("/")] = op["value"]
        return FakeResponse(200, copy.deepcopy(img))

    def delete(self, url: str, **_: Any) -> FakeResponse:
        self.calls.append(("DELETE", url))
        if self.images.pop(url.removeprefix("/images/"), None) is None:
            return FakeResponse(404)
        return FakeResponse(204)


@dataclass
class FakeUsageApi:
    """Nova /servers or Cinder /volumes listing filtered by image."""

    key: str  # "servers" or "volumes"
    used: set[str] = field(default_factory=set)
    status: int = 200
    calls: list[dict[str, Any]] = field(default_factory=list)

    def get(self, url: str, params: dict[str, Any] | None = None, **kwargs: Any) -> FakeResponse:
        params = dict(params or {})
        self.calls.append({"url": url, **params, **kwargs})
        if self.status != 200:
            return FakeResponse(self.status, text="forbidden")
        if self.key == "servers":
            image_id = params["image"]
        else:
            image_id = json.loads(params["glance_metadata"])["image_id"]
        items = [{"id": "x"}] if image_id in self.used else []
        return FakeResponse(200, {self.key: items})

    post = put = patch = delete = None  # type: ignore[assignment]
