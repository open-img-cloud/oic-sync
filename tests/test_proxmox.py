from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

from fakes import (
    ALPINE_UEFI_DATA,
    BASE,
    DEBIAN_DATA,
    FakePve,
    FakeVerifier,
    artifact,
    cdn_transport,
    image,
    index_dict,
    sha256,
)
from oic_sync import cli
from oic_sync.backends import proxmox
from oic_sync.backends.base import BackendError
from oic_sync.backends.proxmox import (
    ProxmoxBackend,
    ProxmoxNaming,
    Pvesh,
    dns_name,
    image_tag,
    parse_description,
    render_description,
    split_tags,
    tag,
)
from oic_sync.engine import CLEANUP, DELETE, IMPORT, PROMOTE, RETIRE, Engine, plan_target
from oic_sync.http import make_client
from oic_sync.index import parse_index, select_targets

NOW = datetime(2026, 11, 13, 12, tzinfo=timezone.utc)
INDEX_URL = f"{BASE}/index.json"
OLD_SHA = "a" * 64


@pytest.fixture
def pve(files):
    return FakePve(remote=dict(files))


@pytest.fixture
def make_pve_backend(pve):
    def make(**kwargs):
        kwargs.setdefault("sleep", lambda _s: None)
        kwargs.setdefault("poll_interval", 0)
        kwargs.setdefault("now", lambda: NOW)
        return ProxmoxBackend(pve, "pve", **kwargs)

    return make


@pytest.fixture
def backend(make_pve_backend):
    return make_pve_backend()


def creates(pve, suffix):
    return [c[2] for c in pve.calls if c[0] == "create" and c[1].endswith(suffix)]


def engine(backend, **kwargs):
    lines: list[str] = []
    kwargs.setdefault("verifier", FakeVerifier())
    e = Engine(backend, out=lines.append, now=lambda: NOW, **kwargs)
    return e, lines


# -- names, tags, description -------------------------------------------------------------


def test_dns_names_and_tags():
    assert dns_name("Debian 13 (13-old)") == "debian-13-13-old"
    assert dns_name("alpine-linux-uefi-3.24.1") == "alpine-linux-uefi-3.24.1"
    assert dns_name("a..b--c-") == "a.b-c"
    with pytest.raises(ValueError, match="VM name"):
        dns_name("...")
    assert tag("oic-Ubuntu 24.04") == "oic-ubuntu-24.04"
    assert image_tag("ubuntu-24.04") == "oic-ubuntu-24.04"
    assert split_tags("oic;oic-debian-13, web  x") == ["oic", "oic-debian-13", "web", "x"]
    assert split_tags(None) == []


def test_naming(targets):
    naming = ProxmoxNaming()
    debian, uefi = targets["debian-13"], targets["alpine-linux/uefi"]
    assert naming.stable(debian) == "debian-13"
    assert naming.stable(uefi) == "alpine-linux-uefi"
    assert naming.versioned(debian, "13-20261103-2651") == "debian-13-13-20261103-2651"
    assert naming.versioned(uefi, "3.24.1", "b" * 64) == "alpine-linux-uefi-3.24.1-bbbbbbbbbbbb"


def test_engine_uses_the_backend_naming(targets, backend):
    debian = targets["debian-13"]
    plan = plan_target(debian, [], keep=2, delete=False, now=NOW, naming=backend.naming)
    assert plan.stable_name == "debian-13"
    assert [(a.kind, a.name) for a in plan.actions] == [(IMPORT, "debian-13-13-20261103-2651")]


def test_description_round_trip(targets):
    meta = {"oic_id": "alpine-linux", "oic_variant": "uefi", "version": "3.24.1", "sha256": "c"}
    text = render_description(targets["alpine-linux/uefi"], meta)
    assert "```oic-sync\n" in text
    assert "Login user: `debian`" in text
    assert chr(0x2014) not in text  # no long dash
    assert parse_description(text) == meta
    edited = "My notes\n\n" + text + "\n\nmore notes = not parsed"
    assert parse_description(edited) == meta
    assert parse_description("no block here") == {}
    assert parse_description(None) == {}


