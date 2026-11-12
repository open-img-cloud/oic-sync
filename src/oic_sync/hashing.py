"""Streaming reader that hashes what it passes through."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator


class HashMismatch(Exception):
    """The downloaded data does not match the digests or size of the index."""


class HashingReader:
    """File-like wrapper around a byte-chunk iterator, computing sha256 and sha512 on the fly.

    It is handed to requests (via keystoneauth) as an upload body: requests reads it in blocks
    and, when the expected size is known, sends a Content-Length instead of a chunked body
    (exposed through the `len` attribute, which requests' super_len looks for).
    Reading more than the expected size aborts the upload at once.
    """

    def __init__(self, chunks: Iterator[bytes], expected_size: int | None = None) -> None:
        self._chunks = chunks
        self._buffer = b""
        self._done = False
        self.expected_size = expected_size
        self.bytes_read = 0
        self._sha256 = hashlib.sha256()
        self._sha512 = hashlib.sha512()
        if expected_size is not None:
            self.len = expected_size

    def _account(self, data: bytes) -> bytes:
        if data:
            self.bytes_read += len(data)
            if self.expected_size is not None and self.bytes_read > self.expected_size:
                raise HashMismatch(
                    f"download is larger than the {self.expected_size} bytes of the index"
                )
            self._sha256.update(data)
            self._sha512.update(data)
        return data

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            parts = [self._buffer]
            self._buffer = b""
            if not self._done:
                parts.extend(self._chunks)
                self._done = True
            return self._account(b"".join(parts))
        while len(self._buffer) < size and not self._done:
            try:
                self._buffer += next(self._chunks)
            except StopIteration:
                self._done = True
        data, self._buffer = self._buffer[:size], self._buffer[size:]
        return self._account(data)

    def __iter__(self) -> Iterator[bytes]:
        while True:
            block = self.read(1024 * 1024)
            if not block:
                return
            yield block

    @property
    def sha256(self) -> str:
        return self._sha256.hexdigest()

    @property
    def sha512(self) -> str:
        return self._sha512.hexdigest()

    def verify(self, sha256: str, sha512: str | None, size: int | None) -> None:
        """Raise HashMismatch unless everything was read and matches the index."""
        if size is not None and self.bytes_read != size:
            raise HashMismatch(f"size mismatch: read {self.bytes_read} bytes, index says {size}")
        if self.sha256 != sha256:
            raise HashMismatch(f"sha256 mismatch: computed {self.sha256}, index says {sha256}")
        if sha512 is not None and self.sha512 != sha512:
            raise HashMismatch(f"sha512 mismatch: computed {self.sha512}, index says {sha512}")
