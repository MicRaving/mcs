"""Screening logic: the master workbook index, per-coder decisions and verdict helpers."""

from datetime import date, datetime
import json
import math
import os
import re
import shutil
import tempfile
import threading
from studio_lib import atomic_replace, safe_token
from studio_state import S, log


# ---------------------------------------------------------------- abstract screening tab
# Human abstract screening against the inclusion criteria (C1-C4 by default,
# the scheme's own criteria when it has some).  Abstracts are read from the
# master screening workbook (master_dataset.xlsx).  Each completed abstract is
# written to Screening/<coder>.json immediately (one file per coder); a manual
# "Merge to master" action writes the coder's decisions back into the
# workbook's <key>_verdict / _certainty / _rationale columns (backup + atomic
# replace, run in a background thread).
#
# The front-end pre-selects "Unsure" for criteria whose keywords do not occur
# in the title/abstract, then drives inclusion with the Tab key: Tab accepts
# the focused criterion without overwriting an existing verdict and advances;
# once the last criterion is done, the next Tab saves the record and loads the
# next abstract in the same DOM position.

SCREENING_CRITERIA = [
    {"label": "C1", "kind": "verdict", "title": "Misinformation intervention",
     "instruction": "Does the study feature an intervention aimed at reducing susceptibility to "
                    "misinformation (inoculation/prebunking, debunking, media literacy, "
                    "source-credibility or lateral-reading training, accuracy nudges, social norms, "
                    "platform exposure reduction)?",
     "options": ["Yes", "No", "Unsure"]},
    {"label": "C2", "kind": "verdict", "title": "Control group",
     "instruction": "Does the study include a control, placebo, or comparison group that does NOT "
                    "receive the misinformation intervention?",
     "options": ["Yes", "No", "Unsure"]},
    {"label": "C3", "kind": "verdict", "title": "Language (English)",
     "instruction": "Is the paper in English?",
     "options": ["Yes", "No", "Unsure"]},
    {"label": "C4", "kind": "verdict", "title": "Experimental (primary quantitative study)",
     "instruction": "Is this a primary empirical study with a quantitative experimental or "
                    "quasi-experimental design?",
     "options": ["Yes", "No", "Unsure"]},
]

_SCREEN_LABELS = [c["label"] for c in SCREENING_CRITERIA]

_SCREEN_INDEX_LOCK = threading.Lock()

_SCREEN_INDEX = {"stamp": None, "rows": None, "by_id": None, "info": None}

_SCREEN_MERGE_LOCK = threading.Lock()

# Held by EVERY writer of master_dataset.xlsx (this merge and the full-text
# "mark" worker in fulltext_lib): each reads the workbook, rewrites it in full
# and replaces the file, so two at once would drop one writer's changes.
MASTER_WRITE_LOCK = threading.Lock()

_SCREEN_MERGE = {"running": False, "coder": None, "ok": None, "error": "", "log": "",
                 "started": None, "finished": None, "matched": 0, "changed": 0, "backup": None}

# Bumped whenever the shape of an index row changes, so a stale on-disk cache
# (temp/screening_index.json) written by an older build is never served.
_INDEX_VERSION = 4

# Decisions are re-read only when the coder's file changed on disk (one coder
# file can hold tens of thousands of records, 30 MB of JSON): the file stays
# authoritative, the cache only saves the re-parse.  One lock serialises every
# read-modify-write of a decision file, so two quick saves cannot lose one.
_DECISIONS_LOCK = threading.RLock()
_DECISIONS_CACHE = {}


def screening_master_path():
    return os.path.join(S.ROOT, "master_dataset.xlsx")


def screening_decision_path(coder):
    return os.path.join(S.SCREENING, f"{safe_token(coder)}.json")


def _file_stamp(path):
    """(size, mtime_ns): changes on every rewrite, even within the same second."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_size, st.st_mtime_ns)


class ScreeningFileError(RuntimeError):
    """A coder's decision file exists but cannot be read; never overwrite it."""


def _read_decisions(path):
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError("the file does not hold a JSON object")
    return data


