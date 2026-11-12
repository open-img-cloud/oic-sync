"""Platform backends. OpenStack (Glance) is implemented; Proxmox VE is planned."""

from oic_sync.backends.base import Backend, BackendError, ManagedImage

__all__ = ["Backend", "BackendError", "ManagedImage"]
