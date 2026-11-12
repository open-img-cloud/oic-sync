from __future__ import annotations

import copy

import httpx
import pytest

from fakes import BASE, cdn_transport, index_dict
from oic_sync.http import make_client
from oic_sync.index import (
    IndexFormatError,
    fetch_index,
    parse_index,
    select_targets,
)


def test_parse_real_shape(index):
    assert index.schema == "oic-index/v1"
    assert index.signing.oidc_issuer == "https://token.actions.githubusercontent.com"
    debian = index.image("debian-13")
    assert debian is not None
    a = debian.artifacts[0]
    assert a.variant is None
    assert a.glance_properties["oic_id"] == "debian-13"
    assert a.min_disk_gib == 4  # 3 GiB + 1 byte rounds up
    assert index.image("octavia-amphora").artifacts[0].sha512 is None


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: d.update(schema="oic-index/v2"), "unsupported index schema"),
        (lambda d: d.pop("signing"), "signing"),
        (lambda d: d["signing"].update(type="gpg"), "unsupported signing type"),
        (lambda d: d["signing"].update(certificate_identity_regexp="("), "regexp"),
        (lambda d: d["images"][0]["artifacts"][0].update(sha256="abc"), "sha256"),
        (lambda d: d["images"][0]["artifacts"][0].update(sha512="zz"), "sha512"),
        (lambda d: d["images"][0]["artifacts"][0].update(url="http://x/y"), "https"),
        (lambda d: d["images"][0]["artifacts"][0].update(size=-1), "size"),
        (lambda d: d.update(images={}), "images"),
    ],
)
def test_parse_rejects_invalid(mutate, message):
    data = copy.deepcopy(index_dict())
    mutate(data)
    with pytest.raises(IndexFormatError, match=message):
        parse_index(data)


def test_parse_ignores_unknown_fields():
    data = index_dict()
    data["new_field"] = 1
    data["images"][0]["artifacts"][0]["new_field"] = {"x": 1}
    assert parse_index(data).image("debian-13")


def test_uppercase_digests_are_normalised():
    data = index_dict()
    a = data["images"][0]["artifacts"][0]
    a["sha256"] = a["sha256"].upper()
    assert parse_index(data).images[0].artifacts[0].sha256 == a["sha256"].lower()


def test_default_selection_skips_service_images(index):
    labels = [t.label for t in select_targets(index)]
    assert labels == ["debian-13", "alpine-linux/bios", "alpine-linux/uefi"]


def test_service_images_when_named_or_included(index):
    assert [t.label for t in select_targets(index, ["octavia-amphora"])] == ["octavia-amphora"]
    assert len(select_targets(index, include_service=True)) == 4


def test_variant_filter_keeps_images_without_variants(index):
    labels = [t.label for t in select_targets(index, variants=["uefi"])]
    assert labels == ["debian-13", "alpine-linux/uefi"]


def test_unknown_image_is_an_error(index):
    with pytest.raises(IndexFormatError, match="debian-99"):
        select_targets(index, ["debian-13", "debian-99"])


def test_stable_names(targets):
    assert targets["debian-13"].stable_name == "Debian 13"
    assert targets["alpine-linux/uefi"].stable_name == "Alpine Linux (UEFI)"


def test_fetch_index_ok(files):
    with make_client(transport=cdn_transport(files)) as c:
        assert fetch_index(c, f"{BASE}/index.json").image("debian-13")


def test_fetch_index_errors():
    def handler(request):
        if request.url.path == "/bad.json":
            return httpx.Response(200, content=b"<html>")
        return httpx.Response(503)

    with make_client(transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(IndexFormatError, match="cannot fetch"):
            fetch_index(c, f"{BASE}/index.json")
        with pytest.raises(IndexFormatError, match="not valid JSON"):
            fetch_index(c, f"{BASE}/bad.json")
