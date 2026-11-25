"""Nextcloud folder archives: the wire format, safe names, streaming extraction.

Nextcloud >= 31 documents a WebDAV extension the plain standard does not cover:
a ``GET`` on a *collection* returns the folder as an archive, chosen with the
``Accept`` header (``application/zip`` or ``application/x-tar``) and optionally
narrowed with the ``files`` query parameter (a JSON array of paths relative to
the collection) or repeated ``X-NC-Files`` headers.

That turns the first sync of a large share from one round trip per file into
one request per batch of files - the owner's first sync was 6.9k small core
files at ~5 files/s, i.e. 20-25 minutes of pure request latency.

This module holds the parts that are *format*, not client policy:

* :func:`archive_kind` / :func:`looks_tar` / :func:`looks_zip` - is this
  response actually the archive we asked for, or an HTML directory page (what
  an older Nextcloud, WsgiDAV and most other WebDAV servers answer)?
* :func:`member_rel` - the only place an archive entry name is turned into a
  project path.  Everything is refused that could escape the mirror
  (``..``, absolute paths, drive letters, NUL) and everything that is not one
  of the paths we asked for (so an on-demand file travelling in the stream is
  skipped, never written).
* :class:`PipeSink` - the writer the HTTP body is streamed into: it validates
  the content type in :meth:`PipeSink.start` **before** the first body byte is
  copied, and feeds a pipe an ``r|`` tar reader drains on the calling thread.
* :func:`extract_tar` / :func:`extract_zip` - iterate members and hand each
  wanted one to the caller's consumer.

Tar is preferred on the wire: ``tarfile`` reads it in streaming mode
(``r|``), so a member can be installed while the next one is still arriving.
A zip needs its central directory (at the end of the file), so the client
spools zips to a temporary file first (see ``NextcloudClient.bulk_fetch``).
"""

from __future__ import annotations

import io
import json
import os
import re
import tarfile
import zipfile

TAR = "application/x-tar"
ZIP = "application/zip"
#: ask for both; Nextcloud picks tar when both are acceptable (its Streamer
#: sets ``tarArchive`` when the tar type is present)
ACCEPT = "application/x-tar, application/zip"
#: the server appends this member when it could not read a file it was asked to
#: stream (a storage error): the member's bytes are zero-padded in a tar
_MISSING_RX = re.compile(r"^missing_files(?: \(\d+\))?\.json$")


class ArchiveUnsupported(Exception):
    """The collection answered with something other than an archive."""


class ArchiveAborted(Exception):
    """The consumer stopped; the body is abandoned (the caller falls back)."""


class ArchiveBroken(Exception):
    """It was an archive, but the stream ended or failed mid-way."""


# ---------------------------------------------------------------------------
# content sniffing
# ---------------------------------------------------------------------------

def archive_kind(content_type: str | None) -> str | None:
    """``"tar"`` / ``"zip"`` from a response ``Content-Type``, else ``None``."""
    ct = str(content_type or "").split(";", 1)[0].strip().lower()
    if ct in ("application/x-tar", "application/tar", "application/x-gtar"):
        return "tar"
    if ct in ("application/zip", "application/x-zip-compressed"):
        return "zip"
    return None


def looks_tar(head: bytes) -> bool:
    """A POSIX tar header carries ``ustar`` at offset 257."""
    return len(head) >= 512 and head[257:262] in (b"ustar", b"ustar\x00")


def looks_zip(head: bytes) -> bool:
    return head[:4] in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")


def sniff(content_type: str | None, head: bytes) -> str | None:
    """The archive format a response really is (content type *and* magic)."""
    kind = archive_kind(content_type)
    if kind == "tar" and looks_tar(head):
        return "tar"
    if kind == "zip" and looks_zip(head):
        return "zip"
    # some proxies rewrite Content-Type; the magic alone is enough then
    if not kind:
        if looks_tar(head):
            return "tar"
        if looks_zip(head):
            return "zip"
    return None


# ---------------------------------------------------------------------------
# member names -> project paths
# ---------------------------------------------------------------------------

def member_rel(name: str, wanted) -> str | None:
    """The project path an archive member names, or ``None`` (skip it).

    *wanted* is the set of paths the archive was asked for.  Only those can
    come out: a traversal entry, an absolute path, a drive letter, a NUL or an
    on-demand file that slipped into the stream never becomes a destination.
    A full-folder download names the folder as its first component, so that
    one component is tried as well (still matched against *wanted*).
    """
    raw = str(name or "").replace("\\", "/")
    if not raw or raw.startswith("/"):
        return None
    if len(raw) > 1 and raw[1] == ":":            # C:/..., also C:x
        return None
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        return None
    if any("\x00" in p for p in parts):
        return None
    rel = "/".join(parts)
    if rel in wanted:
        return rel
    if len(parts) > 1:
        rel = "/".join(parts[1:])
        if rel in wanted:
            return rel
    return None


# ---------------------------------------------------------------------------
# streaming: the HTTP body feeds a pipe the tar reader drains
# ---------------------------------------------------------------------------

