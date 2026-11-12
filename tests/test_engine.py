from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from fakes import FakeVerifier
from oic_sync.backends.base import BackendError, ManagedImage
from oic_sync.engine import (
    CLEANUP,
    DELETE,
    IMPORT,
    IMPORTING,
    MISSING,
    NEEDS_ROTATION,
    OUTDATED,
    PROMOTE,
    RETIRE,
    UP_TO_DATE,
    Engine,
    plan_target,
    versioned_name,
)

NOW = datetime(2026, 11, 12, 12, tzinfo=timezone.utc)


def mi(
    n: int,
    version: str,
    sha: str,
    *,
    name: str = "",
    hidden: bool = True,
    status: str = "active",
    age_h: float = 0,
) -> ManagedImage:
    return ManagedImage(
        id=f"i{n}",
        name=name,
        version=version,
        sha256=sha,
        status=status,
        hidden=hidden,
        created_at=NOW - timedelta(hours=age_h) - timedelta(minutes=100 - n),
    )


def kinds(plan):
    return [(a.kind, a.image.id if a.image else None, a.name) for a in plan.actions]


@pytest.fixture
def debian(targets):
    return targets["debian-13"]


def plan(target, images, keep=2, delete=False):
    return plan_target(target, images, keep=keep, delete=delete, now=NOW)


def test_missing_imports(debian):
    p = plan(debian, [])
    assert p.state == MISSING
    assert kinds(p) == [(IMPORT, None, "Debian 13 (13-20261103-2651)")]


def test_up_to_date_does_nothing(debian):
    cur = mi(5, debian.artifact.version, debian.artifact.sha256, name="Debian 13", hidden=False)
    old = mi(4, "13-old", "a" * 64, name="Debian 13 (13-old)")
    p = plan(debian, [cur, old])
    assert p.state == UP_TO_DATE
    assert p.actions == []
    assert p.current == cur


def test_outdated_imports_then_retires_the_stable_holder(debian):
    old = mi(4, "13-old", "a" * 64, name="Debian 13", hidden=False)
    p = plan(debian, [old])
    assert p.state == OUTDATED
    assert kinds(p) == [
        (IMPORT, None, "Debian 13 (13-20261103-2651)"),
        (RETIRE, "i4", "Debian 13 (13-old)"),
    ]


def test_keep_without_delete_only_notes(debian):
    cur = mi(9, debian.artifact.version, debian.artifact.sha256, name="Debian 13", hidden=False)
    olds = [mi(n, f"13-v{n}", f"{n}" * 64, name=f"Debian 13 (13-v{n})") for n in (6, 7, 8)]
    p = plan(debian, [cur, *olds], keep=2)
    assert p.actions == []
    assert len(p.notes) == 2  # i7 and i6 are beyond --keep 2
    assert "use --delete" in p.notes[0]


def test_keep_with_delete_deletes_oldest(debian):
    cur = mi(9, debian.artifact.version, debian.artifact.sha256, name="Debian 13", hidden=False)
    olds = [mi(n, f"13-v{n}", f"{n}" * 64, name=f"Debian 13 (13-v{n})") for n in (6, 7, 8)]
    p = plan(debian, [cur, *olds], keep=2, delete=True)
    assert kinds(p) == [(DELETE, "i7", None), (DELETE, "i6", None)]


def test_keep_counts_the_image_being_imported(debian):
    olds = [mi(n, f"13-v{n}", f"{n}" * 64, name=f"Debian 13 (13-v{n})") for n in (7, 8)]
    p = plan(debian, olds, keep=2, delete=True)
    assert kinds(p) == [(IMPORT, None, "Debian 13 (13-20261103-2651)"), (DELETE, "i7", None)]


def test_keep_one_keeps_only_current(debian):
    cur = mi(9, debian.artifact.version, debian.artifact.sha256, name="Debian 13", hidden=False)
    old = mi(8, "13-v8", "8" * 64, name="Debian 13 (13-v8)")
    assert kinds(plan(debian, [cur, old], keep=1, delete=True)) == [(DELETE, "i8", None)]


