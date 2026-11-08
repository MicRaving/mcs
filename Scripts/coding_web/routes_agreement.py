"""Agreement routes: reliability, conflicts, settlements and LLM-excluded lists.

Raters are the original coder files; the consensus (``Extraction/<Key>.json``)
is what a settlement writes into; every decision is recorded in
``Codings/_settlements.json`` (see ``agreement_lib``).
"""

from flask import Blueprint, Response, jsonify, request

from agreement_lib import (AGREEMENT_REFERENCE, CONSENSUS, no_majority_fields,
                           PSEUDO_DV_FIELDS, SETTLEMENTS_NAME,
                           _ABSTRACT_CATS, _KEY_CACHE, _abstract_agreement_index,
                           _abstract_case_maps, _agreement_index, abstract_fields,
                           abstract_settlements, clear_settlement, gwet_ac, load_settlements,
                           majority_value, masters, reliability_rows, set_study_status,
                           settle_abstract, settle_cases, settlements_csv, SettleError,
                           SettlementIndex, study_conflicts)
from screening_lib import ai_screen_decision, human_screen_decision, screening_index
from studio_state import S, log, paper_keys

bp = Blueprint("agreement", __name__)

#: {key: (file signature, conflicts)} - conflicts per paper, rebuilt with the index.
_CONFLICTS = {}


def _int_arg(name, default, lo, hi):
    try:
        return max(lo, min(hi, int(request.args.get(name, default))))
    except (TypeError, ValueError):
        return default


def _paper_conflicts(data, key):
    d = data["by_key"].get(key)
    if d is None:
        return []
    sig = (_KEY_CACHE.get(key) or (None,))[0]
    hit = _CONFLICTS.get(key)
    if hit and hit[0] == sig and hit[1] is d:
        return hit[2]
    cases = study_conflicts(d)
    _CONFLICTS[key] = (sig, d, cases)
    return cases


def _store_or_error():
    try:
        return load_settlements(), None
    except RuntimeError as exc:
        log(f"agreement: {exc}")
        return None, (jsonify({"ok": False, "error": str(exc)}), 500)


@bp.get("/api/agreement/reliability")
def api_agreement_reliability():
    exclude = {x.strip() for x in (request.args.get("exclude") or "").split(",") if x.strip()}
    data = _agreement_index()
    rows, overall, summary = reliability_rows(data, exclude)
    return jsonify({"reference": AGREEMENT_REFERENCE if data.get("ext_rater") else None,
                    "consensus": CONSENSUS, "extraction_role": ("rater" if data.get("ext_rater")
                                                                 else "consensus"),
                    "anchor_rater": data.get("anchor"), "exclude": sorted(exclude),
                    "raters": data["rater_names"], "rater_summary": data["rater_summary"],
                    "fields": rows, "overall": overall, "dv_alignment": summary})


@bp.get("/api/agreement/masters")
def api_agreement_masters():
    return jsonify(masters())


def _row_json(c, entry, state="open"):
    f = c["fdef"]
    span = c.get("span") or {}
    t = c.get("target") or {}
    return {
        "key": c["key"], "study_id": c["study_id"], "case_id": c["case_id"],
        "dv_index": t.get("dv_index"), "dv_name": c.get("dv_name"),
        "dv_names": c.get("dv_names"),
        "field": f["name"], "field_label": f["label"], "group": f["group"],
        "kind": f["kind"], "multi": f["multi"], "derived": f["name"] in PSEUDO_DV_FIELDS,
        "levels": f.get("levels") or [], "labels": f.get("labels") or {},
        "values": c["values"], "majority": c.get("majority") or None,
        "consensus_value": c.get("consensus_value"), "has_target": bool(c.get("target")),
        "state": state,
        "settled": state == "settled",
        "rater_only": state == "rater_only",
        "resolution": (entry.get("value") if entry
                       else (c.get("consensus_value") or None) if state == "settled" else None),
        "settled_by": entry.get("master") if entry else None,
        "settled_at": entry.get("decided_at") if entry else None,
        "applied": entry.get("applied") if entry else None,
        "apply_note": entry.get("apply_note") if entry else None,
        "source_page": span.get("page"), "source_quote": span.get("quote"),
        "source_coder": span.get("coder"), "source_rects": span.get("rects") or [],
    }


