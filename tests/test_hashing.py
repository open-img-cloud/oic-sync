from __future__ import annotations

import hashlib

import pytest

from oic_sync.hashing import HashingReader, HashMismatch

DATA = bytes(range(256)) * 1000


def chunks(data: bytes, size: int = 7777):
    return iter([data[i : i + size] for i in range(0, len(data), size)])


def test_reads_in_blocks_and_hashes():
    reader = HashingReader(chunks(DATA), len(DATA))
    out = b""
    while block := reader.read(4096):
        assert len(block) <= 4096
        out += block
    assert out == DATA
    assert reader.sha256 == hashlib.sha256(DATA).hexdigest()
    assert reader.sha512 == hashlib.sha512(DATA).hexdigest()
    reader.verify(hashlib.sha256(DATA).hexdigest(), hashlib.sha512(DATA).hexdigest(), len(DATA))


def test_read_all_and_iteration():
    assert HashingReader(chunks(DATA)).read() == DATA
    assert b"".join(HashingReader(chunks(DATA))) == DATA


def test_len_attribute_only_when_size_known():
    # requests' super_len uses `len` to send a Content-Length instead of a chunked body.
    assert HashingReader(chunks(DATA), 42).len == 42
    assert not hasattr(HashingReader(chunks(DATA)), "len")


def test_aborts_as_soon_as_data_exceeds_size():
    reader = HashingReader(chunks(DATA), 10_000)

    def drain():
        while reader.read(4096):
            pass

    with pytest.raises(HashMismatch, match="larger"):
        drain()
    assert reader.bytes_read < len(DATA)


@pytest.mark.parametrize(
    ("sha256", "sha512", "size", "message"),
    [
        ("0" * 64, None, None, "sha256 mismatch"),
        (hashlib.sha256(DATA).hexdigest(), "0" * 128, None, "sha512 mismatch"),
        (hashlib.sha256(DATA).hexdigest(), None, len(DATA) + 1, "size mismatch"),
    ],
)
def test_verify_mismatches(sha256, sha512, size, message):
    reader = HashingReader(chunks(DATA))
    reader.read()
    with pytest.raises(HashMismatch, match=message):
        reader.verify(sha256, sha512, size)