def test_promotion_is_repaired(debian):
    cur = mi(9, debian.artifact.version, debian.artifact.sha256, name="Debian 13 (x)")
    p = plan(debian, [cur])
    assert p.state == NEEDS_ROTATION
    assert kinds(p) == [(PROMOTE, "i9", "Debian 13")]


def test_unhidden_older_image_is_hidden(debian):
    cur = mi(9, debian.artifact.version, debian.artifact.sha256, name="Debian 13", hidden=False)
    old = mi(8, "13-v8", "8" * 64, name="Debian 13 (13-v8)", hidden=False)
    assert kinds(plan(debian, [cur, old])) == [(RETIRE, "i8", "Debian 13 (13-v8)")]


def test_republished_version_gets_digest_in_name(debian):
    # Same version string, different content (the build was redone under the same version).
    old = mi(8, debian.artifact.version, "f" * 64, name="Debian 13", hidden=False)
    p = plan(debian, [old])
    assert kinds(p)[1] == (
        RETIRE,
        "i8",
        f"Debian 13 ({debian.artifact.version}, {'f' * 12})",
    )


def test_import_in_progress_elsewhere_waits(debian):
    busy = mi(9, debian.artifact.version, debian.artifact.sha256, status="importing", age_h=0)
    old = mi(8, "13-v8", "8" * 64, name="Debian 13", hidden=False)
    p = plan(debian, [busy, old])
    assert p.state == IMPORTING
    assert p.actions == []
    assert p.notes


def test_failed_and_stale_imports_are_cleaned_and_retried(debian):
    killed = mi(9, debian.artifact.version, debian.artifact.sha256, status="killed")
    stale = mi(8, debian.artifact.version, debian.artifact.sha256, status="queued", age_h=7)
    p = plan(debian, [killed, stale])
    assert kinds(p) == [
        (CLEANUP, "i9", None),
        (CLEANUP, "i8", None),
        (IMPORT, None, "Debian 13 (13-20261103-2651)"),
    ]


def test_index_rollback_takes_the_older_image_back(debian):
    # The index went back to a version we still have: it becomes current again.
    newer = mi(9, "13-newer", "9" * 64, name="Debian 13", hidden=False)
    back = mi(8, debian.artifact.version, debian.artifact.sha256, name="Debian 13 (x)")
    p = plan(debian, [newer, back])
    assert kinds(p) == [(PROMOTE, "i8", "Debian 13"), (RETIRE, "i9", "Debian 13 (13-newer)")]


def test_keep_must_be_positive(debian):
    with pytest.raises(ValueError, match="keep"):
        plan(debian, [], keep=0)


def test_versioned_name():
    assert versioned_name("Debian 13", "13-1") == "Debian 13 (13-1)"
    assert versioned_name("Debian 13", "13-1", "ab" * 32) == "Debian 13 (13-1, abababababab)"


# -- execution with a scripted backend ---------------------------------------------------


class ScriptedBackend:
    def __init__(self, images, *, import_error=None, usage=None):
        self.images = list(images)
        self.import_error = import_error
        self.usage = usage or {}
        self.calls = []

    def describe(self):
        return "scripted"

    def list_managed(self, target):
        return list(self.images)

    def import_target(self, target, name):
        self.calls.append(("import", name))
        if self.import_error:
            raise self.import_error
        return mi(99, target.artifact.version, target.artifact.sha256, name=name)

    def rename(self, image, name, hidden):
        self.calls.append(("rename", image.id, name, hidden))

    def in_use(self, image):
        return self.usage.get(image.id, False)

    def delete(self, image):
        self.calls.append(("delete", image.id))


def run(backend, target, **kwargs):
    lines = []
    kwargs.setdefault("verifier", FakeVerifier())
    engine = Engine(backend, out=lines.append, now=lambda: NOW, **kwargs)
    (result,) = engine.sync([target])
    return result, lines