def load_screening_decisions(coder, strict=False):
    """The coder's decisions ``{uid: record}``.

    The returned dict is shared with the cache: treat it as read-only (use
    ``record_screening_decision`` / ``update_screening_decisions`` to write).
    A missing file is ``{}``; an unreadable one is ``{}`` too unless *strict*,
    which raises ``ScreeningFileError`` so a writer never replaces a file it
    could not read (that would silently drop every earlier decision).
    """
    if not coder or not str(coder).strip():
        return {}
    path = screening_decision_path(coder)
    stamp = _file_stamp(path)
    if stamp is None:
        return {}
    with _DECISIONS_LOCK:
        hit = _DECISIONS_CACHE.get(path)
        if hit and hit[0] == stamp:
            return hit[1]
        try:
            data = _read_decisions(path)
        except Exception as exc:  # noqa: BLE001
            if strict:
                raise ScreeningFileError(
                    f"{os.path.basename(path)} cannot be read ({exc}); fix or move it "
                    "before screening more abstracts") from exc
            log(f"screening: cannot read {path}: {exc}")
            return {}
        _DECISIONS_CACHE[path] = (stamp, data)
        return data


def save_screening_decisions(coder, decisions):
    path = screening_decision_path(coder)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    os.makedirs(S.TEMP, exist_ok=True)
    fd, tmp = tempfile.mkstemp(suffix=".json", dir=S.TEMP)
    with _DECISIONS_LOCK:
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(decisions, fh, ensure_ascii=False, indent=1, sort_keys=True)
                fh.write("\n")
            atomic_replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        stamp = _file_stamp(path)
        if stamp is not None:
            _DECISIONS_CACHE[path] = (stamp, decisions)
    return path


def update_screening_decisions(coder, mutate):
    """Read-modify-write one coder's file under the lock: ``mutate(dict)``.

    The dict handed to *mutate* is a fresh shallow copy, so the cached object
    other requests may be iterating is never changed under them.
    """
    with _DECISIONS_LOCK:
        current = dict(load_screening_decisions(coder, strict=True))
        result = mutate(current)
        save_screening_decisions(coder, current)
        return current, result


# ---------------------------------------------------------------- workbook columns
# Column detection is case-, space- and punctuation-insensitive and accepts the
# common export names (EBSCO, Scopus/WoS RIS-style tags, Zotero/EndNote CSV), so
# a re-exported workbook whose header reads "Title" or "Abstract Note" still
# loads.  The first alias that is present wins.
_COLUMN_ALIASES = {
    "id": ["an", "accession number", "accession no", "record id", "recordid", "uid", "id",
           "key", "record number"],
    "title": ["title", "article title", "primary title", "document title", "ti"],
    "abstract": ["abstract", "abstract note", "abstract text", "ab", "summary"],
    "authors": ["authors", "author", "author full names", "author names", "au",
                "contributors", "creators"],
    "year": ["year", "publication year", "pub year", "py", "publicationdate",
             "publication date", "date"],
    "doi": ["doi", "di", "doids"],
    "source": ["source", "source title", "journal", "journal title", "publication title",
               "secondary title", "so", "t2"],
    "fulltext": ["fulltext verdict", "full text verdict", "fulltext_verdict"],
}

# Without a title the tab has nothing to show; without an id the records fall
# back to row numbers (fragile when the workbook is re-sorted) - say so.
_REQUIRED = ["title"]
_RECOMMENDED = ["id", "abstract"]

_NOT_CRITERIA = {"fulltext", "full_text", "effective", "final", "ai", "human"}


def _norm(name):
    return re.sub(r"[^0-9a-z]+", "_", str(name or "").strip().lower()).strip("_")


def _cell(row, i):
    """A workbook cell as JSON-safe text/number ("" for empty or NaN)."""
    if i is None or i >= len(row):
        return ""
    v = row[i]
    if v is None:
        return ""
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            return ""
        return int(v) if v.is_integer() else v
    if isinstance(v, int):
        return v
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return str(v).strip()


def _id_text(value):
    """An id cell as text: 186260530.0 (a numeric cell) reads as "186260530"."""
    if value in (None, ""):
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip()


def _year_of(value):
    if isinstance(value, int):
        return value
    m = re.search(r"(1[89]\d\d|20\d\d)", str(value or ""))
    return int(m.group(1)) if m else (value or "")


