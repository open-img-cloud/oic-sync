"""Command line interface."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections.abc import Callable, Sequence
from typing import Any

from oic_sync import __version__
from oic_sync.http import make_client
from oic_sync.index import (
    DEFAULT_INDEX_URL,
    SERVICE_IMAGES,
    CatalogIndex,
    IndexFormatError,
    Target,
    fetch_index,
    select_targets,
)
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
    common.add_argument("-v", "--verbose", action="count", default=0, help="more logs")

    openstack_common = argparse.ArgumentParser(add_help=False)
    openstack_common.add_argument(
        "--cloud",
        default=os.environ.get("OS_CLOUD"),
        help="cloud name in clouds.yaml (default: $OS_CLOUD, else OS_* variables)",
    )
    openstack_common.add_argument("--region", help="region name, if not set in clouds.yaml")
    openstack_common.add_argument(
        "--keep",
        type=_positive,
        default=2,
        help="versions to keep per image, the current one included (default: %(default)s)",
    )
    openstack_common.add_argument(
        "--delete",
        action="store_true",
        help="delete versions beyond --keep that no instance or volume uses",
    )

    parser = argparse.ArgumentParser(
        prog="oic-sync",
        description="Keep openimages.cloud signed cloud images in sync with your cloud.",
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

    p_os = sub.add_parser("openstack", help="OpenStack Glance backend")
    os_sub = p_os.add_subparsers(dest="action", required=True, metavar="ACTION")

    p_status = os_sub.add_parser(
        "status",
        parents=[common, openstack_common],
        help="compare the cloud with the index (read only)",
    )
    p_status.add_argument("--json", action="store_true", help="JSON output")
    p_status.add_argument(
        "--check", action="store_true", help="exit 1 when an image is not up to date"
    )
    p_status.set_defaults(func=cmd_openstack_status)

    p_sync = os_sub.add_parser(
        "sync",
        parents=[common, openstack_common],
        help="import new versions, rotate names, retire old versions",
    )
    p_sync.add_argument(
        "--dry-run",
        action="store_true",
        help="show what would change; only read the cloud (signatures are still verified)",
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
    p_sync.add_argument(
        "--timeout",
        type=float,
        default=3600.0,
        help="seconds to wait for an import to finish (default: %(default)s)",
    )
    p_sync.add_argument(
        "--no-verify-signature",
        action="store_true",
        help="import without checking the cosign signature (not recommended)",
    )
    p_sync.set_defaults(func=cmd_openstack_sync)
    return parser


def _load(args: argparse.Namespace, client: Any) -> tuple[CatalogIndex, list[Target]]:
    index = fetch_index(client, args.index_url)
    targets = select_targets(
        index, args.image, args.variant, include_service=args.include_service_images
    )
    return index, targets


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
        out(
            f"{t.label:32} {t.artifact.version:24} {t.artifact.firmware:5} {size:>9}  "
            f"{t.stable_name}"
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


def _backend_factory(args: argparse.Namespace, client: Any, **options: Any) -> Any:
    from oic_sync.backends.openstack import connect

    return connect(args.cloud, args.region, client, **options)


def cmd_openstack_status(
    args: argparse.Namespace,
    client: Any,
    out: Callable[[str], None],
    backend: Any = None,
) -> int:
    from oic_sync.engine import UP_TO_DATE, Engine

    _, targets = _load(args, client)
    backend = backend or _backend_factory(args, client)
    engine = Engine(backend, verifier=None, keep=args.keep, delete=args.delete, out=out)
    plans = engine.status(targets)
    rows = []
    for p in plans:
        cloud = p.current or (p.older[0] if p.older else None)
        rows.append(
            {
                "id": p.target.image.id,
                "variant": p.target.artifact.variant,
                "name": p.target.stable_name,
                "index_version": p.target.artifact.version,
                "cloud_version": cloud.version if cloud else None,
                "cloud_image_id": cloud.id if cloud else None,
                "state": p.state,
                "older_versions": len(p.older) - (1 if cloud and not p.current else 0),
                "pending": [a.describe() for a in p.actions],
                "notes": p.notes,
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


def cmd_openstack_sync(
    args: argparse.Namespace,
    client: Any,
    out: Callable[[str], None],
    backend: Any = None,
    verifier: SignatureVerifier | None = None,
) -> int:
    from oic_sync.engine import Engine

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
    except (IndexFormatError, ValueError) as e:
        print(f"oic-sync: error: {e}", file=sys.stderr)
        return EXIT_USAGE
    except KeyboardInterrupt:
        return 130
    except Exception as e:
        log.debug("fatal error", exc_info=True)
        print(f"oic-sync: error: {type(e).__name__}: {e}", file=sys.stderr)
        return EXIT_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
