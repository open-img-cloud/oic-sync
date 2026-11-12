from __future__ import annotations

import copy
from datetime import datetime, timezone

import pytest

from fakes import (
    DEBIAN_DATA,
    PROJECT,
    FakeResponse,
    FakeUsageApi,
    FakeVerifier,
    artifact,
    cdn_transport,
    index_dict,
)
from oic_sync.backends.base import BackendError
from oic_sync.backends.openstack import Glance, OpenStackBackend
from oic_sync.engine import Engine
from oic_sync.hashing import HashMismatch
from oic_sync.http import make_client
from oic_sync.index import parse_index, select_targets

NOW = datetime(2026, 11, 12, 12, tzinfo=timezone.utc)


def glance_images(glance):
    return sorted(glance.images.values(), key=lambda i: i["created_at"])


# -- listing -----------------------------------------------------------------------------


def test_list_managed_sees_hidden_and_filters(glance, make_backend, targets):
    glance.add(
        id="a",
        name="Alpine Linux (UEFI)",
        oic_id="alpine-linux",
        oic_variant="uefi",
        oic_version="3.24.0",
        oic_sha256="A" * 64,
    )
    glance.add(
        id="b",
        name="old",
        oic_id="alpine-linux",
        oic_variant="uefi",
        oic_version="3.23",
        oic_sha256="b" * 64,
        os_hidden=True,
    )
    glance.add(id="c", oic_id="alpine-linux", oic_variant="bios", oic_version="3.24.0")
    glance.add(id="d", oic_id="alpine-linux", oic_variant="uefi", owner="someone-else")
    glance.add(id="e", oic_id="debian-13", oic_version="13-1")
    found = make_backend().list_managed(targets["alpine-linux/uefi"])
    assert sorted(m.id for m in found) == ["a", "b"]
    a = next(m for m in found if m.id == "a")
    assert a.sha256 == "a" * 64  # normalised
    assert not a.hidden
    assert next(m for m in found if m.id == "b").hidden


def test_list_managed_without_variant_ignores_variant_images(glance, make_backend, targets):
    glance.add(id="x", oic_id="debian-13", oic_variant="uefi")
    glance.add(id="y", oic_id="debian-13")
    assert [m.id for m in make_backend().list_managed(targets["debian-13"])] == ["y"]


def test_list_managed_follows_pagination(glance, make_backend, targets):
    glance.page_size = 2
    for n in range(5):
        glance.add(id=f"p{n}", oic_id="debian-13", os_hidden=bool(n % 2))
    assert len(make_backend().list_managed(targets["debian-13"])) == 5
    gets = [c for c in glance.calls if c == ("GET", "/images")]
    assert len(gets) == 3  # visible: 3 images on 2 pages; hidden: 2 images on 1 page


# -- create body -------------------------------------------------------------------------


def test_create_body(make_backend, targets):
    backend = make_backend(
        visibility="public", extra_properties={"hw_disk_bus": "virtio", "x": "1"}
    )
    body = backend.create_body(targets["alpine-linux/uefi"], "Alpine Linux (UEFI) (3.24.1)")
    assert body["os_hidden"] is True
    assert body["visibility"] == "public"
    assert body["disk_format"] == "qcow2"
    assert body["container_format"] == "bare"
    assert body["min_disk"] == 4
    assert body["oic_variant"] == "uefi"
    assert body["oic_id"] == "alpine-linux"
    assert body["hw_firmware_type"] == "uefi"
    assert body["hw_disk_bus"] == "virtio"  # override
    assert body["x"] == "1"
    assert "hw_rng_model" not in body


def test_optional_properties(make_backend, targets):
    body = make_backend(optional_properties=True).create_body(targets["debian-13"], "n")
    assert body["hw_rng_model"] == "virtio"
    assert "oic_variant" not in body


@pytest.mark.parametrize(
    "kwargs",
    [
        {"extra_properties": {"oic_sha256": "x"}},
        {"visibility": "everyone"},
        {"import_method": "copy-image"},
    ],
)
def test_invalid_options(make_backend, kwargs):
    with pytest.raises(ValueError):  # noqa: PT011
        make_backend(**kwargs)


# -- import paths ------------------------------------------------------------------------


def test_upload_when_no_web_download(glance, make_backend, targets, http_log):
    target = targets["debian-13"]
    img = make_backend().import_target(target, "Debian 13 (v)")
    assert img.status == "active"
    assert img.sha256 == target.artifact.sha256
    assert ("PUT", f"/images/{img.id}/file") in glance.calls
    assert target.artifact.url in http_log
    stored = glance.images[img.id]
    assert stored["os_hidden"] is True
    assert stored["name"] == "Debian 13 (v)"


