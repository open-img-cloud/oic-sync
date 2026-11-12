"""Platform-neutral view of the images oic-sync manages, and the backend interface.

A backend (OpenStack now, Proxmox later) only has to list the images it holds for a target,
import a new one, rename/hide one, tell whether one is in use and delete one. All decisions
(what to import, which version holds the stable name, what to retire or delete) are taken by
oic_sync.engine, identically for every platform.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from oic_sync.index import Target

# Usable images: may hold the stable name or be kept as an older version.
USABLE = frozenset({"active", "deactivated"})
# Still being created or imported.
TRANSIENT = frozenset({"queued", "saving", "importing", "uploading"})
# Dead: the import failed and the image can never become usable.
FAILED = frozenset({"killed"})


class BackendError(Exception):
    """A platform call failed."""


@dataclass(frozen=True)
class ManagedImage:
    """An image of the platform recognised as one of ours (oic_id/oic_variant match)."""

    id: str
    name: str
    version: str
    sha256: str
    status: str
    hidden: bool
    created_at: datetime
    visibility: str = "private"
    extra: dict[str, str] = field(default_factory=dict, compare=False)

    @property
    def usable(self) -> bool:
        return self.status in USABLE


class Backend(Protocol):
    """What the engine needs from a platform."""

    def describe(self) -> str:
        """One line naming the platform (cloud, region, project) for logs."""
        ...

    def list_managed(self, target: Target) -> list[ManagedImage]:
        """Every image of the platform (owned by us) for this image id and variant."""
        ...

    def import_target(self, target: Target, name: str) -> ManagedImage:
        """Create the image hidden, under `name`, import and verify it.

        Returns the usable image. On any failure, the half-created image is removed and
        BackendError (or HashMismatch) is raised.
        """
        ...

    def rename(self, image: ManagedImage, name: str, hidden: bool) -> None:
        """Set the name and the hidden flag of an image."""
        ...

    def in_use(self, image: ManagedImage) -> bool | None:
        """Whether an instance or volume uses the image; None when it cannot be told."""
        ...

    def delete(self, image: ManagedImage) -> None: ...