@bp.get("/api/agreement/conflicts")
def api_agreement_conflicts():
    field_filter = (request.args.get("field") or "").strip()
    kind_filter = (request.args.get("kind") or "").strip()
    study_filter = (request.args.get("study") or "").strip()
    status = (request.args.get("status") or "open").strip().lower()
    limit = _int_arg("limit", 300, 1, 2000)
    offset = _int_arg("offset", 0, 0, 10 ** 9)
    data = _agreement_index()
    store, err = _store_or_error()
    if err:
        return err
    sidx = SettlementIndex(store)
    study_state = store.get("studies") or {}
    counts, field_order, studies, rows = {}, [], {}, []
    for key in data["by_key"]:
        for c in _paper_conflicts(data, key):
            if kind_filter and c["kind"] != kind_filter:
                continue
            f = c["fdef"]
            if f["name"] not in counts:
                field_order.append(f["name"])
                counts[f["name"]] = {"name": f["name"], "label": f["label"], "kind": f["kind"],
                                     "group": f["group"], "multi": f["multi"],
                                     "open": 0, "settled": 0, "rater_only": 0}
            state, entry = conflict_state(c, sidx)
            st = studies.setdefault(key, {"key": key, "label": key, "n_open": 0,
                                         "n_settled": 0, "n_rater_only": 0, "fields": set()})
            st["n_" + state] += 1
            st["fields"].add(f["name"])
            if study_filter and key != study_filter:
                continue
            # field counts describe the selected study, whatever field is selected
            counts[f["name"]][state] += 1
            if field_filter and f["name"] != field_filter:
                continue
            if status in ("open", "settled") and state != status:
                continue
            rows.append(_row_json(c, entry, state))
    study_list = [{"key": k, "label": v["label"], "n_open": v["n_open"],
                   "n_settled": v["n_settled"], "n_rater_only": v["n_rater_only"],
                   "n_fields": len(v["fields"]),
                   "status": (study_state.get(k) or {}).get("status")}
                  for k, v in studies.items()
                  if (status != "open" or v["n_open"] > 0)
                  and (status != "settled" or v["n_settled"] > 0)]
    study_list.sort(key=lambda s: (-s["n_open"], str(s["label"]).lower()))
    totals = {"open": sum(v["n_open"] for v in studies.values()),
              "settled": sum(v["n_settled"] for v in studies.values()),
              "rater_only": sum(v["n_rater_only"] for v in studies.values())}
    return jsonify({"total": len(rows), "offset": offset, "limit": limit,
                    "rows": rows[offset:offset + limit],
                    "fields": [counts[name] for name in field_order],
                    "studies": study_list, "study": study_filter,
                    "study_status": study_state.get(study_filter) if study_filter else None,
                    "totals": totals,
                    "raters": data["rater_names"], "consensus": CONSENSUS,
                    "store": SETTLEMENTS_NAME})


def conflict_state(case, sidx):
    """(state, settlement entry) of one conflict case.

    ``open``        raters disagree and the consensus row holds no value yet - a
                    decision is still needed.
    ``settled``     an explicit settlement in ``_settlements.json`` (audited), or
                    the reconciled consensus already carries a value (the master
                    pass wrote its decision straight into ``Extraction/``).
    ``rater_only``  no row in the consensus at all: the disagreement lives in rater
                    files only and cannot be settled without adding a coding row,
                    so it is reported, never counted as open work.
    """
    entry = sidx.find(case)
    if entry:
        return "settled", entry
    if not case.get("target"):
        return "rater_only", None
    if (case.get("consensus_value") or "").strip():
        return "settled", None
    return "open", None



def _find_case(key, case_id, field):
    data = _agreement_index()
    for c in _paper_conflicts(data, key):
        if c["case_id"] == case_id and c["field"] == field:
            return c
    return None


def _known_key(key):
    return bool(key) and (key in S.KEYS or key in set(paper_keys()))


def _err(exc):
    return jsonify({"ok": False, "error": str(exc)}), getattr(exc, "status", 400)