def test_web_download_when_offered(glance, make_backend, targets, http_log):
    target = targets["debian-13"]
    glance.import_methods = ["glance-direct", "web-download"]
    glance.remote[target.artifact.url] = DEBIAN_DATA
    img = make_backend().import_target(target, "n")
    assert img.status == "active"
    assert ("POST", f"/images/{img.id}/import") in glance.calls
    assert target.artifact.url not in http_log  # Glance downloaded it, not us


def test_web_download_needs_index_sha512(glance, make_backend, targets):
    glance.import_methods = ["web-download"]
    target = targets["octavia-amphora"]
    assert make_backend().choose_method(target) == "upload"


def test_web_download_hash_mismatch_deletes_image(glance, make_backend, targets):
    target = targets["debian-13"]
    glance.import_methods = ["web-download"]
    glance.remote[target.artifact.url] = DEBIAN_DATA + b"tampered"
    with pytest.raises(HashMismatch, match="Glance computed sha512"):
        make_backend().import_target(target, "n")
    assert glance.images == {}


def test_web_download_failure_deletes_image(glance, make_backend, targets):
    glance.import_methods = ["web-download"]
    glance.web_download_fails = True
    with pytest.raises(BackendError, match="import failed"):
        make_backend().import_target(targets["debian-13"], "n")
    assert glance.images == {}


def test_web_download_unverifiable_hash_is_refused(glance, make_backend, targets):
    target = targets["debian-13"]
    glance.import_methods = ["web-download"]
    glance.remote[target.artifact.url] = DEBIAN_DATA
    glance.hash_algo = "sha384"  # a digest the index does not carry
    with pytest.raises(HashMismatch, match="cannot verify"):
        make_backend().import_target(target, "n")
    assert glance.images == {}


def test_upload_accepts_other_glance_hash_algo(glance, make_backend, targets):
    glance.hash_algo = "sha384"  # verified locally while streaming
    assert make_backend().import_target(targets["debian-13"], "n").status == "active"


def test_glance_sha256_is_compared(glance, make_backend, targets):
    target = targets["octavia-amphora"]  # no sha512 in the index
    glance.hash_algo = "sha256"
    glance.corrupt_store = True
    with pytest.raises(HashMismatch):
        make_backend().import_target(target, "n")


def test_upload_detects_corrupted_download(glance, targets, files):
    target = targets["debian-13"]
    files[target.artifact.url] = DEBIAN_DATA[:-1] + b"X"
    with make_client(transport=cdn_transport(files)) as c:
        backend = OpenStackBackend(Glance(glance, PROJECT), c, poll_interval=0)
        with pytest.raises(HashMismatch, match="sha256 mismatch"):
            backend.import_target(target, "n")
    assert glance.images == {}


def test_upload_aborts_oversized_download(glance, targets, files):
    target = targets["debian-13"]
    files[target.artifact.url] = DEBIAN_DATA + b"more"
    with make_client(transport=cdn_transport(files)) as c:
        backend = OpenStackBackend(Glance(glance, PROJECT), c, poll_interval=0)
        with pytest.raises(HashMismatch):
            backend.import_target(target, "n")
    assert glance.images == {}


def test_glance_stored_bytes_are_checked(glance, make_backend, targets):
    glance.corrupt_store = True
    with pytest.raises(HashMismatch, match="Glance computed sha512"):
        make_backend().import_target(targets["debian-13"], "n")
    assert glance.images == {}


def test_missing_download_is_a_backend_error(glance, targets, files):
    target = targets["debian-13"]
    del files[target.artifact.url]
    with make_client(transport=cdn_transport(files)) as c:
        backend = OpenStackBackend(Glance(glance, PROJECT), c, poll_interval=0)
        with pytest.raises(BackendError, match="download"):
            backend.import_target(target, "n")
    assert glance.images == {}


def test_glance_direct(glance, make_backend, targets):
    img = make_backend(import_method="glance-direct").import_target(targets["debian-13"], "n")
    assert img.status == "active"
    assert ("PUT", f"/images/{img.id}/stage") in glance.calls


def test_explicit_method_not_offered(glance, make_backend, targets):
    with pytest.raises(BackendError, match="does not offer"):
        make_backend(import_method="web-download").import_target(targets["debian-13"], "n")
    assert glance.writes() == []


def test_import_timeout(glance, make_backend, targets):
    target = targets["debian-13"]
    glance.import_methods = ["web-download"]
    glance.remote[target.artifact.url] = DEBIAN_DATA
    original_post = glance.post

    def post_but_never_finish(url, json=None, **kw):
        if url.endswith("/import"):
            glance.images[url.split("/")[2]]["status"] = "importing"
            return FakeResponse(202)
        return original_post(url, json=json, **kw)

    glance.post = post_but_never_finish
    ticks = iter(range(0, 10_000, 100))
    backend = make_backend(import_timeout=300, clock=lambda: next(ticks))
    with pytest.raises(BackendError, match="not active after 300s"):
        backend.import_target(target, "n")
    assert glance.images == {}