class PipeSink:
    """A ``write`` sink that forwards to a pipe (back-pressure included).

    ``start(status, headers)`` runs before the body is copied: a response that
    is not an archive is refused here, so its (HTML) body is never read into
    the extractor.
    """

    def __init__(self, write_fd: int, *, content_type_only: str | None = None):
        self._fd = write_fd
        self._closed = False
        self.content_type = ""
        self.head = b""
        self.kind: str | None = None
        self._expect = content_type_only

    def start(self, status: int, headers: dict) -> None:
        self.content_type = str(headers.get("content-type") or "")
        kind = archive_kind(self.content_type)
        if self._expect is not None and kind != self._expect:
            raise ArchiveUnsupported(f"not an {self._expect} archive: {self.content_type!r}")
        if kind is None:
            raise ArchiveUnsupported(f"not an archive: {self.content_type!r}")
        self.kind = kind

    def write(self, chunk) -> int:
        if self._closed:
            raise ArchiveAborted("the archive consumer stopped")
        data = memoryview(chunk)
        if len(self.head) < 512:
            self.head += bytes(data[: 512 - len(self.head)])
        try:
            while data:
                n = os.write(self._fd, data)
                data = data[n:]
        except OSError as exc:                    # reader gone: stop the request
            raise ArchiveAborted(f"archive pipe closed: {exc}") from exc
        return len(chunk)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            os.close(self._fd)
        except OSError:
            pass


class HeadSink:
    """Keep at most *limit* bytes (capability probe); refuse more."""

    class TooBig(Exception):
        pass

    def __init__(self, limit: int):
        self.limit = int(limit)
        self.data = bytearray()
        self.content_type = ""

    def start(self, status: int, headers: dict) -> None:
        self.content_type = str(headers.get("content-type") or "")
        if archive_kind(self.content_type) is None:
            raise ArchiveUnsupported(f"not an archive: {self.content_type!r}")

    def write(self, chunk) -> int:
        room = self.limit - len(self.data)
        if len(chunk) > room:
            raise self.TooBig(f"more than {self.limit} bytes")
        self.data += chunk
        return len(chunk)


# ---------------------------------------------------------------------------
# extraction
# ---------------------------------------------------------------------------

def _consume(consumer, rel: str, src, size: int) -> None:
    try:
        consumer(rel, src, int(size))
    finally:
        close = getattr(src, "close", None)
        if close is not None:
            try:
                close()
            except Exception:                 # noqa: BLE001 - a reader close never matters
                pass


def _missing_member(name: str) -> bool:
    return bool(_MISSING_RX.match(str(name or "").replace("\\", "/").rsplit("/", 1)[-1]))


def _report_missing(on_missing, src) -> None:
    if on_missing is None:
        return
    try:
        data = src.read()
        doc = json.loads(data.decode("utf-8")) if data else {}
    except (ValueError, UnicodeDecodeError, OSError, AttributeError):
        return
    if isinstance(doc, dict):
        on_missing({str(k): str(v) for k, v in doc.items()})


def extract_tar(reader, wanted, consumer, *, should_stop=None, on_missing=None) -> list[str]:
    """Stream a tar to *consumer*; returns the project paths handed over.

    ``tarfile``'s ``r|`` mode reads strictly forward, so a member has to be
    consumed before the next one can start - the consumer writes it to a
    temporary file and installs it, which is exactly the streaming behaviour
    this exists for.  The server's ``missing_files.json`` member (files it could
    not read; a tar pads them with zeros) is reported to *on_missing*, never
    extracted.
    """
    out: list[str] = []
    stream = tarfile.open(fileobj=reader, mode="r|")
    try:
        for member in stream:
            if should_stop is not None and should_stop():
                raise ArchiveAborted("the caller stopped the archive")
            if not member.isfile():
                continue
            if _missing_member(member.name):
                src = stream.extractfile(member)
                if src is not None:
                    with src:
                        _report_missing(on_missing, src)
                continue
            rel = member_rel(member.name, wanted)
            if rel is None:
                continue
            src = stream.extractfile(member)
            if src is None:
                continue
            _consume(consumer, rel, src, member.size)
            out.append(rel)
    except (tarfile.TarError, EOFError, OSError) as exc:
        raise ArchiveBroken(f"{type(exc).__name__}: {exc}") from exc
    finally:
        try:
            stream.close()
        except Exception:                     # noqa: BLE001
            pass
    return out


def extract_zip(zf: zipfile.ZipFile, wanted, consumer, *, should_stop=None,
                on_missing=None) -> list[str]:
    out: list[str] = []
    try:
        for info in zf.infolist():
            if should_stop is not None and should_stop():
                raise ArchiveAborted("the caller stopped the archive")
            if info.is_dir():
                continue
            if _missing_member(info.filename):
                with zf.open(info) as src:
                    _report_missing(on_missing, src)
                continue
            rel = member_rel(info.filename, wanted)
            if rel is None:
                continue
            with zf.open(info) as src:
                consumer(rel, src, info.file_size)
            out.append(rel)
    except (zipfile.BadZipFile, EOFError, OSError) as exc:
        raise ArchiveBroken(f"{type(exc).__name__}: {exc}") from exc
    return out


def read_member(data: bytes, kind: str, rel: str) -> bytes | None:
    """One member out of an archive already in memory (the capability probe)."""
    try:
        if kind == "tar":
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tf:
                for member in tf:
                    if member.isfile() and member_rel(member.name, {rel}) == rel:
                        src = tf.extractfile(member)
                        if src is not None:
                            with src:
                                return src.read()
            return None
        if kind == "zip":
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                for info in zf.infolist():
                    if not info.is_dir() and member_rel(info.filename, {rel}) == rel:
                        return zf.read(info)
    except (tarfile.TarError, zipfile.BadZipFile, OSError, EOFError):
        return None
    return None