def _locate_columns(head):
    """Map the header row to the fields the tab needs + the criterion columns.

    Returns ``(cols, criteria)``: ``cols[field] -> index`` for the fields that
    were found, ``criteria[key] -> {"verdict": i, "certainty": i, "rationale": i}``
    for every ``<key>_verdict`` column (``C1_verdict``, ``Intervention verdict`` ...).
    """
    normed = {}
    for i, h in enumerate(head):
        n = _norm(h)
        if n and n not in normed:
            normed[n] = i
    cols = {}
    for field, aliases in _COLUMN_ALIASES.items():
        for alias in aliases:
            i = normed.get(_norm(alias))
            if i is not None:
                cols[field] = i
                break
    criteria = {}
    for i, h in enumerate(head):
        raw = str(h or "").strip()
        m = re.match(r"^(.+?)[\s_\-]*verdict$", raw, flags=re.IGNORECASE)
        if not m:
            continue
        key = m.group(1).strip()
        if not key or _norm(key) in _NOT_CRITERIA:
            continue
        nk = _norm(key)
        criteria[key] = {"verdict": i,
                         "certainty": normed.get(f"{nk}_certainty"),
                         "rationale": normed.get(f"{nk}_rationale")}
    return cols, criteria


def _find_header(rows):
    """The header among the first rows: the first that names a title column."""
    for n, row in enumerate(rows):
        cols, _ = _locate_columns(row or ())
        if "title" in cols:
            return n
    return 0


def _unique_uid(an, rownum, seen):
    """The record id: the accession number, else ``row-<excel row>``.

    A repeated accession number gets ``#2``, ``#3`` ... in workbook order.  The
    index and the merge both use this helper, so a decision saved for
    ``X#2`` is merged back into the second ``X`` row and nowhere else.
    """
    uid = an or f"row-{rownum}"
    if uid in seen:
        seen[uid] += 1
        return f"{uid}#{seen[uid]}"
    seen[uid] = 1
    return uid


def _pick_sheet(wb):
    """The first sheet whose header names a title column (else the first)."""
    for ws in wb.worksheets:
        head = []
        for row in ws.iter_rows(max_row=10, values_only=True):
            head.append(row)
        n = _find_header(head)
        if head and "title" in _locate_columns(head[n] or ())[0]:
            return ws, n
    return wb.worksheets[0], 0


def _build_screening_index(path=None):
    """Extract the columns the screening tab needs from the master workbook."""
    import openpyxl

    path = path or screening_master_path()
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        ws, header_row = _pick_sheet(wb)
        it = ws.iter_rows(values_only=True)
        head = ()
        for _ in range(header_row + 1):
            head = next(it, None) or ()
        cols, crits = _locate_columns(head)
        info = {
            "sheet": ws.title,
            "header_row": header_row + 1,
            "columns": {f: str(head[i]).strip() for f, i in cols.items()},
            "criteria_columns": sorted(crits),
            "missing": [f for f in _REQUIRED if f not in cols],
            "missing_recommended": [f for f in _RECOMMENDED if f not in cols],
        }
        rows = []
        seen = {}
        for n, row in enumerate(it):
            rownum = header_row + 2 + n        # the Excel row number
            if not any(v not in (None, "") for v in row):
                continue                       # a blank spacer row is no record
            an = _id_text(_cell(row, cols.get("id")))
            uid = _unique_uid(an, rownum, seen)
            ai = {}
            for key, ix in crits.items():
                ai[key] = {
                    "verdict": _cell(row, ix["verdict"]),
                    "certainty": _cell(row, ix["certainty"]),
                    "rationale": _cell(row, ix["rationale"]),
                }
            extra = {}
            if "#" in uid:
                # Some decision files (e.g. a batch-coded Screening/OG.json) key a
                # repeated accession number by its Excel row instead of "X#2".
                # Both spellings must find the record; see decision_for().
                extra["alt"] = f"row-{rownum}"
            rows.append({
                **extra,
                "id": uid,
                "an": an,
                "row": rownum,
                "title": str(_cell(row, cols.get("title"))),
                "abstract": str(_cell(row, cols.get("abstract"))),
                "authors": str(_cell(row, cols.get("authors"))),
                "year": _year_of(_cell(row, cols.get("year"))),
                "doi": str(_cell(row, cols.get("doi"))),
                "source": str(_cell(row, cols.get("source"))),
                "ai": ai,
                "fulltext": str(_cell(row, cols.get("fulltext"))),
            })
        info["records"] = len(rows)
        info["duplicate_ids"] = sum(v - 1 for v in seen.values() if v > 1)
        info["row_ids"] = sum(1 for r in rows if not r["an"])
        return rows, info
    finally:
        wb.close()


