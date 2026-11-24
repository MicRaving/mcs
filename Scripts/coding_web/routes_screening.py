"""Screening routes: the abstract master dataset, per-coder decisions and merge.

Outcome rule (owner decision 2026-11-24, ``screening_lib.screen_outcome``):
one failing gating criterion excludes, all passing includes, all answered with
an "Unsure" and no failure is **Unsure** (the record goes on to full text);
the criteria verdicts always override a stored ``decision``.  A positional
(``row-<n>``) decision whose title fingerprint no longer matches its row is
**stale** and is shown, counted and merged as unscreened.
"""

from datetime import datetime
from flask import Blueprint, jsonify, request
import os
import threading
from screening_lib import (PREFILL_SOURCES, RATIONALE_MAX, ScreeningFileError, _SCREEN_MERGE,
                           _SCREEN_MERGE_LOCK, _scheme_criteria, _screen_merge_worker,
                           abstract_record, ai_screen_decision, ai_screen_outcome,
                           decision_for, human_screen_decision, human_screen_outcome,
                           live_decision_for, load_screening_decisions, norm_certainty,
                           norm_verdict, record_screening_decision, screening_criteria,
                           screening_index, screening_master_path, title_fingerprint)
import master_job

bp = Blueprint("screening", __name__)


def _coder_arg():
    return (request.args.get("coder") or "").strip()


def _decisions_view(coder):
    """``(decisions, error)``: the coder's decisions, and why they could not be
    read (an unreadable file is reported, never silently shown as empty)."""
    if not coder:
        return {}, ""
    try:
        return load_screening_decisions(coder, strict=True), ""
    except ScreeningFileError as exc:
        return {}, str(exc)


def _counts(idx, decisions, crits):
    """The coder's decisions by outcome, plus stale and orphan (no such row)."""
    by_id = idx.get("by_id") or {}
    out = {"Include": 0, "Exclude": 0, "Unsure": 0, "": 0}
    stale = orphan = 0
    seen = set()
    for uid, dec in decisions.items():
        rec = by_id.get(uid)
        if rec is None:
            orphan += 1
            continue
        if rec["id"] in seen:            # an id and its row alias: one record
            continue
        seen.add(rec["id"])
        live, old = live_decision_for(decisions, rec)
        if live is None and old is not None:
            stale += 1
            continue
        out[human_screen_outcome(live, crits)] += 1
    return {"included": out["Include"], "excluded": out["Exclude"], "unsure": out["Unsure"],
            "incomplete": out[""], "stale": stale, "orphan": orphan,
            "complete": out["Include"] + out["Exclude"] + out["Unsure"]}


@bp.get("/api/screening/meta")
def api_screening_meta():
    idx = screening_index()
    coder = _coder_arg()
    decisions, dec_error = _decisions_view(coder)
    info = idx.get("info") or {}
    # The source is the master workbook, or the decision-free records file when
    # the workbook is not readable (a blinded coder's mirror does not get it).
    master = info.get("source_path") or screening_master_path()
    return jsonify({
        "available": idx["available"],
        "error": idx.get("error") or "",
        "decisions_error": dec_error,
        "source": info.get("source") or "missing",
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
        "counts": _counts(idx, decisions, _scheme_criteria()),
        "coder": coder,
    })


@bp.get("/api/screening/list")
def api_screening_list():
    idx = screening_index()
    coder = _coder_arg()
    decisions, dec_error = _decisions_view(coder)
    flt = (request.args.get("filter") or "all").strip().lower()
    search = (request.args.get("search") or "").strip().lower()
    crits = _scheme_criteria()          # once per request, never per row
    out = []
    for rec in idx["rows"]:
        dec, _stale = live_decision_for(decisions, rec)
        if flt == "toscreen" and dec:
            continue
        if flt == "screened" and not dec:
            continue
        human = human_screen_outcome(dec, crits)
        ai = ai_screen_outcome(rec, crits)
        eff = human or ai
        if flt == "included" and eff != "Include":
            continue
        if flt == "excluded" and eff != "Exclude":
            continue
        if flt == "unsure" and eff != "Unsure":
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
                    "available": idx["available"], "error": idx.get("error") or "",
                    "decisions_error": dec_error})


