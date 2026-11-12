"""HTTP client shared by the index, bundle and image downloads."""

from __future__ import annotations

import httpx

from oic_sync import USER_AGENT

# Large reads (qcow2 streaming into Glance) can stall while Glance writes to its store:
# keep a generous read timeout, but fail fast on connect.
DEFAULT_TIMEOUT = httpx.Timeout(connect=15.0, read=300.0, write=60.0, pool=30.0)


def make_client(
    *, transport: httpx.BaseTransport | None = None, timeout: httpx.Timeout | None = None
) -> httpx.Client:
    """httpx client with the generic oic-sync User-Agent (no personal data) and retries.

    Cloudflare in front of images.openimages.cloud rejects some default library agents,
    so the User-Agent is always set explicitly.
    """
    return httpx.Client(
        headers={"User-Agent": USER_AGENT},
        timeout=timeout or DEFAULT_TIMEOUT,
        follow_redirects=True,
        transport=transport or httpx.HTTPTransport(retries=3),
    )
