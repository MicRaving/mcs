"""Shared helpers: paper/file access, PDF rendering and text extraction, and the coding-schema index."""

from bisect import bisect_left
from collections import OrderedDict
from datetime import datetime
import glob
import hashlib
import json
import os
import re
import sys
import tempfile
import threading
import time
from studio_state import S, SCHEMA, log, on_reroot, paper_keys

try:
    import scheme_lib as _SL  # noqa: E402
except Exception:  # pragma: no cover - source layout fallback
    for _d in (os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
               os.path.join(S.ROOT, "Scripts")):
        if _d and _d not in sys.path:
            sys.path.insert(0, _d)
    import scheme_lib as _SL  # noqa: E402

#: The value types a scheme field may hold (``scheme_lib.FIELD_TYPES``).
FIELD_TYPES = _SL.FIELD_TYPES

#: Version stamp for the portable build (D6).  Bump on every coordinated rebuild
#: so a frozen binary can be matched against the source it was built from.
APP_VERSION = "0.4.1"

# ``contract.py`` is the ONE shared coding contract (stdlib-only, bundled in the
# packaged app): the DV-completeness rule lives there so ``B_validate`` and the
# app can never disagree again (audit M2).  It sits one level up (Scripts/).
try:
    import contract as CT  # noqa: E402
except Exception:  # pragma: no cover - source layout fallback
    for _d in (os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
               os.path.join(S.ROOT, "Scripts")):
        if _d and _d not in sys.path:
            sys.path.insert(0, _d)
    import contract as CT  # noqa: E402

# The project layer lives one level up (Scripts/): ``project_context`` answers
# "which project is active?" and ``scheme_legacy`` converts the legacy v3.1
# schema into a scheme.  The app re-roots through ``project_context``, so the
# scheme the app serves always follows the data root.
try:
    import project_context  # noqa: E402
    import scheme_legacy  # noqa: E402
except Exception:  # pragma: no cover - source layout fallback
    for _d in (os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
               os.path.join(S.ROOT, "Scripts")):
        if _d and _d not in sys.path:
            sys.path.insert(0, _d)
    import project_context  # noqa: E402
    import scheme_legacy  # noqa: E402


def atomic_replace(src, dst, attempts=6, base_delay=0.5):
    """``os.replace`` with a short retry for Windows/Nextcloud file locks.

    The sync client, Excel and antivirus can briefly hold the destination open;
    retry with a linear back-off and only raise if every attempt fails.
    """
    last = None
    for i in range(max(1, attempts)):
        try:
            os.replace(src, dst)
            return True
        except PermissionError as e:
            last = e
            if i < attempts - 1:
                time.sleep(base_delay * (i + 1))
    if last is not None:
        raise last
    return False


def write_json_atomic(target, data, indent=2):
    """Write ``data`` as UTF-8 JSON to ``target`` via a temp file + atomic replace.

    The temp file lives in ``<root>/temp`` (never in a synced data folder) and is
    flushed to disk before the replace, so a crash or a sync-client lock leaves
    either the old or the new file, never half of one.
    """
    folder = os.path.dirname(target) or "."
    os.makedirs(folder, exist_ok=True)
    os.makedirs(S.TEMP, exist_ok=True)
    fd, tmp = tempfile.mkstemp(suffix=".json", dir=S.TEMP)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=indent)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        atomic_replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    invalidate_dir(folder)
    return target


# ---------------------------------------------------------------- folder index
# ``/api/papers`` used to run four ``glob`` calls per paper, and each one lists
# the whole folder (Codings/ holds ~4000 files): ~3300 directory listings and
# ~8M fnmatch calls per request, i.e. a minute on the synced drive.  One
# ``scandir`` per folder now serves every lookup; it is refreshed when the
# folder's mtime changes (files added, removed or atomically replaced), after
# our own writes (:func:`invalidate_dir`), and at the latest after ``_DIR_TTL``
# seconds so an in-place edit by another tool is picked up too.
_DIR_TTL = 15.0
_DIR_RECHECK = 1.0
_DIR_INDEX = {}
_DIR_LOCK = threading.Lock()


class _DirIndex:
    __slots__ = ("mtime", "scanned", "checked", "entries", "lower", "names", "bylower")

    def __init__(self, mtime, entries):
        self.mtime = mtime
        self.scanned = self.checked = time.monotonic()
        self.entries = entries                      # name -> (mtime, size)
        pairs = sorted((n.lower(), n) for n in entries)
        self.lower = [p[0] for p in pairs]          # sorted, for prefix search
        self.names = [p[1] for p in pairs]
        self.bylower = dict(pairs)                  # case-insensitive lookup


def _scan_dir(folder):
    entries = {}
    try:
        with os.scandir(folder) as it:
            for e in it:
                try:
                    if e.is_file():
                        st = e.stat()
                        entries[e.name] = (st.st_mtime, st.st_size)
                except OSError:
                    continue
    except FileNotFoundError:
        pass
    except OSError as exc:
        log(f"scandir failed for {folder}: {exc!r}")
    return entries


def dir_index(folder):
    """Cached listing of the plain files in ``folder`` (see the note above)."""
    if not folder:
        return _DirIndex(None, {})
    key = os.path.normcase(os.path.abspath(folder))
    now = time.monotonic()
    idx = _DIR_INDEX.get(key)
    if idx is not None and now - idx.checked < _DIR_RECHECK:
        return idx
    try:
        mtime = os.stat(folder).st_mtime_ns
    except OSError:
        mtime = None
    if idx is not None and idx.mtime == mtime and now - idx.scanned < _DIR_TTL:
        idx.checked = now
        return idx
    with _DIR_LOCK:
        idx = _DirIndex(mtime, _scan_dir(folder) if mtime is not None else {})
        _DIR_INDEX[key] = idx
    return idx


def invalidate_dir(folder=None):
    """Forget the cached listing of ``folder`` (every folder when ``None``)."""
    with _DIR_LOCK:
        if folder is None:
            _DIR_INDEX.clear()
        else:
            _DIR_INDEX.pop(os.path.normcase(os.path.abspath(folder)), None)


def dir_names(folder, prefix="", suffix=""):
    """File names in ``folder`` starting with ``prefix`` and ending with ``suffix``.

    Case-insensitive, like ``glob`` on Windows; the prefix is literal (no
    wildcards), so keys with brackets or parentheses need no escaping.
    """
    idx = dir_index(folder)
    p, s = prefix.lower(), suffix.lower()
    i = bisect_left(idx.lower, p)
    out = []
    while i < len(idx.lower) and idx.lower[i].startswith(p):
        if idx.lower[i].endswith(s):
            out.append(idx.names[i])
        i += 1
    return out


