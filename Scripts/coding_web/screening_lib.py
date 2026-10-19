"""Screening logic: the master workbook index, per-coder decisions and verdict helpers."""

from datetime import datetime
import json
import os
import shutil
import tempfile
import threading
from studio_lib import atomic_replace, safe_token
from studio_state import S, log


# ---------------------------------------------------------------- abstract screening tab
# Human abstract screening against the four inclusion criteria (C1-C4) plus the
# intervention-type classification.  Abstracts are read from the master
# screening workbook (master_dataset.xlsx, first sheet).  Each completed
# abstract is written to Screening/<coder>.json immediately (one file per
# coder); a manual "Merge to master" action writes the coder's decisions back
# into the workbook's canonical C*_verdict / Intervention_verdict columns
# (backup + atomic replace, run in a background thread).
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

_SCREEN_INDEX = {"mtime": None, "rows": None, "by_id": None}

_SCREEN_MERGE_LOCK = threading.Lock()

_SCREEN_MERGE = {"running": False, "coder": None, "ok": None, "error": "", "log": "",
                 "started": None, "finished": None, "matched": 0, "changed": 0, "backup": None}

def screening_master_path():
    return os.path.join(S.ROOT, "master_dataset.xlsx")

def screening_decision_path(coder):
    return os.path.join(S.SCREENING, f"{safe_token(coder)}.json")

def load_screening_decisions(coder):
    if not coder or not str(coder).strip():
        return {}
    path = screening_decision_path(coder)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}

def save_screening_decisions(coder, decisions):
    path = screening_decision_path(coder)
    fd, tmp = tempfile.mkstemp(suffix=".json", dir=S.TEMP)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(decisions, fh, ensure_ascii=False, indent=1, sort_keys=True)
            fh.write("\n")
        atomic_replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return path

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

def _build_screening_index():
    """Extract the columns the screening tab needs from the master workbook."""
    import openpyxl

    path = screening_master_path()
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb.worksheets[0]
        it = ws.iter_rows(values_only=True)
        head = next(it, None) or ()
        header = {str(h).strip(): i for i, h in enumerate(head) if h is not None}
        rows = []
        seen = {}
        for n, row in enumerate(it):
            an = str(_screen_cell(row, header, "an") or "").strip()
            uid = an or f"row-{n + 2}"
            if uid in seen:
                seen[uid] += 1
                uid = f"{uid}#{seen[uid]}"
            else:
                seen[uid] = 1
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

def screening_index():
    path = screening_master_path()
    if not os.path.exists(path):
        return {"available": False, "rows": [], "by_id": {}}
    mtime = int(os.path.getmtime(path))
    with _SCREEN_INDEX_LOCK:
        if _SCREEN_INDEX["rows"] is not None and _SCREEN_INDEX["mtime"] == mtime:
            return {"available": True, "rows": _SCREEN_INDEX["rows"],
                    "by_id": _SCREEN_INDEX["by_id"]}
    cache = os.path.join(S.TEMP, "screening_index.json")
    rows = None
    try:
        if os.path.exists(cache):
            with open(cache, encoding="utf-8") as fh:
                blob = json.load(fh)
            if blob.get("mtime") == mtime and isinstance(blob.get("rows"), list):
                rows = blob["rows"]
    except Exception:
        rows = None
    if rows is None:
        rows = _build_screening_index()
        try:
            tmp = cache + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"mtime": mtime, "rows": rows}, fh, ensure_ascii=False)
            atomic_replace(tmp, cache)
        except OSError:
            pass
    by_id = {r["id"]: r for r in rows}
    with _SCREEN_INDEX_LOCK:
        _SCREEN_INDEX.update(mtime=mtime, rows=rows, by_id=by_id)
    return {"available": True, "rows": rows, "by_id": by_id}

def _screen_verdict_of(ai, label):
    return str((ai.get(label) or {}).get("verdict") or "").strip()

def ai_screen_decision(rec):
    """Include iff C1-C4 are all Yes; Exclude as soon as one is No; else blank."""
    verdicts = [_screen_verdict_of(rec.get("ai", {}), lab) for lab in ("C1", "C2", "C3", "C4")]
    if any(v == "No" for v in verdicts):
        return "Exclude"
    if all(v == "Yes" for v in verdicts):
        return "Include"
    return ""