def test_sync_order_import_promote_retire(debian):
    old = mi(4, "13-old", "a" * 64, name="Debian 13", hidden=False)
    backend = ScriptedBackend([old])
    result, lines = run(backend, debian)
    assert result.ok
    assert backend.calls == [
        ("import", "Debian 13 (13-20261103-2651)"),
        ("rename", "i99", "Debian 13", False),
        ("rename", "i4", "Debian 13 (13-old)", True),
    ]
    assert any("signature verified" in line for line in lines)


def test_dry_run_changes_nothing_but_verifies(debian):
    old = mi(4, "13-old", "a" * 64, name="Debian 13", hidden=False)
    backend = ScriptedBackend([old])
    verifier = FakeVerifier()
    result, lines = run(backend, debian, dry_run=True, verifier=verifier)
    assert result.ok
    assert backend.calls == []
    assert verifier.calls == [debian.artifact.sha256]
    assert any(line.startswith("debian-13: would import") for line in lines)


def test_bad_signature_blocks_everything(debian):
    old = mi(4, "13-old", "a" * 64, name="Debian 13", hidden=False)
    backend = ScriptedBackend([old])
    verifier = FakeVerifier(fail_for={debian.artifact.sha256})
    result, lines = run(backend, debian, verifier=verifier)
    assert not result.ok
    assert backend.calls == []
    assert any("refusing to import" in line for line in lines)


def test_no_signature_check_when_up_to_date(debian):
    cur = mi(5, debian.artifact.version, debian.artifact.sha256, name="Debian 13", hidden=False)
    verifier = FakeVerifier()
    result, _ = run(ScriptedBackend([cur]), debian, verifier=verifier)
    assert result.ok
    assert verifier.calls == []


def test_import_in_progress_is_not_reported_up_to_date(debian):
    busy = mi(9, debian.artifact.version, debian.artifact.sha256, status="importing")
    result, lines = run(ScriptedBackend([busy]), debian)
    assert result.state == IMPORTING
    assert not any("up to date" in line for line in lines)
    assert any("already in progress" in line for line in lines)


def test_without_verifier_warns(debian):
    result, lines = run(ScriptedBackend([]), debian, verifier=None)
    assert result.ok
    assert any("NOT verified" in line for line in lines)


def test_import_failure_leaves_old_images_untouched(debian):
    old = mi(4, "13-old", "a" * 64, name="Debian 13", hidden=False)
    backend = ScriptedBackend([old], import_error=BackendError("boom"))
    result, lines = run(backend, debian)
    assert not result.ok
    assert backend.calls == [("import", "Debian 13 (13-20261103-2651)")]
    assert lines[-1] == "debian-13: ERROR: boom"


def test_delete_respects_usage(debian):
    cur = mi(9, debian.artifact.version, debian.artifact.sha256, name="Debian 13", hidden=False)
    olds = [mi(n, f"13-v{n}", f"{n}" * 64, name=f"Debian 13 (13-v{n})") for n in (5, 6, 7, 8)]
    backend = ScriptedBackend([cur, *olds], usage={"i7": True, "i6": None})
    result, lines = run(backend, debian, keep=2, delete=True)
    assert result.ok
    assert backend.calls == [("delete", "i5")]
    assert any("keep i7: used" in line for line in lines)
    assert any("keep i6: cannot tell" in line for line in lines)


def test_unexpected_error_does_not_stop_other_targets(debian, targets):
    class Exploding(ScriptedBackend):
        def list_managed(self, target):
            if target.image.id == "debian-13":
                raise RuntimeError("keystone down")
            return []

    backend = Exploding([])
    lines = []
    engine = Engine(backend, verifier=FakeVerifier(), out=lines.append, now=lambda: NOW)
    results = engine.sync([debian, targets["alpine-linux/uefi"]])
    assert [r.ok for r in results] == [False, True]
