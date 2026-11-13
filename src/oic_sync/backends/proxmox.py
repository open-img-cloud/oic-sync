"""Proxmox VE backend: one VM template per imported version, on the local node.

oic-sync runs on a Proxmox VE node (like pveam) and talks to the local API through the `pvesh`
command line tool (`pvesh get|create|set|delete <path> --output-format json`), as root: no
API token and no remote mode in this version. Every call goes through a small client (Pvesh)
so the tests use a fake.

A managed image is a VM template. Ours carry the tags `oic` and `oic-<image id>`, and a
machine-readable block in the VM description:

    ```oic-sync
    oic_id=debian-13
    version=13-20261103-2651
    sha256=...
    imported_at=2026-11-13T09:00:00Z
    import_file=local:import/oic-9001-debian-13-13-20261103-2651-x86_64.qcow2
    ```

Proxmox has no hidden flag: retired versions get the tag `oic-retired`. VM names must be DNS
names, so the stable name is the image id ("debian-13", "alpine-linux-uefi") and retired
versions are "<image id>-<version>".

Import, in an order that leaves a trace of every step in the VM itself:

1. allocate a VMID and create the VM shell (name, tags, description, hardware), not a
   template yet: a non-template VM carrying our tags is an import in progress;
2. download the qcow2 into the `import` content of a file storage with `download-url`;
   Proxmox checks the sha256 of the index (the digest the cosign bundle signs);
3. import the disk (`scsi0: <storage>:0,import-from=<file>`) and add the cloud-init drive;
4. convert to a template, then delete the downloaded file.

On any failure the VM and the file are removed. A crashed run leaves a non-template VM that
the engine removes after 6 hours (with the file named in its description).
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import socket
import subprocess
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any, Protocol
from urllib.parse import urlsplit

from oic_sync.backends.base import BackendError, ManagedImage
from oic_sync.index import Target

log = logging.getLogger(__name__)

MANAGED_TAG = "oic"
RETIRED_TAG = "oic-retired"
BLOCK_LANG = "oic-sync"
IMPORT_FORMATS = ("qcow2", "raw", "vmdk")

# Disk-like keys of a VM config: a linked clone of template X references base-X-disk-N there.
DISK_KEY = re.compile(r"^(?:scsi|virtio|sata|ide|unused)\d+$|^(?:efidisk|tpmstate)0$")
_BLOCK = re.compile(r"```" + BLOCK_LANG + r"[ \t]*\r?\n(.*?)\r?\n?```", re.DOTALL)
_TAG_SPLIT = re.compile(r"[;,\s]+")
_EPOCH = datetime.fromtimestamp(0, timezone.utc)


# -- names and tags ----------------------------------------------------------------------


def dns_name(text: str) -> str:
    """A valid Proxmox VM name (format dns-name): lowercase labels of [a-z0-9-]."""
    labels = []
    for label in text.lower().split("."):
        label = re.sub(r"[^a-z0-9-]+", "-", label)
        label = re.sub(r"-{2,}", "-", label).strip("-")[:63].strip("-")
        if label:
            labels.append(label)
    if not labels:
        raise ValueError(f"cannot make a VM name out of {text!r}")
    return ".".join(labels)


def tag(text: str) -> str:
    """A valid Proxmox tag ([a-z0-9_][a-z0-9_+.-]*)."""
    value = re.sub(r"[^a-z0-9_+.-]+", "-", text.lower()).lstrip("-+.")
    if not value:
        raise ValueError(f"cannot make a tag out of {text!r}")
    return value


def image_tag(image_id: str) -> str:
    return tag(f"oic-{image_id}")


def split_tags(value: str | None) -> list[str]:
    return [t for t in _TAG_SPLIT.split(value or "") if t]


class ProxmoxNaming:
    """Stable name "debian-13" ("alpine-linux-uefi" for a variant), retired "debian-13-<ver>"."""

    def stable(self, target: Target) -> str:
        base = target.image.id
        if target.artifact.variant and len(target.image.artifacts) > 1:
            base += f"-{target.artifact.variant}"
        return dns_name(base)

    def versioned(self, target: Target, version: str, sha256: str | None = None) -> str:
        name = f"{self.stable(target)}-{version}"
        if sha256:
            name += f"-{sha256[:12]}"
        return dns_name(name)


# -- description block -------------------------------------------------------------------


def parse_description(text: str | None) -> dict[str, str]:
    """key=value lines of the ```oic-sync block of a VM description ({} when absent)."""
    match = _BLOCK.search(text or "")
    if not match:
        return {}
    meta = {}
    for line in match.group(1).splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key:
            meta[key.strip()] = value.strip()
    return meta


def render_description(target: Target, meta: Mapping[str, str]) -> str:
    image, artifact = target.image, target.artifact
    title = image.title + (f" ({artifact.variant})" if artifact.variant else "")
    lines = [
        f"**{title}** {artifact.version}, from [openimages.cloud](https://openimages.cloud).",
        "",
        "Imported and rotated by oic-sync: do not edit the block below.",
    ]
    if image.login_user:
        lines.append(f"Login user: `{image.login_user}` (cloud-init).")
    lines += ["", f"```{BLOCK_LANG}", *(f"{k}={v}" for k, v in meta.items()), "```"]
    return "\n".join(lines)


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _meta_ctime(meta: str | None) -> datetime | None:
    """ctime of the VM config `meta` property (creation-qemu=...,ctime=1700000000)."""
    match = re.search(r"(?:^|,)ctime=(\d+)", meta or "")
    return datetime.fromtimestamp(int(match.group(1)), timezone.utc) if match else None


# -- pvesh client ------------------------------------------------------------------------


class PveApi(Protocol):
    """The local Proxmox VE API: method is get, create (POST), set (PUT) or delete."""

    def call(self, method: str, path: str, params: Mapping[str, Any] | None = None) -> Any: ...


class Pvesh:
    """Runs `pvesh <method> <path> --key value ... --output-format json`."""

    METHODS = ("get", "create", "set", "delete")

    def __init__(
        self,
        binary: str = "pvesh",
        *,
        runner: Callable[..., Any] = subprocess.run,
        timeout: float = 600.0,
    ) -> None:
        self.binary = binary
        self.runner = runner
        self.timeout = timeout

    def argv(self, method: str, path: str, params: Mapping[str, Any] | None = None) -> list[str]:
        if method not in self.METHODS:
            raise ValueError(f"unknown pvesh method {method!r}")
        argv = [self.binary, method, path]
        for key, value in (params or {}).items():
            if value is None:
                continue
            if isinstance(value, bool):
                value = int(value)
            argv += [f"--{key}", str(value)]
        return [*argv, "--output-format", "json"]

    def call(self, method: str, path: str, params: Mapping[str, Any] | None = None) -> Any:
        argv = self.argv(method, path, params)
        log.debug("run %s", " ".join(argv))
        try:
            proc = self.runner(
                argv, capture_output=True, text=True, timeout=self.timeout, check=False
            )
        except (OSError, subprocess.SubprocessError) as e:
            raise BackendError(f"pvesh {method} {path}: {e}") from e
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip().splitlines()
            detail = " / ".join(err[-3:])[:500]
            raise BackendError(f"pvesh {method} {path} failed ({proc.returncode}): {detail}")
        return _decode(proc.stdout)


def _decode(stdout: str) -> Any:
    text = (stdout or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        # Some calls print a task log before (or instead of) the JSON result: keep the UPID.
        match = re.search(r"UPID:[^\s\"]+", text)
        return match.group(0) if match else text


# -- backend -----------------------------------------------------------------------------


class ProxmoxBackend:
    naming = ProxmoxNaming()

    def __init__(
        self,
        api: PveApi,
        node: str,
        *,
        import_storage: str = "local",
        disk_storage: str = "local-lvm",
        bridge: str = "vmbr0",
        vmid_base: int | None = None,
        memory: int = 2048,
        cores: int = 2,
        task_timeout: float = 3600.0,
        poll_interval: float = 2.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if vmid_base is not None and not 100 <= vmid_base <= 999_999_999:
            raise ValueError("vmid base must be between 100 and 999999999")
        if memory < 16 or cores < 1:
            raise ValueError("memory must be at least 16 MiB and cores at least 1")
        self.api = api
        self.node = node
        self.import_storage = import_storage
        self.disk_storage = disk_storage
        self.bridge = bridge
        self.vmid_base = vmid_base
        self.memory = memory
        self.cores = cores
        self.task_timeout = task_timeout
        self.poll_interval = poll_interval
        self.sleep = sleep
        self.clock = clock
        self.now = now

    def describe(self) -> str:
        return (
            f"proxmox node {self.node}, disks on {self.disk_storage}, "
            f"downloads on {self.import_storage}"
        )

    @property
    def _qemu(self) -> str:
        return f"/nodes/{self.node}/qemu"

    # -- tasks ---------------------------------------------------------------------------

    def wait_task(self, upid: Any, what: str) -> None:
        """Wait for a worker task (UPID) to stop; raise BackendError unless it ended OK."""
        if not isinstance(upid, str) or not upid.startswith("UPID:"):
            return  # synchronous call, nothing to wait for
        parts = upid.split(":")
        node = parts[1] if len(parts) > 1 and parts[1] else self.node
        path = f"/nodes/{node}/tasks/{upid}"
        deadline = self.clock() + self.task_timeout
        while True:
            status = self.api.call("get", f"{path}/status") or {}
            if status.get("status") == "stopped":
                break
            if self.clock() >= deadline:
                raise BackendError(f"{what}: task {upid} still running after {self.task_timeout}s")
            self.sleep(self.poll_interval)
        exit_status = str(status.get("exitstatus") or "")
        if exit_status == "OK":
            return
        if exit_status.startswith("WARNINGS"):
            log.warning("%s: task ended with %s", what, exit_status)
            return
        raise BackendError(f"{what} failed: {exit_status or 'unknown'}{self._task_tail(path)}")

    def _task_tail(self, path: str) -> str:
        try:
            lines = self.api.call("get", f"{path}/log", {"limit": 500}) or []
            tail = [str(line.get("t", "")) for line in lines if isinstance(line, dict)][-3:]
        except BackendError:
            return ""
        return f" ({' / '.join(t for t in tail if t)})" if tail else ""

    # -- listing -------------------------------------------------------------------------

    def _config(self, vmid: int | str, node: str | None = None) -> dict[str, Any]:
        cfg = self.api.call("get", f"/nodes/{node or self.node}/qemu/{vmid}/config")
        return cfg if isinstance(cfg, dict) else {}

    def list_managed(self, target: Target) -> list[ManagedImage]:
        wanted_tag = image_tag(target.image.id)
        managed = []
        for vm in self.api.call("get", self._qemu) or []:
            tags = split_tags(vm.get("tags"))
            if MANAGED_TAG not in tags or wanted_tag not in tags:
                continue
            vmid = vm["vmid"]
            cfg = self._config(vmid)
            meta = parse_description(cfg.get("description"))
            if meta.get("oic_id") != target.image.id:
                continue
            if (meta.get("oic_variant") or None) != target.artifact.variant:
                continue
            managed.append(self._managed(vmid, cfg, meta))
        return managed

    def _managed(
        self, vmid: int | str, cfg: Mapping[str, Any], meta: Mapping[str, str]
    ) -> ManagedImage:
        tags = split_tags(cfg.get("tags"))
        lock = str(cfg.get("lock") or "")
        # A template is a finished import, even when locked for a while (backup, migration):
        # only a VM not converted yet is an import in progress (or an abandoned one).
        status = "active" if str(cfg.get("template") or "0") in ("1", "true") else "importing"
        created = _parse_time(meta.get("imported_at")) or _meta_ctime(cfg.get("meta")) or _EPOCH
        return ManagedImage(
            id=str(vmid),
            name=str(cfg.get("name") or ""),
            version=meta.get("version", ""),
            sha256=meta.get("sha256", "").lower(),
            status=status,
            hidden=RETIRED_TAG in tags,
            created_at=created,
            extra={
                "vmid": str(vmid),
                "tags": ";".join(tags),
                "lock": lock,
                "import_file": meta.get("import_file", ""),
            },
        )

    # -- import --------------------------------------------------------------------------

    def allocate_vmid(self) -> int:
        if self.vmid_base is None:
            return int(self.api.call("get", "/cluster/nextid"))
        used = {
            int(r["vmid"])
            for r in self.api.call("get", "/cluster/resources", {"type": "vm"}) or []
            if r.get("vmid") is not None
        }
        vmid = self.vmid_base
        while vmid in used:
            vmid += 1
        # Asserts the id is free (fails if it was taken meanwhile or is reserved).
        return int(self.api.call("get", "/cluster/nextid", {"vmid": vmid}))

    def import_filename(self, target: Target, vmid: int) -> str:
        base = urlsplit(target.artifact.url).path.rsplit("/", 1)[-1]
        base = re.sub(r"[^A-Za-z0-9._-]+", "_", base) or f"image.{target.artifact.format}"
        if not base.endswith(f".{target.artifact.format}"):
            base += f".{target.artifact.format}"
        return f"oic-{vmid}-{base}"

    def create_params(
        self, target: Target, name: str, vmid: int, import_volid: str
    ) -> dict[str, Any]:
        """Parameters of POST /nodes/<node>/qemu: the VM shell, without the disk."""
        artifact = target.artifact
        meta = {"oic_id": target.image.id}
        if artifact.variant:
            meta["oic_variant"] = artifact.variant
        meta.update(
            version=artifact.version,
            sha256=artifact.sha256,
            imported_at=self.now().strftime("%Y-%m-%dT%H:%M:%SZ"),
            import_file=import_volid,
            source=artifact.url,
        )
        props = artifact.glance_properties
        params: dict[str, Any] = {
            "vmid": vmid,
            "name": name,
            "tags": ";".join([MANAGED_TAG, image_tag(target.image.id), RETIRED_TAG]),
            "description": render_description(target, meta),
            "memory": self.memory,
            "cores": self.cores,
            "net0": f"virtio,bridge={self.bridge}",
            "scsihw": "virtio-scsi-single",
            "serial0": "socket",
            "vga": "serial0",
        }
        if props.get("os_type", "linux") == "linux":
            params["ostype"] = "l26"
        if props.get("hw_qemu_guest_agent", "yes").lower() not in ("no", "false", "0"):
            params["agent"] = "enabled=1"
        if artifact.firmware == "uefi":
            params.update(bios="ovmf", machine="q35", efidisk0=f"{self.disk_storage}:1,efitype=4m")
        return params

    def disk_params(self, target: Target, import_volid: str) -> dict[str, Any]:
        """Parameters of POST /nodes/<node>/qemu/<vmid>/config: disk import and cloud-init."""
        params: dict[str, Any] = {
            "scsi0": f"{self.disk_storage}:0,import-from={import_volid},discard=on",
            "ide2": f"{self.disk_storage}:cloudinit",
            "boot": "order=scsi0",
            "ipconfig0": "ip=dhcp",
        }
        if target.image.login_user:
            params["ciuser"] = target.image.login_user
        return params

    def import_target(self, target: Target, name: str) -> ManagedImage:
        artifact = target.artifact
        if artifact.format not in IMPORT_FORMATS:
            raise BackendError(f"Proxmox cannot import {artifact.format} images")
        vmid = self.allocate_vmid()
        filename = self.import_filename(target, vmid)
        volid = f"{self.import_storage}:import/{filename}"
        downloaded = False
        try:
            log.info("%s: creating VM %s", target.label, vmid)
            params = self.create_params(target, name, vmid, volid)
            upid = self.api.call("create", self._qemu, params)
            self.wait_task(upid, f"create VM {vmid}")

            log.info("%s: downloading %s", target.label, artifact.url)
            downloaded = True
            upid = self.api.call(
                "create",
                f"/nodes/{self.node}/storage/{self.import_storage}/download-url",
                {
                    "content": "import",
                    "filename": filename,
                    "url": artifact.url,
                    "checksum": artifact.sha256,
                    "checksum-algorithm": "sha256",
                },
            )
            self.wait_task(upid, f"download of {artifact.url}")

            log.info("%s: importing the disk into %s", target.label, self.disk_storage)
            upid = self.api.call(
                "create", f"{self._qemu}/{vmid}/config", self.disk_params(target, volid)
            )
            self.wait_task(upid, f"disk import into VM {vmid}")

            upid = self.api.call("create", f"{self._qemu}/{vmid}/template")
            self.wait_task(upid, f"conversion of VM {vmid} to a template")
        except BaseException:
            log.warning("%s: removing incomplete VM %s", target.label, vmid)
            self._destroy_if_ours(vmid, volid)
            if downloaded:
                self.remove_import_file(volid)
            raise
        self.remove_import_file(volid)
        cfg = self._config(vmid)
        image = self._managed(vmid, cfg, parse_description(cfg.get("description")))
        if image.status != "active" or image.sha256 != artifact.sha256:
            raise BackendError(f"VM {vmid} is not a template of {artifact.sha256} after import")
        return image

    def remove_import_file(self, volid: str) -> None:
        """Delete a downloaded file from the import storage, if it is there (best effort)."""
        storage = volid.split(":", 1)[0]
        base = f"/nodes/{self.node}/storage/{storage}/content"
        try:
            content = self.api.call("get", base, {"content": "import"}) or []
            if not any(item.get("volid") == volid for item in content):
                return
            # With a delay, the API waits for the task and returns null once it is done.
            upid = self.api.call("delete", f"{base}/{volid}", {"delay": 30})
            self.wait_task(upid, f"removal of {volid}")
        except BackendError as e:
            log.warning("could not remove %s: %s", volid, e)

    # -- rotation ------------------------------------------------------------------------

    def rename(self, image: ManagedImage, name: str, hidden: bool) -> None:
        tags = [t for t in split_tags(image.extra.get("tags")) if t != RETIRED_TAG]
        if hidden:
            tags.append(RETIRED_TAG)
        self.api.call(
            "set", f"{self._qemu}/{image.id}/config", {"name": name, "tags": ";".join(tags)}
        )

    def _destroy(self, vmid: int | str, *, skiplock: bool = False, quiet: bool = False) -> None:
        params: dict[str, Any] = {"purge": 1, "destroy-unreferenced-disks": 1}
        if skiplock:
            params["skiplock"] = 1
        try:
            upid = self.api.call("delete", f"{self._qemu}/{vmid}", params)
            self.wait_task(upid, f"destruction of VM {vmid}")
        except BackendError as e:
            if not quiet:
                raise
            log.error("could not remove VM %s: %s", vmid, e)

    def _destroy_if_ours(self, vmid: int, volid: str) -> None:
        """Remove the VM of a failed import, only if it is the one this import created.

        The VMID could have been taken by someone else between allocation and creation:
        the description must name this import's file.
        """
        try:
            cfg = self._config(vmid)
        except BackendError:
            return  # never created
        if parse_description(cfg.get("description")).get("import_file") != volid:
            log.warning("VM %s is not the one this import created; left alone", vmid)
            return
        self._destroy(vmid, skiplock=True, quiet=True)

    def delete(self, image: ManagedImage) -> None:
        # An abandoned import (not a template, older than the stale delay) may still hold the
        # "create" lock of a crashed run: skip it. Templates are never force-unlocked.
        self._destroy(image.id, skiplock=not image.usable)
        if image.extra.get("import_file"):
            self.remove_import_file(image.extra["import_file"])

    def in_use(self, image: ManagedImage) -> bool | None:
        """True when a VM of the cluster is a linked clone of the template.

        A linked clone references the template base volumes (base-<vmid>-disk-N) in its
        disks; every VM config of the cluster is checked. None when a call fails.
        """
        base = re.compile(rf"(?:^|[:/])base-{re.escape(image.id)}-disk-\d+")
        try:
            for vm in self.api.call("get", "/cluster/resources", {"type": "vm"}) or []:
                if vm.get("type") != "qemu" or str(vm.get("vmid")) == image.id:
                    continue
                cfg = self._config(vm["vmid"], vm.get("node") or self.node)
                for key, value in cfg.items():
                    if DISK_KEY.match(key) and isinstance(value, str) and base.search(value):
                        log.info("template %s is used by VM %s (%s)", image.id, vm["vmid"], key)
                        return True
        except (BackendError, KeyError, TypeError, AttributeError) as e:
            log.warning("usage check for %s failed: %s", image.id, e)
            return None
        return False


def local_node(api: PveApi, node: str | None = None) -> str:
    """The node to work on: --node, else the one named like this host, else the only one."""
    nodes = [str(n.get("node")) for n in api.call("get", "/nodes") or [] if n.get("node")]
    if node:
        if nodes and node not in nodes:
            raise BackendError(f"node {node!r} not in the cluster ({', '.join(nodes)})")
        return node
    short = socket.gethostname().split(".", 1)[0]
    if short in nodes:
        return short
    if len(nodes) == 1:
        return nodes[0]
    raise BackendError(f"cannot tell the local node among {', '.join(nodes) or 'none'}: use --node")


def connect(
    node: str | None = None, *, api: PveApi | None = None, **options: Any
) -> ProxmoxBackend:
    """Backend on the local node, through pvesh (must run on a Proxmox VE node, as root)."""
    if api is None:
        if shutil.which("pvesh") is None:
            raise BackendError("pvesh not found: run oic-sync on a Proxmox VE node, as root")
        api = Pvesh()
    return ProxmoxBackend(api, local_node(api, node), **options)