def file_stamp(folder, name):
    """``(mtime, size)`` of ``folder/name`` from the cached listing, else ``None``.

    Case-insensitive like the Windows file system."""
    idx = dir_index(folder)
    real = idx.bylower.get(name.lower())
    return idx.entries.get(real) if real is not None else None


def has_file(folder, name):
    return file_stamp(folder, name) is not None


_MISSING = object()


class LRU:
    """A small bounded mapping (least recently used entries are dropped)."""

    def __init__(self, maxsize):
        self.maxsize = maxsize
        self._d = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key, default=None):
        with self._lock:
            if key not in self._d:
                return default
            self._d.move_to_end(key)
            return self._d[key]

    def __contains__(self, key):
        with self._lock:
            return key in self._d

    def __getitem__(self, key):
        with self._lock:
            self._d.move_to_end(key)
            return self._d[key]

    def __setitem__(self, key, value):
        with self._lock:
            self._d[key] = value
            self._d.move_to_end(key)
            while len(self._d) > self.maxsize:
                self._d.popitem(last=False)

    def clear(self):
        with self._lock:
            self._d.clear()

    def __len__(self):
        return len(self._d)


_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

_PAGE_NUM = re.compile(r"^\s*\[?\d{1,4}\]?\s*$")

_SUPP = re.compile(r"[ _\-\u2013]*Supplement[ _\-]*(\d{1,2})?", re.IGNORECASE)

_pdf_pages = LRU(4096)

# Extracted full text is large (tens of KB per paper): keep only the papers the
# coder has recently opened instead of every paper of the session.
_pdf_text = LRU(24)

# Controlled dv_domain vocabulary (schema v3.0) for the harmonizer editor.
# 2026-09-19: `confidence` and `affect` are not misinformation outcomes,
# `engagement_behavior` folds into `sharing_intention`, and `belief_accuracy` is
# merged into `credibility` (see Scripts/domain_restriction.py and
# Scripts/DV_DOMAIN_MERGE_LOG.md), so none is offered here.
# This list is the FALLBACK: the active project's scheme wins (see
# :func:`domain_vocab`).
_DV_DOMAINS_FALLBACK = [
    "technique_recognition", "credibility", "sharing_intention",
    "search_behavior", "attitude_policy", "trust",
    "media_literacy_attitudes", "critical_thinking", "other",
]

#: The live vocabulary.  It is refreshed in place by :func:`domain_vocab`, so
#: the module-level name stays valid for every importer.
DOMAIN_VOCAB = list(_DV_DOMAINS_FALLBACK)

_DOMAIN_CACHE = {"stamp": None, "value": None}


def domain_vocab():
    """The ``dv_domains`` vocabulary of the active project.

    The project's scheme wins; the list above is the fallback for a project
    that declares no vocabulary of its own.  The lookup is cached until the
    scheme file changes.
    """
    stamp = _scheme_stamp()
    cached = _DOMAIN_CACHE.get("value")
    if cached and _DOMAIN_CACHE.get("stamp") == stamp:
        DOMAIN_VOCAB[:] = cached
        return DOMAIN_VOCAB
    values = None
    try:
        values = project_context.vocabulary("dv_domains", default=None)
    except Exception:  # noqa: BLE001
        values = None
    if not values:
        values = list(_DV_DOMAINS_FALLBACK)
    values = [str(v) for v in values]
    _DOMAIN_CACHE["stamp"] = stamp
    _DOMAIN_CACHE["value"] = values
    DOMAIN_VOCAB[:] = values
    return DOMAIN_VOCAB

