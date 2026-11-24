"""Screening logic: the master workbook index, per-coder decisions and verdict helpers."""

from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
import os
import re
import tempfile
import threading
import time
from studio_lib import atomic_replace, safe_token
from studio_state import S, log


# ---------------------------------------------------------------- abstract screening tab
# Human abstract screening against the four inclusion criteria (C1-C4).
# Abstracts are read (never written) from the master screening workbook
# (master_dataset.xlsx; the "Search" sheet when present, else the first sheet,
# the same rule as Scripts/prisma_report.py).  Each completed abstract is
# written to Screening/<coder>.json immediately (one file per coder).  The
# "Merge" action writes a COPY of the workbook with the coder's verdicts in the
# C*_verdict columns to Results/screening_merge/; the master itself stays the
# AI screening reference the Agreement tab compares against.
#
# Decision rule (matches the PRISMA flow of master_dataset.xlsx, where every
# record with an Unsure and no No went on to full text):
#   any No                      -> Exclude
#   all Yes                     -> Include
#   no No, every criterion set  -> Unsure (advances to full text)
#   otherwise                   -> ""      (incomplete)
# human_screen_decision / ai_screen_decision keep their historical two-valued
# return ("Include" / "Exclude" / ""); screen_outcome() is the four-valued form.

SCREENING_CRITERIA = [
    {"label": "C1", "key": "C1", "kind": "verdict", "title": "Misinformation intervention",
     "instruction": "Does the study feature an intervention aimed at reducing susceptibility to "
                    "misinformation (inoculation/prebunking, debunking, media literacy, "
                    "source-credibility or lateral-reading training, accuracy nudges, social norms, "
                    "platform exposure reduction)?",
     "options": ["Yes", "No", "Unsure"]},
    {"label": "C2", "key": "C2", "kind": "verdict", "title": "Control group",
     "instruction": "Does the study include a control, placebo, or comparison group that does NOT "
                    "receive the misinformation intervention?",
     "options": ["Yes", "No", "Unsure"]},
    {"label": "C3", "key": "C3", "kind": "verdict", "title": "Language (English)",
     "instruction": "Is the paper in English?",
     "options": ["Yes", "No", "Unsure"]},
    {"label": "C4", "key": "C4", "kind": "verdict", "title": "Experimental (primary quantitative study)",
     "instruction": "Is this a primary empirical study with a quantitative experimental or "
                    "quasi-experimental design?",
     "options": ["Yes", "No", "Unsure"]},
]

_SCREEN_LABELS = [c["label"] for c in SCREENING_CRITERIA]

_VERDICTS = {"yes": "Yes", "no": "No", "unsure": "Unsure"}

_CERTAINTIES = {"high": "High", "medium": "Medium", "low": "Low"}

_RATIONALE_MAX = 2000

_MASTER_SHEET = "Search"

_SCREEN_INDEX_LOCK = threading.Lock()

_SCREEN_BUILD_LOCK = threading.Lock()

_SCREEN_INDEX = {"sig": None, "path": None, "rows": None, "by_id": None, "aux": None,
                 "error": ""}

_SCREEN_MERGE_LOCK = threading.Lock()

_SCREEN_MERGE = {"running": False, "coder": None, "ok": None, "error": "", "log": "",
                 "started": None, "finished": None, "matched": 0, "changed": 0,
                 "unmatched": 0, "output": None, "backup": None}

_DEC_CACHE = {}           # path -> {"sig": (mtime_ns, size), "data": dict}
_DEC_CACHE_LOCK = threading.Lock()
_DEC_LOCKS = {}           # path -> threading.Lock (serialises read-modify-write)
_DEC_LOCKS_GUARD = threading.Lock()

_LOCK_TIMEOUT = 15.0
_LOCK_STALE = 60.0

_WIN_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
                 *(f"lpt{i}" for i in range(1, 10))}


class ScreeningStoreError(Exception):
    """A coder's decision file cannot be read or written safely."""


def screening_criteria():
    """The abstract criteria (C1-C4): the one list the screening and agreement tabs share.

    There is no per-project screening scheme on disk, so this is the built-in
    list.  It is returned as-is (no copy) because the agreement tab calls it once
    per record; callers must treat it as read-only.
    """
    return SCREENING_CRITERIA


def screening_master_path():
    return os.path.join(S.ROOT, "master_dataset.xlsx")