# -- rotation and usage ------------------------------------------------------------------


def test_rename_patch(glance, make_backend, targets):
    glance.add(id="a", oic_id="debian-13", name="Debian 13")
    backend = make_backend()
    (m,) = backend.list_managed(targets["debian-13"])
    backend.rename(m, "Debian 13 (old)", hidden=True)
    assert glance.images["a"]["name"] == "Debian 13 (old)"
    assert glance.images["a"]["os_hidden"] is True


def managed(glance, backend, target, **fields):
    glance.add(id="u", oic_id=target.image.id, **fields)
    (m,) = backend.list_managed(target)
    return m


def test_in_use_by_server_or_volume(glance, make_backend, targets):
    servers, volumes = FakeUsageApi("servers"), FakeUsageApi("volumes")
    backend = make_backend(compute=servers, volume=volumes)
    m = managed(glance, backend, targets["debian-13"])
    assert backend.in_use(m) is False
    volumes.used.add("u")
    assert backend.in_use(m) is True
    assert volumes.calls[-1]["microversion"] == "3.4"
    servers.used.add("u")
    assert backend.in_use(m) is True
    assert "all_tenants" not in servers.calls[-1]  # private image: own project only


def test_in_use_public_image_needs_all_projects(glance, make_backend, targets):
    servers, volumes = FakeUsageApi("servers"), FakeUsageApi("volumes")
    backend = make_backend(compute=servers, volume=volumes)
    m = managed(glance, backend, targets["debian-13"], visibility="public")
    assert backend.in_use(m) is False
    assert servers.calls[-1]["all_tenants"] == 1
    assert volumes.calls[-1]["all_tenants"] == 1


def test_in_use_unknown_when_forbidden_or_no_compute(glance, make_backend, targets):
    backend = make_backend(compute=FakeUsageApi("servers", status=403))
    m = managed(glance, backend, targets["debian-13"], visibility="public")
    assert backend.in_use(m) is None
    assert make_backend().in_use(m) is None
    backend = make_backend(
        compute=FakeUsageApi("servers"), volume=FakeUsageApi("volumes", status=403)
    )
    assert backend.in_use(m) is None


# -- end to end with the engine ----------------------------------------------------------


def engine_for(backend, **kwargs):
    lines = []
    return Engine(
        backend, verifier=FakeVerifier(), out=lines.append, now=lambda: NOW, **kwargs
    ), lines


def test_sync_is_idempotent(glance, make_backend, targets):
    backend = make_backend()
    engine, _ = engine_for(backend)
    selected = [targets["debian-13"], targets["alpine-linux/bios"], targets["alpine-linux/uefi"]]
    assert all(r.ok for r in engine.sync(selected))
    names = sorted(i["name"] for i in glance.images.values())
    assert names == ["Alpine Linux (BIOS)", "Alpine Linux (UEFI)", "Debian 13"]
    assert not any(i["os_hidden"] for i in glance.images.values())
    glance.calls.clear()
    results = engine.sync(selected)
    assert all(r.state == "up-to-date" for r in results)
    assert glance.writes() == []


def test_new_version_rotates_names_and_keeps_n(glance, client, targets, files):
    servers = FakeUsageApi("servers")
    volumes = FakeUsageApi("volumes")

    def backend():
        return OpenStackBackend(
            Glance(glance, PROJECT), client, poll_interval=0, compute=servers, volume=volumes
        )

    def release(n: int):
        data = index_dict()
        data["images"] = [copy.deepcopy(data["images"][0])]
        a = data["images"][0]["artifacts"][0]
        payload = DEBIAN_DATA + bytes([n])
        new = artifact("debian-13", f"13-v{n}", payload)
        a.update(new)
        files[a["url"]] = payload
        return select_targets(parse_index(data))[0]

    for n in range(1, 5):
        engine, _ = engine_for(backend(), keep=2, delete=True)
        (result,) = engine.sync([release(n)])
        assert result.ok
        if n == 2:
            servers.used.add(
                next(i["id"] for i in glance.images.values() if i["oic_version"] == "13-v1")
            )

    by_version = {i["oic_version"]: i for i in glance.images.values()}
    # v4 current, v3 kept (keep 2), v2 deleted, v1 kept because a server uses it.
    assert sorted(by_version) == ["13-v1", "13-v3", "13-v4"]
    assert by_version["13-v4"]["name"] == "Debian 13"
    assert by_version["13-v4"]["os_hidden"] is False
    assert by_version["13-v3"]["name"] == "Debian 13 (13-v3)"
    assert by_version["13-v3"]["os_hidden"] is True
    assert by_version["13-v1"]["name"] == "Debian 13 (13-v1)"
