from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from fakes import (
    BASE,
    DEBIAN_DATA,
    PROJECT,
    FakeGlance,
    FakeVerifier,
    artifact,
    cdn_files,
    index_dict,
    sha256,
)
from oic_sync import cli
from oic_sync.backends.openstack import Glance, OpenStackBackend
from oic_sync.http import make_client
from oic_sync.index import parse_index, select_targets
from oic_sync.mirror import Mirror, MirrorError

INDEX_URL = f"{BASE}/index.json"
MIRROR = "https://mirror.internal.test/oic"
NEW = "13-20261103-2651"
OLD = "13-20261020-2640"
OLD_DATA = b"older debian-13 payload " * 900


def versions_doc(image_id: str, entries: list[dict]) -> bytes:
    return json.dumps(
        {
            "schema": "oic-versions/v1",
            "name": image_id,
            "generated_at": "2026-11-12T09:00:00Z",
            "versions": entries,
        }
    ).encode()


def entry(a: dict) -> dict:
    filename = a["url"].rsplit("/", 1)[-1]
    manifest = a["manifest_url"].rsplit("/", 1)[-1]
    return {
        "version": a["version"],
        "built_at": a["built_at"],
        "artifacts": [
            {
                "variant": a["variant"],
                "filename": filename,
                "sha256": a["sha256"],
                "sha512": a["sha512"],
                "size": a["size"],
                "virtual_size": a["virtual_size"],
                "firmware": a["firmware"],
                "arch": "x86_64",
                "path": f"{a['version']}/{filename}",
                "manifest": f"{a['version']}/{manifest}",
            }
        ],
    }


@pytest.fixture
def cdn():
    """The fake CDN: index, images and bundles, plus versions.json and sidecar files."""
    index = index_dict()
    files = cdn_files(index)
    debian = index["images"][0]["artifacts"][0]
    old = artifact("debian-13", OLD, OLD_DATA)
    files[old["url"]] = OLD_DATA
    files[old["bundle_url"]] = json.dumps({"fake-bundle-for": old["sha256"]}).encode()
    files[f"{BASE}/debian-13/versions.json"] = versions_doc(
        "debian-13", [entry(debian), entry(old)]
    )
    alpine = index["images"][1]["artifacts"]
    alpine_entry = entry(alpine[0])
    alpine_entry["artifacts"] += entry(alpine[1])["artifacts"]
    files[f"{BASE}/alpine-linux/versions.json"] = versions_doc("alpine-linux", [alpine_entry])
    for a in [debian, old, *alpine]:
        vdir = a["url"].rsplit("/", 1)[0]
        files[a["manifest_url"]] = json.dumps({"version": a["version"]}).encode()
        files[f"{vdir}/SHA256SUMS"] = f"{a['sha256']}  {a['url'].rsplit('/', 1)[-1]}\n".encode()
        files[f"{vdir}/index.html"] = b"<html>listing</html>"
        files[a["url"] + ".sha256"] = a["sha256"].encode()
    return files