def safe_token(name):
    name = (name or "").strip()
    name = _UNSAFE.sub("_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name or "coder"

_SUPP_EXT = {".pdf": "pdf", ".docx": "file", ".doc": "file", ".png": "file",
             ".jpg": "file", ".jpeg": "file", ".ipynb": "file", ".zip": "file",
             ".csv": "file", ".xlsx": "file", ".xls": "file", ".txt": "file"}

def supplements(key):
    """Supplementary files named ``<Key> Supplement N.<ext>``.

    Scans both ``Papers/`` (legacy) and the root ``Supplements/`` folder, and
    recognises any extension: PDFs open in the built-in viewer, other files are
    opened/downloaded.
    """
    found = {}
    for folder in (S.SUPPLEMENTS, S.PAPERS):
        if not folder or not os.path.isdir(folder):
            if not (S.online and S.store is not None):
                continue
        if S.online and S.store is not None:
            rel = S.relpath(folder)
            names = [e["name"] for e in S.listdir(rel) if not e.get("is_dir")] if rel is not None else []
        else:
            names = [os.path.basename(f) for f in
                     glob.glob(os.path.join(glob.escape(folder), glob.escape(key) + "*"))]
        for base in names:
            if not base.startswith(key):
                continue
            rest = base[len(key):]
            m = _SUPP.search(rest)
            if not m:
                continue
            ext = os.path.splitext(base)[1].lower()
            n = int(m.group(1)) if m.group(1) else 0
            found[(folder, base)] = {
                "n": n, "file": base, "name": base, "ext": ext,
                "kind": _SUPP_EXT.get(ext, "file"),
                "folder": os.path.basename(folder),
            }
    return [found[k] for k in sorted(found, key=lambda k: (found[k]["n"], found[k]["file"]))]

def pdf_pages(path):
    path = S.ensure_local(path)
    if not path or not os.path.exists(path):
        return None
    stamp = (path, os.path.getmtime(path))
    cached = _pdf_pages.get(stamp, _MISSING)
    if cached is not _MISSING:
        return cached
    pages = None
    try:
        import pymupdf

        with pymupdf.open(path) as doc:
            pages = doc.page_count
    except Exception as exc:
        log(f"pdf_pages failed for {os.path.basename(path)}: {exc!r}")
        pages = None
    _pdf_pages[stamp] = pages
    return pages

_pdf_size = LRU(4096)

PAGE_SCALE = 2.0

def pdf_page_size(path):
    """First-page size in PDF points, used to size the rendered page images."""
    path = S.ensure_local(path)
    if not path or not os.path.exists(path):
        return None
    stamp = (path, os.path.getmtime(path))
    cached = _pdf_size.get(stamp, _MISSING)
    if cached is not _MISSING:
        return cached
    size = None
    try:
        import pymupdf

        with pymupdf.open(path) as doc:
            if doc.page_count:
                r = doc[0].rect
                size = {"w": round(r.width, 2), "h": round(r.height, 2)}
    except Exception:
        size = None
    _pdf_size[stamp] = size
    return size

def render_page(path, page_no, scale=PAGE_SCALE):
    """Render one PDF page to a cached PNG under ``temp/pdfimg`` and return its path."""
    path = S.ensure_local(path)
    if not path or not os.path.exists(path):
        return None
    stamp = f"{os.path.basename(path)}|{os.path.getmtime(path):.0f}|{page_no}|{scale}"
    name = hashlib.sha1(stamp.encode("utf-8")).hexdigest()[:20] + ".png"
    folder = _pdfimg_dir()
    out = os.path.join(folder, name)
    if os.path.exists(out):
        return out
    try:
        fd, tmp = tempfile.mkstemp(suffix=".part", dir=folder)
    except OSError as exc:
        # An unwritable cache folder must not blank the page: re-probe (the sync
        # client may have dehydrated it since start-up) and fall back locally.
        log(f"render_page: cache folder unusable ({exc!r}); falling back")
        folder = _pdfimg_dir(force=True)
        out = os.path.join(folder, name)
        if os.path.exists(out):
            return out
        try:
            fd, tmp = tempfile.mkstemp(suffix=".part", dir=folder)
        except OSError as exc2:
            log(f"render_page: no writable cache folder ({exc2!r})")
            return None
    try:
        import pymupdf

        with pymupdf.open(path) as doc:
            if page_no < 1 or page_no > doc.page_count:
                return None
            pix = doc[page_no - 1].get_pixmap(
                matrix=pymupdf.Matrix(scale, scale), alpha=False, colorspace=pymupdf.csRGB
            )
            with os.fdopen(fd, "wb") as fh:
                fd = None
                fh.write(pix.tobytes("png"))
        atomic_replace(tmp, out)
        return out
    except Exception as exc:
        log(f"render_page failed for {os.path.basename(path)} p{page_no}: {exc!r}")
        return None
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass

#: Upper bound for the rendered-page PNG cache in ``temp/pdfimg``; the oldest
#: pages are removed beyond it (they are re-rendered on demand).
PAGE_CACHE_MAX_BYTES = 1500 * 1024 * 1024


_PDFIMG = {"key": None, "dir": None}


def _pdfimg_dir(force=False):
    """A writable folder for rendered page images.

    ``temp/pdfimg`` lives on the (possibly cloud-synced) project tree; a sync
    client can leave it as a dehydrated placeholder where creating a file fails,
    which used to make every PDF page render blank.  Probe once and fall back to
    the local application folder."""
    key = S.PDFIMG
    if not force and _PDFIMG["key"] == key and _PDFIMG["dir"]:
        return _PDFIMG["dir"]
    folder = key
    try:
        os.makedirs(folder, exist_ok=True)
        fd, probe = tempfile.mkstemp(suffix=".wtest", dir=folder)
        os.close(fd)
        os.remove(probe)
    except OSError:
        folder = os.path.join(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir(),
                             "CodingStudio", "pdfimg")
        os.makedirs(folder, exist_ok=True)
        log(f"pdfimg: {key} is not writable; using {folder}")
    _PDFIMG.update(key=key, dir=folder)
    return folder


def prune_page_cache(max_bytes=PAGE_CACHE_MAX_BYTES):
    """Trim the page-image cache to ``max_bytes``, oldest-accessed pages first."""
    folder = _pdfimg_dir()
    if not folder or not os.path.isdir(folder) or os.path.normcase(folder) == os.path.normcase(S.TEMP or ""):
        return 0
    files = []
    total = 0
    try:
        with os.scandir(folder) as it:
            for e in it:
                if not e.is_file():
                    continue
                st = e.stat()
                if e.name.endswith(".part") and time.time() - st.st_mtime > 3600:
                    files.append((0, st.st_size, e.path))   # stale partial write
                elif e.name.endswith(".png"):
                    files.append((max(st.st_atime, st.st_mtime), st.st_size, e.path))
                total += st.st_size
    except OSError as exc:
        log(f"prune_page_cache: {exc!r}")
        return 0
    removed = 0
    files.sort()
    for stamp, size, path in files:
        if total <= max_bytes and stamp:
            break
        try:
            os.remove(path)
            total -= size
            removed += 1
        except OSError:
            continue
    if removed:
        log(f"prune_page_cache: removed {removed} cached page images")
    return removed

_pdf_search = {}

def search_pdf(path, query):
    """Locate every occurrence of ``query`` in a PDF (case-insensitive).

    Returns ``{"query", "count", "hits": [{"page", "rect": [x0, y0, x1, y1]}]}``
    where each rect is expressed as a fraction (0-1) of its page box, so the
    client can position highlights over the server-rendered page images at any
    zoom.  ``None`` when the file cannot be read.
    """
    path = S.ensure_local(path)
    if not path or not os.path.exists(path):
        return None
    q = str(query or "").strip()
    if len(q) < 2:
        return {"query": q, "count": 0, "hits": []}
    ck = (os.path.basename(path), int(os.path.getmtime(path)), q.lower())
    cached = _pdf_search.get(ck)
    if cached is not None:
        return cached
    hits = []
    try:
        import pymupdf

        with pymupdf.open(path) as doc:
            for i, page in enumerate(doc):
                box = page.rect
                if box.width <= 0 or box.height <= 0:
                    continue
                for r in page.search_for(q):
                    hits.append({
                        "page": i + 1,
                        "rect": [round((r.x0 - box.x0) / box.width, 5),
                                 round((r.y0 - box.y0) / box.height, 5),
                                 round((r.x1 - box.x0) / box.width, 5),
                                 round((r.y1 - box.y0) / box.height, 5)],
                    })
                    if len(hits) >= 4000:
                        break
                if len(hits) >= 4000:
                    break
    except Exception:
        return None
    out = {"query": q, "count": len(hits), "hits": hits}
    if len(_pdf_search) > 64:
        _pdf_search.clear()
    _pdf_search[ck] = out
    return out

_pdf_spans = LRU(256)

def pdf_page_spans(path, page_no, max_spans=4000):
    """Text spans of one PDF page as fraction boxes, for the viewer text layer.

    Returns ``{"page", "w", "h", "spans": [[x0, y0, x1, y1, text], ...]}`` with
    each coordinate expressed as a fraction (0-1) of the page box, so the client
    can lay invisible-but-selectable spans over the server-rendered page image
    at any zoom.  ``None`` when the file cannot be read.
    """
    path = S.ensure_local(path)
    if not path or not os.path.exists(path):
        return None
    stamp = (os.path.basename(path), int(os.path.getmtime(path)), int(page_no))
    cached = _pdf_spans.get(stamp)
    if cached is not None:
        return cached
    out = None
    try:
        import pymupdf

        with pymupdf.open(path) as doc:
            if page_no < 1 or page_no > doc.page_count:
                return None
            page = doc[page_no - 1]
            box = page.rect
            if box.width <= 0 or box.height <= 0:
                return None
            spans = []
            for blk in page.get_text("dict").get("blocks", []):
                if blk.get("type") != 0:
                    continue
                for line in blk.get("lines", []):
                    for sp in line.get("spans", []):
                        text = str(sp.get("text") or "")
                        if not text.strip():
                            continue
                        x0, y0, x1, y1 = sp.get("bbox") or (0, 0, 0, 0)
                        spans.append([
                            round((x0 - box.x0) / box.width, 5),
                            round((y0 - box.y0) / box.height, 5),
                            round((x1 - box.x0) / box.width, 5),
                            round((y1 - box.y0) / box.height, 5),
                            text,
                        ])
                        if len(spans) >= max_spans:
                            break
                    if len(spans) >= max_spans:
                        break
                if len(spans) >= max_spans:
                    break
            out = {"page": int(page_no), "w": round(box.width, 2),
                   "h": round(box.height, 2), "spans": spans}
    except Exception as exc:
        log(f"pdf_page_spans failed for {os.path.basename(path)} p{page_no}: {exc!r}")
        return None
    _pdf_spans[stamp] = out
    return out

def extraction_path(key):
    return os.path.join(S.EXTRACTION, key + ".json")

def load_extraction(key):
    path = extraction_path(key)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        log(f"load_extraction({key!r}) failed: {exc!r}")
        return None
    return data if isinstance(data, list) else [data]

def coder_files(key):
    """The coder files of one study: ``{coder, file, modified, local}``.

    Listed from the **manifest** in an online project (``S.names``), so a
    route file that is on demand (``Codings/<Key> - SB.json``, see
    ``protocol.AI_ROUTE_TAGS``) is still counted in ``n_coders`` and named in
    the coder list without being downloaded.  A local project lists the disk.
    """
    prefix = key + " - "
    out = []
    for entry in S.names("Codings", ".json"):
        name = entry["name"]
        if not name.startswith(prefix):
            continue
        base = name[: -len(".json")]
        # A harmonizer coder file (`<Key> - harmonizer - <coder>.json`) is NOT a paper
        # coding: it holds harmonized-variable overrides.  Without this guard it would be
        # counted as an extra ratER by the agreement layer and would inflate `n_coders`.
        if " - harmonizer - " in base:
            continue
        coder = base[len(key) + 3:]
        # "Consensus" and "_*" are the settlement route's names, not coders.
        if not coder or coder == "Consensus" or coder.startswith("_"):
            continue
        mtime_ns = entry.get("mtime_ns")
        try:
            stamp = datetime.fromtimestamp(mtime_ns / 1e9).isoformat(timespec="seconds") \
                if mtime_ns else None
        except (OSError, OverflowError, ValueError):
            stamp = None
        out.append({"coder": coder, "file": name, "modified": stamp,
                    "local": bool(entry.get("local", True))})
    return sorted(out, key=lambda d: d["coder"].lower())

def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)