def screening_index():
    """The workbook index, cached by file stamp (size + mtime_ns).

    The xlsx is parsed once per change (it takes tens of seconds for 30k
    records); the parsed rows are also kept in temp/screening_index.json so a
    restart does not re-parse an unchanged workbook.  The file on disk stays
    authoritative: any rewrite (a merge, an edit in Excel) changes the stamp.
    Returns ``{"available", "rows", "by_id", "info", "error"}``.
    """
    path = screening_master_path()
    stamp = _file_stamp(path)
    if stamp is None:
        return {"available": False, "rows": [], "by_id": {}, "info": {},
                "error": f"{os.path.basename(path)} was not found in the project folder "
                         f"({S.ROOT})."}
    key = [os.path.abspath(path), stamp[0], stamp[1], _INDEX_VERSION]
    with _SCREEN_INDEX_LOCK:
        if _SCREEN_INDEX["rows"] is not None and _SCREEN_INDEX["stamp"] == key:
            return {"available": True, "rows": _SCREEN_INDEX["rows"],
                    "by_id": _SCREEN_INDEX["by_id"], "info": _SCREEN_INDEX["info"],
                    "error": _SCREEN_INDEX["info"].get("error", "")}
        # Built under the lock: the first requests after a start arrive together
        # (meta + list + agreement) and must not parse a 30 MB workbook in parallel.
        cache = os.path.join(S.TEMP, "screening_index.json")
        rows = info = None
        try:
            if os.path.exists(cache):
                with open(cache, encoding="utf-8") as fh:
                    blob = json.load(fh)
                if blob.get("key") == key and isinstance(blob.get("rows"), list):
                    rows, info = blob["rows"], blob.get("info") or {}
        except Exception:  # noqa: BLE001
            rows = None
        if rows is None:
            try:
                rows, info = _build_screening_index(path)
            except Exception as exc:  # noqa: BLE001
                log(f"screening: cannot read {path}: {exc}")
                return {"available": False, "rows": [], "by_id": {}, "info": {},
                        "error": f"{os.path.basename(path)} could not be read: {exc}"}
            try:
                os.makedirs(S.TEMP, exist_ok=True)
                tmp = cache + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump({"key": key, "info": info, "rows": rows}, fh, ensure_ascii=False)
                atomic_replace(tmp, cache)
            except OSError:
                pass
        if info.get("missing"):
            looked = "; ".join(f"{f}: {', '.join(_COLUMN_ALIASES[f])}" for f in info["missing"])
            info["error"] = (f"{os.path.basename(path)} (sheet '{info.get('sheet')}') has no "
                             f"{', '.join(info['missing'])} column (looked for {looked}, "
                             "case-insensitive).")
        by_id = {r["id"]: r for r in rows}
        alias = {}
        for r in rows:
            alt = r.get("alt")
            if alt and alt not in by_id:
                by_id[alt] = r          # /abstract/row-30348 opens the "#2" record
                alias[alt] = r["id"]
        info["alias"] = alias
        _SCREEN_INDEX.update(stamp=key, rows=rows, by_id=by_id, info=info)
        return {"available": True, "rows": rows, "by_id": by_id, "info": info,
                "error": info.get("error", "")}


def decision_for(decisions, rec):
    """The coder's decision for an index record, under its id or its row alias."""
    if not decisions or not rec:
        return None
    dec = decisions.get(rec["id"])
    if dec is None and rec.get("alt"):
        dec = decisions.get(rec["alt"])
    return dec


def canonical_uid(uid, idx=None):
    """A decision-file key as the index id (``row-30348`` -> ``X#2``)."""
    idx = idx if idx is not None else screening_index()
    return ((idx.get("info") or {}).get("alias") or {}).get(uid, uid)


def ai_entry(ai, key):
    """The workbook's verdict entry for a criterion key (case-insensitive)."""
    ai = ai or {}
    if key in ai:
        return ai[key] or {}
    nk = _norm(key)
    for k, v in ai.items():
        if _norm(k) == nk:
            return v or {}
    return {}


def ai_for_criteria(ai, criteria):
    """The AI verdicts keyed exactly by the criteria keys the tab shows."""
    out = dict(ai or {})
    for c in criteria:
        key = c.get("key") or c.get("label") or ""
        if key and key not in out:
            entry = ai_entry(ai, key)
            if entry:
                out[key] = entry
    return out


def _screen_verdict_of(ai, label):
    return str(ai_entry(ai, label).get("verdict") or "").strip()