def coder_token(coder):
    """The file-name form of a coder name, or "" when the name is unusable."""
    name = str(coder or "").strip()
    if not name:
        return ""
    if not re.search(r"[0-9A-Za-z\u00c0-\uffff]", name):
        return ""
    tok = safe_token(name)
    if not re.search(r"[0-9A-Za-z\u00c0-\uffff]", tok):
        return ""
    if tok.split(".")[0].lower() in _WIN_RESERVED:
        tok = "_" + tok
    return tok


def screening_decision_path(coder):
    tok = coder_token(coder)
    return os.path.join(S.SCREENING, f"{tok}.json") if tok else ""


# ---------------------------------------------------------------- decision files
def _sig(path):
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _parse_decisions(path):
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    if not text.strip():
        return {}
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("top-level value is not an object")
    return data


def _read_cached(path):
    """The parsed decision file (shared, read-only); raises ScreeningStoreError."""
    sig = _sig(path)
    if sig is None:
        return {}
    with _DEC_CACHE_LOCK:
        hit = _DEC_CACHE.get(path)
        if hit and hit["sig"] == sig:
            return hit["data"]
    try:
        data = _parse_decisions(path)
    except Exception as exc:  # noqa: BLE001
        raise ScreeningStoreError(
            f"{os.path.basename(path)} cannot be read ({exc}); it is left untouched") from exc
    with _DEC_CACHE_LOCK:
        _DEC_CACHE[path] = {"sig": sig, "data": data}
    return data


def _copy_decisions(data):
    out = {}
    for uid, rec in data.items():
        if isinstance(rec, dict):
            rec = dict(rec)
            crits = rec.get("criteria")
            if isinstance(crits, dict):
                rec["criteria"] = {k: (dict(v) if isinstance(v, dict) else v)
                                   for k, v in crits.items()}
        out[uid] = rec
    return out


def decisions_view(coder):
    """Read-only decisions of a coder (shared cache, do NOT mutate) and an error string."""
    path = screening_decision_path(coder)
    if not path:
        return {}, ""
    try:
        return _read_cached(path), ""
    except ScreeningStoreError as exc:
        log(f"screening: {exc}")
        return {}, str(exc)


def load_screening_decisions(coder):
    """A private (mutable) copy of a coder's decisions; {} when none or unreadable."""
    data, _err = decisions_view(coder)
    return _copy_decisions(data)


def _thread_lock(path):
    with _DEC_LOCKS_GUARD:
        lock = _DEC_LOCKS.get(path)
        if lock is None:
            lock = _DEC_LOCKS[path] = threading.Lock()
        return lock


@contextmanager
def _coder_lock(path):
    """Serialise writers of one decision file, across threads and app instances."""
    with _thread_lock(path):
        lock_dir = os.path.join(S.TEMP, "screening_locks")
        os.makedirs(lock_dir, exist_ok=True)
        lock = os.path.join(lock_dir, os.path.basename(path) + ".lock")
        deadline = time.monotonic() + _LOCK_TIMEOUT
        while True:
            try:
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError:
                try:
                    if time.time() - os.path.getmtime(lock) > _LOCK_STALE:
                        os.remove(lock)
                        continue
                except OSError:
                    continue
                if time.monotonic() > deadline:
                    raise ScreeningStoreError(
                        f"{os.path.basename(path)} is being saved by another window; try again")
                time.sleep(0.05)
        try:
            os.write(fd, str(os.getpid()).encode())
            yield
        finally:
            os.close(fd)
            try:
                os.remove(lock)
            except OSError:
                pass


def _dump_decisions(decisions):
    """One record per line: valid JSON, diff-friendly and ~5x faster than indent=1."""
    if not decisions:
        return "{}\n"
    lines = [json.dumps(str(uid), ensure_ascii=False) + ": "
             + json.dumps(decisions[uid], ensure_ascii=False, sort_keys=True)
             for uid in sorted(decisions, key=str)]
    return "{\n" + ",\n".join(lines) + "\n}\n"


def _write_decisions(path, decisions):
    """Atomic write (caller holds the coder lock); refuses to clobber an unreadable file."""
    if os.path.exists(path):
        _read_cached(path)          # raises if the existing file is corrupt / mid-sync
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(suffix=".json", dir=S.TEMP)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(_dump_decisions(decisions))
        atomic_replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    sig = _sig(path)
    with _DEC_CACHE_LOCK:
        if sig is not None:
            _DEC_CACHE[path] = {"sig": sig, "data": decisions}
        else:
            _DEC_CACHE.pop(path, None)