# -- pvesh client -------------------------------------------------------------------------


def test_pvesh_argv_and_decoding():
    runs = []

    def runner(argv, **kwargs):
        runs.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, '"UPID:pve:1:2:3:qmcreate:100:root@pam:"\n', "")

    api = Pvesh(runner=runner)
    result = api.call(
        "create", "/nodes/pve/qemu", {"vmid": 100, "template": True, "bios": None, "a-b": "x"}
    )
    assert result == "UPID:pve:1:2:3:qmcreate:100:root@pam:"
    argv, kwargs = runs[0]
    assert argv == [
        "pvesh",
        "create",
        "/nodes/pve/qemu",
        "--vmid",
        "100",
        "--template",
        "1",
        "--a-b",
        "x",
        "--output-format",
        "json",
    ]
    assert kwargs["capture_output"] is True
    assert kwargs["check"] is False
    with pytest.raises(ValueError, match="method"):
        api.argv("post", "/nodes")


def test_pvesh_errors_and_odd_output():
    def failing(argv, **_):
        return subprocess.CompletedProcess(argv, 255, "", "no such VM\nVM 42 not found\n")

    with pytest.raises(BackendError, match="VM 42 not found"):
        Pvesh(runner=failing).call("get", "/nodes/pve/qemu/42/config")

    def missing(argv, **_):
        raise FileNotFoundError("pvesh")

    with pytest.raises(BackendError, match="pvesh"):
        Pvesh(runner=missing).call("get", "/nodes")

    def task_log(argv, **_):
        return subprocess.CompletedProcess(
            argv, 0, "starting\nUPID:pve:00A:1:2:x:1:root@pam:\n", ""
        )

    assert Pvesh(runner=task_log).call("create", "/x") == "UPID:pve:00A:1:2:x:1:root@pam:"

    def empty(argv, **_):
        return subprocess.CompletedProcess(argv, 0, "", "")

    assert Pvesh(runner=empty).call("set", "/x") is None


# -- tasks and node -----------------------------------------------------------------------


def test_wait_task_polls_and_reports_errors(pve, backend):
    ok = pve._task("x", 1)
    backend.wait_task(ok, "thing")
    assert pve.tasks[ok]["polls"] == 2  # running once, then stopped
    bad = pve._task("x", 2, ok=False, error="boom")
    with pytest.raises(BackendError, match=r"thing failed: boom \(starting / TASK ERROR: boom\)"):
        backend.wait_task(bad, "thing")
    backend.wait_task(None, "synchronous call")  # nothing to wait for


def test_wait_task_timeout(pve, make_pve_backend):
    ticks = iter(range(0, 1000, 10))
    backend = make_pve_backend(task_timeout=25, clock=lambda: next(ticks))
    pve.running_polls = 100
    with pytest.raises(BackendError, match="still running"):
        backend.wait_task(pve._task("x", 1), "slow")


def test_local_node(monkeypatch):
    api = FakePve(nodes=["pve1", "pve2"])
    monkeypatch.setattr(proxmox.socket, "gethostname", lambda: "pve2.example.org")
    assert proxmox.local_node(api) == "pve2"
    assert proxmox.local_node(api, "pve1") == "pve1"
    with pytest.raises(BackendError, match="not in the cluster"):
        proxmox.local_node(api, "pve9")
    monkeypatch.setattr(proxmox.socket, "gethostname", lambda: "other")
    with pytest.raises(BackendError, match="--node"):
        proxmox.local_node(api)
    assert proxmox.local_node(FakePve(nodes=["solo"])) == "solo"
    backend = proxmox.connect(api=FakePve(nodes=["solo"]), memory=1024)
    assert backend.node == "solo"
    assert backend.memory == 1024