@bp.post("/api/agreement/resolve")
def api_agreement_resolve():
    """Settle (``value``) or undo (``value`` null) one full-text conflict."""
    payload = request.get_json(force=True, silent=True) or {}
    master = str(payload.get("master") or "").strip()
    key = payload.get("key")
    case_id = str(payload.get("case_id") or "").strip()
    field = str(payload.get("field") or "").strip()
    value = payload.get("value")
    if not master:
        return jsonify({"ok": False, "error": "master name is required"}), 400
    if not _known_key(key):
        return jsonify({"ok": False, "error": "unknown paper key"}), 400
    case = _find_case(key, case_id, field)
    if case is None:
        return jsonify({"ok": False, "error": "this conflict no longer exists; reload"}), 409
    try:
        if value is None or (isinstance(value, str) and not value.strip()):
            res = clear_settlement(key, case, master)
            return jsonify({"ok": True, "cleared": True, **res})
        entry = settle_cases(key, [(case, value)], master,
                             note=str(payload.get("note") or "").strip())[0]
    except SettleError as exc:
        return _err(exc)
    except (OSError, ValueError, RuntimeError) as exc:
        log(f"agreement: settle failed for {key} {case_id} {field}: {exc!r}")
        return jsonify({"ok": False, "error": f"could not save: {exc}"}), 500
    return jsonify({"ok": True, "value": entry["value"], "applied": entry["applied"],
                    "apply_note": entry["apply_note"], "previous": entry["previous_value"],
                    "path": f"Extraction/{key}.json", "store": f"Codings/{SETTLEMENTS_NAME}"})


@bp.post("/api/agreement/settle-bulk")
def api_agreement_settle_bulk():
    """Settle every OPEN conflict of one paper with the rater majority or the consensus."""
    payload = request.get_json(force=True, silent=True) or {}
    master = str(payload.get("master") or "").strip()
    key = payload.get("key")
    mode = str(payload.get("mode") or "").strip()
    field = str(payload.get("field") or "").strip()
    if not master:
        return jsonify({"ok": False, "error": "master name is required"}), 400
    if not _known_key(key):
        return jsonify({"ok": False, "error": "unknown paper key"}), 400
    if mode not in ("majority", "consensus"):
        return jsonify({"ok": False, "error": "mode must be majority or consensus"}), 400
    store, err = _store_or_error()
    if err:
        return err
    sidx = SettlementIndex(store)
    decisions, skipped = [], 0
    blocked = no_majority_fields()
    for c in _paper_conflicts(_agreement_index(), key):
        if (field and c["field"] != field):
            continue
        state, _entry = conflict_state(c, sidx)
        if state != "open":
            if state == "rater_only":
                skipped += 1
            continue
        if mode == "majority" and c["field"] in blocked:
            skipped += 1
            continue
        value = c.get("majority") if mode == "majority" else c.get("consensus_value")
        if not value:
            skipped += 1
            continue
        decisions.append((c, value))
    if not decisions:
        return jsonify({"ok": True, "settled": 0, "skipped": skipped})
    try:
        out = settle_cases(key, decisions, master, source=f"app: bulk {mode}")
    except SettleError as exc:
        return _err(exc)
    except (OSError, ValueError, RuntimeError) as exc:
        log(f"agreement: bulk settle failed for {key}: {exc!r}")
        return jsonify({"ok": False, "error": f"could not save: {exc}"}), 500
    return jsonify({"ok": True, "settled": len(out), "skipped": skipped,
                    "not_applied": sum(1 for e in out if not e["applied"])})


@bp.post("/api/agreement/study-status")
def api_agreement_study_status():
    payload = request.get_json(force=True, silent=True) or {}
    master = str(payload.get("master") or "").strip()
    key = str(payload.get("key") or "").strip()
    status = str(payload.get("status") or "").strip()
    scope = str(payload.get("scope") or "fulltext").strip()
    if not master:
        return jsonify({"ok": False, "error": "master name is required"}), 400
    if status not in ("complete", "open"):
        return jsonify({"ok": False, "error": "status must be complete or open"}), 400
    open_n = None
    if scope == "fulltext":
        if not _known_key(key):
            return jsonify({"ok": False, "error": "unknown paper key"}), 400
        store, err = _store_or_error()
        if err:
            return err
        sidx = SettlementIndex(store)
        open_n = sum(1 for c in _paper_conflicts(_agreement_index(), key)
                     if conflict_state(c, sidx)[0] == "open")
    try:
        set_study_status(key, status, master, scope=scope, open_conflicts=open_n)
    except (OSError, RuntimeError) as exc:
        return jsonify({"ok": False, "error": f"could not save: {exc}"}), 500
    return jsonify({"ok": True, "status": status, "open_conflicts": open_n})