def dv_metrics_state(dv):
    """Return ``(complete, reason)`` using the shared DV-completeness rule.

    Delegates to :func:`contract.dv_has_effect` — the SAME rule ``B_validate``
    applies — so a DV the app counts as complete can never fail the pipeline gate
    (audit M2).  ``reason`` is retained for the caller's signature.
    """
    return (bool(CT.dv_has_effect(dv)), "")

def is_fully_within_record(rec):
    """Delegates to the canonical :func:`contract.is_fully_within` (audit M2).

    Primary marker: the explicit ``Fully_Within`` field is ``True``.  Documented
    fallback only when that field is absent: one arm (``n_arms == 1``) and every
    eligible primary DV a within-person contrast (``contrast_type == 4``).  The
    coded ``Title`` is never consulted.
    """
    return bool(CT.is_fully_within(rec))


#: Required coded fields, mirroring ``Scripts/B_validate.py`` (REQUIRED).
#: The active project's scheme may declare its own list
#: (``rules.required_fields``); this is the fallback.
_VALIDATE_REQUIRED = ["Study_ID", "Year", "Authors", "Title", "Study_Description",
                      "Intervention_Type", "DV_Metrics"]


def required_fields():
    """The required coded fields: the scheme's list when it declares one."""
    rules = {}
    try:
        rules = project_scheme().get("rules") or {}
    except Exception:  # noqa: BLE001
        rules = {}
    required = rules.get("required_fields")
    if isinstance(required, list) and required:
        return [str(name) for name in required]
    return list(_VALIDATE_REQUIRED)


def validate_records(records):
    """Save-time warnings for codings that would fail ``B_validate`` (audit M3).

    Drafts are never blocked — the app returns the warnings to the coder.  The
    checks mirror ``B_validate.check_file`` and use the SAME completeness rule
    (:func:`contract.dv_has_effect`).  A per-intervention record is validated as
    coded (the ``Interventions`` overrides are optional at save time).
    """
    warnings = []
    if not isinstance(records, list) or not records:
        return ["top level must be a non-empty JSON array"]
    required = required_fields()
    for i, rec in enumerate(records):
        if not isinstance(rec, dict):
            warnings.append(f"[{i}] not an object")
            continue
        for k in required:
            if k not in rec or rec[k] in (None, "", []):
                warnings.append(f"[{i}] missing required field {k}")
        it = rec.get("Intervention_Type")
        it_codes = it if isinstance(it, list) else ([it] if it not in (None, "") else [])
        if any(str(c) == "other" for c in it_codes) and not rec.get("Intervention_Type_Detail"):
            warnings.append(f"[{i}] Intervention_Type={it} requires Intervention_Type_Detail")
        dvs = rec.get("DV_Metrics")
        if not isinstance(dvs, list):
            warnings.append(f"[{i}] DV_Metrics must be a list")
            continue
        for j, dv in enumerate(dvs):
            if not isinstance(dv, dict):
                warnings.append(f"[{i}].DV_Metrics[{j}] not an object")
                continue
            if dv.get("dv_name") in (None, ""):
                warnings.append(f"[{i}].DV_Metrics[{j}] missing dv_name")
            if dv.get("contrast_type") is None:
                warnings.append(f"[{i}].DV_Metrics[{j}] missing contrast_type")
            if dv.get("eligible_primary") is None:
                warnings.append(f"[{i}].DV_Metrics[{j}] missing eligible_primary")
            if CT.dv_missing_reason_contradiction(dv):
                warnings.append(f"[{i}].DV_Metrics[{j}] has both a coded effect and a "
                                "missing_metrics_reason")
            reason = dv.get("missing_metrics_reason")
            if not (CT.dv_has_effect(dv) or (reason and str(reason).strip())):
                warnings.append(
                    f"[{i}].DV_Metrics[{j}] no descriptives/fallback and empty "
                    "missing_metrics_reason")
    return warnings

