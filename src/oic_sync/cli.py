"""Command line interface."""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import sys
from collections.abc import Callable, Sequence
from typing import Any

from oic_sync import __version__
from oic_sync.backends.base import BackendError
from oic_sync.http import make_client
from oic_sync.index import (
    DEFAULT_INDEX_URL,
    SERVICE_IMAGES,
    CatalogIndex,
    IndexFormatError,
    Target,
    fetch_index,
    fetch_index_json,
    parse_index,
    select_targets,
)
from oic_sync.mirror import MirrorError
from oic_sync.signature import SignatureError, SignatureVerifier

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2

log = logging.getLogger("oic_sync")


def _property(value: str) -> tuple[str, str]:
    key, sep, val = value.partition("=")
    if not sep or not key:
        raise argparse.ArgumentTypeError(f"expected KEY=VALUE, got {value!r}")
    return key, val


def _positive(value: str) -> int:
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return n


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--index-url",
        default=os.environ.get("OIC_INDEX_URL", DEFAULT_INDEX_URL),
        help="catalog index (default: %(default)s, or $OIC_INDEX_URL)",
    )
    common.add_argument(
        "--image",
        action="append",
        default=[],
        metavar="ID",
        help="image id to handle, e.g. debian-13 (repeatable; default: every image except "
        f"the service images {', '.join(sorted(SERVICE_IMAGES))})",
    )
    common.add_argument(
        "--variant",
        action="append",
        default=[],
        metavar="NAME",
        help="only this variant of multi-variant images, e.g. uefi (repeatable)",
    )
    common.add_argument(
        "--include-service-images",
        action="store_true",
        help="also handle the service images when no --image is given",
    )
    common.add_argument(
        "--eol",
        choices=("warn", "skip", "fail"),
        default="warn",
        help="images whose upstream release is past its end of life (endoflife.date, as "
        "published in the index): warn and handle them, skip them, or fail before doing "
        "anything (default: %(default)s; an end of life within six months always only warns)",
    )
    common.add_argument("-v", "--verbose", action="count", default=0, help="more logs")

    rotation = argparse.ArgumentParser(add_help=False)
    rotation.add_argument(
        "--keep",
        type=_positive,
        default=2,
        help="versions to keep per image, the current one included (default: %(default)s)",
    )
    rotation.add_argument(
        "--delete",
        action="store_true",
        help="delete versions beyond --keep that nothing uses (instance, volume, linked clone)",
    )

    status_opts = argparse.ArgumentParser(add_help=False)
    status_opts.add_argument("--json", action="store_true", help="JSON output")
    status_opts.add_argument(
        "--check", action="store_true", help="exit 1 when an image is not up to date"
    )

    sync_opts = argparse.ArgumentParser(add_help=False)
    sync_opts.add_argument(
        "--dry-run",
        action="store_true",
        help="show what would change; only read the platform (signatures are still verified)",
    )
    sync_opts.add_argument(
        "--timeout",
        type=float,
        default=3600.0,
        help="seconds to wait for an import step to finish (default: %(default)s)",
    )
    sync_opts.add_argument(
        "--no-verify-signature",
        action="store_true",
        help="import without checking the cosign signature (not recommended)",
    )

    openstack_common = argparse.ArgumentParser(add_help=False)
    openstack_common.add_argument(
        "--cloud",
        default=os.environ.get("OS_CLOUD"),
        help="cloud name in clouds.yaml (default: $OS_CLOUD, else OS_* variables)",
    )
    openstack_common.add_argument("--region", help="region name, if not set in clouds.yaml")

    proxmox_common = argparse.ArgumentParser(add_help=False)
    proxmox_common.add_argument(
        "--node", help="Proxmox VE node (default: the node named like this host)"
    )

    parser = argparse.ArgumentParser(
        prog="oic-sync",
        description="Keep openimages.cloud signed cloud images in sync with your cloud "
        "(OpenStack Glance or Proxmox VE).",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    p_list = sub.add_parser("list", parents=[common], help="list the images of the index")
    p_list.add_argument("--json", action="store_true", help="JSON output")
    p_list.set_defaults(func=cmd_list)

    p_verify = sub.add_parser(
        "verify",
        parents=[common],
        help="verify the cosign signature of every selected artifact (no cloud needed)",
    )
    p_verify.set_defaults(func=cmd_verify)

    p_mirror = sub.add_parser(
        "mirror",
        parents=[common],
        help="copy the signed catalog to a directory or an S3 bucket, as an internal mirror",
        description="Copy the selected images (qcow2, cosign bundles, checksums, manifests) "
        "to DEST in the CDN layout, with an index.json whose URLs point to BASE_URL. Serve "
        "DEST at BASE_URL and use --index-url BASE_URL/index.json with the other commands. "
        "Every file is verified (size, sha256, sha512, cosign signature) before it is moved "
        "into place; files already present with the right digests are kept. DEST can be "
        "s3://bucket/prefix (needs oic-sync[s3]; credentials and endpoint from the usual AWS "
        "settings or --s3-endpoint-url).",
    )
    p_mirror.add_argument(
        "--dest",
        required=True,
        metavar="DEST",
        help="mirror directory (/srv/oic-mirror) or S3 bucket (s3://bucket/oic)",
    )
    p_mirror.add_argument(
        "--s3-endpoint-url",
        metavar="URL",
        help="S3 endpoint of a non-AWS object storage (default: $AWS_ENDPOINT_URL or AWS)",
    )
    p_mirror.add_argument(
        "--staging-dir",
        metavar="DIR",
        help="S3 only: local directory qcow2 files are downloaded and verified in before upload "
        "(default: a directory under $TMPDIR); interrupted downloads resume from it",
    )
    p_mirror.add_argument(
        "--base-url",
        required=True,
        metavar="URL",
        help="URL DEST will be served at, e.g. https://mirror.example.net/oic",
    )
    p_mirror.add_argument(
        "--keep",
        type=_positive,
        default=1,
        help="versions to mirror per image, the latest included (default: %(default)s)",
    )
    p_mirror.add_argument(
        "--prune",
        action="store_true",
        help="delete mirrored versions beyond --keep (only when the kept ones are complete)",
    )
    p_mirror.add_argument(
        "--dry-run",
        action="store_true",
        help="show what would be downloaded or pruned; write nothing (signatures are verified)",
    )
    p_mirror.set_defaults(func=cmd_mirror)

    p_os = sub.add_parser("openstack", help="OpenStack Glance backend")
    os_sub = p_os.add_subparsers(dest="action", required=True, metavar="ACTION")

    p_status = os_sub.add_parser(
        "status",
        parents=[common, openstack_common, rotation, status_opts],
        help="compare the cloud with the index (read only)",
    )
    p_status.set_defaults(func=cmd_openstack_status)

    p_sync = os_sub.add_parser(
        "sync",
        parents=[common, openstack_common, rotation, sync_opts],
        help="import new versions, rotate names, retire old versions",
    )
    p_sync.add_argument(
        "--visibility",
        choices=("private", "shared", "community", "public"),
        default="private",
        help="visibility of new images: public for operators, private or shared for "
        "projects (default: %(default)s)",
    )
    p_sync.add_argument(
        "--import-method",
        choices=("auto", "web-download", "glance-direct", "upload"),
        default="auto",
        help="auto: web-download when Glance offers it, else streaming upload "
        "(default: %(default)s)",
    )
    p_sync.add_argument(
        "--property",
        action="append",
        type=_property,
        default=[],
        metavar="KEY=VALUE",
        help="extra or overriding Glance property for new images (repeatable)",
    )
    p_sync.add_argument(
        "--with-optional-properties",
        action="store_true",
        help="add the index optional properties (hw_rng_model=virtio: the flavors must "
        "allow hw_rng:allowed)",
    )
    p_sync.set_defaults(func=cmd_openstack_sync)

    p_pve = sub.add_parser(
        "proxmox", help="Proxmox VE backend (VM templates; run on a node, as root)"
    )
    pve_sub = p_pve.add_subparsers(dest="action", required=True, metavar="ACTION")
    p_pstatus = pve_sub.add_parser(
        "status",
        parents=[common, proxmox_common, rotation, status_opts],
        help="compare the node templates with the index (read only)",
    )
    p_pstatus.set_defaults(func=cmd_proxmox_status)

    p_psync = pve_sub.add_parser(
        "sync",
        parents=[common, proxmox_common, rotation, sync_opts],
        help="import new versions as templates, rotate names, retire old versions",
    )
    p_psync.add_argument(
        "--import-storage",
        default="local",
        help="file storage with the 'import' content type, for downloads (default: %(default)s)",
    )
    p_psync.add_argument(
        "--disk-storage",
        default="local-lvm",
        help="storage of the template disks, EFI and cloud-init drives (default: %(default)s)",
    )
    p_psync.add_argument("--bridge", default="vmbr0", help="bridge of net0 (default: %(default)s)")
    p_psync.add_argument(
        "--vmid-base",
        type=int,
        metavar="N",
        help="first VMID to use for templates, e.g. 9000 (default: the next free VMID)",
    )
    p_psync.add_argument(
        "--memory",
        type=_positive,
        default=2048,
        metavar="MIB",
        help="memory of the templates in MiB (default: %(default)s)",
    )
    p_psync.add_argument(
        "--cores",
        type=_positive,
        default=2,
        help="CPU cores of the templates (default: %(default)s)",
    )
    p_psync.set_defaults(func=cmd_proxmox_sync)
    return parser


class EolError(Exception):
    """--eol fail and a selected image is past its end of life."""


def apply_eol_policy(targets: list[Target], policy: str) -> list[Target]:
    """Warn on stderr about end of life; drop (skip) or refuse (fail) images past it."""
    seen: set[str] = set()
    eol: list[str] = []
    for t in targets:
        image = t.image
        if image.id in seen or not (image.eol or image.eol_soon):
            continue
        seen.add(image.id)
        assert image.support is not None
        if image.eol:
            eol.append(image.id)
            action = {"warn": "handled anyway", "skip": "skipped", "fail": "refused"}[policy]
        else:
            action = "plan the move to a newer release"
        print(
            f"oic-sync: warning: {image.id}: {image.support.describe()} ({action})", file=sys.stderr
        )
    if eol and policy == "fail":
        raise EolError(
            f"image(s) past their upstream end of life: {', '.join(eol)} "
            "(--eol warn to handle them anyway, --eol skip to leave them out)"
        )
    if policy == "skip":
        return [t for t in targets if not t.image.eol]
    return targets


def _load(args: argparse.Namespace, client: Any) -> tuple[CatalogIndex, list[Target]]:
    index = fetch_index(client, args.index_url)
    targets = select_targets(
        index, args.image, args.variant, include_service=args.include_service_images
    )
    return index, apply_eol_policy(targets, args.eol)


def _support_json(t: Target) -> dict[str, Any] | None:
    s = t.image.support
    if s is None:
        return None
    return {
        "release": s.release,
        "security_until": s.security_until,
        "is_eol": s.is_eol,
        "eol_soon": s.eol_soon,
        "source": s.source,
    }


def cmd_list(args: argparse.Namespace, client: Any, out: Callable[[str], None]) -> int:
    index, targets = _load(args, client)
    if args.json:
        out(
            json.dumps(
                [
                    {
                        "id": t.image.id,
                        "variant": t.artifact.variant,
                        "name": t.stable_name,
                        "version": t.artifact.version,
                        "firmware": t.artifact.firmware,
                        "url": t.artifact.url,
                        "sha256": t.artifact.sha256,
                        "size": t.artifact.size,
                        "support": _support_json(t),
                    }
                    for t in targets
                ],
                indent=2,
            )
        )
        return EXIT_OK
    out(f"# {index.schema}, generated {index.generated_at}")
    for t in targets:
        size = f"{t.artifact.size / 2**20:.0f} MiB" if t.artifact.size else "?"
        mark = "  [EOL]" if t.image.eol else "  [EOL soon]" if t.image.eol_soon else ""
        out(
            f"{t.label:32} {t.artifact.version:24} {t.artifact.firmware:5} {size:>9}  "
            f"{t.stable_name}{mark}"
        )
    return EXIT_OK


def cmd_verify(
    args: argparse.Namespace,
    client: Any,
    out: Callable[[str], None],
    verifier: SignatureVerifier | None = None,
) -> int:
    index, targets = _load(args, client)
    verifier = verifier or SignatureVerifier(client, index.signing)
    failures = 0
    for t in targets:
        try:
            identity = verifier.verify(t.artifact)
        except SignatureError as e:
            failures += 1
            out(f"{t.label}: FAIL {t.artifact.version}: {e}")
            continue
        out(f"{t.label}: OK {t.artifact.version} signed by {identity}")
    out(f"{len(targets) - failures}/{len(targets)} signatures verified")
    return EXIT_FAILED if failures else EXIT_OK


def cmd_mirror(
    args: argparse.Namespace,
    client: Any,
    out: Callable[[str], None],
    verifier: Any = None,
) -> int:
    from pathlib import Path

    from oic_sync.mirror import Mirror
    from oic_sync.stores import open_store

    raw = fetch_index_json(client, args.index_url)
    index = parse_index(raw)
    targets = apply_eol_policy(
        select_targets(
            index, args.image, args.variant, include_service=args.include_service_images
        ),
        args.eol,
    )
    mirror = Mirror(
        client,
        verifier or SignatureVerifier(client, index.signing),
        open_store(
            args.dest,
            endpoint_url=args.s3_endpoint_url,
            staging_dir=Path(args.staging_dir) if args.staging_dir else None,
        ),
        args.base_url,
        index_url=args.index_url,
        keep=args.keep,
        prune=args.prune,
        dry_run=args.dry_run,
        out=out,
    )
    out(
        f"# mirror {args.index_url} -> {mirror.store.describe()} ({mirror.base_url})"
        + (" (dry run)" if args.dry_run else "")
    )
    results = mirror.run(raw, index, targets)
    failed = [r for r in results if not r.ok]
    out(
        f"{len(results) - len(failed)}/{len(results)} images mirrored"
        + (f", {len(failed)} failed: {', '.join(r.image_id for r in failed)}" if failed else "")
    )
    return EXIT_FAILED if failed else EXIT_OK


def _backend_factory(args: argparse.Namespace, client: Any, **options: Any) -> Any:
    from oic_sync.backends.openstack import connect

    return connect(args.cloud, args.region, client, **options)


def _proxmox_factory(args: argparse.Namespace, **options: Any) -> Any:
    from oic_sync.backends.proxmox import connect

    return connect(args.node, **options)


def _status(args: argparse.Namespace, client: Any, out: Callable[[str], None], backend: Any) -> int:
    from oic_sync.engine import UP_TO_DATE, Engine

    _, targets = _load(args, client)
    engine = Engine(backend, verifier=None, keep=args.keep, delete=args.delete, out=out)
    plans = engine.status(targets)
    rows = []
    for p in plans:
        cloud = p.current or (p.older[0] if p.older else None)
        rows.append(
            {
                "id": p.target.image.id,
                "variant": p.target.artifact.variant,
                "name": p.stable_name,
                "index_version": p.target.artifact.version,
                "cloud_version": cloud.version if cloud else None,
                "cloud_image_id": cloud.id if cloud else None,
                "state": p.state,
                "older_versions": len(p.older) - (1 if cloud and not p.current else 0),
                "pending": [a.describe() for a in p.actions],
                "notes": p.notes,
                "support": _support_json(p.target),
            }
        )
    if args.json:
        out(json.dumps({"backend": backend.describe(), "images": rows}, indent=2))
    else:
        out(f"# {backend.describe()}")
        out(f"{'IMAGE':32} {'INDEX':24} {'CLOUD':24} {'STATE':15} OLDER")
        for p, r in zip(plans, rows, strict=True):
            out(
                f"{p.target.label:32} {r['index_version']:24} {r['cloud_version'] or '-':24} "
                f"{r['state']:15} {r['older_versions']}"
            )
            for pending in r["pending"]:
                out(f"    pending: {pending}")
    if args.check and any(p.state != UP_TO_DATE for p in plans):
        return EXIT_FAILED
    return EXIT_OK


def _sync(
    args: argparse.Namespace,
    client: Any,
    out: Callable[[str], None],
    index: CatalogIndex,
    targets: list[Target],
    backend: Any,
    verifier: SignatureVerifier | None,
) -> int:
    from oic_sync.engine import Engine

    if args.no_verify_signature:
        verifier = None
    else:
        verifier = verifier or SignatureVerifier(client, index.signing)
    engine = Engine(
        backend,
        verifier=verifier,
        keep=args.keep,
        delete=args.delete,
        dry_run=args.dry_run,
        out=out,
    )
    out(f"# {backend.describe()}{' (dry run)' if args.dry_run else ''}")
    results = engine.sync(targets)
    failed = [r for r in results if not r.ok]
    out(
        f"{len(results) - len(failed)}/{len(results)} images in sync"
        + (f", {len(failed)} failed: {', '.join(r.target.label for r in failed)}" if failed else "")
    )
    return EXIT_FAILED if failed else EXIT_OK


def cmd_openstack_status(
    args: argparse.Namespace,
    client: Any,
    out: Callable[[str], None],
    backend: Any = None,
) -> int:
    return _status(args, client, out, backend or _backend_factory(args, client))


def cmd_openstack_sync(
    args: argparse.Namespace,
    client: Any,
    out: Callable[[str], None],
    backend: Any = None,
    verifier: SignatureVerifier | None = None,
) -> int:
    index, targets = _load(args, client)
    backend = backend or _backend_factory(
        args,
        client,
        visibility=args.visibility,
        import_method=args.import_method,
        extra_properties=dict(args.property),
        optional_properties=args.with_optional_properties,
        import_timeout=args.timeout,
    )
    return _sync(args, client, out, index, targets, backend, verifier)


def cmd_proxmox_status(
    args: argparse.Namespace,
    client: Any,
    out: Callable[[str], None],
    backend: Any = None,
) -> int:
    return _status(args, client, out, backend or _proxmox_factory(args, read_only=True))


def cmd_proxmox_sync(
    args: argparse.Namespace,
    client: Any,
    out: Callable[[str], None],
    backend: Any = None,
    verifier: SignatureVerifier | None = None,
) -> int:
    index, targets = _load(args, client)
    backend = backend or _proxmox_factory(
        args,
        import_storage=args.import_storage,
        disk_storage=args.disk_storage,
        bridge=args.bridge,
        vmid_base=args.vmid_base,
        memory=args.memory,
        cores=args.cores,
        task_timeout=args.timeout,
    )
    return _sync(args, client, out, index, targets, backend, verifier)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.WARNING - 10 * min(args.verbose, 2),
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    if args.verbose < 2:
        # openstacksdk and httpx are chatty at INFO level.
        for name in ("openstack", "keystoneauth", "httpx", "httpcore", "urllib3"):
            logging.getLogger(name).setLevel(logging.WARNING)

    def out(line: str) -> None:
        print(line, flush=True)

    try:
        with make_client() as client:
            return args.func(args, client, out)
    except (BackendError, MirrorError, EolError) as e:  # e.g. pvesh not found, mirror locked
        print(f"oic-sync: error: {e}", file=sys.stderr)
        return EXIT_FAILED
    except (IndexFormatError, ValueError) as e:
        print(f"oic-sync: error: {e}", file=sys.stderr)
        return EXIT_USAGE
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        # `oic-sync list | head`: the reader went away. Not an error; silence the final
        # flush of stdout as well.
        # No real file descriptor (tests, embedding): nothing to silence.
        with contextlib.suppress(OSError, ValueError):
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return EXIT_OK
    except Exception as e:
        log.debug("fatal error", exc_info=True)
        print(f"oic-sync: error: {type(e).__name__}: {e}", file=sys.stderr)
        return EXIT_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
