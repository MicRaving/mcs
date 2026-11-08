"""A file-stamp cache for the corpus index.

The app's slowest request by far is the paper list: it summarises every study,
and a summary opens the study's ``Extraction/`` record and every coder's file.
On a large review that is tens of seconds, and every other request queues behind
it.

The files stay the authority.  A cached summary is only reused while the files
it was derived from are the ones it saw: a directory listing carries each file's
size and modification time, so one pass per folder (one syscall per folder, not
one per file) is enough to decide it.  A file touched by the app or by hand - an
editor, a script, a sync client - changes its stamp, and only that study is
summarised again.  The app's own writes go to the same files, so they need no
separate invalidation path.

The cache is also written to ``<root>/temp/papers_index.json``, so a restart is
warm: the snapshot is read only while its stamps still match the folders.
"""

import json
import os
import time

from studio_lib import APP_VERSION
from studio_state import S, log

# Bump when the shape of a summary changes, so an old snapshot is discarded
# instead of feeding the page fields it no longer understands.
SNAPSHOT_VERSION = 1

# ``root`` pins the in-memory index to one project: a re-root (POST /api/root)
# must not reuse the previous project's summaries, even for a study key that
# both folders share.
_INDEX = {"root": None, "stamps": None, "papers": None}


def _listing(folder, suffix):
    """``{file name: [size, mtime_ns]}`` for one folder, in one pass."""
    out = {}
    try:
        with os.scandir(folder) as it:
            for entry in it:
                if suffix and not entry.name.lower().endswith(suffix):
                    continue
                try:
                    if not entry.is_file():
                        continue
                    st = entry.stat()
                except OSError:
                    continue
                out[entry.name] = [st.st_size, st.st_mtime_ns]
    except OSError:
        return {}
    return out


def _dir_listing(folder):
    """``{folder name: [mtime_ns]}`` for the sub-folders of one folder."""
    out = {}
    try:
        with os.scandir(folder) as it:
            for entry in it:
                try:
                    if not entry.is_dir():
                        continue
                    out[entry.name] = [entry.stat().st_mtime_ns]
                except OSError:
                    continue
    except OSError:
        return {}
    return out


def _prefixed(mapping, key):
    """The ``(name, info)`` pairs of ``mapping`` that belong to one study.

    Matched by the ``"<key> - "`` prefix rather than by splitting on the
    separator: a key may contain the separator itself, and a harmonizer file
    (``<Key> - harmonizer - <coder>.json``) is not a paper coding.
    """
    prefix = key + " - "
    # lists, not tuples: the stamp is written to the snapshot as JSON, and a
    # tuple would come back as a list and never compare equal again.
    return [[name, info] for name, info in sorted(mapping.items())
            if name.startswith(prefix)]


def _corpus_stamps(keys):
    """``{key: stamp}`` - everything a study's summary was derived from."""
    extraction = _listing(S.EXTRACTION, ".json")
    notes = _listing(S.EXTRACTION, ".md")
    codings = _listing(S.CODINGS, ".json")
    papers = _listing(S.PAPERS, ".pdf")
    texts = _listing(S.TEXT, ".txt")
    supps = _listing(S.SUPPLEMENTS, "")
    datasets = _dir_listing(S.DATASETS)

    stamps = {}
    for key in keys:
        stamps[key] = [
            extraction.get(key + ".json"),
            notes.get(key + ".md"),
            papers.get(key + ".pdf"),
            texts.get(key + ".txt"),
            datasets.get(key),
            _prefixed(codings, key),
            _prefixed(supps, key),
        ]
    return stamps


def _snapshot_path():
    return os.path.join(S.TEMP, "papers_index.json")