def is_fully_within(records):
    """True when every experiment record of one paper is fully-within."""
    recs = [r for r in (records or []) if isinstance(r, dict)]
    return bool(recs) and all(is_fully_within_record(r) for r in recs)

#: key -> (extraction stamp, extraction-derived summary part)
_SUMMARY_CACHE = {}


def _extraction_summary(key):
    """The part of :func:`summarise` that depends only on ``Extraction/<key>.json``.

    Cached on the file's (mtime, size), so ``/api/papers`` re-parses only the
    extractions that changed since the last call instead of all ~800.
    """
    stamp = file_stamp(S.EXTRACTION, key + ".json")
    hit = _SUMMARY_CACHE.get(key)
    if hit is not None and stamp is not None and hit[0] == stamp:
        return hit[1]
    part = _summarise_records(key, load_extraction(key) if stamp is not None else None)
    _SUMMARY_CACHE[key] = (stamp, part)
    return part


def _summarise_records(key, records):
    module = None
    n_dv = 0
    missing = 0
    needs_reason = 0
    facets = {"module": set(), "type": set(), "age": set(), "setting": set(),
              "design": set(), "availability": set()}
    if records:
        mods = _record_modules(records[0])
        module = ", ".join(mods) if mods else None
        for rec in records:
            if not isinstance(rec, dict):
                continue
            for m in _record_modules(rec):
                facets["module"].add(m)
            for dim, field in (("type", "Intervention_Type"),
                               ("age", "Age_Group"), ("setting", "Setting"),
                               ("design", "within_between"),
                               ("availability", "dataset_availability_status")):
                for val in _values(rec.get(field)):
                    if val not in (None, ""):
                        facets[dim].add(val)
            for dv in rec.get("DV_Metrics") or []:
                if not isinstance(dv, dict):
                    continue
                n_dv += 1
                ok, _ = dv_metrics_state(dv)
                if not ok:
                    missing += 1
                    if not str(dv.get("missing_metrics_reason") or "").strip():
                        needs_reason += 1
    # Study name / bibliographic meta for the coding leftbar, which mirrors the
    # screening list (title + authors · year).  The extraction's ``Title`` is the
    # study name; fall back to the study key so the entry is never blank.
    first = records[0] if records and isinstance(records[0], dict) else {}
    n_interventions = 0
    for rec in (records or []):
        ivs = rec.get("Interventions") if isinstance(rec, dict) else None
        if isinstance(ivs, list):
            n_interventions += len(ivs)
    title = first.get("Title")
    return {
        "name": (title.strip() if isinstance(title, str) else "") or key,
        "authors": (first.get("Authors") or "").strip() if isinstance(first.get("Authors"), str) else "",
        "year": first.get("Year") or "",
        "n_interventions": n_interventions,
        "module": module,
        "n_records": len(records or []),
        "n_dv": n_dv,
        "missing_metrics": missing,
        "needs_reason": needs_reason,
        "facets": {k: sorted(v, key=lambda x: str(x).lower()) for k, v in facets.items()},
        "has_extraction": bool(records),
        "has_pdf": S.exists(os.path.join(S.PAPERS, key + ".pdf")),
        "has_text": S.exists(os.path.join(S.TEXT, key + ".txt")),
        "has_notes": os.path.exists(os.path.join(S.EXTRACTION, key + ".md")),
        "has_harmonized": S.has_file(os.path.join(S.DATASETS or "", key,
                                                  key + " - harmonized_variables.csv")),
        "has_harmonizer": bool(glob.glob(os.path.join(S.CODINGS,
                                                      glob.escape(key) + " - harmonizer - *.json"))),
        # Fully-within (single-arm, no-control) studies: excluded from pooled
        # analysis by default; the front end marks them and the analysis tab
        # opts them in explicitly.
        "fully_within": is_fully_within(records),
    }


def summarise(key):
    out = {"key": key}
    out.update(_extraction_summary(key))
    codings = coder_files(key)
    out.update({
        "n_coders": len(codings),
        # S.exists / S.has_file also see an online project's manifest (a paper
        # or dataset that is listed but not downloaded yet)
        "has_pdf": S.exists(os.path.join(S.PAPERS, key + ".pdf")),
        "has_text": S.exists(os.path.join(S.TEXT, key + ".txt")),
        "has_notes": has_file(S.EXTRACTION, key + ".md"),
        "has_harmonized": S.has_file(os.path.join(S.DATASETS or "", key,
                                                  key + " - harmonized_variables.csv")),
        "has_harmonizer": bool(dir_names(S.CODINGS, key + " - harmonizer - ", ".json")),
        "supplements": supplements(key),
        "codings": codings,
    })
    return out

# ---------------------------------------------------------------- summary tab
# Paper-coding summary: aggregate every categorical coding field (schema fields
# with an explicit option list) and report how many experiments / DV metrics sit
# at each level.  Clicking a level lists the contributing rows with context and
# lets the coder edit the values inline; edits are written to the coder's own
# Codings file (the canonical Extraction JSON is never touched).

_SCHEMA_CACHE = {"obj": None}

_CODING_INDEX = {"at": 0.0, "data": None}


def _legacy_document():
    """The legacy v3.1 schema document of the active project (if it has one).

    The project's own ``Scripts/coding_web/schema.json`` wins; the copy shipped
    with the app is the fallback (the packaged build keeps it beside the exe).
    """
    paths = []
    try:
        paths.append(project_context.legacy_schema_path())
    except Exception:  # noqa: BLE001
        pass
    paths.append(SCHEMA)
    for path in paths:
        try:
            if path and os.path.exists(path):
                with open(path, encoding="utf-8") as fh:
                    doc = json.load(fh)
                if isinstance(doc, dict) and doc:
                    return doc
        except (OSError, json.JSONDecodeError):
            continue
    return {}


