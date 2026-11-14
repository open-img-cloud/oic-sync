"""Stateless, idempotent reconciliation of index targets with a platform.

For each target (an artifact of the index), the engine lists the images the backend recognises
as ours, plans what to change, then executes it:

1. import the artifact when no usable image has its sha256 (after a cosign check);
2. give the stable name ("Debian 13") to that image and unhide it;
3. retire older versions: rename them "<stable name> (<version>)" and hide them (os_hidden),
   so they stay bootable by ID but leave image lists;
4. keep the `keep` newest versions (the current one included); older ones are deleted only
   with `delete`, and only when nothing uses them;
5. remove failed or abandoned imports.

Nothing is stored locally: running it twice in a row changes nothing the second time.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Protocol

from oic_sync.backends.base import FAILED, TRANSIENT, Backend, BackendError, ManagedImage
from oic_sync.hashing import HashMismatch
from oic_sync.index import Target
from oic_sync.signature import SignatureError, SignatureVerifier

log = logging.getLogger(__name__)

# An import that has not finished after this long is considered abandoned (crashed run).
DEFAULT_STALE_AFTER = timedelta(hours=6)

IMPORT = "import"
PROMOTE = "promote"
RETIRE = "retire"
DELETE = "delete"
CLEANUP = "cleanup"

UP_TO_DATE = "up-to-date"
NEEDS_ROTATION = "needs-rotation"
OUTDATED = "outdated"
MISSING = "missing"
IMPORTING = "importing"


def versioned_name(stable: str, version: str, sha256: str | None = None) -> str:
    """Name of a retired version; the digest prefix disambiguates a republished version."""
    if sha256:
        return f"{stable} ({version}, {sha256[:12]})"
    return f"{stable} ({version})"


class Naming(Protocol):
    """How a platform names the image holding the stable name and the retired versions.

    A backend may expose its own as a `naming` attribute (Proxmox VE needs DNS names);
    otherwise the engine uses GlanceNaming.
    """

    def stable(self, target: Target) -> str: ...

    def versioned(self, target: Target, version: str, sha256: str | None = None) -> str: ...


class GlanceNaming:
    """The current version is "Debian 13", older ones "Debian 13 (13-20261026-2643)"."""

    def stable(self, target: Target) -> str:
        return target.stable_name

    def versioned(self, target: Target, version: str, sha256: str | None = None) -> str:
        return versioned_name(target.stable_name, version, sha256)


DEFAULT_NAMING: Naming = GlanceNaming()


def naming_of(backend: object) -> Naming:
    """The naming scheme of a backend: its `naming` attribute, else the Glance one."""
    naming = getattr(backend, "naming", None)
    return naming if naming is not None else DEFAULT_NAMING


@dataclass(frozen=True)
class Action:
    kind: str
    image: ManagedImage | None = None
    name: str | None = None

    def describe(self) -> str:
        if self.kind == IMPORT:
            return f"import as '{self.name}' (hidden until verified)"
        assert self.image is not None
        ref = f"{self.image.id} '{self.image.name}'"
        if self.kind == PROMOTE:
            return f"promote {ref} to '{self.name}'"
        if self.kind == RETIRE:
            return f"retire {ref} as '{self.name}' (hidden)"
        if self.kind == DELETE:
            return f"delete {ref} (beyond --keep, if unused)"
        return f"remove failed import {ref} (status {self.image.status})"


@dataclass
class Plan:
    target: Target
    state: str
    current: ManagedImage | None
    older: list[ManagedImage]
    actions: list[Action]
    notes: list[str] = field(default_factory=list)
    stable_name: str = ""

    def has(self, kind: str) -> bool:
        return any(a.kind == kind for a in self.actions)


def _retire_name(
    target: Target, image: ManagedImage, others: Iterable[ManagedImage], naming: Naming
) -> str:
    clash = image.version == target.artifact.version or any(
        o.id != image.id and o.version == image.version and o.sha256 != image.sha256 for o in others
    )
    return naming.versioned(target, image.version, image.sha256 if clash else None)


def plan_target(
    target: Target,
    images: Sequence[ManagedImage],
    *,
    keep: int,
    delete: bool,
    now: datetime,
    stale_after: timedelta = DEFAULT_STALE_AFTER,
    naming: Naming = DEFAULT_NAMING,
) -> Plan:
    """Decide what to do for one target, from the images the platform holds for it."""
    if keep < 1:
        raise ValueError("keep must be at least 1")
    sha = target.artifact.sha256
    stable = naming.stable(target)
    actions: list[Action] = []
    notes: list[str] = []

    def stale(m: ManagedImage) -> bool:
        return m.status in FAILED or (m.status in TRANSIENT and now - m.created_at > stale_after)

    newest_first = sorted(images, key=lambda m: m.created_at, reverse=True)
    failed = [m for m in newest_first if stale(m)]
    in_progress = [m for m in newest_first if m.status in TRANSIENT and not stale(m)]
    current = next((m for m in newest_first if m.usable and m.sha256 == sha), None)
    older = [m for m in newest_first if m.usable and m is not current]

    actions.extend(Action(CLEANUP, m) for m in failed)

    importing_elsewhere = current is None and any(m.sha256 == sha for m in in_progress)
    if current is None and not importing_elsewhere:
        actions.append(Action(IMPORT, name=naming.versioned(target, target.artifact.version)))
    if current is not None and (current.name != stable or current.hidden):
        actions.append(Action(PROMOTE, current, stable))

    if importing_elsewhere:
        notes.append("an import of this version is already in progress; nothing else done")
    else:
        for rank, m in enumerate(older, start=1):
            name = _retire_name(target, m, older, naming)
            if m.name != name or not m.hidden:
                actions.append(Action(RETIRE, m, name))
            if rank >= keep:  # the current version counts as the first kept one
                if delete:
                    actions.append(Action(DELETE, m))
                else:
                    notes.append(f"{m.id} ({m.version}) is beyond --keep {keep}; use --delete")

    if current is not None:
        state = UP_TO_DATE if not actions else NEEDS_ROTATION
    elif importing_elsewhere:
        state = IMPORTING
    elif older:
        state = OUTDATED
    else:
        state = MISSING
    return Plan(target, state, current, older, actions, notes, stable_name=stable)


@dataclass
class TargetResult:
    target: Target
    state: str
    ok: bool = True
    messages: list[str] = field(default_factory=list)


class Engine:
    def __init__(
        self,
        backend: Backend,
        *,
        verifier: SignatureVerifier | None,
        keep: int = 2,
        delete: bool = False,
        dry_run: bool = False,
        out: Callable[[str], None] = print,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        stale_after: timedelta = DEFAULT_STALE_AFTER,
    ) -> None:
        if keep < 1:
            raise ValueError("keep must be at least 1")
        self.backend = backend
        self.verifier = verifier
        self.keep = keep
        self.delete = delete
        self.dry_run = dry_run
        self.out = out
        self.now = now
        self.stale_after = stale_after
        self.naming = naming_of(backend)

    def plan(self, target: Target) -> Plan:
        images = self.backend.list_managed(target)
        return plan_target(
            target,
            images,
            keep=self.keep,
            delete=self.delete,
            now=self.now(),
            stale_after=self.stale_after,
            naming=self.naming,
        )

    def status(self, targets: Iterable[Target]) -> list[Plan]:
        return [self.plan(t) for t in targets]

    def sync(self, targets: Iterable[Target]) -> list[TargetResult]:
        results = []
        for target in targets:
            result = self._sync_one(target)
            results.append(result)
        return results

    def _say(self, result: TargetResult, message: str) -> None:
        result.messages.append(message)
        self.out(f"{result.target.label}: {message}")

    def _sync_one(self, target: Target) -> TargetResult:
        try:
            plan = self.plan(target)
        except Exception as e:
            result = TargetResult(target, "error", ok=False)
            self._say(result, f"ERROR listing images: {e}")
            return result
        result = TargetResult(target, plan.state)
        prefix = "would " if self.dry_run else ""
        if not plan.actions and plan.state == UP_TO_DATE:
            self._say(result, f"up to date ({target.artifact.version})")
        for note in plan.notes:
            self._say(result, f"note: {note}")
        if not plan.actions:
            return result

        if plan.has(IMPORT):
            if self.verifier is None:
                self._say(result, "WARNING: cosign signature NOT verified (--no-verify-signature)")
            else:
                try:
                    identity = self.verifier.verify(target.artifact)
                except SignatureError as e:
                    result.ok = False
                    self._say(result, f"ERROR: {e}; refusing to import {target.artifact.version}")
                    return result
                self._say(result, f"signature verified ({identity})")

        try:
            self._execute(plan, result, prefix)
        except (BackendError, HashMismatch) as e:
            result.ok = False
            self._say(result, f"ERROR: {e}")
        except Exception as e:  # keep going with the other targets
            log.debug("unexpected error", exc_info=True)
            result.ok = False
            self._say(result, f"ERROR: {type(e).__name__}: {e}")
        return result

    def _execute(self, plan: Plan, result: TargetResult, prefix: str) -> None:
        stable = plan.stable_name or self.naming.stable(plan.target)
        by_kind: dict[str, list[Action]] = {}
        for a in plan.actions:
            by_kind.setdefault(a.kind, []).append(a)

        for a in by_kind.get(CLEANUP, []):
            self._say(result, prefix + a.describe())
            if not self.dry_run and a.image is not None:
                self.backend.delete(a.image)

        for a in by_kind.get(IMPORT, []):
            self._say(result, prefix + a.describe() + f": {plan.target.artifact.version}")
            if not self.dry_run:
                assert a.name is not None
                new = self.backend.import_target(plan.target, a.name)
                self._say(result, f"imported {new.id}, digests verified")
                self.backend.rename(new, stable, hidden=False)
                self._say(result, f"promoted {new.id} to '{stable}'")
            else:
                self._say(result, f"would promote it to '{stable}'")

        for a in by_kind.get(PROMOTE, []):
            self._say(result, prefix + a.describe())
            if not self.dry_run and a.image is not None and a.name is not None:
                self.backend.rename(a.image, a.name, hidden=False)

        for a in by_kind.get(RETIRE, []):
            self._say(result, prefix + a.describe())
            if not self.dry_run and a.image is not None and a.name is not None:
                self.backend.rename(a.image, a.name, hidden=True)

        for a in by_kind.get(DELETE, []):
            assert a.image is not None
            used = self.backend.in_use(a.image)
            if used is None:
                self._say(result, f"keep {a.image.id}: cannot tell whether it is in use")
                continue
            if used:
                self._say(
                    result, f"keep {a.image.id}: used by an instance, a volume or a linked clone"
                )
                continue
            self._say(result, prefix + a.describe())
            if not self.dry_run:
                self.backend.delete(a.image)