def save_screening_decisions(coder, decisions):
    """Replace a coder's whole decision file (atomic, locked).

    Prefer :func:`update_screening_decisions` for a read-modify-write: it reads
    the file under the lock, so a save from another window is never lost.
    """
    path = screening_decision_path(coder)
    if not path:
        raise ScreeningStoreError("a valid coder name is required")
    with _coder_lock(path):
        _write_decisions(path, _copy_decisions(decisions))
    return path


def update_screening_decisions(coder, mutate):
    """Locked read-modify-write: ``mutate(decisions)`` edits a private copy in place.

    Returns ``(path, decisions_after)``.  Raises ScreeningStoreError when the
    coder name is unusable, the file is unreadable or another writer holds it.
    """
    path = screening_decision_path(coder)
    if not path:
        raise ScreeningStoreError("a valid coder name is required")
    with _coder_lock(path):
        current = _read_cached(path)
        # shallow top-level copy: records are replaced, never edited in place
        work = dict(current)
        mutate(work)
        _write_decisions(path, work)
    return path, work


# ---------------------------------------------------------------- verdict helpers
def norm_verdict(value):
    return _VERDICTS.get(str(value or "").strip().lower(), "")


def norm_certainty(value):
    return _CERTAINTIES.get(str(value or "").strip().lower(), "")


def screen_outcome(verdicts):
    """Four-valued decision from C1-C4 verdicts: Include / Exclude / Unsure / ""."""
    vs = [norm_verdict(v) for v in verdicts]
    if any(v == "No" for v in vs):
        return "Exclude"
    if vs and all(v == "Yes" for v in vs):
        return "Include"
    if vs and all(vs):
        return "Unsure"
    return ""


def _ai_verdicts(rec):
    ai = (rec or {}).get("ai") or {}
    return [(ai.get(lab) or {}).get("verdict") for lab in _SCREEN_LABELS]


def _human_verdicts(dec):
    crits = (dec or {}).get("criteria") or {}
    return [(crits.get(lab) or {}).get("verdict") if isinstance(crits.get(lab), dict) else ""
            for lab in _SCREEN_LABELS]


def ai_screen_outcome(rec):
    return screen_outcome(_ai_verdicts(rec))


def human_screen_outcome(dec):
    if not dec:
        return ""
    vs = _human_verdicts(dec)
    if any(norm_verdict(v) for v in vs):
        return screen_outcome(vs)
    stored = str(dec.get("decision") or "").strip()
    return stored if stored in ("Include", "Exclude", "Unsure") else ""


def ai_screen_decision(rec):
    """Include iff C1-C4 are all Yes; Exclude as soon as one is No; else blank."""
    out = ai_screen_outcome(rec)
    return out if out in ("Include", "Exclude") else ""


def human_screen_decision(dec):
    """Like ai_screen_decision, from the criteria (the stored ``decision`` is only a
    fallback for a record without verdicts, so a later edit of a criterion is never
    masked by a stale stored decision)."""
    out = human_screen_outcome(dec)
    return out if out in ("Include", "Exclude") else ""


# ---------------------------------------------------------------- master index
def title_fingerprint(title):
    norm = re.sub(r"[^0-9a-z]+", " ", str(title or "").lower()).strip()
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:12] if norm else ""


def decision_is_stale(rec, dec):
    """A positional ``row-N`` decision whose stored title fingerprint no longer
    matches the row: the workbook was re-ordered since it was saved."""
    if not rec or not dec or not str(rec.get("id", "")).startswith("row-"):
        return False
    fp = dec.get("fp")
    return bool(fp) and fp != title_fingerprint(rec.get("title"))


def _screen_cell(row, header, name):
    i = header.get(name)
    if i is None or i >= len(row):
        return ""
    v = row[i]
    if v is None:
        return ""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return v
    return str(v).strip()


def _master_sheet(wb):
    return wb[_MASTER_SHEET] if _MASTER_SHEET in wb.sheetnames else wb.worksheets[0]


class _RowIds:
    """Record ids: the ``an`` accession, else ``row-<excel row>``; repeats get ``#n``.

    The index and the merge both use this so they can never disagree.
    """

    def __init__(self):
        self.seen = {}

    def __call__(self, row, header, excel_row):
        an = str(_screen_cell(row, header, "an") or "").strip()
        uid = an or f"row-{excel_row}"
        if uid in self.seen:
            self.seen[uid] += 1
            uid = f"{uid}#{self.seen[uid]}"
        else:
            self.seen[uid] = 1
        return an, uid


