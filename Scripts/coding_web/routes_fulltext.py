"""Full-text exclusion routes: per-coder decisions, agreement and merge status."""

from flask import Blueprint, abort, jsonify, request
from fulltext_lib import (AGREE_MIN, criteria, load_agreed, load_decisions, mark_status,
                          recompute_agreed, save_decision)
from studio_state import S

bp = Blueprint("fulltext", __name__)


@bp.get("/api/fulltext/criteria")
def api_fulltext_criteria():
    return jsonify(criteria())


@bp.get("/api/fulltext/excluded")
def api_fulltext_excluded():
    return jsonify({"agreed": load_agreed(), "agree_min": AGREE_MIN,
                    "mark": mark_status()})


@bp.get("/api/fulltext/<path:key>")
def api_fulltext_get(key):
    if key not in S.KEYS:
        abort(404)
    coder = (request.args.get("coder") or "").strip()
    return jsonify({
        "key": key,
        "coder": coder,
        "mine": load_decisions(coder).get(key) if coder else None,
        "agreed": load_agreed().get(key),
        "criteria": criteria()["criteria"],
        "agree_min": AGREE_MIN,
    })


@bp.post("/api/fulltext/<path:key>")
def api_fulltext_set(key):
    if key not in S.KEYS:
        return jsonify({"ok": False, "error": "unknown paper key"}), 400
    payload = request.get_json(force=True, silent=True) or {}
    coder = str(payload.get("coder") or "").strip()
    if not coder:
        return jsonify({"ok": False, "error": "coder name is required"}), 400
    try:
        save_decision(coder, key, bool(payload.get("exclude")),
                      payload.get("criterion"), payload.get("note"))
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    # A derived master is rebuilt from the decision files (debounced, background);
    # a legacy project was marked directly by save_decision().
    import master_job

    compiled = master_job.schedule("fulltext decision")
    return jsonify({"ok": True, "key": key, "mine": load_decisions(coder).get(key),
                    "agreed": load_agreed().get(key),
                    "master": "scheduled" if compiled else "legacy"})


@bp.post("/api/fulltext/merge")
def api_fulltext_merge():
    agreed = recompute_agreed(mark=True)
    return jsonify({"ok": True, "agreed": len(agreed), "mark": mark_status()})