def _scheme_stamp():
    """A cheap fingerprint of the scheme source, so a rewrite is noticed."""
    paths = []
    try:
        # the file the scheme is actually read from (library, the project's own
        # Project/scheme.json, or the legacy schema), so an edit there counts
        src = project_context.scheme_source() or {}
        if src.get("path"):
            paths.append(src["path"])
    except Exception:  # noqa: BLE001
        pass
    try:
        paths.append(project_context.scheme_path(project_context.active_scheme_id() or ""))
        paths.append(project_context.legacy_schema_path())
    except Exception:  # noqa: BLE001
        pass
    paths.append(SCHEMA)
    for path in paths:
        try:
            st = os.stat(path)
        except OSError:
            continue
        return (path, st.st_mtime_ns, st.st_size)
    return None


def project_scheme():
    """The active project's scheme, normalised (the new shape).

    A project that carries its own ``Project/scheme.json`` is read from there;
    a project without one falls back to its legacy schema, converted on the
    fly.  The document is cached until the source file changes, so a rewritten
    scheme is picked up without a restart.
    """
    stamp = _scheme_stamp()
    cached = _SCHEMA_CACHE.get("project")
    if cached and cached.get("stamp") == stamp:
        return cached["scheme"]
    scheme = {}
    try:
        scheme = project_context.scheme() or {}
    except Exception:  # noqa: BLE001
        scheme = {}
    if not (scheme.get("fields") or (scheme.get("metrics") or {}).get("fields")):
        legacy = _legacy_document()
        if legacy:
            try:
                scheme = scheme_legacy.convert_to_normalised(legacy)
            except Exception:  # noqa: BLE001
                pass
    _SCHEMA_CACHE["project"] = {"stamp": stamp, "scheme": scheme}
    return scheme


def _base_type_of(field_type):
    """The base part of a v3.1 type: "string|null" -> "string"."""
    return str(field_type or "").split("|")[0].strip()


def legacy_schema_view(scheme):
    """Render a scheme in the legacy v3.1 shape.

    The v3.1 document is what the coder form, the agreement tab and the
    analysis tab consume: a list of group names, a flat list of study fields,
    a flat list of DV fields, the intervention modules and the controlled
    vocabularies.  A scheme is the same information with roles, conditions and
    kinds added, so it is rendered back into that shape here.

    One asymmetry is repaired on the way: a scheme holds **one** field per
    name, while the v3.1 document repeated a field name in several modules
    (each with its own option list).  The conversion keeps the first
    occurrence and parks the per-module option lists in ``module_options``;
    the view puts those fields back into the modules they belong to, so the
    coder form still offers every input the v3.1 document had.
    """
    scheme = scheme if isinstance(scheme, dict) else {}

    def field_view(field, group=None):
        name = field.get("name") or ""
        base = field.get("type") or "string"
        options = list(field.get("options") or [])
        vocab = list(field.get("vocab") or [])
        vocab_labels = {}
        if options and field.get("editor") in ("multiselect", "chips"):
            # The scheme may carry human display labels for a token field's
            # levels.  They ride on ``vocab_labels`` so the coding form can
            # show them beside every stored token; a value is never touched.
            vocab_labels = {
                str(o.get("value")): str(o.get("label"))
                for o in options if o.get("value") is not None
            }
        if options and vocab and field.get("editor") in ("multiselect", "chips"):
            # The v3.1 document carried the levels of a multi-label field in
            # its ``vocab`` and left ``options`` empty.  The scheme derives an
            # option list from the vocabulary, which would hide the fact that
            # the field holds several labels at once (the agreement layer
            # reads that difference), so the option list is dropped again.
            values = {str(o.get("value")) for o in options}
            if values == {str(v) for v in vocab}:
                options = []
        return {
            "name": name,
            "label": field.get("label") or name,
            "group": group if group is not None else (field.get("group") or ""),
            "type": base,
            "base_type": base,
            "description": field.get("description") or "",
            "required": bool(field.get("required")),
            "derived": bool(field.get("derived")),
            "moderator": bool(field.get("moderator", True)),
            "module": field.get("module"),
            "editor": field.get("editor") or "text",
            "options": options,
            "vocab": vocab,
            "vocab_labels": vocab_labels,
            "role": field.get("role"),
            # the scheme's own coder-visibility mechanisms, passed through so
            # the form can honor them (fail-open: an absent key shows)
            "show_if": field.get("show_if") or None,
            "coder": field.get("coder") or {},
        }

    # Every field of the scheme, by name (the first occurrence wins).
    by_name = {}
    for field in (scheme.get("fields") or []):
        by_name.setdefault(field.get("name"), field)
    for mod in (scheme.get("modules") or {}).values():
        for field in (mod.get("fields") or []):
            by_name.setdefault(field.get("name"), field)
    for field in ((scheme.get("metrics") or {}).get("fields") or []):
        by_name.setdefault(field.get("name"), field)

    groups = [g.get("name") for g in (scheme.get("groups") or []) if g.get("name")]
    common = [field_view(f) for f in (scheme.get("fields") or [])]
    dv = [field_view(f) for f in ((scheme.get("metrics") or {}).get("fields") or [])]
    modules = {}
    for key, mod in (scheme.get("modules") or {}).items():
        group = f"Intervention module ({key})"
        fields = [field_view(f, group) for f in (mod.get("fields") or [])]
        seen = {f["name"] for f in fields}
        # Put back the fields the v3.1 document repeated in this module.
        for name, field in by_name.items():
            options = (field.get("module_options") or {}).get(key)
            if options is None or name in seen:
                continue
            clone = field_view(field, group)
            clone["options"] = list(options)
            fields.append(clone)
            seen.add(name)
        modules[key] = {
            "label": mod.get("label") or key,
            "family": mod.get("family"),
            "intervention_types": list(mod.get("intervention_types") or []),
            "description": mod.get("description") or "",
            "fields": fields,
        }
    return {
        "schema_version": "3.1",
        "groups": groups,
        "common_fields": common,
        "dv_fields": dv,
        "modules": modules,
        "intervention_type_modules": {
            str(k): [str(m) for m in v]
            for k, v in (scheme.get("intervention_type_modules") or {}).items()
            if isinstance(v, list)
        },
        "vocabularies": scheme.get("vocabularies") or {},
    }