def _scheme_criteria():
    """The abstract criteria of the active project's scheme.

    The hardcoded ``SCREENING_CRITERIA`` below is the FALLBACK for a project
    that carries no criteria of its own; a scheme's criteria win, so a
    criterion added in the Scheme editor really appears in the screening tab.

    This normalises the scheme's criteria on every call: call it ONCE per
    request and pass the result to the decision helpers below, never per row
    (30k calls made the list take over a minute).
    """
    try:
        import project_context
        criteria = (project_context.criteria() or {}).get("abstract") or []
    except Exception:  # noqa: BLE001
        criteria = []
    if criteria:
        return criteria
    return [
        {"key": c["label"], "label": c["title"], "kind": c.get("kind", "verdict"),
         "prompt_text": c.get("instruction", ""), "options": c.get("options") or [],
         "pass_values": ["Yes"], "gating": True}
        for c in SCREENING_CRITERIA
    ]


def screening_criteria():
    """The abstract criteria, in the shape the screening tab expects.

    The scheme's criteria win: a criterion added in the Scheme editor really
    appears in the screening tab, with its own options, pass values, gating
    flag and auto-approval rules.  The hardcoded ``SCREENING_CRITERIA`` is the
    fallback for a project that carries no criteria of its own.
    """
    out = []
    for criterion in _scheme_criteria():
        key = str(criterion.get("key") or criterion.get("label") or "").strip()
        if not key:
            continue
        out.append({
            "key": key,
            "label": key,
            "kind": criterion.get("kind") or "verdict",
            "title": criterion.get("label") or criterion.get("title") or key,
            "instruction": criterion.get("prompt_text")
                          or criterion.get("instruction") or "",
            "options": criterion.get("options") or ["Yes", "No", "Unsure"],
            "pass_values": criterion.get("pass_values") or ["Yes"],
            "gating": criterion.get("gating", True),
            "auto_approve": criterion.get("auto_approve") or {},
        })
    return out


def _criterion_passes(criterion, verdict):
    """Whether a criterion's verdict passes it (its own pass values decide)."""
    verdict = str(verdict or "").strip().lower()
    if not verdict:
        return False
    wanted = criterion.get("pass_values") or ["yes"]
    return verdict in {str(v).strip().lower() for v in wanted}


def _decide(criteria, verdict_of):
    """Include iff every decided GATING criterion passes; Exclude if one fails."""
    decided = []
    for criterion in criteria:
        key = criterion.get("key") or criterion.get("label") or ""
        if not key or not criterion.get("gating", True):
            continue        # informational: reported, never blocking
        verdict = verdict_of(key)
        if verdict:
            decided.append((criterion, verdict))
    if any(not _criterion_passes(c, v) for c, v in decided):
        return "Exclude"
    if decided:
        return "Include"
    return ""


def ai_screen_decision(rec, criteria=None):
    """The workbook (AI) decision of one record.

    The criteria and their pass values come from the project's scheme, so a
    criterion added in the Scheme editor takes part in the decision.  Pass
    *criteria* (``_scheme_criteria()``) when calling this in a loop.
    """
    criteria = _scheme_criteria() if criteria is None else criteria
    verdicts = rec.get("ai", {}) or {}
    return _decide(criteria, lambda key: _screen_verdict_of(verdicts, key))


def human_screen_decision(dec, criteria=None):
    if not dec:
        return ""
    decision = str(dec.get("decision") or "").strip()
    if decision:
        return decision
    criteria = _scheme_criteria() if criteria is None else criteria
    crits = dec.get("criteria") or {}
    return _decide(criteria,
                   lambda key: str((crits.get(key) or {}).get("verdict") or "").strip())


def record_screening_decision(coder, uid, record, alt=None):
    """Store one record in the coder's file (locked read-modify-write).

    *alt* is the record's row alias: an older decision stored under it is
    replaced by this one, so a record is never counted (or merged) twice.
    """
    def mutate(current):
        existed = uid in current or bool(alt and alt in current)
        if alt:
            current.pop(alt, None)
        current[uid] = record
        return existed
    decisions, existed = update_screening_decisions(coder, mutate)
    return len(decisions), existed