def _load_snapshot():
    try:
        with open(_snapshot_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("version") != SNAPSHOT_VERSION or data.get("app") != APP_VERSION:
        return None
    if not isinstance(data.get("stamps"), dict) or not isinstance(data.get("papers"), dict):
        return None
    return data


def _save_snapshot(stamps, papers):
    path = _snapshot_path()
    payload = {"version": SNAPSHOT_VERSION, "app": APP_VERSION,
               "stamps": stamps, "papers": papers}
    tmp = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError as exc:
        log(f"papers index snapshot not written: {exc}")
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def papers_index(keys, build):
    """The summary of every study, reusing the ones the files have not changed.

    ``keys`` is the project's study keys in display order; ``build(key)`` is the
    expensive summary (``studio_lib.summarise``).  Returns a list in the same
    order as ``keys``.
    """
    _PROGRESS.update(phase="stamping", done=0, total=len(keys), at=time.time())
    stamps = _corpus_stamps(keys)
    _PROGRESS.update(phase="reading", done=0)

    if _INDEX["root"] != S.ROOT:
        # another project: forget the old one and read this one's snapshot
        _INDEX.update(root=S.ROOT, papers=None, stamps=None)
    papers = _INDEX["papers"]
    old_stamps = _INDEX["stamps"]
    if papers is None:
        snap = _load_snapshot()
        if snap is not None and set(snap["stamps"]) == set(stamps):
            papers, old_stamps = snap["papers"], snap["stamps"]
            _INDEX["papers"], _INDEX["stamps"] = papers, old_stamps

    if papers is None:
        papers, old_stamps = {}, {}

    out = []
    fresh = {}
    changed = False
    for n, key in enumerate(keys):
        _PROGRESS["done"] = n
        stamp = stamps[key]
        if key in papers and old_stamps.get(key) == stamp:
            out.append(papers[key])
            fresh[key] = papers[key]
            continue
        try:
            value = build(key)
        except Exception as exc:  # noqa: BLE001 - one bad study must not break the list
            log(f"summarise({key}) failed: {exc}")
            value = papers.get(key) or {"key": key}
        out.append(value)
        fresh[key] = value
        changed = True

    if set(fresh) != set(papers):
        changed = True
    _INDEX["papers"] = fresh
    _INDEX["stamps"] = stamps
    if changed:
        _PROGRESS.update(phase="saving", done=len(keys))
        _save_snapshot(stamps, fresh)
    _PROGRESS.update(phase="done", done=len(keys), at=time.time())
    return out


_PROGRESS = {"phase": "idle", "done": 0, "total": 0, "at": 0.0}


def papers_progress():
    """How far the current (or last) :func:`papers_index` run is, for the
    first-load progress bar.  ``phase`` is idle / stamping / reading / saving / done."""
    return dict(_PROGRESS)


# --------------------------------------------------------------------------- #
# the response cache
#
# A few tabs compute an answer over the WHOLE corpus on every request: the
# agreement table, the coding summary, the variable list.  On a large review
# those cost 20-60 seconds, on every visit.  They are pure functions of the
# files, so their answers are cached against the same kind of stamp as the
# papers index: the answer is reused while Extraction/, Codings/ and
# Screening/ are untouched, and recomputed the moment any of them changes -
# by the app or by hand.
#
# The cache sits in front of the view, so the route modules stay unaware of it:
# a GET on one of these paths is answered from the cache, and the first real
# answer is stored on the way out.  Small answers are also kept on disk, so the
# first visit after a restart is fast too.
# --------------------------------------------------------------------------- #

RESPONSE_PATHS = {
    "/api/papers",
    "/api/summary/dimensions",
    "/api/summary/levels",
    "/api/summary/variables",
    "/api/agreement/reliability",
    "/api/agreement/conflicts",
    "/api/agreement/abstract/reliability",
    "/api/agreement/abstract/conflicts",
    "/api/agreement/abstract/llm-excluded",
}

# a 14 MB body is fine to hold in memory for the session, but not to write to
# the snapshot on every change
MAX_BODY = 40_000_000
MAX_SNAPSHOT_BODY = 1_500_000

_RESP = {}          # path + query -> (stamp, body)
_RESP_LOADED = False
_RESP_DIRTY = False
_RESP_ROOT = None   # the project the entries above belong to
_LAST_CORE = {}     # the last coding-folder stamp a miss saw (see _from_cache)


def _follow_root():
    """Drop the cached answers when the app was pointed at another project.

    Without this a re-root kept serving the previous project's answers (two
    empty or freshly-copied projects carry the same stamps), and the new
    project's own snapshot was never read because the old one counted as
    loaded.
    """
    global _RESP_LOADED, _RESP_DIRTY, _RESP_ROOT
    if _RESP_ROOT == S.ROOT:
        return
    _RESP.clear()
    _RESP_LOADED = False
    _RESP_DIRTY = False
    _RESP_ROOT = S.ROOT


def _resp_snapshot_path():
    return os.path.join(S.TEMP, "response_cache.json")


def _load_resp_snapshot():
    global _RESP_LOADED
    if _RESP_LOADED:
        return
    _RESP_LOADED = True
    try:
        with open(_resp_snapshot_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return
    if not isinstance(data, dict) or data.get("version") != SNAPSHOT_VERSION:
        return
    if data.get("app") != APP_VERSION:
        return
    for key, entry in (data.get("entries") or {}).items():
        if isinstance(entry, dict) and "stamp" in entry and "body" in entry:
            _RESP[key] = (entry["stamp"], entry["body"])


def _save_resp_snapshot():
    global _RESP_DIRTY
    if not _RESP_DIRTY:
        return
    entries = {}
    for key, (stamp, body) in _RESP.items():
        if isinstance(body, str) and len(body) <= MAX_SNAPSHOT_BODY:
            entries[key] = {"stamp": stamp, "body": body}
    path = _resp_snapshot_path()
    tmp = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"version": SNAPSHOT_VERSION, "app": APP_VERSION,
                       "entries": entries}, fh)
        os.replace(tmp, path)
        _RESP_DIRTY = False
    except (OSError, TypeError) as exc:
        log(f"response cache snapshot not written: {exc}")
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def _file_stamp(path):
    try:
        st = os.stat(path)
    except (OSError, TypeError):
        return None
    return [st.st_size, st.st_mtime_ns]