def human_screen_decision(dec):
    if not dec:
        return ""
    decision = str(dec.get("decision") or "").strip()
    if decision:
        return decision
    crits = dec.get("criteria") or {}
    verdicts = [str((crits.get(lab) or {}).get("verdict") or "").strip()
                for lab in ("C1", "C2", "C3", "C4")]
    if any(v == "No" for v in verdicts):
        return "Exclude"
    if all(v == "Yes" for v in verdicts):
        return "Include"
    return ""

def _screen_merge_worker(coder):
    state = _SCREEN_MERGE
    try:
        import openpyxl
        from openpyxl import Workbook
    except Exception as exc:  # noqa: BLE001
        state.update(running=False, ok=False, error=f"openpyxl unavailable: {exc}",
                     finished=datetime.now().isoformat(timespec="seconds"))
        return
    decisions = load_screening_decisions(coder)
    if not decisions:
        state.update(running=False, ok=False, error="no screened abstracts for this coder",
                     finished=datetime.now().isoformat(timespec="seconds"))
        return
    master = screening_master_path()
    if not os.path.exists(master):
        state.update(running=False, ok=False, error="master_dataset.xlsx not found",
                     finished=datetime.now().isoformat(timespec="seconds"))
        return
    backup_dir = os.path.join(S.ROOT, "archive", "coding_app_ui_backups",
                              "screening_merge_" + datetime.now().strftime("%Y%m%d_%H%M%S"))
    os.makedirs(backup_dir, exist_ok=True)
    backup = os.path.join(backup_dir, os.path.basename(master))
    shutil.copy2(master, backup)
    state["backup"] = os.path.relpath(backup, S.ROOT).replace("\\", "/")
    state["log"] = f"backup -> {state['backup']}\n"

    verdict_cols = {}
    for lab in _SCREEN_LABELS:
        verdict_cols[lab] = (f"{lab}_verdict", f"{lab}_certainty", f"{lab}_rationale")
    extra = ["Screened_By", "Screened_At"]

    wb_in = openpyxl.load_workbook(master, read_only=True, data_only=False)
    wb_out = Workbook(write_only=True)
    matched = 0
    try:
        ws_in = wb_in.worksheets[0]
        it = ws_in.iter_rows(values_only=True)
        header = [str(h).strip() if h is not None else "" for h in (next(it, None) or ())]
        H = {h: i for i, h in enumerate(header)}
        for name in extra:
            if name not in H:
                H[name] = len(header)
                header.append(name)
        ws_out = wb_out.create_sheet(title=ws_in.title or "Sheet1")
        ws_out.append(header)
        for r, row in enumerate(it, start=2):
            an = str(_screen_cell(row, H, "an") or "").strip()
            uid = an or f"row-{r}"
            dec = decisions.get(uid)
            vals = list(row) + [None] * (len(header) - len(row))
            if dec:
                matched += 1
                crits = dec.get("criteria") or {}
                for lab in _SCREEN_LABELS:
                    entry = crits.get(lab) or {}
                    for suffix, key in zip(verdict_cols[lab], ("verdict", "certainty", "rationale")):
                        value = str(entry.get(key) or "").strip()
                        if value:
                            vals[H[suffix]] = value
                vals[H["Screened_By"]] = str(coder).strip()
                vals[H["Screened_At"]] = dec.get("saved_at") or datetime.now().isoformat(timespec="seconds")
            ws_out.append(vals)
        tmp = os.path.join(S.TEMP, "master_dataset.merge.xlsx")
        wb_out.save(tmp)
        wb_in.close()
        atomic_replace(tmp, master)
        state.update(matched=matched, changed=matched)
        state["log"] += f"merged {matched} screened record(s) into {os.path.basename(master)}\n"
    except Exception as exc:  # noqa: BLE001
        state.update(ok=False, error=str(exc))
        state["log"] += f"! merge failed: {exc}\n"
    finally:
        try:
            wb_in.close()
        except Exception:
            pass
        state["running"] = False
        if state.get("ok") is None:
            state["ok"] = True
        state["finished"] = datetime.now().isoformat(timespec="seconds")
        log(f"screening merge coder={coder} matched={matched} ok={state['ok']}")