def test_connect_without_pvesh(monkeypatch):
    monkeypatch.setattr(proxmox.shutil, "which", lambda _name: None)
    with pytest.raises(BackendError, match="Proxmox VE node"):
        proxmox.connect()


# -- import -------------------------------------------------------------------------------


def test_import_bios(targets, pve, backend):
    debian = targets["debian-13"]
    img = backend.import_target(debian, "debian-13-13-20261103-2651")

    (create,) = creates(pve, "/nodes/pve/qemu")
    assert create["vmid"] == 100
    assert create["name"] == "debian-13-13-20261103-2651"
    assert create["tags"] == "oic;oic-debian-13;oic-retired"
    assert create["memory"] == 2048
    assert create["cores"] == 2
    assert create["net0"] == "virtio,bridge=vmbr0"
    assert create["scsihw"] == "virtio-scsi-single"
    assert create["serial0"] == "socket"
    assert create["vga"] == "serial0"
    assert create["agent"] == "enabled=1"
    assert create["ostype"] == "l26"
    assert not {"bios", "machine", "efidisk0"} & set(create)
    meta = parse_description(create["description"])
    filename = "oic-100-debian-13-13-20261103-2651-x86_64.qcow2"
    assert meta == {
        "oic_id": "debian-13",
        "version": "13-20261103-2651",
        "sha256": debian.artifact.sha256,
        "imported_at": "2026-11-13T12:00:00Z",
        "import_file": f"local:import/{filename}",
        "source": debian.artifact.url,
    }

    (download,) = creates(pve, "/storage/local/download-url")
    assert download == {
        "content": "import",
        "filename": filename,
        "url": debian.artifact.url,
        "checksum": debian.artifact.sha256,
        "checksum-algorithm": "sha256",
    }
    (disk,) = creates(pve, "/qemu/100/config")
    assert disk == {
        "scsi0": f"local-lvm:0,import-from=local:import/{filename},discard=on",
        "ide2": "local-lvm:cloudinit",
        "boot": "order=scsi0",
        "ipconfig0": "ip=dhcp",
        "ciuser": "debian",
    }
    assert creates(pve, "/qemu/100/template") == [{}]
    # Order: shell, download, disk, template, then the downloaded file is removed.
    order = [c[1].rsplit("/", 1)[-1] for c in pve.writes()]
    assert order == ["qemu", "download-url", "config", "template", filename]
    assert pve.files() == []

    assert img.id == "100"
    assert img.status == "active"
    assert img.sha256 == debian.artifact.sha256
    assert img.version == debian.artifact.version
    assert img.hidden is True
    assert img.created_at == NOW
    assert pve.vms[100]["template"] == 1


def test_import_uefi_and_options(targets, pve, make_pve_backend):
    backend = make_pve_backend(
        disk_storage="ceph", import_storage="nfs", bridge="vmbr1", memory=4096, cores=4
    )
    pve.add_vm(100, name="someone-else")
    uefi = targets["alpine-linux/uefi"]
    img = backend.import_target(uefi, "alpine-linux-uefi-3.24.1")
    (create,) = creates(pve, "/nodes/pve/qemu")
    assert create["vmid"] == 101
    assert create["bios"] == "ovmf"
    assert create["machine"] == "q35"
    assert create["efidisk0"] == "ceph:1,efitype=4m"
    assert create["net0"] == "virtio,bridge=vmbr1"
    assert (create["memory"], create["cores"]) == (4096, 4)
    assert parse_description(create["description"])["oic_variant"] == "uefi"
    (disk,) = creates(pve, "/qemu/101/config")
    assert disk["scsi0"].startswith("ceph:0,import-from=nfs:import/oic-101-alpine-linux-")
    assert disk["ide2"] == "ceph:cloudinit"
    assert creates(pve, "/storage/nfs/download-url")
    assert img.sha256 == sha256(ALPINE_UEFI_DATA)