def _build_screening_index():
    """Extract the columns the screening tab needs from the master workbook."""
    import openpyxl

    path = screening_master_path()
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        ws = _master_sheet(wb)
        it = ws.iter_rows(values_only=True)
        head = next(it, None) or ()
        header = {str(h).strip(): i for i, h in enumerate(head) if h is not None}
        rows = []
        ids = _RowIds()
        for n, row in enumerate(it):
            an, uid = ids(row, header, n + 2)
            ai = {}
            for lab in _SCREEN_LABELS:
                ai[lab] = {
                    "verdict": _screen_cell(row, header, f"{lab}_verdict"),
                    "certainty": _screen_cell(row, header, f"{lab}_certainty"),
                    "rationale": _screen_cell(row, header, f"{lab}_rationale"),
                }
            rows.append({
                "id": uid,
                "an": an,
                "title": _screen_cell(row, header, "title"),
                "abstract": _screen_cell(row, header, "abstract"),
                "authors": _screen_cell(row, header, "authors"),
                "year": _screen_cell(row, header, "year"),
                "doi": _screen_cell(row, header, "doi"),
                "source": _screen_cell(row, header, "source"),
                "ai": ai,
                "fulltext": _screen_cell(row, header, "Fulltext_Verdict"),
            })
        return rows
    finally:
        wb.close()


def _aux_for(rows):
    """Per-row values the list route needs on every request, computed once per index."""
    aux = {}
    for r in rows:
        hay = " ".join(str(r.get(k) or "") for k in ("title", "authors", "doi", "an")).lower()
        aux[r["id"]] = {"hay": hay, "ai": ai_screen_outcome(r)}
    return aux


