"""Screening routes: the abstract master dataset, per-coder decisions and merge."""

from datetime import datetime
from flask import Blueprint, abort, jsonify, request
import os
import threading
from screening_lib import SCREENING_CRITERIA, _SCREEN_LABELS, _SCREEN_MERGE, _SCREEN_MERGE_LOCK, _screen_merge_worker, ai_screen_decision, human_screen_decision, load_screening_decisions, save_screening_decisions, screening_index, screening_master_path

bp = Blueprint("screening", __name__)


@bp.get("/api/screening/meta")
def api_screening_meta():
    idx = screening_index()
    coder = (request.args.get("coder") or "").strip()
    decisions = load_screening_decisions(coder)
    master = screening_master_path()
    return jsonify({
        "available": idx["available"],
        "master": os.path.basename(master),
        "master_mtime": (datetime.fromtimestamp(os.path.getmtime(master)).isoformat(timespec="seconds")
                         if os.path.exists(master) else None),
        "criteria": SCREENING_CRITERIA,
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
    out = []
    for rec in idx["rows"]:
        dec = decisions.get(rec["id"])
        human = human_screen_decision(dec)
        eff = human or ai_screen_decision(rec)
        if flt == "toscreen" and dec:
            continue
        if flt == "screened" and not dec:
            continue
        if flt == "included" and eff != "Include":
            continue
        if flt == "excluded" and eff != "Exclude":
            continue
        if search:
            hay = (f"{rec['title']} {rec['authors']} {rec['doi']} {rec['an']}").lower()
            if search not in hay:
                continue
        out.append({"i": rec["id"], "t": rec["title"], "a": rec["authors"],
                    "y": rec["year"], "s": rec["source"], "ai": ai_screen_decision(rec),
                    "h": human})
    return jsonify({"total": len(out), "screened": len(decisions), "rows": out})

@bp.get("/api/screening/abstract/<path:uid>")
def api_screening_abstract(uid):
    idx = screening_index()
    rec = idx["by_id"].get(uid)
    if rec is None:
        abort(404)
    coder = (request.args.get("coder") or "").strip()
    decisions = load_screening_decisions(coder)
    return jsonify({"record": rec, "human": decisions.get(uid),
                    "ai_decision": ai_screen_decision(rec)})

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
    clean = {}
    for lab in _SCREEN_LABELS:
        entry = criteria.get(lab) or {}
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
    decisions = load_screening_decisions(coder)
    existed = str(uid) in decisions
    decisions[str(uid)] = record
    save_screening_decisions(coder, decisions)
    return jsonify({"ok": True, "id": str(uid), "decision": record["decision"],
                    "screened": len(decisions), "overwrote": existed})

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