def serve(cdn: dict[str, bytes], dest: Path, log: list[str]) -> httpx.MockTransport:
    """Upstream CDN from a dict, the mirror from the destination directory; Range aware."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        assert request.headers["User-Agent"].startswith("oic-sync/")
        log.append(url)
        if url.startswith(MIRROR + "/"):
            path = dest / url.removeprefix(MIRROR + "/")
            body = path.read_bytes() if path.is_file() else None
        else:
            body = cdn.get(url)
        if body is None:
            return httpx.Response(404, text="not found")
        rng = request.headers.get("Range")
        if rng:
            start = int(rng.removeprefix("bytes=").rstrip("-"))
            return httpx.Response(
                206,
                content=body[start:],
                headers={"Content-Range": f"bytes {start}-{len(body) - 1}/{len(body)}"},
            )
        return httpx.Response(200, content=body)

    return httpx.MockTransport(handler)


@pytest.fixture
def dest(tmp_path):
    return tmp_path / "mirror"


@pytest.fixture
def run(cdn, dest, monkeypatch, capsys):
    """Run the CLI against the fake CDN and the mirror directory; return (code, stdout)."""
    verifier = FakeVerifier()
    log: list[str] = []
    monkeypatch.setattr(
        cli, "make_client", lambda **_kw: make_client(transport=serve(cdn, dest, log))
    )
    monkeypatch.setattr(cli, "SignatureVerifier", lambda client, signing: verifier)

    def go(*argv: str) -> tuple[int, str]:
        log.clear()
        code = cli.main(list(argv))
        return code, capsys.readouterr().out

    go.verifier = verifier
    go.log = log
    return go


def mirror(run, dest, *extra: str) -> tuple[int, str]:
    return run(
        "mirror",
        "--index-url",
        INDEX_URL,
        "--dest",
        str(dest),
        "--base-url",
        MIRROR + "/",
        "--image",
        "debian-13",
        "--image",
        "alpine-linux",
        *extra,
    )


def tree(dest: Path) -> set[str]:
    return {str(p.relative_to(dest)) for p in dest.rglob("*") if p.is_file()}


def test_fresh_mirror_reproduces_the_cdn_layout(run, dest, cdn):
    code, out = mirror(run, dest, "--keep", "2")
    assert code == 0, out
    assert "2/2 images mirrored" in out
    debian_file = f"debian-13-{NEW}-x86_64.qcow2"
    old_file = f"debian-13-{OLD}-x86_64.qcow2"
    assert {
        "index.json",
        "debian-13/latest.json",
        "debian-13/versions.json",
        f"debian-13/{NEW}/{debian_file}",
        f"debian-13/{NEW}/{debian_file}.bundle",
        f"debian-13/{NEW}/{debian_file}.sha256",
        f"debian-13/{NEW}/MANIFEST.json",
        f"debian-13/{NEW}/SHA256SUMS",
        f"debian-13/{NEW}/index.html",
        f"debian-13/{OLD}/{old_file}",
        f"debian-13/{OLD}/{old_file}.bundle",
        "alpine-linux/3.24.1/alpine-linux-3.24.1-uefi-x86_64.qcow2",
        "alpine-linux/3.24.1/MANIFEST-bios.json",
        "alpine-linux/3.24.1/MANIFEST-uefi.json",
    } <= tree(dest)
    assert not any(".part" in p or ".tmp" in p for p in tree(dest))
    assert not (dest / "octavia-amphora").exists()  # service image, not selected

    # Payloads and bundles are copied untouched.
    assert (dest / f"debian-13/{NEW}/{debian_file}").read_bytes() == DEBIAN_DATA
    assert (dest / f"debian-13/{NEW}/{debian_file}.bundle").read_bytes() == cdn[
        f"{BASE}/debian-13/{NEW}/{debian_file}.bundle"
    ]
    assert json.loads((dest / "debian-13/latest.json").read_text()) == {"version": NEW}
    versions = json.loads((dest / "debian-13/versions.json").read_text())
    assert [v["version"] for v in versions["versions"]] == [NEW, OLD]

    # The index points to the mirror, keeps the signing block, lists only what is mirrored.
    index = json.loads((dest / "index.json").read_text())
    assert index["base_url"] == MIRROR
    assert index["mirror"]["source"] == INDEX_URL
    assert index["signing"] == index_dict()["signing"]
    assert [i["id"] for i in index["images"]] == ["debian-13", "alpine-linux"]
    debian = index["images"][0]
    assert debian["versions_url"] == f"{MIRROR}/debian-13/versions.json"
    (a,) = debian["artifacts"]
    assert a["url"] == f"{MIRROR}/debian-13/{NEW}/{debian_file}"
    assert a["bundle_url"] == a["url"] + ".bundle"
    assert a["manifest_url"] == f"{MIRROR}/debian-13/{NEW}/MANIFEST.json"
    assert a["sha256"] == sha256(DEBIAN_DATA)
    parse_index(index)  # still a valid oic-index/v1

    # Every qcow2 was checked against its signature.
    assert sha256(OLD_DATA) in run.verifier.calls


def test_second_run_downloads_nothing(run, dest):
    assert mirror(run, dest, "--keep", "2")[0] == 0
    before = {p: (dest / p).stat().st_mtime_ns for p in tree(dest) if p.endswith(".qcow2")}
    code, out = mirror(run, dest, "--keep", "2")
    assert code == 0, out
    assert "downloading" not in out
    assert "present, digests and signature verified" in out
    assert not [u for u in run.log if u.endswith(".qcow2")]
    after = {p: (dest / p).stat().st_mtime_ns for p in tree(dest) if p.endswith(".qcow2")}
    assert before == after


def test_dry_run_writes_nothing(run, dest):
    code, out = mirror(run, dest, "--dry-run")
    assert code == 0, out
    assert "(dry run)" in out
    assert "would download" in out
    assert not dest.exists()
    assert not [u for u in run.log if u.endswith(".qcow2")]


def test_corrupt_download_is_rejected(run, dest, cdn):
    url = f"{BASE}/debian-13/{NEW}/debian-13-{NEW}-x86_64.qcow2"
    cdn[url] = b"X" + DEBIAN_DATA[1:]  # same size, other digest
    code, out = mirror(run, dest)
    assert code == 1
    assert "sha256 mismatch" in out
    assert "1/2 images mirrored, 1 failed: debian-13" in out
    assert not (dest / "debian-13" / NEW / url.rsplit("/", 1)[-1]).exists()
    assert not any(".part" in p for p in tree(dest))
    index = json.loads((dest / "index.json").read_text())
    assert [i["id"] for i in index["images"]] == ["alpine-linux"]


def test_oversized_download_is_rejected(run, dest, cdn):
    cdn[f"{BASE}/debian-13/{NEW}/debian-13-{NEW}-x86_64.qcow2"] = DEBIAN_DATA + b"more"
    code, out = mirror(run, dest)
    assert code == 1
    assert "size mismatch" in out


def test_bad_signature_downloads_nothing(run, dest):
    run.verifier.fail_for.add(sha256(DEBIAN_DATA))
    code, out = mirror(run, dest)
    assert code == 1
    assert "digest mismatch" in out
    assert not (dest / "debian-13" / NEW).exists()
    assert not [u for u in run.log if u.endswith(f"debian-13-{NEW}-x86_64.qcow2")]


def test_corrupt_local_copy_is_replaced(run, dest):
    assert mirror(run, dest)[0] == 0
    path = dest / "debian-13" / NEW / f"debian-13-{NEW}-x86_64.qcow2"
    path.write_bytes(b"Y" + DEBIAN_DATA[1:])
    code, out = mirror(run, dest)
    assert code == 0, out
    assert "local copy does not match" in out
    assert path.read_bytes() == DEBIAN_DATA


def test_interrupted_download_resumes(run, dest):
    vdir = dest / "debian-13" / NEW
    vdir.mkdir(parents=True)
    (vdir / f".debian-13-{NEW}-x86_64.qcow2.part").write_bytes(DEBIAN_DATA[:1000])
    seen: list[str] = []
    original = cli.make_client

    def recording(**kw):
        client = original(**kw)
        client.event_hooks["request"].append(lambda r: seen.append(r.headers.get("Range", "")))
        return client

    cli.make_client = recording
    try:
        code, out = mirror(run, dest)
    finally:
        cli.make_client = original
    assert code == 0, out
    assert "resuming after" in out
    assert "bytes=1000-" in seen
    assert (vdir / f"debian-13-{NEW}-x86_64.qcow2").read_bytes() == DEBIAN_DATA


def test_older_versions_are_pruned_only_with_prune(run, dest):
    assert mirror(run, dest, "--keep", "2")[0] == 0
    assert (dest / "debian-13" / OLD).is_dir()

    code, out = mirror(run, dest)
    assert code == 0
    assert "1 older version(s) kept on disk (use --prune)" in out
    assert (dest / "debian-13" / OLD).is_dir()
    versions = json.loads((dest / "debian-13/versions.json").read_text())
    assert [v["version"] for v in versions["versions"]] == [NEW, OLD]

    code, out = mirror(run, dest, "--prune", "--dry-run")
    assert "would prune 13-20261020-2640" in out
    assert (dest / "debian-13" / OLD).is_dir()

    code, out = mirror(run, dest, "--prune")
    assert code == 0, out
    assert f"pruned {OLD}" in out
    assert not (dest / "debian-13" / OLD).exists()
    versions = json.loads((dest / "debian-13/versions.json").read_text())
    assert [v["version"] for v in versions["versions"]] == [NEW]


def test_no_prune_when_the_latest_failed(run, dest, cdn):
    assert mirror(run, dest, "--keep", "2")[0] == 0
    run.verifier.fail_for.add(sha256(DEBIAN_DATA))
    code, out = mirror(run, dest, "--prune")
    assert code == 1
    assert "not pruning" in out
    assert (dest / "debian-13" / OLD).is_dir()


def test_failed_update_keeps_the_previous_index_entry(run, dest, cdn):
    assert mirror(run, dest)[0] == 0
    first = json.loads((dest / "index.json").read_text())
    run.verifier.fail_for.add(sha256(DEBIAN_DATA))
    assert mirror(run, dest)[0] == 1
    index = json.loads((dest / "index.json").read_text())
    assert index["images"][0] == first["images"][0]


def test_variant_selection(run, dest):
    code, out = run(
        "mirror",
        "--index-url",
        INDEX_URL,
        "--dest",
        str(dest),
        "--base-url",
        MIRROR,
        "--image",
        "alpine-linux",
        "--variant",
        "uefi",
    )
    assert code == 0, out
    files = tree(dest)
    assert "alpine-linux/3.24.1/alpine-linux-3.24.1-uefi-x86_64.qcow2" in files
    assert "alpine-linux/3.24.1/alpine-linux-3.24.1-bios-x86_64.qcow2" not in files
    (img,) = json.loads((dest / "index.json").read_text())["images"]
    assert [a["variant"] for a in img["artifacts"]] == ["uefi"]
    versions = json.loads((dest / "alpine-linux/versions.json").read_text())
    assert [a["variant"] for a in versions["versions"][0]["artifacts"]] == ["uefi"]


def test_unsafe_names_are_refused(run, dest, cdn):
    doc = json.loads(cdn[f"{BASE}/debian-13/versions.json"])
    doc["versions"][1]["version"] = "../../etc"
    cdn[f"{BASE}/debian-13/versions.json"] = json.dumps(doc).encode()
    code, out = mirror(run, dest, "--keep", "2")
    assert code == 1
    assert "unexpected path" in out or "unsafe version" in out
    assert not (dest.parent / "etc").exists()


def test_base_url_must_be_http(run, dest):
    code, _ = run("mirror", "--index-url", INDEX_URL, "--dest", str(dest), "--base-url", "/srv/x")
    assert code == 2


def test_dest_must_be_a_directory(run, tmp_path):
    f = tmp_path / "file"
    f.write_text("x")
    code, _ = run("mirror", "--index-url", INDEX_URL, "--dest", str(f), "--base-url", MIRROR)
    assert code == 1


def test_lock_refuses_a_second_run(dest, cdn):
    import fcntl

    dest.mkdir()
    with make_client(transport=serve(cdn, dest, [])) as client:
        raw = json.loads(cdn[INDEX_URL])
        index = parse_index(raw)
        m = Mirror(client, FakeVerifier(), dest, MIRROR, index_url=INDEX_URL)
        with (dest / ".oic-sync-mirror.lock").open("a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            with pytest.raises(MirrorError, match="another oic-sync mirror"):
                m.run(raw, index, select_targets(index))


def test_mirror_without_versions_json(run, dest, cdn):
    del cdn[f"{BASE}/debian-13/versions.json"]
    code, out = mirror(run, dest, "--keep", "2")
    assert code == 0, out
    assert "no versions.json upstream" in out
    versions = json.loads((dest / "debian-13/versions.json").read_text())
    (v,) = versions["versions"]
    assert v["version"] == NEW
    assert v["artifacts"][0]["path"] == f"{NEW}/debian-13-{NEW}-x86_64.qcow2"


# -- the mirror as a source for the other commands ---------------------------------------


def test_list_and_verify_from_the_mirror(run, dest):
    assert mirror(run, dest)[0] == 0
    code, out = run("list", "--index-url", f"{MIRROR}/index.json", "--json")
    assert code == 0, out
    entries = json.loads(out)
    assert {e["id"] for e in entries} == {"debian-13", "alpine-linux"}
    assert all(e["url"].startswith(MIRROR + "/") for e in entries)

    code, out = run("verify", "--index-url", f"{MIRROR}/index.json")
    assert code == 0, out
    assert "3/3 signatures verified" in out


def test_openstack_sync_from_the_mirror(run, dest, monkeypatch):
    assert mirror(run, dest, "--image", "debian-13")[0] == 0
    glance = FakeGlance()
    monkeypatch.setattr(
        cli,
        "_backend_factory",
        lambda args, client, **options: OpenStackBackend(
            Glance(glance, PROJECT), client, poll_interval=0, **options
        ),
    )
    code, out = run(
        "openstack", "sync", "--index-url", f"{MIRROR}/index.json", "--image", "debian-13"
    )
    assert code == 0, out
    assert "1/1 images in sync" in out
    assert all(u.startswith(MIRROR) for u in run.log), run.log
    assert any(u.endswith(".qcow2") for u in run.log)
    (img,) = glance.images.values()
    assert img["oic_sha256"] == sha256(DEBIAN_DATA)


def test_mirror_of_a_mirror(run, dest, tmp_path):
    assert mirror(run, dest)[0] == 0
    second = tmp_path / "second"
    code, out = run(
        "mirror",
        "--index-url",
        f"{MIRROR}/index.json",
        "--dest",
        str(second),
        "--base-url",
        "http://second.test",
        "--image",
        "debian-13",
    )
    assert code == 0, out
    index = json.loads((second / "index.json").read_text())
    assert index["images"][0]["artifacts"][0]["url"].startswith("http://second.test/debian-13/")


def test_now_is_injectable(dest, cdn):
    dest.mkdir()
    with make_client(transport=serve(cdn, dest, [])) as client:
        raw = json.loads(cdn[INDEX_URL])
        index = parse_index(raw)
        m = Mirror(
            client,
            FakeVerifier(),
            dest,
            MIRROR,
            index_url=INDEX_URL,
            out=lambda _l: None,
            now=lambda: datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
        )
        m.run(raw, index, select_targets(index, ["debian-13"]))
    assert json.loads((dest / "index.json").read_text())["mirror"]["mirrored_at"] == (
        "2026-01-02T03:04:05Z"
    )
