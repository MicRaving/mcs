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

from studio_lib import APP_VERSION
from studio_state import S, log

# Bump when the shape of a summary changes, so an old snapshot is discarded
# instead of feeding the page fields it no longer understands.
SNAPSHOT_VERSION = 1

_INDEX = {"stamps": None, "papers": None}


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
    stamps = _corpus_stamps(keys)

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
    for key in keys:
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
        _save_snapshot(stamps, fresh)
    return out


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


def response_stamp():
    """What the corpus-wide answers are derived from."""
    return [
        [list(x) for x in sorted(_listing(S.EXTRACTION, ".json").items())],
        [list(x) for x in sorted(_listing(S.CODINGS, ".json").items())],
        [list(x) for x in sorted(_listing(S.SCREENING, ".json").items())],
    ]


def cached_body(key):
    """The stored body for ``key``, or None when it is stale or absent."""
    _load_resp_snapshot()
    hit = _RESP.get(key)
    if not hit:
        return None
    return hit[1] if hit[0] == response_stamp() else None


def store_body(key, body):
    global _RESP_DIRTY
    _RESP[key] = (response_stamp(), body)
    _RESP_DIRTY = True


def register_response_cache(app):
    """Serve the corpus-wide GETs from the cache, and fill it on the way out."""
    from flask import Response, request

    @app.before_request
    def _from_cache():
        if request.method != "GET" or request.path not in RESPONSE_PATHS:
            return None
        key = request.path + "?" + request.query_string.decode("utf-8", "replace")
        body = cached_body(key)
        if body is None:
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
            store_body(key, body)
            _save_resp_snapshot()
        return resp
