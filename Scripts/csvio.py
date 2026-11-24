"""Resolve dataset CSV products that may be stored gzip-compressed.

A ``Datasets/<Study Key>/<Key> - harmonized.csv`` product can be kept on disk as
``… - harmonized.csv.gz`` (the size pass of 2026-11-24 compresses ~700 MB of
them).  pandas reads gzip transparently from the file name, so the only thing a
reader needs is the *right path*: :func:`resolve` returns the plain file when it
exists and the ``.gz`` twin otherwise, and :func:`exists` answers the same
question for the many ``os.path.isfile(...)`` guards.

Rules that keep this predictable:

* the plain ``.csv`` always wins when both exist (a harmonizer that re-ran wrote
  it back).  Nothing here ever converts a file; :func:`compress` is the only
  writer and it is only ever called by the dataset compression pass;
* a caller keeps building the same literal path it always built
  (``os.path.join(folder, key + " - harmonized.csv")``) and passes it through
  :func:`resolve` / :func:`exists` / :func:`read_csv` / :func:`glob`;
* **per-study scripts stay untouched** (harmonizer.py, ``- prepare*.py``,
  ``- reproduction.py`` and their ``- SB``/``- MM`` twins read their inputs with
  literal paths).  A file such scripts read therefore stays uncompressed: the
  compression pass only compresses a product whose every reader goes through
  this module.
"""
from __future__ import annotations

import glob as _glob
import gzip
import os

GZ = ".gz"

#: Suffixes this module knows how to resolve.
CSV_SUFFIXES = (".csv", ".csv.gz")


def is_compressed(path: str) -> bool:
    """True when *path* names a gzip-compressed file."""
    return str(path).lower().endswith(GZ)


def resolve(path: str) -> str:
    """The path that actually exists: *path*, else ``path + '.gz'``, else *path*.

    Returning *path* unchanged when neither twin exists keeps the caller's own
    error message (``FileNotFoundError: … - harmonized.csv``) and any log line
    naming the canonical product.
    """
    if not path:
        return path
    if os.path.isfile(path):
        return path
    if is_compressed(path):
        return path
    gz = path + GZ
    return gz if os.path.isfile(gz) else path


def exists(path: str) -> bool:
    """``os.path.isfile`` that also accepts the ``.gz`` twin."""
    return bool(path) and (os.path.isfile(path)
                           or (not is_compressed(path) and os.path.isfile(path + GZ)))


def resolve_pair(path: str) -> tuple[str, str]:
    """``(plain, stored)`` - the canonical name and the file actually on disk."""
    return path, resolve(path)


def glob(pattern: str) -> list[str]:
    """``glob.glob`` that also matches the ``.gz`` twins, in a stable order.

    ``… /* - harmonized.csv`` therefore returns both the plain and the
    compressed product of every study, each exactly once.
    """
    found = list(_glob.glob(pattern))
    if not pattern.lower().endswith(GZ):
        found += _glob.glob(pattern + GZ)
    # a plain file wins over its own twin, so a study never appears twice
    plain = {p for p in found if not is_compressed(p)}
    out = plain | {p for p in found if is_compressed(p) and p[:-len(GZ)] not in plain}
    return sorted(out)


def iglob(pattern: str):
    """Generator twin of :func:`glob`."""
    yield from glob(pattern)


def open_binary(path: str, mode: str = "rb"):
    """Open *path* (or its ``.gz`` twin) for binary reading."""
    if "b" not in mode:
        mode += "b"
    stored = resolve(path)
    if is_compressed(stored):
        return gzip.open(stored, mode)
    return open(stored, mode)


def open_text(path: str, mode: str = "r", encoding: str = "utf-8-sig", **kw):
    """Open *path* (or its ``.gz`` twin) for text reading."""
    stored = resolve(path)
    mode = mode.replace("b", "")
    if is_compressed(stored):
        return gzip.open(stored, mode + "t", encoding=encoding, **kw)
    return open(stored, mode, encoding=encoding, **kw)


def read_csv(path: str, **kw):
    """``pandas.read_csv`` through :func:`resolve` (lazy import: stdlib elsewhere)."""
    import pandas as pd

    return pd.read_csv(resolve(path), **kw)


def read_header(path: str, **kw) -> list[str]:
    """Column names of a dataset CSV, without reading the body."""
    import pandas as pd

    head = pd.read_csv(resolve(path), nrows=0, **kw)
    return [str(c).strip().lstrip("\ufeff") for c in head.columns]


def count_rows(path: str) -> int:
    """Data rows of a CSV (header excluded), streaming, gzip-aware.

    Assumes no embedded newlines inside quoted fields - true for the
    machine-written wide numeric tables this is used on.
    """
    n = 0
    tail = b""
    with open_binary(path) as fh:
        while True:
            chunk = fh.read(1 << 20)
            if not chunk:
                break
            n += chunk.count(b"\n")
            tail = chunk
    if n == 0:
        return 0
    return n if tail[-1:] != b"\n" else n - 1


def compress(src: str) -> tuple[str, int, int]:
    """Write ``src + '.gz'`` (deterministic gzip), return (dst, plain_bytes, gz_bytes).

    The caller decides what happens to the plain file: the dataset size pass
    moves the original into ``archive/_size_audit_<date>/`` so nothing is ever
    deleted.  An existing ``.gz`` is never overwritten (a second run is a no-op
    for that file).
    """
    dst = src + GZ
    if os.path.exists(dst):
        return dst, os.path.getsize(src) if os.path.isfile(src) else 0, os.path.getsize(dst)
    if not os.path.isfile(src):
        raise FileNotFoundError(src)
    tmp = dst + ".part"
    with open(src, "rb") as fin, gzip.GzipFile(tmp, "wb", compresslevel=6,
                                              mtime=0) as fout:
        while True:
            chunk = fin.read(1 << 22)
            if not chunk:
                break
            fout.write(chunk)
    os.replace(tmp, dst)
    return dst, os.path.getsize(src), os.path.getsize(dst)
