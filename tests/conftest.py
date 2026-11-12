from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from fakes import PROJECT, FakeGlance, cdn_files, cdn_transport, index_dict
from oic_sync.backends.openstack import Glance, OpenStackBackend
from oic_sync.http import make_client
from oic_sync.index import parse_index, select_targets


@pytest.fixture
def index():
    return parse_index(index_dict())


@pytest.fixture
def targets(index):
    return {t.label: t for t in select_targets(index, include_service=True)}


@pytest.fixture
def files():
    return cdn_files()


@pytest.fixture
def http_log():
    return []


@pytest.fixture
def client(files, http_log):
    with make_client(transport=cdn_transport(files, http_log)) as c:
        yield c


@pytest.fixture
def glance():
    return FakeGlance()


@pytest.fixture
def make_backend(glance, client):
    def make(**kwargs):
        kwargs.setdefault("sleep", lambda _s: None)
        kwargs.setdefault("poll_interval", 0)
        return OpenStackBackend(Glance(glance, PROJECT), client, **kwargs)

    return make