def _sorted_listing(folder, suffix):
    return [list(x) for x in sorted(_listing(folder, suffix).items())]


def response_stamp(path=None):
    """What the corpus-wide answers are derived from.

    Beyond the three coding folders: the project root (two projects never share
    an answer), the scheme (the summary and agreement fields come from it) and
    the screening master sheet (the abstract agreement reads it).  The paper
    list also reads the PDFs, texts, notes, supplements and datasets - a PDF
    added by hand must show up in the list, so those folders are part of its
    stamp.
    """
    try:
        from studio_lib import _scheme_stamp
        scheme = _scheme_stamp()
        scheme = list(scheme) if scheme else None
    except Exception:  # noqa: BLE001 - a broken scheme must not break the cache
        scheme = None
    stamp = [
        S.ROOT,
        scheme,
        _file_stamp(os.path.join(S.ROOT, "master_dataset.xlsx")),
        _sorted_listing(S.EXTRACTION, ".json"),
        _sorted_listing(S.CODINGS, ".json"),
        _sorted_listing(S.SCREENING, ".json"),
    ]
    if path == "/api/papers":
        stamp += [
            _sorted_listing(S.EXTRACTION, ".md"),
            _sorted_listing(S.PAPERS, ""),
            _sorted_listing(S.TEXT, ".txt"),
            _sorted_listing(S.SUPPLEMENTS, ""),
            [list(x) for x in sorted(_dir_listing(S.DATASETS).items())],
        ]
    return stamp


def _drop_inner_caches():
    """Forget the short-lived (time-based) caches the corpus-wide views keep.

    The coding summary and the agreement tables keep their own 15-20 s cache.
    When the files changed, the response cache asks the view again - and the
    view would answer from that inner cache, computed BEFORE the change, and
    the stale answer would then be stored under the NEW stamp, where it stayed
    until the next change.  So a miss on a changed stamp clears them first.
    """
    try:
        from studio_lib import _CODING_INDEX
        _CODING_INDEX["data"] = None
    except Exception:  # noqa: BLE001
        pass
    try:
        from agreement_lib import _invalidate_agreement
        _invalidate_agreement()
    except Exception:  # noqa: BLE001
        pass


def cached_body(key, stamp=None):
    """The stored body for ``key``, or None when it is stale or absent."""
    _follow_root()
    _load_resp_snapshot()
    hit = _RESP.get(key)
    if not hit:
        return None
    if stamp is None:
        stamp = response_stamp(key.split("?", 1)[0])
    return hit[1] if hit[0] == stamp else None


def store_body(key, body, stamp=None):
    global _RESP_DIRTY
    _follow_root()
    if stamp is None:
        stamp = response_stamp(key.split("?", 1)[0])
    _RESP[key] = (stamp, body)
    _RESP_DIRTY = True


def register_response_cache(app):
    """Serve the corpus-wide GETs from the cache, and fill it on the way out."""
    from flask import Response, request

    from flask import g

    @app.before_request
    def _from_cache():
        if request.method != "GET" or request.path not in RESPONSE_PATHS:
            return None
        key = request.path + "?" + request.query_string.decode("utf-8", "replace")
        # The stamp is taken BEFORE the view runs and stored with its answer: a
        # file written while the view was computing then leaves the entry stale
        # (recomputed next time) instead of marking an old answer as current.
        stamp = response_stamp(request.path)
        g.cs_cache_stamp = stamp
        body = cached_body(key, stamp)
        if body is None:
            # the coding-folder part of the stamp (everything bar the paper
            # list's extra folders) is what the inner caches depend on
            core = stamp[:6]
            if _LAST_CORE.get("root") != S.ROOT or _LAST_CORE.get("stamp") != core:
                if _LAST_CORE.get("stamp") is not None:
                    _drop_inner_caches()
                _LAST_CORE.update(root=S.ROOT, stamp=core)
            return None
        resp = Response(body, mimetype="application/json")
        resp.headers["X-CodingStudio-Cache"] = "hit"
        return resp

    @app.after_request
    def _to_cache(resp):
        if request.method != "GET" or request.path not in RESPONSE_PATHS:
            return resp
        if resp.status_code != 200 or resp.direct_passthrough:
            return resp
        body = resp.get_data(as_text=True)
        if body and len(body) <= MAX_BODY:
            key = request.path + "?" + request.query_string.decode("utf-8", "replace")
            store_body(key, body, getattr(g, "cs_cache_stamp", None))
            _save_resp_snapshot()
        return resp