def _restore_module_membership(doc, legacy):
    """Put back the module fields a scheme cannot keep.

    A scheme holds **one** field per name, while the v3.1 document repeated a
    field name in several modules (each with its own description and option
    list).  The conversion keeps the first occurrence, so the membership of
    the later ones is gone from the scheme.

    The v3.1 document still knows the membership, and the scheme still knows
    the name, so the field is put back into the module it belonged to.  The
    scheme's own version of a field always wins; the v3.1 document is only
    consulted for the membership and for the fields the scheme never placed
    in that module.
    """
    if not legacy:
        return doc
    known = {f.get("name") for f in (doc.get("common_fields") or [])}
    for mod in (doc.get("modules") or {}).values():
        known.update(f.get("name") for f in (mod.get("fields") or []))
    known.update(f.get("name") for f in (doc.get("dv_fields") or []))
    # The names the v3.1 document repeated across modules (or repeated between
    # the common core and a module): for those the scheme cannot hold a
    # per-module definition, so the v3.1 one is used.
    counts = {}
    for field in (legacy.get("common_fields") or []):
        name = field.get("name")
        if name:
            counts[name] = counts.get(name, 0) + 1
    for mod in (legacy.get("modules") or {}).values():
        for field in (mod.get("fields") or []):
            name = field.get("name")
            if name:
                counts[name] = counts.get(name, 0) + 1
    repeated = {name for name, n in counts.items() if n > 1}
    for key, mod in (legacy.get("modules") or {}).items():
        target = (doc.get("modules") or {}).get(key)
        if not target:
            continue
        own = {f.get("name"): f for f in (target.get("fields") or [])}
        ordered, seen = [], set()
        for field in (mod.get("fields") or []):
            name = field.get("name")
            if not name or name in seen:
                continue
            seen.add(name)
            if name in repeated:
                clone = dict(field)
                clone["group"] = f"Intervention module ({key})"
                ordered.append(clone)
            elif name in own:
                ordered.append(own[name])
            elif name in known:
                clone = dict(field)
                clone["group"] = f"Intervention module ({key})"
                ordered.append(clone)
        for field in (target.get("fields") or []):
            if field.get("name") not in seen:
                ordered.append(field)
                seen.add(field.get("name"))
        target["fields"] = ordered
    _restore_legacy_field_detail(doc, legacy)
    return doc


def _restore_legacy_field_detail(doc, legacy):
    """Restore the v3.1 details a scheme cannot carry.

    Three things are lost in the conversion and are taken back from the v3.1
    document when it is still there:

    * the **nullable marker** of a field (``"string|null"``) and the couple of
      types the scheme does not model (``"multiselect"``): the scheme knows the
      base type, and the coder form reads the marker when it initialises a
      blank value;
    * the **group of the DV fields** (the v3.1 document put them in "Other"),
      and with it the group list the coder form walks through;
    * the **field list itself**: a scheme may declare fields the v3.1 document
      never had (the corpus-only fields the pipeline reads).  The
      compatibility view renders the v3.1 document, so those stay out of the
      coder form until the form renders the derived blocks instead.
    """
    if not legacy:
        return doc
    legacy_fields = {}
    for bucket in ("common_fields", "dv_fields"):
        for field in (legacy.get(bucket) or []):
            legacy_fields[field.get("name")] = field
    for mod in (legacy.get("modules") or {}).values():
        for field in (mod.get("fields") or []):
            legacy_fields.setdefault(field.get("name"), field)
    groups = list(doc.get("groups") or [])
    for bucket in ("common_fields", "dv_fields"):
        kept = []
        for field in (doc.get(bucket) or []):
            old = legacy_fields.get(field.get("name"))
            if not old:
                continue          # a corpus-only field: not part of v3.1
            kept.append(field)
            old_type = old.get("type")
            if old_type and (_base_type_of(old_type) == field.get("type")
                             or _base_type_of(old_type) not in FIELD_TYPES):
                # the v3.1 spelling carries the nullable marker ("string|null")
                # and a couple of types the scheme does not model ("multiselect")
                field["type"] = old_type
                field["base_type"] = old.get("base_type") or _base_type_of(old_type)
            elif field.get("nullable"):
                field["type"] = "%s|null" % field.get("type")
            if bucket == "dv_fields" and old.get("group"):
                field["group"] = old["group"]
                if old["group"] not in groups:
                    groups.append(old["group"])
        doc[bucket] = kept
    for mod in (doc.get("modules") or {}).values():
        kept = []
        for field in (mod.get("fields") or []):
            old = legacy_fields.get(field.get("name"))
            if not old:
                continue
            kept.append(field)
            old_type = old.get("type")
            if old_type and (_base_type_of(old_type) == field.get("type")
                             or _base_type_of(old_type) not in FIELD_TYPES):
                field["type"] = old_type
                field["base_type"] = old.get("base_type") or _base_type_of(old_type)
            elif field.get("nullable"):
                field["type"] = "%s|null" % field.get("type")
        mod["fields"] = kept
    doc["groups"] = groups
    return doc


def legacy_schema_document():
    """The legacy v3.1 schema document of the active project.

    A project that carries its own scheme is rendered back into the v3.1
    shape; a project without one serves its legacy file unchanged.  This is
    the compatibility view of the deprecated ``/api/schema`` route.
    """
    raw = {}
    try:
        raw = project_context.raw_scheme() or {}
    except Exception:  # noqa: BLE001
        raw = {}
    if raw:
        doc = legacy_schema_view(project_scheme())
        _restore_module_membership(doc, _legacy_document())
        _restore_legacy_field_detail(doc, _legacy_document())
        doc["source"] = "project scheme"
    else:
        doc = dict(_legacy_document())
        doc["source"] = "legacy schema.json"
    doc.setdefault("schema_version", "3.1")
    return doc


def _schema():
    """The coding-schema index in the v3.1 shape, read from the project.

    The document is rendered from the active project's scheme, so a rewritten
    scheme is picked up without a restart and a re-root switches the scheme
    with the project.
    """
    stamp = _scheme_stamp()
    if _SCHEMA_CACHE["obj"] is None or _SCHEMA_CACHE.get("stamp") != stamp:
        doc = legacy_schema_view(project_scheme())
        _restore_legacy_field_detail(doc, _legacy_document())
        _SCHEMA_CACHE["obj"] = doc
        _SCHEMA_CACHE["stamp"] = stamp
    return _SCHEMA_CACHE["obj"]

def _record_modules(rec):
    """Module keys implied by a record's list-valued Intervention_Type."""
    if not isinstance(rec, dict):
        return []
    s = _schema()
    modules = s.get("modules") or {}
    types = _values(rec.get("Intervention_Type"))
    out = []
    for t in types:
        for m in ((s.get("intervention_type_modules") or {}).get(t) or []):
            if m in modules and m not in out:
                out.append(m)
    for name, mod in modules.items():
        if name not in out and any(t in (mod.get("intervention_types") or []) for t in types):
            out.append(name)
    return out

