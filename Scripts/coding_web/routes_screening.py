"""Screening routes: the abstract master dataset, per-coder decisions and merge."""

from datetime import datetime
from flask import Blueprint, abort, jsonify, request
import os
import threading
from screening_lib import (SCREENING_CRITERIA, _RATIONALE_MAX, _SCREEN_LABELS, _SCREEN_MERGE,
                           _SCREEN_MERGE_LOCK, ScreeningStoreError, _screen_merge_worker,
                           ai_screen_decision, coder_token, decision_is_stale, decisions_view,
                           human_screen_decision, human_screen_outcome, norm_certainty,
                           norm_verdict, screening_index, screening_master_path,
                           title_fingerprint, update_screening_decisions)

bp = Blueprint("screening", __name__)

_PREFILL_SOURCES = {"ai", "keyword", "language"}


def _coder_arg():
    return (request.args.get("coder") or "").strip()


def _counts(idx, decisions):
    by = idx["by_id"]
    out = {"Include": 0, "Exclude": 0, "Unsure": 0, "": 0}
    stale = orphan = 0
    for uid, dec in decisions.items():
        rec = by.get(uid)
        if rec is None:
            orphan += 1
            continue
        if decision_is_stale(rec, dec):
            stale += 1
            continue
        out[human_screen_outcome(dec)] += 1
    return {"included": out["Include"], "excluded": out["Exclude"], "unsure": out["Unsure"],
            "incomplete": out[""], "stale": stale, "orphan": orphan,
            "complete": out["Include"] + out["Exclude"] + out["Unsure"]}


@bp.get("/api/screening/meta")
def api_screening_meta():
    idx = screening_index()
    coder = _coder_arg()
    decisions, dec_error = decisions_view(coder)
    master = screening_master_path()
    return jsonify({
        "available": idx["available"],
        "error": idx.get("error", ""),
        "decisions_error": dec_error,
        "master": os.path.basename(master),
        "master_mtime": (datetime.fromtimestamp(os.path.getmtime(master)).isoformat(timespec="seconds")
                         if os.path.exists(master) else None),
        "criteria": SCREENING_CRITERIA,
        "total": len(idx["rows"]),
        "screened": len(decisions),
        "counts": _counts(idx, decisions),
        "coder": coder,
        "coder_ok": bool(coder_token(coder)),
    })


@bp.get("/api/screening/list")
def api_screening_list():
    idx = screening_index()
    coder = _coder_arg()
    decisions, dec_error = decisions_view(coder)
    flt = (request.args.get("filter") or "all").strip().lower()
    search = (request.args.get("search") or "").strip().lower()
    aux = idx.get("aux") or {}
    out = []
    for rec in idx["rows"]:
        uid = rec["id"]
        a = aux.get(uid) or {}
        dec = decisions.get(uid)
        if dec is not None and decision_is_stale(rec, dec):
            dec = None
        human = human_screen_outcome(dec)
        ai = a.get("ai", "")
        eff = human or ai
        if flt == "toscreen" and dec:
            continue
        if flt == "screened" and not dec:
            continue
        if flt == "included" and eff != "Include":
            continue
        if flt == "excluded" and eff != "Exclude":
            continue
        if flt == "unsure" and eff != "Unsure":
            continue
        if search and search not in a.get("hay", ""):
            continue
        row = {"i": uid, "t": rec["title"], "a": rec["authors"], "y": rec["year"],
               "ai": ai, "h": human}
        if dec:
            row["d"] = 1
        out.append(row)
    return jsonify({"total": len(out), "screened": len(decisions), "rows": out,
                    "decisions_error": dec_error})


@bp.get("/api/screening/abstract/<path:uid>")
def api_screening_abstract(uid):
    idx = screening_index()
    rec = idx["by_id"].get(uid)
    if rec is None:
        abort(404)
    decisions, dec_error = decisions_view(_coder_arg())
    human = decisions.get(uid)
    stale = decision_is_stale(rec, human)
    return jsonify({"record": rec, "human": None if stale else human,
                    "human_stale": human if stale else None,
                    "decisions_error": dec_error,
                    "ai_decision": ai_screen_decision(rec)})