def test_agent_off_when_the_index_says_no(files):
    data = index_dict()
    data["images"] = [
        image(
            "cirros",
            "CirrOS",
            [artifact("cirros", "0.6.3", DEBIAN_DATA, extra_props={"hw_qemu_guest_agent": "no"})],
        )
    ]
    (target,) = select_targets(parse_index(data), ["cirros"])
    pve = FakePve(remote={target.artifact.url: DEBIAN_DATA})
    backend = ProxmoxBackend(pve, "pve", sleep=lambda _s: None, poll_interval=0)
    backend.import_target(target, "cirros-0.6.3")
    (create,) = creates(pve, "/nodes/pve/qemu")
    assert "agent" not in create


def test_vmid_base(targets, pve, make_pve_backend):
    backend = make_pve_backend(vmid_base=9000)
    pve.add_vm(9000, name="a")
    pve.add_vm(9001, name="b", node="pve2")  # VMIDs are cluster wide
    assert backend.allocate_vmid() == 9002
    assert ("get", "/cluster/nextid", {"vmid": 9002}) in pve.calls
    with pytest.raises(ValueError, match="vmid base"):
        make_pve_backend(vmid_base=5)


@pytest.mark.parametrize("step", ["disk", "template", "download"])
def test_failed_import_is_cleaned(targets, pve, backend, step):
    pve.fail.add(step)
    with pytest.raises(BackendError):
        backend.import_target(targets["debian-13"], "debian-13-x")
    assert pve.vms == {}
    assert pve.files() == []
    destroy = [c for c in pve.calls if c[0] == "delete" and c[1] == "/nodes/pve/qemu/100"]
    assert destroy[0][2] == {"purge": 1, "destroy-unreferenced-disks": 1, "skiplock": 1}


def test_checksum_mismatch_is_refused(targets, pve, backend):
    debian = targets["debian-13"]
    pve.remote[debian.artifact.url] = b"tampered"
    with pytest.raises(BackendError, match="checksum mismatch"):
        backend.import_target(debian, "debian-13-x")
    assert pve.vms == {}
    assert pve.files() == []


def test_failed_creation_never_destroys_a_vm_of_someone_else(targets, pve, backend):
    pve.add_vm(100, name="production-db")
    pve.nextid_override = 100  # VMID taken between allocation and creation
    with pytest.raises(BackendError, match="already exists"):
        backend.import_target(targets["debian-13"], "debian-13-x")
    assert pve.vms[100]["name"] == "production-db"
    assert not [c for c in pve.calls if c[0] == "delete"]


def test_unsupported_format(targets, backend):
    data = index_dict()
    data["images"][0]["artifacts"][0]["format"] = "iso"
    (target,) = select_targets(parse_index(data), ["debian-13"])
    with pytest.raises(BackendError, match="iso"):
        backend.import_target(target, "x")


# -- listing ------------------------------------------------------------------------------


def test_list_managed_filters_and_parses(targets, pve, backend):
    debian = targets["debian-13"]
    pve.add_template(200, "debian-13", "13-old", OLD_SHA, name="debian-13-13-old", retired=True)
    pve.add_template(
        201,
        "debian-13",
        "13-new",
        "b" * 64,
        name="debian-13",
        imported_at="2026-11-12T08:00:00Z",
        lock="backup",
    )
    pve.add_template(
        202, "debian-13", "13-x", "c" * 64, name="debian-13-13-x", template=False, lock="create"
    )
    pve.add_template(203, "debian-12", "12-x", "d" * 64, name="debian-12")
    pve.add_vm(204, name="web", tags="oic-debian-13")  # no "oic" tag
    forged = pve.add_template(205, "debian-13", "13-y", "e" * 64, name="x")
    forged["description"] = "no block"
    pve.add_template(206, "debian-13", "13-z", "f" * 64, name="debian-13-v", variant="uefi")

    found = {m.id: m for m in backend.list_managed(debian)}
    assert set(found) == {"200", "201", "202"}
    old, new, partial = found["200"], found["201"], found["202"]
    assert (old.name, old.version, old.sha256, old.hidden) == (
        "debian-13-13-old",
        "13-old",
        OLD_SHA,
        True,
    )
    assert old.status == "active"
    assert new.status == "active"  # a locked template is still a finished import
    assert new.hidden is False
    assert new.extra["lock"] == "backup"
    assert new.created_at == datetime(2026, 11, 12, 8, tzinfo=timezone.utc)
    assert partial.status == "importing"
    assert not partial.usable
    # Only the VMs tagged for this image have their config read.
    configs = {c[1] for c in pve.calls if c[1].endswith("/config")}
    assert configs == {f"/nodes/pve/qemu/{n}/config" for n in (200, 201, 202, 205, 206)}