def _scalar(v):
    return v is None or isinstance(v, (bool, int, float, str))

def _norm(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)

def _values(v):
    """Normalised values for a coded cell (list cells contribute to each element)."""
    if isinstance(v, (list, tuple)):
        seen, out = set(), []
        for x in v:
            if _scalar(x):
                n = _norm(x)
                if n not in seen:
                    seen.add(n); out.append(n)
        return out
    if _scalar(v):
        return [_norm(v)]
    return []

def _is_continuous(field):
    """True for continuous/scale fields (numeric with no fixed option list)."""
    if field.get("options"):
        return False
    bt = str(field.get("base_type") or field.get("type") or "").lower()
    return any(k in bt for k in ("integer", "number", "float", "double", "int", "real"))

def _is_object_field(field):
    """True for list/object container fields that have no categorical levels.

    ``Covariates``, ``factor_levels``, ``cell_descriptives`` and
    ``conversion_inputs`` are object/list containers, not categories: showing a
    "0 levels" entry for them in the variables/levels summary is misleading, so
    they are excluded from it.
    """
    if field.get("name") in ("Covariates", "factor_levels", "cell_descriptives",
                             "conversion_inputs", "Gender_Ratio"):
        return True
    bt = str(field.get("base_type") or field.get("type") or "").lower()
    ed = str(field.get("editor") or "").lower()
    return bt == "object" or ed in ("objectlist", "json", "kv")

def _summary_field_lists():
    """Categorical coding fields (common + DV).

    Continuous/scale fields and list/object containers are excluded (the latter
    because they are not categorical, so a level count is meaningless); derived
    fields are excluded because they are not paper-coded.
    """
    s = _schema()

    def keep(f):
        return bool(f.get("name")) and not _is_continuous(f) \
            and not _is_object_field(f) and not f.get("derived")

    common = [f for f in (s.get("common_fields") or []) if keep(f)]
    dv = [f for f in (s.get("dv_fields") or []) if keep(f)]
    return common, dv

def _observed_levels(rows, names):
    out = {}
    for n in names:
        vals = set()
        for r in rows:
            vals.update(_values(r.get(n)))
        out[n] = sorted(vals, key=lambda x: (x == "", x))
    return out

def _own_coding(key, coder):
    """The records of ``Codings/<key> - <coder>.json``, else None."""
    path = os.path.join(S.CODINGS, f"{key} - {safe_token(coder)}.json")
    # the coder's own file is core, but an AI route tag (SB/MM/DS) is on demand:
    # this read is a single explicit request for one study, so fetch it
    S.ensure_local(path)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    data = data if isinstance(data, list) else [data]
    return [r for r in data if isinstance(r, dict)] or None


def coding_index(coder=""):
    """Per-experiment and per-DV rows for every coded paper, plus field metadata.

    With a ``coder`` the rows come from that coder's own file where one exists
    (``source`` = "mine"), else from the reference ``Extraction/<Key>.json``
    (``source`` = "reference"), so the Summary shows what a coder has saved
    instead of looking reverted to the reference.
    """
    coder = str(coder or "").strip()
    if coder.lower() in ("og", "original", "original (og)"):
        coder = ""
    now = time.time()
    # the index belongs to one project (and one coder): after a re-root or a
    # coder switch it is rebuilt at once, not served for the rest of its 20 s
    if (_CODING_INDEX["data"] is not None and now - _CODING_INDEX["at"] < 20
            and _CODING_INDEX.get("root") == S.ROOT
            and _CODING_INDEX.get("coder", "") == coder):
        return _CODING_INDEX["data"]
    common, dvfields = _summary_field_lists()
    cnames = [f["name"] for f in common]
    dnames = [f["name"] for f in dvfields]
    exps, dvs = [], []
    for key in paper_keys():
        records = _own_coding(key, coder) if coder else None
        source = "mine" if records else "reference"
        if not records:
            records = load_extraction(key)
        if not records:
            continue
        coders = coder_files(key)
        for rec in records:
            n_dv = missing = 0
            for d in rec.get("DV_Metrics") or []:
                n_dv += 1
                try:
                    if not dv_metrics_state(d)[0]:
                        missing += 1
                except Exception:
                    pass
            row = {n: rec.get(n) for n in cnames}
            row.update(study_key=key, experiment_id=rec.get("Study_ID"), source=source,
                       n_dv=n_dv, n_missing=missing, n_coders=len(coders),
                       intervention_types=", ".join(_values(rec.get("Intervention_Type"))))
            exps.append(row)
            for i, d in enumerate(rec.get("DV_Metrics") or []):
                drow = {n: d.get(n) for n in dnames}
                drow.update(study_key=key, experiment_id=rec.get("Study_ID"), source=source,
                            dv_index=i, dv_name=d.get("dv_name"))
                dvs.append(drow)
    data = {"experiments": exps, "dvs": dvs, "common_fields": common, "dv_fields": dvfields,
            "common_names": set(cnames), "dv_names": set(dnames),
            "common_observed": _observed_levels(exps, cnames),
            "dv_observed": _observed_levels(dvs, dnames)}
    _CODING_INDEX.update(at=now, data=data, root=S.ROOT, coder=coder)
    return data


#: M's per-paper coding-index cache; kept for the re-root hook below (the merged
#: ``coding_index`` uses the feature's root/coder-keyed ``_CODING_INDEX``).
_CODING_ROWS = {}


@on_reroot
def _reset_root_caches(_old=None, _new=None):
    """Drop every cache that holds data of the previous data root."""
    invalidate_dir()
    _PDFIMG.update(key=None, dir=None)
    _SUMMARY_CACHE.clear()
    _CODING_ROWS.clear()
    _CODING_INDEX.update(at=0.0, data=None, sig=None)
    for cache in (_pdf_pages, _pdf_size, _pdf_text, _pdf_spans, _pdf_search):
        cache.clear()

def _field_by_name(name):
    common, dv = _summary_field_lists()
    for f in common:
        if f["name"] == name:
            return f, "experiment"
    for f in dv:
        if f["name"] == name:
            return f, "dv"
    return None, None

def _field_options(field, observed):
    opts = field.get("options") or []
    if opts:
        return [{"value": _norm(o.get("value")), "label": o.get("label", _norm(o.get("value")))}
                for o in opts]
    vals = observed if isinstance(observed, list) else sorted(observed or [], key=str)
    return [{"value": v, "label": (v if v != "" else None)} for v in vals]
