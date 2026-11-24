"""Content-addressed blob store (INTERFACES §3.6).

Layout: ``<data>/blobs/<aa>/<sha256 hex>`` — the first two hex characters are
the directory, so a project with tens of thousands of files does not put them
all in one directory.

The store is deliberately streaming: a ``PUT`` body is written chunk by chunk
to a temporary file **in the destination directory**, hashed while it is
written, and only moved into place (``os.replace``, atomic) once the digest
matches the requested name.  A mismatching or oversized upload leaves no
partial blob behind.  Nothing here reads a whole body into memory, so the
server's RAM stays flat for a multi-hundred-MB paper or dataset zip.

``gzip`` request bodies are decoded incrementally by :class:`GzipDecoder`
(``zlib.decompressobj`` with the gzip wrapper) — one chunk in, one chunk out.

The size limit is configurable: ``STUDIO_MAX_BLOB_MB`` (megabytes, float
allowed) or the ``max_bytes`` constructor argument (tests); the default is
250 MB.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
import zlib
from pathlib import Path
from typing import AsyncIterable, AsyncIterator, Iterable, Iterator

#: §3.6 default upload limit (250 MB).
DEFAULT_MAX_BYTES = 250 * 1024 * 1024
#: Streaming chunk size (1 MiB).
CHUNK = 1024 * 1024

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


class BlobError(Exception):
    """Base class for a rejected blob operation."""


class InvalidHash(BlobError):
    """The URL/blob name is not a lower-case hex sha256."""


class BlobHashMismatch(BlobError):
    """The decoded body does not hash to the requested name."""


class BlobTooLarge(BlobError):
    """The body exceeds the configured upload limit."""


class BadRange(BlobError):
    """A ``Range`` header the store cannot serve (→ 416)."""


# ── names / limits ─────────────────────────────────────────────────────


def is_valid_hash(value: object) -> bool:
    return isinstance(value, str) and _HASH_RE.match(value) is not None


def validate_hash(value: object) -> str:
    """Return the hash lower-cased, or raise :class:`InvalidHash`."""
    if not isinstance(value, str):
        raise InvalidHash(f"not a blob hash: {value!r}")
    text = value.strip().lower()
    if not _HASH_RE.match(text):
        raise InvalidHash(f"not a sha256 hex digest: {value!r}")
    return text


def default_max_bytes() -> int:
    """``STUDIO_MAX_BLOB_MB`` (MB) or :data:`DEFAULT_MAX_BYTES`."""
    raw = (os.environ.get("STUDIO_MAX_BLOB_MB") or "").strip()
    if raw:
        try:
            megabytes = float(raw)
        except ValueError:
            megabytes = 0.0
        if megabytes > 0:
            return int(megabytes * 1024 * 1024)
    return DEFAULT_MAX_BYTES


# ── gzip decoding ──────────────────────────────────────────────────────


class GzipDecoder:
    """Incremental gzip decoder (``zlib`` with the gzip wrapper)."""

    def __init__(self) -> None:
        self._zlib = zlib.decompressobj(16 + zlib.MAX_WBITS)

    def feed(self, data: bytes) -> bytes:
        if not data:
            return b""
        return self._zlib.decompress(data)

    def flush(self) -> bytes:
        return self._zlib.flush()


def gunzip_iter(chunks: Iterable[bytes]) -> Iterator[bytes]:
    """Decode a synchronous gzip chunk iterator (tests/CLI)."""
    decoder = GzipDecoder()
    for chunk in chunks:
        out = decoder.feed(chunk)
        if out:
            yield out
    tail = decoder.flush()
    if tail:
        yield tail


async def gunzip_aiter(chunks: AsyncIterable[bytes]) -> AsyncIterator[bytes]:
    """Decode ``request.stream()``-style gzip chunks without buffering them."""
    decoder = GzipDecoder()
    async for chunk in chunks:
        out = decoder.feed(chunk)
        if out:
            yield out
    tail = decoder.flush()
    if tail:
        yield tail


# ── store ──────────────────────────────────────────────────────────────


def _unlink(path: str | Path) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


class BlobStore:
    """The ``<data>/blobs`` tree, addressed by sha256 hex digest."""

    def __init__(self, data_dir: str | Path, max_bytes: int | None = None) -> None:
        self.data_dir = Path(data_dir)
        self.root = self.data_dir / "blobs"
        self.max_bytes = int(max_bytes) if max_bytes is not None else default_max_bytes()

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    # ── paths / reads ──────────────────────────────────────────────

    def path(self, hash: str) -> Path:
        h = validate_hash(hash)
        return self.root / h[:2] / h

    def exists(self, hash: object) -> bool:
        if not is_valid_hash(hash):
            return False
        return self.path(str(hash)).is_file()

    def size(self, hash: str) -> int:
        return self.path(hash).stat().st_size

    def read_bytes(self, hash: str) -> bytes:
        with open(self.path(hash), "rb") as fh:
            return fh.read()

    def open(self, hash: str):  # noqa: A003 - mirror of builtins.open
        return open(self.path(hash), "rb")

    # ── writes ────────────────────────────────────────────────────

    def write_bytes(self, hash: str, data: bytes) -> int:
        """Store *data* under *hash* (verifying it); idempotent."""
        h = validate_hash(hash)
        if len(data) > self.max_bytes:
            raise BlobTooLarge(
                f"blob is {len(data)} bytes; the limit is {self.max_bytes}"
            )
        digest = hashlib.sha256(data).hexdigest()
        if digest != h:
            raise BlobHashMismatch(f"content hashes to {digest}, not {h}")
        destination = self.path(h)
        if destination.is_file():
            return destination.stat().st_size
        destination.parent.mkdir(parents=True, exist_ok=True)
        handle, temp_name = tempfile.mkstemp(prefix=".tmp-", dir=str(destination.parent))
        try:
            with os.fdopen(handle, "wb") as fh:
                fh.write(data)
            os.replace(temp_name, destination)
        except BaseException:
            _unlink(temp_name)
            raise
        return len(data)

    async def write_stream(
        self,
        hash: str,
        chunks: AsyncIterable[bytes],
        max_bytes: int | None = None,
    ) -> int:
        """Stream *chunks* to a temp file, verify sha256, then ``os.replace``.

        Returns the stored size.  Raises :class:`BlobTooLarge` (before writing
        past the limit), :class:`BlobHashMismatch` and :class:`InvalidHash`;
        in every error case the temp file is removed and no blob appears.
        """
        h = validate_hash(hash)
        limit = int(max_bytes) if max_bytes is not None else self.max_bytes
        destination = self.path(h)
        destination.parent.mkdir(parents=True, exist_ok=True)
        handle, temp_name = tempfile.mkstemp(prefix=".tmp-", dir=str(destination.parent))
        digest = hashlib.sha256()
        size = 0
        try:
            with os.fdopen(handle, "wb") as fh:
                async for chunk in chunks:
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > limit:
                        raise BlobTooLarge(
                            f"blob exceeds the {limit}-byte upload limit"
                        )
                    digest.update(chunk)
                    fh.write(chunk)
            actual = digest.hexdigest()
            if actual != h:
                raise BlobHashMismatch(f"content hashes to {actual}, not {h}")
            os.replace(temp_name, destination)
        except BaseException:
            _unlink(temp_name)
            raise
        return size


# ── serving ────────────────────────────────────────────────────────────


def iter_file(
    path: str | Path,
    offset: int = 0,
    length: int | None = None,
    chunk_size: int = CHUNK,
) -> Iterator[bytes]:
    """Yield *length* bytes of *path* starting at *offset* (streaming)."""
    with open(path, "rb") as fh:
        fh.seek(offset)
        remaining = length
        while True:
            if remaining is not None and remaining <= 0:
                break
            want = chunk_size if remaining is None else min(chunk_size, remaining)
            data = fh.read(want)
            if not data:
                break
            if remaining is not None:
                remaining -= len(data)
            yield data


def iter_gzip_file(path: str | Path, chunk_size: int = CHUNK) -> Iterator[bytes]:
    """Yield the gzip encoding of *path* without loading it (Content-Encoding)."""
    compressor = zlib.compressobj(6, zlib.DEFLATED, 16 + zlib.MAX_WBITS)
    with open(path, "rb") as fh:
        while True:
            data = fh.read(chunk_size)
            if not data:
                break
            out = compressor.compress(data)
            if out:
                yield out
    tail = compressor.flush()
    if tail:
        yield tail


def parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """Parse one ``Range: bytes=…`` header.

    Returns inclusive ``(start, end)``, ``None`` for "serve the whole file"
    (no header, or a multi-range request, which the contract does not need),
    and raises :class:`BadRange` for an unsatisfiable/invalid range.
    """
    text = (header or "").strip()
    if not text:
        return None
    if not text.lower().startswith("bytes="):
        raise BadRange(f"unsupported range unit: {header!r}")
    spec = text[len("bytes=") :].strip()
    if "," in spec:  # multiple ranges: fall back to the full representation
        return None
    if "-" not in spec:
        raise BadRange(f"malformed range: {header!r}")
    first, last = (part.strip() for part in spec.split("-", 1))

    if not first:
        if not last.isdigit() or int(last) <= 0:
            raise BadRange(f"malformed suffix range: {header!r}")
        if size <= 0:
            raise BadRange("empty representation")
        start = max(0, size - int(last))
        end = size - 1
    else:
        if not first.isdigit():
            raise BadRange(f"malformed range start: {header!r}")
        start = int(first)
        if not last:
            end = size - 1
        elif last.isdigit():
            end = int(last)
        else:
            raise BadRange(f"malformed range end: {header!r}")
        if start >= size:
            raise BadRange("range starts past the end")
        end = min(end, size - 1)
        if end < start:
            raise BadRange("range end precedes its start")
    return start, end


__all__ = [
    "BadRange",
    "BlobError",
    "BlobHashMismatch",
    "BlobStore",
    "BlobTooLarge",
    "CHUNK",
    "DEFAULT_MAX_BYTES",
    "GzipDecoder",
    "InvalidHash",
    "default_max_bytes",
    "gunzip_aiter",
    "gunzip_iter",
    "is_valid_hash",
    "iter_file",
    "iter_gzip_file",
    "parse_range",
    "validate_hash",
]