def test_created_at_falls_back_to_meta_ctime(pve, backend, targets):
    cfg = pve.add_template(300, "debian-13", "1", OLD_SHA, name="debian-13")
    cfg["description"] = cfg["description"].replace(
        "imported_at=2026-11-01T00:00:00Z", "imported_at=garbage"
    )
    cfg["meta"] = "creation-qemu=10.0.2,ctime=1762900000"
    (m,) = backend.list_managed(targets["debian-13"])
    assert m.created_at == datetime.fromtimestamp(1762900000, timezone.utc)


# -- rotation through the engine ----------------------------------------------------------


def test_sync_imports_promotes_retires_and_deletes(targets, pve, backend):
    debian = targets["debian-13"]
    pve.add_template(
        200, "debian-13", "13-old", OLD_SHA, name="debian-13", imported_at="2026-10-01T00:00:00Z"
    )
    pve.vms[200]["tags"] = "oic;oic-debian-13;team-a"  # user tags are kept
    e, lines = engine(backend, keep=1, delete=True)
    (result,) = e.sync([debian])
    assert result.ok, lines
    new = next(v for v in pve.vms.values() if v.get("name") == "debian-13")
    assert "oic-retired" not in split_tags(new["tags"])
    assert new["template"] == 1
    assert 200 not in pve.vms  # retired then deleted (beyond --keep 1, unused)
    renames = [c for c in pve.calls if c[0] == "set"]
    assert renames[0][2] == {"name": "debian-13", "tags": "oic;oic-debian-13"}
    assert renames[1] == (
        "set",
        "/nodes/pve/qemu/200/config",
        {"name": "debian-13-13-old", "tags": "oic;oic-debian-13;team-a;oic-retired"},
    )
    assert any("promoted 100 to 'debian-13'" in line for line in lines)

    # Second run: nothing to do.
    before = len(pve.writes())
    (again,) = e.sync([debian])
    assert again.ok
    assert len(pve.writes()) == before
    assert any("up to date" in line for line in lines)


def test_retired_template_kept_when_a_linked_clone_uses_it(targets, pve, backend):
    debian = targets["debian-13"]
    pve.add_template(
        200,
        "debian-13",
        "13-old",
        OLD_SHA,
        name="debian-13-13-old",
        retired=True,
        imported_at="2026-10-01T00:00:00Z",
    )
    pve.add_template(
        201, "debian-13", debian.artifact.version, debian.artifact.sha256, name="debian-13"
    )
    pve.add_vm(300, node="pve2", name="web", scsi0="local-lvm:base-200-disk-0/vm-300-disk-0")
    e, lines = engine(backend, keep=1, delete=True)
    plan = e.plan(debian)
    assert [(a.kind, a.image.id) for a in plan.actions] == [(DELETE, "200")]
    (result,) = e.sync([debian])
    assert result.ok
    assert 200 in pve.vms
    assert any("keep 200: used by" in line for line in lines)
    # The clone on the other node was read through its own node path.
    assert ("get", "/nodes/pve2/qemu/300/config", {}) in pve.calls