@bp.get("/api/screening/abstract/<path:uid>")
def api_screening_abstract(uid):
    idx = screening_index()
    rec = idx["by_id"].get(uid)
    if rec is None:
        return jsonify({"ok": False, "error": f"no abstract with id {uid!r} in the master "
                                              "workbook"}), 404
    decisions, dec_error = _decisions_view(_coder_arg())
    crits = _scheme_criteria()
    # the record's own dict is lean (no abstract / long AI text in memory): the
    # text comes from the side file for this one record, and the AI verdicts are
    # keyed exactly like the criteria (the workbook's column prefix may differ in
    # case: "c1_verdict" still feeds criterion "C1")
    record = abstract_record(rec, crits)
    human, stale = live_decision_for(decisions, rec)
    return jsonify({"record": record, "human": human, "human_stale": stale,
                    "decisions_error": dec_error,
                    "ai_decision": ai_screen_decision(rec, crits)})


@bp.post("/api/screening/decision")
def api_screening_decision():
    payload = request.get_json(force=True, silent=True)
    if not isinstance(payload, dict):
        payload = {}
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
        verdict = norm_verdict(criterion, entry.get("verdict"))
        if verdict is None:
            options = " / ".join(str(o) for o in (criterion.get("options") or []))
            return jsonify({"ok": False, "error": f"{lab}: {str(entry.get('verdict')).strip()!r} "
                                                  f"is not a verdict ({options})"}), 400
        clean[lab] = {
            "verdict": verdict,
            "certainty": norm_certainty(entry.get("certainty")),
            "rationale": str(entry.get("rationale") or "").strip()[:RATIONALE_MAX],
        }
    # Where a pre-filled verdict came from (provenance, never a verdict).  An AI
    # pre-fill can only have happened on the master workbook: a records-file
    # source (a blinded mirror) has no AI verdict, so such a claim is dropped.
    source = (idx.get("info") or {}).get("source") or ""
    prefilled = {}
    raw_prefill = payload.get("prefilled")
    if isinstance(raw_prefill, dict):
        for lab, src in raw_prefill.items():
            if (lab in clean and src in PREFILL_SOURCES and clean[lab]["verdict"]
                    and not (src == "ai" and source != "master")):
                prefilled[lab] = src
    record = {
        "id": str(uid), "an": rec.get("an"), "criteria": clean,
        "decision": "", "coder": str(coder).strip(),
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        # the title fingerprint lets a positional (row-N) decision be recognised
        # as stale when the workbook is re-ordered (decision_is_stale)
        "fp": title_fingerprint(rec.get("title")),
    }
    if prefilled:
        record["prefilled"] = prefilled
    # Keep the coder's verdicts for criteria the scheme no longer shows (e.g. a
    # legacy "Intervention" classification) instead of silently dropping them.
    previous = decision_for(load_screening_decisions(coder), rec) or {}
    for key, entry in ((previous.get("criteria") or {}).items()):
        record["criteria"].setdefault(key, entry)
    record["decision"] = human_screen_decision(record)
    outcome = human_screen_outcome(record)
    try:
        screened, existed = record_screening_decision(str(coder).strip(), str(uid), record,
                                                      alt=rec.get("alt"))
    except ScreeningFileError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 409
    except OSError as exc:
        return jsonify({"ok": False, "error": f"could not write the decision file: {exc}"}), 500
    # The master is a derived product: a saved decision schedules a rebuild
    # (debounced, background) so the PRISMA numbers follow the decision files.
    compiled = master_job.schedule("screening decision")
    return jsonify({"ok": True, "id": str(uid), "decision": record["decision"],
                    "outcome": outcome, "saved_at": record["saved_at"],
                    "screened": screened, "overwrote": existed,
                    "master": "scheduled" if compiled else "legacy"})


@bp.post("/api/screening/merge")
def api_screening_merge():
    payload = request.get_json(force=True, silent=True) or {}
    coder = (payload.get("coder") or "").strip()
    if not coder:
        return jsonify({"ok": False, "error": "coder name is required"}), 400
    # In a compiled project there is nothing to merge into the workbook: the
    # compiler writes every coder's decisions into the derived master at once.
    if master_job.enabled():
        master_job.schedule("screening merge", delay=0)
        return jsonify({"ok": True, "running": False, "compiled": True,
                        "coder": coder,
                        "message": "the master is derived; a recompile was scheduled"})
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