def _screen_merge_worker(coder):
    state = _SCREEN_MERGE
    matched = 0
    wb_in = None
    try:
        import openpyxl
        from openpyxl import Workbook
    except Exception as exc:  # noqa: BLE001
        state.update(running=False, ok=False, error=f"openpyxl unavailable: {exc}",
                     finished=datetime.now().isoformat(timespec="seconds"))
        return
    try:
        decisions = load_screening_decisions(coder, strict=True)
    except ScreeningFileError as exc:
        state.update(running=False, ok=False, error=str(exc),
                     finished=datetime.now().isoformat(timespec="seconds"))
        return
    if not decisions:
        state.update(running=False, ok=False, error="no screened abstracts for this coder",
                     finished=datetime.now().isoformat(timespec="seconds"))
        return
    master = screening_master_path()
    if not os.path.exists(master):
        state.update(running=False, ok=False, error="master_dataset.xlsx not found",
                     finished=datetime.now().isoformat(timespec="seconds"))
        return
    MASTER_WRITE_LOCK.acquire()
    try:
        backup_dir = os.path.join(S.ROOT, "archive", "coding_app_ui_backups",
                                  "screening_merge_" + datetime.now().strftime("%Y%m%d_%H%M%S"))
        os.makedirs(backup_dir, exist_ok=True)
        backup = os.path.join(backup_dir, os.path.basename(master))
        shutil.copy2(master, backup)
        state["backup"] = os.path.relpath(backup, S.ROOT).replace("\\", "/")
        state["log"] = f"backup -> {state['backup']}\n"

        # The criteria written back: the scheme's, plus any other criterion the
        # coder's decisions carry that already has a column (e.g. the legacy
        # "Intervention" classification).
        scheme_keys = [c["key"] for c in screening_criteria()]

        wb_in = openpyxl.load_workbook(master, read_only=True, data_only=False)
        ws_in, header_row = _pick_sheet(wb_in)
        wb_out = Workbook(write_only=True)
        ws_out = wb_out.create_sheet(title=ws_in.title or "Sheet1")
        it = ws_in.iter_rows(values_only=True)
        for _ in range(header_row):
            ws_out.append(list(next(it, None) or ()))       # rows above the header, verbatim
        header = [str(h).strip() if h is not None else "" for h in (next(it, None) or ())]
        cols, crit_cols = _locate_columns(header)
        normed = {_norm(h): i for i, h in reversed(list(enumerate(header))) if h}

        def column(name):
            i = normed.get(_norm(name))
            if i is None:
                i = len(header)
                header.append(name)
                normed[_norm(name)] = i
            return i

        existing = {_norm(k): k for k in crit_cols}
        dec_keys = {k for d in decisions.values() for k in ((d or {}).get("criteria") or {})}
        keys = list(scheme_keys) + sorted(k for k in dec_keys
                                          if k not in scheme_keys and _norm(k) in existing)
        target = {}
        for key in keys:
            base = existing.get(_norm(key), key)
            target[key] = {part: column(f"{base}_{part}")
                           for part in ("verdict", "certainty", "rationale")}
        by_col = column("Screened_By")
        at_col = column("Screened_At")
        ws_out.append(header)
        seen = {}
        for r, row in enumerate(it, start=header_row + 2):
            vals = list(row) + [None] * (len(header) - len(row))
            if not any(v not in (None, "") for v in row):
                ws_out.append(vals)
                continue
            an = _id_text(_cell(row, cols.get("id")))
            uid = _unique_uid(an, r, seen)
            dec = decisions.get(uid)
            if dec is None and "#" in uid:
                dec = decisions.get(f"row-{r}")      # the row alias, see decision_for()
            if dec:
                matched += 1
                crits = dec.get("criteria") or {}
                for key, where in target.items():
                    entry = crits.get(key) or {}
                    for part, i in where.items():
                        value = str(entry.get(part) or "").strip()
                        if value:
                            vals[i] = value
                vals[by_col] = str(coder).strip()
                vals[at_col] = dec.get("saved_at") or datetime.now().isoformat(timespec="seconds")
            ws_out.append(vals)
        os.makedirs(S.TEMP, exist_ok=True)
        tmp = os.path.join(S.TEMP, "master_dataset.merge.xlsx")
        wb_out.save(tmp)
        wb_in.close()
        atomic_replace(tmp, master)
        state.update(matched=matched, changed=matched)
        state["log"] += f"merged {matched} screened record(s) into {os.path.basename(master)}\n"
        missing = len(decisions) - matched
        if missing > 0:
            state["log"] += (f"! {missing} decision(s) match no row of the workbook "
                             "(their id is not in the current master)\n")
    except Exception as exc:  # noqa: BLE001
        state.update(ok=False, error=str(exc))
        state["log"] += f"! merge failed: {exc}\n"
    finally:
        try:
            if wb_in is not None:
                wb_in.close()
        except Exception:  # noqa: BLE001
            pass
        MASTER_WRITE_LOCK.release()
        state["running"] = False
        if state.get("ok") is None:
            state["ok"] = True
        state["finished"] = datetime.now().isoformat(timespec="seconds")
        log(f"screening merge coder={coder} matched={matched} ok={state['ok']}")
