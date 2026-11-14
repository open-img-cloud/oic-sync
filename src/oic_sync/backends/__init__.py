"""Platform backends: OpenStack (Glance) and Proxmox VE (VM templates, through pvesh)."""

from oic_sync.backends.base import Backend, BackendError, ManagedImage

__all__ = ["Backend", "BackendError", "ManagedImage"]
