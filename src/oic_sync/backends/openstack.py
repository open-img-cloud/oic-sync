"""OpenStack backend: Glance v2 images, Nova and Cinder for usage checks.

Calls go through the keystoneauth adapters of an openstacksdk Connection (`conn.image`,
`conn.compute`, `conn.block_storage`) with plain REST paths, which keeps this module small
and easy to test with fakes. Authentication and endpoints come from clouds.yaml.

Our images are recognised by their properties: oic_id (+ oic_variant for multi-variant
images) and owner = current project. oic_version and oic_sha256 tell which version an image
holds.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Iterator, Mapping
from datetime import datetime, timezone
from typing import Any, Protocol

import httpx

from oic_sync.backends.base import BackendError, ManagedImage
from oic_sync.hashing import HashingReader, HashMismatch
from oic_sync.index import Target

log = logging.getLogger(__name__)

WEB_DOWNLOAD = "web-download"
GLANCE_DIRECT = "glance-direct"
UPLOAD = "upload"
AUTO = "auto"
IMPORT_METHODS = (AUTO, WEB_DOWNLOAD, GLANCE_DIRECT, UPLOAD)

VISIBILITIES = ("private", "shared", "community", "public")

JSON_PATCH = "application/openstack-images-v2.1-json-patch"
CHUNK = 1024 * 1024
PAGE = 200

# Keys oic-sync owns: never overridable with --property.
RESERVED_PROPERTIES = frozenset({"oic_id", "oic_variant", "oic_version", "oic_sha256"})


class RestAdapter(Protocol):
    """The subset of a keystoneauth Adapter / openstacksdk Proxy used here."""

    def get(self, url: str, **kwargs: Any) -> Any: ...
    def post(self, url: str, **kwargs: Any) -> Any: ...
    def put(self, url: str, **kwargs: Any) -> Any: ...
    def patch(self, url: str, **kwargs: Any) -> Any: ...
    def delete(self, url: str, **kwargs: Any) -> Any: ...


def _check(response: Any, what: str, ok: tuple[int, ...] = (200, 201, 202, 204)) -> Any:
    if response.status_code not in ok:
        body = (getattr(response, "text", "") or "")[:300]
        raise BackendError(f"{what}: HTTP {response.status_code} {body}".rstrip())
    return response


def _parse_time(value: str | None) -> datetime:
    if not value:
        return datetime.fromtimestamp(0, timezone.utc)
    value = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class Glance:
    """Thin Glance v2 client."""

    def __init__(self, api: RestAdapter, project_id: str) -> None:
        self.api = api
        self.project_id = project_id

    def import_methods(self) -> list[str]:
        response = self.api.get("/info/import")
        if response.status_code == 404:  # interoperable import disabled or very old Glance
            return []
        _check(response, "GET /info/import")
        return list(response.json().get("import-methods", {}).get("value", []))

    def list_images(self, filters: Mapping[str, str]) -> list[dict[str, Any]]:
        images: list[dict[str, Any]] = []
        params: dict[str, Any] = {**filters, "limit": PAGE}
        while True:
            body = _check(self.api.get("/images", params=params), "GET /images").json()
            page = body.get("images", [])
            images.extend(page)
            if not page or not body.get("next"):
                return images
            params = {**params, "marker": page[-1]["id"]}

    def get_image(self, image_id: str) -> dict[str, Any]:
        return _check(self.api.get(f"/images/{image_id}"), f"GET image {image_id}").json()

    def create_image(self, body: Mapping[str, Any]) -> dict[str, Any]:
        return _check(self.api.post("/images", json=dict(body)), "POST /images").json()

    def update_image(self, image_id: str, ops: list[dict[str, Any]]) -> None:
        _check(
            self.api.patch(
                f"/images/{image_id}",
                data=json.dumps(ops),
                headers={"Content-Type": JSON_PATCH},
            ),
            f"PATCH image {image_id}",
        )

    def upload(self, image_id: str, data: Any) -> None:
        _check(
            self.api.put(
                f"/images/{image_id}/file",
                data=data,
                headers={"Content-Type": "application/octet-stream"},
            ),
            f"PUT image {image_id} data",
        )

    def stage(self, image_id: str, data: Any) -> None:
        _check(
            self.api.put(
                f"/images/{image_id}/stage",
                data=data,
                headers={"Content-Type": "application/octet-stream"},
            ),
            f"PUT image {image_id} stage",
        )

    def import_image(self, image_id: str, method: str, uri: str | None = None) -> None:
        body: dict[str, Any] = {"method": {"name": method}}
        if uri:
            body["method"]["uri"] = uri
        _check(self.api.post(f"/images/{image_id}/import", json=body), f"import {image_id}")

    def delete_image(self, image_id: str) -> None:
        _check(self.api.delete(f"/images/{image_id}"), f"DELETE image {image_id}", (204, 404))


class OpenStackBackend:
    def __init__(
        self,
        glance: Glance,
        http: httpx.Client,
        *,
        compute: RestAdapter | None = None,
        volume: RestAdapter | None = None,
        visibility: str = "private",
        import_method: str = AUTO,
        extra_properties: Mapping[str, str] | None = None,
        optional_properties: bool = False,
        import_timeout: float = 3600.0,
        poll_interval: float = 10.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        description: str = "openstack",
    ) -> None:
        if visibility not in VISIBILITIES:
            raise ValueError(f"visibility must be one of {', '.join(VISIBILITIES)}")
        if import_method not in IMPORT_METHODS:
            raise ValueError(f"import method must be one of {', '.join(IMPORT_METHODS)}")
        bad = RESERVED_PROPERTIES.intersection(extra_properties or {})
        if bad:
            raise ValueError(f"properties reserved to oic-sync: {', '.join(sorted(bad))}")
        self.glance = glance
        self.http = http
        self.compute = compute
        self.volume = volume
        self.visibility = visibility
        self.import_method = import_method
        self.extra_properties = dict(extra_properties or {})
        self.optional_properties = optional_properties
        self.import_timeout = import_timeout
        self.poll_interval = poll_interval
        self.sleep = sleep
        self.clock = clock
        self._description = description
        self._methods: list[str] | None = None

    def describe(self) -> str:
        return self._description

    # -- listing -------------------------------------------------------------------------

    def list_managed(self, target: Target) -> list[ManagedImage]:
        base = {"oic_id": target.image.id, "owner": self.glance.project_id}
        raw: dict[str, dict[str, Any]] = {}
        # Hidden images (os_hidden=true) are left out of the default list: query both.
        for extra in ({}, {"os_hidden": "true"}):
            for img in self.glance.list_images({**base, **extra}):
                raw[img["id"]] = img
        managed = []
        for img in raw.values():
            # Server-side property filters are exact, but check again: an old Glance or a
            # proxy could ignore unknown filters.
            if img.get("oic_id") != target.image.id:
                continue
            if img.get("owner") not in (None, self.glance.project_id):
                continue
            if (img.get("oic_variant") or None) != target.artifact.variant:
                continue
            managed.append(self._managed(img))
        return managed

    @staticmethod
    def _managed(img: Mapping[str, Any]) -> ManagedImage:
        return ManagedImage(
            id=img["id"],
            name=img.get("name") or "",
            version=str(img.get("oic_version") or ""),
            sha256=str(img.get("oic_sha256") or "").lower(),
            status=img.get("status") or "unknown",
            hidden=bool(img.get("os_hidden", False)),
            created_at=_parse_time(img.get("created_at")),
            visibility=img.get("visibility") or "private",
        )

    # -- import --------------------------------------------------------------------------

    def properties(self, target: Target) -> dict[str, str]:
        """Glance properties for the artifact: index set, variant, options, then overrides."""
        artifact = target.artifact
        props: dict[str, str] = {}
        if self.optional_properties:
            props.update(target.image.optional_glance_properties)
        props.update(artifact.glance_properties)
        props.update(self.extra_properties)
        props["oic_id"] = target.image.id
        props["oic_version"] = artifact.version
        props["oic_sha256"] = artifact.sha256
        if artifact.variant:
            props["oic_variant"] = artifact.variant
        return props

    def create_body(self, target: Target, name: str) -> dict[str, Any]:
        body: dict[str, Any] = {
            "name": name,
            "disk_format": target.artifact.format,
            "container_format": "bare",
            "visibility": self.visibility,
            "os_hidden": True,
            **self.properties(target),
        }
        if target.artifact.min_disk_gib:
            body["min_disk"] = target.artifact.min_disk_gib
        return body

    def _available_methods(self) -> list[str]:
        if self._methods is None:
            self._methods = self.glance.import_methods()
            log.info("Glance import methods: %s", ", ".join(self._methods) or "none")
        return self._methods

    def choose_method(self, target: Target) -> str:
        methods = self._available_methods()
        if self.import_method == AUTO:
            # web-download is only usable when the result can be checked: Glance hashes with
            # sha512 by default, which needs the index sha512.
            if WEB_DOWNLOAD in methods and target.artifact.sha512:
                return WEB_DOWNLOAD
            return UPLOAD
        if self.import_method in (WEB_DOWNLOAD, GLANCE_DIRECT) and self.import_method not in (
            methods
        ):
            raise BackendError(f"this cloud does not offer the {self.import_method} import")
        return self.import_method

    def import_target(self, target: Target, name: str) -> ManagedImage:
        method = self.choose_method(target)
        log.info("%s: importing with %s", target.label, method)
        created = self.glance.create_image(self.create_body(target, name))
        image_id = created["id"]
        try:
            locally_verified = False
            if method == WEB_DOWNLOAD:
                self.glance.import_image(image_id, WEB_DOWNLOAD, target.artifact.url)
            elif method == GLANCE_DIRECT:
                self._stream(target, lambda data: self.glance.stage(image_id, data))
                locally_verified = True
                self.glance.import_image(image_id, GLANCE_DIRECT)
            else:
                self._stream(target, lambda data: self.glance.upload(image_id, data))
                locally_verified = True
            img = self._wait_active(image_id)
            self._verify_glance(target, img, locally_verified)
            return self._managed(img)
        except BaseException:
            log.warning("%s: removing incomplete image %s", target.label, image_id)
            try:
                self.glance.delete_image(image_id)
            except BackendError as e:
                log.error("%s: could not remove image %s: %s", target.label, image_id, e)
            raise

    def _stream(self, target: Target, send: Callable[[Any], None]) -> None:
        """Download the artifact and hand it to Glance, hashing on the fly."""
        artifact = target.artifact
        try:
            with self.http.stream("GET", artifact.url) as response:
                response.raise_for_status()
                length = response.headers.get("Content-Length")
                if (
                    length is not None
                    and artifact.size is not None
                    and int(length) != (artifact.size)
                ):
                    raise HashMismatch(
                        f"server announces {length} bytes, index says {artifact.size}"
                    )
                chunks: Iterator[bytes] = response.iter_bytes(CHUNK)
                reader = HashingReader(chunks, artifact.size)
                send(reader)
                # Drain anything Glance did not read (it should have read everything).
                while reader.read(CHUNK):
                    pass
        except httpx.HTTPError as e:
            raise BackendError(f"download of {artifact.url} failed: {e}") from e
        reader.verify(artifact.sha256, artifact.sha512, artifact.size)

    def _wait_active(self, image_id: str) -> dict[str, Any]:
        deadline = self.clock() + self.import_timeout
        while True:
            img = self.glance.get_image(image_id)
            status = img.get("status")
            failed_stores = img.get("os_glance_failed_import")
            if failed_stores:
                raise BackendError(f"Glance import failed (stores: {failed_stores})")
            if status == "active":
                return img
            if status in ("killed", "deleted", "pending_delete"):
                raise BackendError(f"image {image_id} ended in status {status}")
            if self.clock() >= deadline:
                raise BackendError(
                    f"image {image_id} not active after {self.import_timeout:.0f}s ({status})"
                )
            self.sleep(self.poll_interval)

    @staticmethod
    def _verify_glance(target: Target, img: Mapping[str, Any], locally_verified: bool) -> None:
        """Compare what Glance stored with the index (os_hash_algo/os_hash_value, size)."""
        artifact = target.artifact
        algo = (img.get("os_hash_algo") or "").lower()
        value = (img.get("os_hash_value") or "").lower()
        expected = {"sha512": artifact.sha512, "sha256": artifact.sha256}.get(algo)
        if expected and value:
            if value != expected:
                raise HashMismatch(f"Glance computed {algo} {value}, index says {expected}")
        elif not locally_verified:
            raise HashMismatch(
                f"cannot verify the imported data: Glance reports {algo or 'no'} multihash"
                " and the index has no matching digest"
            )
        size = img.get("size")
        if artifact.size is not None and size is not None and int(size) != artifact.size:
            raise HashMismatch(f"Glance stored {size} bytes, index says {artifact.size}")

    # -- rotation ------------------------------------------------------------------------

    def rename(self, image: ManagedImage, name: str, hidden: bool) -> None:
        self.glance.update_image(
            image.id,
            [
                {"op": "replace", "path": "/name", "value": name},
                {"op": "replace", "path": "/os_hidden", "value": hidden},
            ],
        )

    def delete(self, image: ManagedImage) -> None:
        self.glance.delete_image(image.id)

    def in_use(self, image: ManagedImage) -> bool | None:
        """True when a server or a volume references the image; None when unknown.

        Images other projects can see (public, community, shared) need an admin view of
        every project (all_tenants); a refusal means "unknown", so nothing is deleted.
        """
        all_projects = image.visibility != "private"
        scope: dict[str, Any] = {"all_tenants": 1} if all_projects else {}
        if self.compute is None:
            return None
        try:
            response = self.compute.get("/servers", params={"image": image.id, "limit": 1, **scope})
            if response.status_code != 200:
                log.warning("server list for %s: HTTP %s", image.id, response.status_code)
                return None
            if response.json().get("servers"):
                return True
            if self.volume is not None:
                response = self.volume.get(
                    "/volumes",
                    params={
                        "glance_metadata": json.dumps({"image_id": image.id}),
                        "limit": 1,
                        **scope,
                    },
                    microversion="3.4",
                )
                if response.status_code != 200:
                    log.warning("volume list for %s: HTTP %s", image.id, response.status_code)
                    return None
                if response.json().get("volumes"):
                    return True
        except Exception as e:
            log.warning("usage check for %s failed: %s", image.id, e)
            return None
        return False


def connect(
    cloud: str | None,
    region: str | None,
    http: httpx.Client,
    **backend_options: Any,
) -> OpenStackBackend:
    """Build the backend from clouds.yaml (or OS_* variables when no cloud is named)."""
    import openstack

    from oic_sync import __version__

    conn = openstack.connect(
        cloud=cloud, region_name=region, app_name="oic-sync", app_version=__version__
    )
    project_id = conn.current_project_id
    if not project_id:
        raise BackendError("cannot determine the current project (scoped token required)")
    volume = conn.block_storage if conn.has_service("block-storage") else None
    compute = conn.compute if conn.has_service("compute") else None
    where = f"cloud {cloud or '(environment)'}"
    region_name = region or getattr(conn.config, "region_name", None)
    if region_name:
        where += f", region {region_name}"
    where += f", project {project_id}"
    return OpenStackBackend(
        Glance(conn.image, project_id),
        http,
        compute=compute,
        volume=volume,
        description=where,
        **backend_options,
    )