def test_in_use(pve, backend, targets):
    pve.add_template(200, "debian-13", "1", OLD_SHA, name="debian-13")
    pve.add_template(2000, "debian-12", "1", OLD_SHA, name="debian-12")
    (m,) = backend.list_managed(targets["debian-13"])
    assert backend.in_use(m) is False
    pve.add_vm(301, name="full-clone", scsi0="local-lvm:vm-301-disk-0")
    pve.add_vm(302, name="other", virtio0="local-lvm:base-2000-disk-0/vm-302-disk-0")
    assert backend.in_use(m) is False  # base-2000 is not base-200
    pve.add_vm(303, name="qcow", unused0="local:200/base-200-disk-0.qcow2/303/vm-303-disk-0.qcow2")
    assert backend.in_use(m) is True
    pve.fail.add("resources")
    assert backend.in_use(m) is None


def test_stale_import_is_cleaned_with_its_file(targets, pve, backend):
    debian = targets["debian-13"]
    volid = "local:import/oic-200-debian.qcow2"
    pve.storage["local"] = {volid: {"volid": volid, "format": "qcow2", "content": "import"}}
    pve.add_template(
        200,
        "debian-13",
        debian.artifact.version,
        debian.artifact.sha256,
        name="debian-13-x",
        template=False,
        lock="create",
        imported_at=(NOW - timedelta(hours=7)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        import_file=volid,
    )
    e, lines = engine(backend)
    plan = e.plan(debian)
    assert [a.kind for a in plan.actions] == [CLEANUP, IMPORT]
    (result,) = e.sync([debian])
    assert result.ok, lines
    assert 200 not in pve.vms
    assert volid not in pve.files()
    destroy = next(c for c in pve.calls if c[0] == "delete" and c[1] == "/nodes/pve/qemu/200")
    assert destroy[2]["skiplock"] == 1


def test_recent_import_in_progress_is_left_alone(targets, pve, backend):
    debian = targets["debian-13"]
    pve.add_template(
        200,
        "debian-13",
        debian.artifact.version,
        debian.artifact.sha256,
        name="debian-13-x",
        template=False,
        lock="create",
        imported_at=(NOW - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    e, _ = engine(backend)
    plan = e.plan(debian)
    assert plan.actions == []
    assert plan.state == "importing"


def test_promote_when_current_is_retired(targets, pve, backend):
    debian = targets["debian-13"]
    pve.add_template(
        200,
        "debian-13",
        debian.artifact.version,
        debian.artifact.sha256,
        name="debian-13-13-20261103-2651",
        retired=True,
    )
    e, _ = engine(backend)
    plan = e.plan(debian)
    assert [(a.kind, a.name) for a in plan.actions] == [(PROMOTE, "debian-13")]
    e.sync([debian])
    assert pve.vms[200]["name"] == "debian-13"
    assert pve.vms[200]["tags"] == "oic;oic-debian-13"
    assert RETIRE not in [a.kind for a in e.plan(debian).actions]


# -- CLI ----------------------------------------------------------------------------------


def test_cli_parsing():
    parser = cli.build_parser()
    args = parser.parse_args(["proxmox", "sync"])
    assert (args.node, args.import_storage, args.disk_storage, args.bridge) == (
        None,
        "local",
        "local-lvm",
        "vmbr0",
    )
    assert (args.vmid_base, args.memory, args.cores, args.keep, args.delete) == (
        None,
        2048,
        2,
        2,
        False,
    )
    assert args.func is cli.cmd_proxmox_sync
    args = parser.parse_args(
        [
            "proxmox", "sync", "--node", "pve1", "--import-storage", "nfs", "--disk-storage",
            "ceph", "--bridge", "vmbr1", "--vmid-base", "9000", "--memory", "1024", "--cores",
            "1", "--keep", "3", "--delete", "--dry-run", "--image", "debian-13", "--variant",
            "uefi", "--no-verify-signature", "--timeout", "60",
        ]
    )  # fmt: skip
    assert args.node == "pve1"
    assert (args.import_storage, args.disk_storage, args.bridge) == ("nfs", "ceph", "vmbr1")
    assert (args.vmid_base, args.memory, args.cores, args.keep) == (9000, 1024, 1, 3)
    assert (args.delete, args.dry_run, args.no_verify_signature) == (True, True, True)
    assert (args.image, args.variant, args.timeout) == (["debian-13"], ["uefi"], 60.0)
    args = parser.parse_args(["proxmox", "status", "--check", "--json", "--node", "x"])
    assert args.func is cli.cmd_proxmox_status
    assert (args.check, args.json) == (True, True)
    with pytest.raises(SystemExit):
        parser.parse_args(["proxmox", "status", "--bridge", "vmbr1"])  # sync only
    with pytest.raises(SystemExit):
        parser.parse_args(["proxmox", "sync", "--cores", "0"])


@pytest.fixture
def run_pve(files, pve, monkeypatch):
    verifier = FakeVerifier()
    built = {}

    def fake_make_client(**_kw):
        return make_client(transport=cdn_transport(files))

    def fake_factory(args, **options):
        built.update(options, node=args.node)
        options.setdefault("task_timeout", 60)
        return ProxmoxBackend(
            pve, args.node or "pve", poll_interval=0, sleep=lambda _s: None, **options
        )

    monkeypatch.setattr(cli, "make_client", fake_make_client)
    monkeypatch.setattr(cli, "_proxmox_factory", fake_factory)
    monkeypatch.setattr(cli, "SignatureVerifier", lambda client, signing: verifier)

    def go(*argv, capsys):
        code = cli.main(list(argv))
        return code, capsys.readouterr().out

    go.verifier = verifier
    go.built = built
    return go


def test_cli_status_then_sync_then_status(run_pve, capsys, pve):
    base = ["--index-url", INDEX_URL]
    code, out = run_pve("proxmox", "status", *base, "--check", capsys=capsys)
    assert code == 1
    assert "missing" in out

    code, out = run_pve("proxmox", "sync", *base, "--dry-run", capsys=capsys)
    assert code == 0
    assert "would import as 'debian-13-13-20261103-2651'" in out
    assert "would promote it to 'alpine-linux-uefi'" in out
    assert pve.vms == {}

    code, out = run_pve(
        "proxmox", "sync", *base, "--vmid-base", "9000", "--bridge", "vmbr1", capsys=capsys
    )
    assert code == 0, out
    assert "3/3 images in sync" in out
    assert run_pve.built["vmid_base"] == 9000
    assert sorted(pve.vms) == [9000, 9001, 9002]
    assert {v["name"] for v in pve.vms.values()} == {
        "debian-13",
        "alpine-linux-bios",
        "alpine-linux-uefi",
    }
    assert len(run_pve.verifier.calls) == 6  # the dry run verifies signatures too

    code, out = run_pve("proxmox", "status", *base, "--check", "--json", capsys=capsys)
    assert code == 0
    data = json.loads(out)
    assert data["backend"].startswith("proxmox node pve")
    assert {r["state"] for r in data["images"]} == {"up-to-date"}
    assert {r["name"] for r in data["images"]} == {
        "debian-13",
        "alpine-linux-bios",
        "alpine-linux-uefi",
    }


def test_cli_reports_backend_errors(monkeypatch, capsys, files):
    monkeypatch.setattr(cli, "make_client", lambda **_: make_client(transport=cdn_transport(files)))
    monkeypatch.setattr(proxmox.shutil, "which", lambda _name: None)
    code = cli.main(["proxmox", "status", "--index-url", INDEX_URL])
    assert code == 1
    assert "oic-sync: error: pvesh not found" in capsys.readouterr().err


def test_openstack_extra_missing(monkeypatch, capsys, files):
    monkeypatch.setitem(sys.modules, "openstack", None)  # import openstack -> ImportError
    monkeypatch.setattr(cli, "make_client", lambda **_: make_client(transport=cdn_transport(files)))
    code = cli.main(["openstack", "status", "--index-url", INDEX_URL, "--cloud", "x"])
    assert code == 1
    assert "pip install 'oic-sync[openstack]'" in capsys.readouterr().err
