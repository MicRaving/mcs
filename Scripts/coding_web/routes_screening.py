"""Screening routes: the abstract master dataset, per-coder decisions and merge."""

from datetime import datetime
from flask import Blueprint, jsonify, request
import os
import threading
from screening_lib import (ScreeningFileError, _SCREEN_MERGE, _SCREEN_MERGE_LOCK,
                           _scheme_criteria, _screen_merge_worker, ai_for_criteria, decision_for,
                           ai_screen_decision, human_screen_decision, load_screening_decisions,
                           record_screening_decision, screening_criteria, screening_index,
                           screening_master_path)

bp = Blueprint("screening", __name__)


@bp.get("/api/screening/meta")
def api_screening_meta():
    idx = screening_index()
    coder = (request.args.get("coder") or "").strip()
    decisions = load_screening_decisions(coder)
    master = screening_master_path()
    info = idx.get("info") or {}
    return jsonify({
        "available": idx["available"],
        "error": idx.get("error") or "",
        "master": os.path.basename(master),
        "master_path": master,
        "master_mtime": (datetime.fromtimestamp(os.path.getmtime(master)).isoformat(timespec="seconds")
                         if os.path.exists(master) else None),
        "sheet": info.get("sheet"),
        "columns": info.get("columns") or {},
        "criteria_columns": info.get("criteria_columns") or [],
        "missing": info.get("missing") or [],
        "missing_recommended": info.get("missing_recommended") or [],
        "duplicate_ids": info.get("duplicate_ids") or 0,
        "row_ids": info.get("row_ids") or 0,
        "criteria": screening_criteria(),
        "total": len(idx["rows"]),
        "screened": len(decisions),
        "coder": coder,
    })


@bp.get("/api/screening/list")
def api_screening_list():
    idx = screening_index()
    coder = (request.args.get("coder") or "").strip()
    decisions = load_screening_decisions(coder)
    flt = (request.args.get("filter") or "all").strip().lower()
    search = (request.args.get("search") or "").strip().lower()
    crits = _scheme_criteria()          # once per request, never per row
    out = []
    for rec in idx["rows"]:
        dec = decision_for(decisions, rec)
        if flt == "toscreen" and dec:
            continue
        if flt == "screened" and not dec:
            continue
        human = human_screen_decision(dec, crits)
        ai = ai_screen_decision(rec, crits)
        eff = human or ai
        if flt == "included" and eff != "Include":
            continue
        if flt == "excluded" and eff != "Exclude":
            continue
        if search:
            hay = (f"{rec['title']} {rec['authors']} {rec['doi']} {rec['an']} "
                   f"{rec['year']}").lower()
            if search not in hay:
                continue
        out.append({"i": rec["id"], "t": rec["title"], "a": rec["authors"],
                    "y": rec["year"], "s": rec["source"], "ai": ai,
                    "h": human, "d": 1 if dec else 0})
    return jsonify({"total": len(out), "screened": len(decisions), "rows": out,
                    "available": idx["available"], "error": idx.get("error") or ""})


@bp.get("/api/screening/abstract/<path:uid>")
def api_screening_abstract(uid):
    idx = screening_index()
    rec = idx["by_id"].get(uid)
    if rec is None:
        return jsonify({"ok": False, "error": f"no abstract with id {uid!r} in the master "
                                              "workbook"}), 404
    coder = (request.args.get("coder") or "").strip()
    decisions = load_screening_decisions(coder)
    crits = _scheme_criteria()
    # the AI verdicts keyed exactly like the criteria (the workbook's column
    # prefix may differ in case: "c1_verdict" still feeds criterion "C1")
    record = dict(rec)
    record["ai"] = ai_for_criteria(rec.get("ai"), crits)
    return jsonify({"record": record, "human": decision_for(decisions, rec),
                    "ai_decision": ai_screen_decision(rec, crits)})


@bp.post("/api/screening/decision")
def api_screening_decision():
    payload = request.get_json(force=True, silent=True) or {}
    coder = payload.get("coder")
    uid = payload.get("id")
    criteria = payload.get("criteria")
    if not coder or not str(coder).strip():
        return jsonify({"ok": False, "error": "coder name is required"}), 400
    if not uid or not isinstance(criteria, dict):
        return jsonify({"ok": False, "error": "id and criteria are required"}), 400
    idx = screening_index()
    rec = idx["by_id"].get(str(uid))
    if rec is None:
        return jsonify({"ok": False, "error": "unknown abstract id"}), 400
    uid = rec["id"]              # a row alias ("row-30348") saves under the record id
    clean = {}
    for criterion in screening_criteria():
        lab = criterion.get("label") or ""
        if not lab:
            continue
        entry = criteria.get(lab) or criteria.get(criterion.get("key") or "") or {}
        if not isinstance(entry, dict):
            entry = {}
        clean[lab] = {
            "verdict": str(entry.get("verdict") or "").strip(),
            "certainty": str(entry.get("certainty") or "").strip(),
            "rationale": str(entry.get("rationale") or "").strip(),
        }
    record = {
        "id": str(uid), "an": rec.get("an"), "criteria": clean,
        "decision": "", "coder": str(coder).strip(),
        "saved_at": datetime.now().isoformat(timespec="seconds"),
    }
    record["decision"] = human_screen_decision(record)
    # Keep the coder's verdicts for criteria the scheme no longer shows (e.g. a
    # legacy "Intervention" classification) instead of silently dropping them.
    previous = decision_for(load_screening_decisions(coder), rec) or {}
    for key, entry in ((previous.get("criteria") or {}).items()):
        record["criteria"].setdefault(key, entry)
    try:
        screened, existed = record_screening_decision(str(coder).strip(), str(uid), record,
                                                      alt=rec.get("alt"))
    except ScreeningFileError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 409
    except OSError as exc:
        return jsonify({"ok": False, "error": f"could not write the decision file: {exc}"}), 500
    return jsonify({"ok": True, "id": str(uid), "decision": record["decision"],
                    "screened": screened, "overwrote": existed})


@bp.post("/api/screening/merge")
def api_screening_merge():
    payload = request.get_json(force=True, silent=True) or {}
    coder = (payload.get("coder") or "").strip()
    if not coder:
        return jsonify({"ok": False, "error": "coder name is required"}), 400
    with _SCREEN_MERGE_LOCK:
        if _SCREEN_MERGE["running"]:
            return jsonify({"ok": False, "error": "a merge is already in progress"}), 409
        decisions = load_screening_decisions(coder)
        if not decisions:
            return jsonify({"ok": False, "error": "no screened abstracts for this coder"}), 400
        now = datetime.now().isoformat(timespec="seconds")
        _SCREEN_MERGE.update(running=True, coder=coder, ok=None, error="", log="",
                             started=now, finished=None, matched=0, changed=0, backup=None)
    threading.Thread(target=_screen_merge_worker, args=(coder,), daemon=True).start()
    return jsonify({"ok": True, "running": True, "coder": coder, "screened": len(decisions)})


@bp.get("/api/screening/merge/status")
def api_screening_merge_status():
    return jsonify(_SCREEN_MERGE)