@bp.get("/api/agreement/settlements")
def api_agreement_settlements():
    store, err = _store_or_error()
    if err:
        return err
    by_scope = {}
    for e in store.get("settlements") or []:
        s = by_scope.setdefault(e.get("scope", "fulltext"), {"settled": 0, "not_applied": 0})
        s["settled"] += 1
        if not e.get("applied"):
            s["not_applied"] += 1
    return jsonify({"path": f"Codings/{SETTLEMENTS_NAME}", "updated_at": store.get("updated_at"),
                    "by_scope": by_scope, "masters": masters(),
                    "studies": store.get("studies") or {},
                    "history": len(store.get("history") or []),
                    "migrated_from": store.get("migrated_from") or []})


@bp.get("/api/agreement/settlements.csv")
def api_agreement_settlements_csv():
    scope = (request.args.get("scope") or "").strip() or None
    try:
        body = settlements_csv(scope)
    except RuntimeError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    return Response("﻿" + body, mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=settlements.csv"})


# ---------------------------------------------------------------- abstract stage
@bp.get("/api/agreement/abstract/reliability")
def api_agreement_abstract_reliability():
    exclude = {x.strip() for x in (request.args.get("exclude") or "").split(",") if x.strip()}
    index = _abstract_agreement_index()
    maps = _abstract_case_maps(index)
    fields = []
    wsum = acc = 0.0
    for lab, name in abstract_fields():
        cmap = maps.get(lab) or {}
        case_ratings = []
        for v in cmap.values():
            vals = [val for r, val in v.items() if r not in exclude]
            if len(vals) >= 2:
                case_ratings.append(vals)
        res = gwet_ac(case_ratings, _ABSTRACT_CATS) if case_ratings else None
        ac1 = res["coeff"] if res else None
        if res and res["coeff"] is not None:
            acc += res["coeff"] * res["n2"]
            wsum += res["n2"]
        raters_seen = {r for v in cmap.values() for r in v if r not in exclude}
        fields.append({"name": lab, "label": name, "group": "Abstract screening",
                       "kind": "exp", "multi": False, "ordered": False, "n_levels": 3,
                       "n_items": len(cmap), "n_multi": len(case_ratings),
                       "n_raters": len(raters_seen),
                       "ac1": ac1, "ac1_pa": res["pa"] if res else None,
                       "ac1_pe": res["pe"] if res else None,
                       "ac2": None, "ac2_pa": None, "ac2_pe": None})
    raters = sorted(index.keys())
    return jsonify({"reference": AGREEMENT_REFERENCE, "exclude": sorted(exclude), "raters": raters,
                    "rater_summary": [{"rater": r, "keys": len(index[r]),
                                       "experiments": len(index[r])} for r in raters],
                    "fields": fields,
                    "overall": {"ac1": (acc / wsum) if wsum else None, "ac2": None,
                                "n_fields": len([f for f in fields if f["ac1"] is not None]),
                                "n_items_total": sum(f["n_multi"] for f in fields)}})


@bp.get("/api/agreement/abstract/conflicts")
def api_agreement_abstract_conflicts():
    field_filter = (request.args.get("field") or "").strip()
    study_filter = (request.args.get("study") or "").strip()
    status = (request.args.get("status") or "open").strip().lower()
    limit = _int_arg("limit", 300, 1, 2000)
    offset = _int_arg("offset", 0, 0, 10 ** 9)
    title_of = {}
    try:
        for rec in (screening_index().get("rows") or []):
            title_of[str(rec.get("id"))] = rec.get("title") or str(rec.get("id"))
    except Exception as exc:  # noqa: BLE001
        log(f"agreement: screening titles unavailable: {exc!r}")
    store, err = _store_or_error()
    if err:
        return err
    settled_of = abstract_settlements(store)
    maps = _abstract_case_maps(_abstract_agreement_index())
    rows, counts, studies = [], [], {}
    for lab, name in abstract_fields():
        if field_filter and lab != field_filter:
            continue
        open_n = settled_n = 0
        for uid, vals in (maps.get(lab) or {}).items():
            if len(set(vals.values())) < 2:
                continue
            entry = (settled_of.get(uid) or {}).get(lab)
            settled = bool(entry)
            if settled:
                settled_n += 1
            else:
                open_n += 1
            st = studies.setdefault(uid, {"key": uid, "label": title_of.get(uid, uid),
                                          "n_open": 0, "n_settled": 0, "fields": set()})
            st["n_settled" if settled else "n_open"] += 1
            st["fields"].add(lab)
            if (status == "open" and settled) or (status == "settled" and not settled):
                continue
            if study_filter and uid != study_filter:
                continue
            rows.append({"key": uid, "study_id": uid, "case_id": "abs:" + uid,
                         "dv_index": None, "field": lab,
                         "field_label": name, "group": "Abstract screening", "kind": "exp",
                         "multi": False, "derived": False, "levels": list(_ABSTRACT_CATS),
                         "labels": {v: v for v in _ABSTRACT_CATS},
                         "values": dict(sorted(vals.items(), key=lambda kv: (
                             kv[0] == AGREEMENT_REFERENCE, kv[0].lower()))),
                         "majority": majority_value(vals) or None,
                         "consensus_value": None, "has_target": True,
                         "resolution": entry.get("value") if entry else None,
                         "settled_by": entry.get("master") if entry else None,
                         "settled_at": entry.get("decided_at") if entry else None,
                         "applied": True if entry else None, "apply_note": None,
                         "settled": settled})
        counts.append({"name": lab, "label": name, "kind": "exp",
                       "group": "Abstract screening", "multi": False,
                       "open": open_n, "settled": settled_n})
    study_list = [{"key": k, "label": v["label"], "n_open": v["n_open"],
                   "n_settled": v["n_settled"], "n_fields": len(v["fields"])}
                  for k, v in studies.items()
                  if (status != "open" or v["n_open"] > 0)
                  and (status != "settled" or v["n_settled"] > 0)]
    study_list.sort(key=lambda s: (-s["n_open"], str(s["label"]).lower()))
    return jsonify({"total": len(rows), "offset": offset, "limit": limit,
                    "rows": rows[offset:offset + limit], "fields": counts,
                    "studies": study_list, "study": study_filter,
                    "raters": sorted({r for m in maps.values() for v in m.values() for r in v}),
                    "store": SETTLEMENTS_NAME})


@bp.post("/api/agreement/abstract/resolve")
def api_agreement_abstract_resolve():
    payload = request.get_json(force=True, silent=True) or {}
    master = str(payload.get("master") or "").strip()
    uid = str(payload.get("key") or payload.get("study_id") or "").strip()
    lab = str(payload.get("field") or "").strip()
    if not master:
        return jsonify({"ok": False, "error": "master name is required"}), 400
    if lab not in [l for l, _ in abstract_fields()]:
        return jsonify({"ok": False, "error": "unknown criterion"}), 400
    if not uid:
        return jsonify({"ok": False, "error": "record id is required"}), 400
    vals = (_abstract_case_maps(_abstract_agreement_index()).get(lab) or {}).get(uid) or {}
    try:
        entry = settle_abstract(uid, lab, payload.get("value"), master, vals,
                                note=str(payload.get("note") or "").strip())
    except SettleError as exc:
        return _err(exc)
    except (OSError, RuntimeError) as exc:
        return jsonify({"ok": False, "error": f"could not save: {exc}"}), 500
    return jsonify({"ok": True, "window": "abstract", "cleared": entry is None,
                    "store": f"Codings/{SETTLEMENTS_NAME}"})


@bp.get("/api/agreement/abstract/llm-excluded")
def api_agreement_abstract_llm_excluded():
    """Records the LLM abstract screen excluded but at least one human rater would include."""
    idx = screening_index()
    index = _abstract_agreement_index()
    rows = []
    for rec in (idx.get("rows") or []):
        if ai_screen_decision(rec) != "Exclude":
            continue
        uid = str(rec.get("id"))
        raters = {}
        for coder, by_uid in index.items():
            if coder == AGREEMENT_REFERENCE:
                continue
            vals = by_uid.get(uid) or {}
            if not vals:
                continue
            decision = human_screen_decision(
                {"criteria": {lab: {"verdict": v} for lab, v in vals.items()}})
            if decision:
                raters[coder] = decision
        include_raters = sorted(c for c, v in raters.items() if v == "Include")
        if not include_raters:
            continue
        rows.append({"key": uid, "an": rec.get("an"), "title": rec.get("title"),
                     "authors": rec.get("authors"), "year": rec.get("year"),
                     "doi": rec.get("doi"), "source": rec.get("source"),
                     "ai": "Exclude", "raters": dict(sorted(raters.items())),
                     "include_raters": include_raters})
    rows.sort(key=lambda r: (str(r.get("title") or r.get("key") or "")).lower())
    return jsonify({"total": len(rows), "reference": AGREEMENT_REFERENCE, "rows": rows})
