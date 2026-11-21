from __future__ import annotations

import json

import pytest

import oic_sync
from fakes import BASE, PROJECT, FakeVerifier, cdn_transport
from oic_sync import cli
from oic_sync.backends.openstack import Glance, OpenStackBackend
from oic_sync.http import make_client

INDEX_URL = f"{BASE}/index.json"


@pytest.fixture
def run(files, glance, monkeypatch):
    """Run the CLI against the fake CDN and the fake Glance; return (exit code, stdout)."""
    verifier = FakeVerifier()
    built = {}

    def fake_make_client(**_kw):
        return make_client(transport=cdn_transport(files))

    def fake_backend_factory(args, client, **options):
        built.update(options, cloud=args.cloud)
        return OpenStackBackend(Glance(glance, PROJECT), client, poll_interval=0, **options)

    monkeypatch.setattr(cli, "make_client", fake_make_client)
    monkeypatch.setattr(cli, "_backend_factory", fake_backend_factory)
    monkeypatch.setattr(cli, "SignatureVerifier", lambda client, signing: verifier)

    def go(*argv, capsys):
        code = cli.main([*argv[:1], *argv[1:]])
        return code, capsys.readouterr().out

    go.verifier = verifier
    go.built = built
    return go


def test_version(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["--version"])
    assert e.value.code == 0
    assert f"oic-sync {oic_sync.__version__}" in capsys.readouterr().out


def test_list(run, capsys):
    code, out = run("list", "--index-url", INDEX_URL, capsys=capsys)
    assert code == 0
    assert "alpine-linux/uefi" in out
    assert "octavia-amphora" not in out


def test_list_json(run, capsys):
    code, out = run(
        "list", "--index-url", INDEX_URL, "--json", "--image", "debian-13", capsys=capsys
    )
    assert code == 0
    (entry,) = json.loads(out)
    assert entry["name"] == "Debian 13"


def test_verify_reports_failures(run, capsys, index):
    debian_sha = index.image("debian-13").artifacts[0].sha256
    run.verifier.fail_for.add(debian_sha)
    code, out = run("verify", "--index-url", INDEX_URL, capsys=capsys)
    assert code == 1
    assert "debian-13: FAIL" in out
    assert "2/3 signatures verified" in out


def test_unknown_image_is_usage_error(run, capsys):
    code, _ = run("list", "--index-url", INDEX_URL, "--image", "nope", capsys=capsys)
    assert code == 2


def test_bad_property_syntax(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["openstack", "sync", "--property", "novalue"])
    assert e.value.code == 2


def test_keep_must_be_positive(capsys):
    with pytest.raises(SystemExit):
        cli.main(["openstack", "sync", "--keep", "0"])


def test_status_then_sync_then_status(run, capsys, glance):
    base = ["--index-url", INDEX_URL, "--cloud", "test"]
    code, out = run("openstack", "status", *base, "--check", capsys=capsys)
    assert code == 1
    assert "missing" in out

    code, out = run("openstack", "sync", *base, "--dry-run", capsys=capsys)
    assert code == 0
    assert "would import" in out
    assert glance.images == {}

    code, out = run(
        "openstack",
        "sync",
        *base,
        "--visibility",
        "public",
        "--property",
        "hw_rng_model=virtio",
        capsys=capsys,
    )
    assert code == 0, out
    assert "3/3 images in sync" in out
    assert run.built["visibility"] == "public"
    assert run.built["cloud"] == "test"
    assert all(i["visibility"] == "public" for i in glance.images.values())
    assert all(i["hw_rng_model"] == "virtio" for i in glance.images.values())

    code, out = run("openstack", "status", *base, "--check", "--json", capsys=capsys)
    assert code == 0
    data = json.loads(out)
    assert {r["state"] for r in data["images"]} == {"up-to-date"}


def test_sync_failure_exit_code(run, capsys, index):
    run.verifier.fail_for.add(index.image("debian-13").artifacts[0].sha256)
    code, out = run(
        "openstack", "sync", "--index-url", INDEX_URL, "--image", "debian-13", capsys=capsys
    )
    assert code == 1
    assert "1 failed: debian-13" in out


def test_no_verify_signature(run, capsys):
    code, out = run(
        "openstack",
        "sync",
        "--index-url",
        INDEX_URL,
        "--image",
        "debian-13",
        "--no-verify-signature",
        capsys=capsys,
    )
    assert code == 0
    assert "NOT verified" in out
    assert run.verifier.calls == []


def test_broken_pipe_is_not_an_error(monkeypatch, capsys):
    from oic_sync import cli as cli_mod

    def boom(*_a, **_k):
        raise BrokenPipeError

    monkeypatch.setattr(cli_mod, "cmd_list", boom)
    parser = cli_mod.build_parser()
    for action in parser._subparsers._group_actions[0].choices.values():
        if action.prog.endswith(" list"):
            action.set_defaults(func=boom)
    monkeypatch.setattr(cli_mod, "build_parser", lambda: parser)
    assert cli_mod.main(["list"]) == cli_mod.EXIT_OK
    assert "error" not in capsys.readouterr().err


def _with_support(files, **support):
    """Marks debian-13 with an endoflife.date support block in the fake index."""
    index = json.loads(files[INDEX_URL])
    index["images"][0]["support"] = {
        "product": "debian",
        "release": "13",
        "active_until": None,
        "security_until": "2026-06-30",
        "extended_until": None,
        "follows_releases": False,
        "source": "https://endoflife.date/debian",
        **support,
    }
    files[INDEX_URL] = json.dumps(index).encode()


def test_eol_warns_by_default(run, capsys, files):
    _with_support(files, is_eol=True, eol_soon=False)
    code, out = run("list", "--index-url", INDEX_URL, capsys=capsys)
    assert code == 0
    assert "debian-13" in out
    assert "[EOL]" in out


def test_eol_warning_on_stderr(files, capsys, run):
    _with_support(files, is_eol=True, eol_soon=False)
    cli.main(["list", "--index-url", INDEX_URL])
    err = capsys.readouterr().err
    assert "debian-13: release 13 reached its upstream end of life on 2026-06-30" in err


def test_eol_skip(run, capsys, files):
    _with_support(files, is_eol=True, eol_soon=False)
    code, out = run("list", "--index-url", INDEX_URL, "--eol", "skip", "--json", capsys=capsys)
    assert code == 0
    assert "debian-13" not in [e["id"] for e in json.loads(out)]


def test_eol_fail(run, capsys, files):
    _with_support(files, is_eol=True, eol_soon=False)
    code, _ = run("list", "--index-url", INDEX_URL, "--eol", "fail", capsys=capsys)
    assert code == 1


def test_eol_soon_only_warns(run, capsys, files):
    _with_support(files, is_eol=False, eol_soon=True)
    code, out = run("list", "--index-url", INDEX_URL, "--eol", "fail", "--json", capsys=capsys)
    assert code == 0
    (debian,) = [e for e in json.loads(out) if e["id"] == "debian-13"]
    assert debian["support"]["eol_soon"] is True


def test_malformed_support_is_ignored(run, capsys, files):
    index = json.loads(files[INDEX_URL])
    index["images"][0]["support"] = "soon"
    files[INDEX_URL] = json.dumps(index).encode()
    code, _ = run("list", "--index-url", INDEX_URL, "--eol", "fail", capsys=capsys)
    assert code == 0