def _load_disk_cache(cache, path, sig):
    if not os.path.exists(cache):
        return None
    try:
        with open(cache, encoding="utf-8") as fh:
            blob = json.load(fh)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(blob.get("rows"), list):
        return None
    if blob.get("sig") == list(sig) and blob.get("path") == path:
        return blob["rows"]
    # cache written before the (mtime_ns, size) signature: whole-second mtime only
    if "sig" not in blob and blob.get("mtime") == int(sig[0] // 1_000_000_000):
        return blob["rows"]
    return None


def screening_index():
    """{"available", "rows", "by_id", "aux", "error"} for the master workbook.

    Cached in memory and in temp/screening_index.json, keyed by the workbook's
    (mtime_ns, size): a 30k-row workbook takes ~25 s to parse with openpyxl.
    """
    path = screening_master_path()
    sig = _sig(path)
    if sig is None:
        return {"available": False, "rows": [], "by_id": {}, "aux": {},
                "error": "master_dataset.xlsx not found"}
    with _SCREEN_INDEX_LOCK:
        if (_SCREEN_INDEX["rows"] is not None and _SCREEN_INDEX["sig"] == sig
                and _SCREEN_INDEX["path"] == path):
            return {"available": True, "rows": _SCREEN_INDEX["rows"],
                    "by_id": _SCREEN_INDEX["by_id"], "aux": _SCREEN_INDEX["aux"], "error": ""}
    with _SCREEN_BUILD_LOCK:          # one build at a time; the others wait and reuse it
        with _SCREEN_INDEX_LOCK:
            if (_SCREEN_INDEX["rows"] is not None and _SCREEN_INDEX["sig"] == sig
                    and _SCREEN_INDEX["path"] == path):
                return {"available": True, "rows": _SCREEN_INDEX["rows"],
                        "by_id": _SCREEN_INDEX["by_id"], "aux": _SCREEN_INDEX["aux"],
                        "error": ""}
        cache = os.path.join(S.TEMP, "screening_index.json")
        rows = _load_disk_cache(cache, path, sig)
        if rows is None:
            try:
                rows = _build_screening_index()
            except Exception as exc:  # noqa: BLE001
                log(f"screening: cannot read {path}: {exc}")
                return {"available": False, "rows": [], "by_id": {}, "aux": {},
                        "error": f"master_dataset.xlsx cannot be read: {exc}"}
            fd, tmp = tempfile.mkstemp(suffix=".json", dir=S.TEMP)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump({"sig": list(sig), "path": path, "rows": rows}, fh,
                              ensure_ascii=False)
                atomic_replace(tmp, cache)
            except OSError as exc:
                log(f"screening: index cache not written: {exc}")
            finally:
                if os.path.exists(tmp):
                    os.remove(tmp)
        by_id = {r["id"]: r for r in rows}
        aux = _aux_for(rows)
        with _SCREEN_INDEX_LOCK:
            _SCREEN_INDEX.update(sig=sig, path=path, rows=rows, by_id=by_id, aux=aux, error="")
    return {"available": True, "rows": rows, "by_id": by_id, "aux": aux, "error": ""}


# ---------------------------------------------------------------- merge (copy)
def screening_merge_output(coder):
    return os.path.join(S.ROOT, "Results", "screening_merge",
                        f"master_dataset - {coder_token(coder)}.xlsx")


def _screen_merge_worker(coder):
    """Write a copy of the master with the coder's verdicts; the master is never written."""
    state = _SCREEN_MERGE
    matched = 0
    wb_in = None
    try:
        import openpyxl
        from openpyxl import Workbook

        decisions, err = decisions_view(coder)
        if err:
            raise ScreeningStoreError(err)
        if not decisions:
            raise ScreeningStoreError("no screened abstracts for this coder")
        master = screening_master_path()
        if not os.path.exists(master):
            raise ScreeningStoreError("master_dataset.xlsx not found")
        out = screening_merge_output(coder)
        os.makedirs(os.path.dirname(out), exist_ok=True)

        verdict_cols = {lab: (f"{lab}_verdict", f"{lab}_certainty", f"{lab}_rationale")
                        for lab in _SCREEN_LABELS}
        extra = ["Screened_By", "Screened_At"]

        wb_in = openpyxl.load_workbook(master, read_only=True, data_only=False)
        wb_out = Workbook(write_only=True)
        ws_in = _master_sheet(wb_in)
        it = ws_in.iter_rows(values_only=True)
        header = [str(h).strip() if h is not None else "" for h in (next(it, None) or ())]
        H = {h: i for i, h in enumerate(header) if h}
        for name in extra + [c for cols in verdict_cols.values() for c in cols]:
            if name not in H:
                H[name] = len(header)
                header.append(name)
        ws_out = wb_out.create_sheet(title=ws_in.title or "Sheet1")
        ws_out.append(header)
        ids = _RowIds()
        seen = set()
        for r, row in enumerate(it, start=2):
            _an, uid = ids(row, H, r)
            dec = decisions.get(uid)
            vals = list(row) + [None] * (len(header) - len(row))
            if dec and not decision_is_stale({"id": uid, "title": _screen_cell(row, H, "title")}, dec):
                matched += 1
                seen.add(uid)
                crits = dec.get("criteria") or {}
                for lab in _SCREEN_LABELS:
                    entry = crits.get(lab) or {}
                    for col, key in zip(verdict_cols[lab], ("verdict", "certainty", "rationale")):
                        value = str(entry.get(key) or "").strip()
                        if value:
                            vals[H[col]] = value
                vals[H["Screened_By"]] = str(coder).strip()
                vals[H["Screened_At"]] = dec.get("saved_at") or datetime.now().isoformat(timespec="seconds")
            ws_out.append(vals)
        wb_in.close()
        wb_in = None
        fd, tmp = tempfile.mkstemp(suffix=".xlsx", dir=S.TEMP)
        os.close(fd)
        try:
            wb_out.save(tmp)
            atomic_replace(tmp, out)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        rel = os.path.relpath(out, S.ROOT).replace("\\", "/")
        state.update(matched=matched, changed=matched, unmatched=len(set(decisions) - seen),
                     output=rel, ok=True)
        state["log"] += (f"wrote {matched} screened record(s) to {rel}; "
                         f"{os.path.basename(master)} was not modified\n")
    except Exception as exc:  # noqa: BLE001
        state.update(ok=False, error=str(exc))
        state["log"] += f"! merge failed: {exc}\n"
    finally:
        if wb_in is not None:
            try:
                wb_in.close()
            except Exception:  # noqa: BLE001
                pass
        state["running"] = False
        if state.get("ok") is None:
            state["ok"] = False
        state["finished"] = datetime.now().isoformat(timespec="seconds")
        log(f"screening merge coder={coder} matched={matched} ok={state['ok']}")