@bp.post("/api/screening/decision")
def api_screening_decision():
    payload = request.get_json(force=True, silent=True)
    if not isinstance(payload, dict):
        payload = {}
    coder = str(payload.get("coder") or "").strip()
    uid = str(payload.get("id") or "").strip()
    criteria = payload.get("criteria")
    if not coder:
        return jsonify({"ok": False, "error": "coder name is required"}), 400
    if not coder_token(coder):
        return jsonify({"ok": False, "error": "coder name must contain a letter or digit"}), 400
    if not uid or not isinstance(criteria, dict):
        return jsonify({"ok": False, "error": "id and criteria are required"}), 400
    idx = screening_index()
    rec = idx["by_id"].get(uid)
    if rec is None:
        return jsonify({"ok": False, "error": "unknown abstract id"}), 400
    clean = {}
    for lab in _SCREEN_LABELS:
        entry = criteria.get(lab)
        entry = entry if isinstance(entry, dict) else {}
        raw = str(entry.get("verdict") or "").strip()
        verdict = norm_verdict(raw)
        if raw and not verdict:
            return jsonify({"ok": False,
                            "error": f"{lab}: '{raw}' is not a verdict (Yes / No / Unsure)"}), 400
        clean[lab] = {
            "verdict": verdict,
            "certainty": norm_certainty(entry.get("certainty")),
            "rationale": str(entry.get("rationale") or "").strip()[:_RATIONALE_MAX],
        }
    prefill = payload.get("prefilled")
    prefilled = {}
    if isinstance(prefill, dict):
        for lab, src in prefill.items():
            if lab in clean and src in _PREFILL_SOURCES and clean[lab]["verdict"]:
                prefilled[lab] = src
    now = datetime.now().isoformat(timespec="seconds")
    result = {}

    def apply(decisions):
        prior = decisions.get(uid)
        prior = prior if isinstance(prior, dict) else {}
        crits = dict(prior.get("criteria") or {})   # keep criteria this form does not show
        crits.update(clean)
        record = {
            "id": uid, "an": rec.get("an"), "criteria": crits,
            "decision": "", "coder": coder, "saved_at": now,
            "fp": title_fingerprint(rec.get("title")),
        }
        if prefilled:
            record["prefilled"] = prefilled
        record["decision"] = human_screen_decision(record)
        result["existed"] = bool(prior)
        result["record"] = record
        decisions[uid] = record

    try:
        _path, decisions = update_screening_decisions(coder, apply)
    except ScreeningStoreError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 409
    except OSError as exc:
        return jsonify({"ok": False, "error": f"could not write the decision file: {exc}"}), 500
    record = result["record"]
    return jsonify({"ok": True, "id": uid, "decision": record["decision"],
                    "outcome": human_screen_outcome(record), "saved_at": now,
                    "screened": len(decisions), "overwrote": result["existed"]})


@bp.post("/api/screening/merge")
def api_screening_merge():
    payload = request.get_json(force=True, silent=True)
    if not isinstance(payload, dict):
        payload = {}
    coder = str(payload.get("coder") or "").strip()
    if not coder or not coder_token(coder):
        return jsonify({"ok": False, "error": "coder name is required"}), 400
    with _SCREEN_MERGE_LOCK:
        if _SCREEN_MERGE["running"]:
            return jsonify({"ok": False, "error": "a merge is already in progress"}), 409
        decisions, err = decisions_view(coder)
        if err:
            return jsonify({"ok": False, "error": err}), 409
        if not decisions:
            return jsonify({"ok": False, "error": "no screened abstracts for this coder"}), 400
        now = datetime.now().isoformat(timespec="seconds")
        _SCREEN_MERGE.update(running=True, coder=coder, ok=None, error="", log="",
                             started=now, finished=None, matched=0, changed=0, unmatched=0,
                             output=None, backup=None)
    threading.Thread(target=_screen_merge_worker, args=(coder,), daemon=True).start()
    return jsonify({"ok": True, "running": True, "coder": coder, "screened": len(decisions)})


@bp.get("/api/screening/merge/status")
def api_screening_merge_status():
    return jsonify(dict(_SCREEN_MERGE))
