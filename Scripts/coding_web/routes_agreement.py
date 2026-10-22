"""Agreement routes: reliability, conflicts, resolutions and LLM-excluded lists."""

from datetime import datetime
from flask import Blueprint, jsonify, request
import json
import os
import re
import tempfile
from agreement_lib import AGREEMENT_REFERENCE, DV_MIN_COVERAGE, DV_MIN_MATCHED, _ABSTRACT_CATS, _LABEL_MAX_LEVELS, abstract_fields, _abstract_agreement_index, _abstract_case_maps, _agreement_field_defs, _agreement_index, _canon_level, _dv_agreement_defs, _dv_alignment_for, _field_case_map, _field_value, _invalidate_agreement, _linear_weights, _norm_dv_name, _rater_sort_key, dv_alignment_summary, dv_conflict_cases, dv_field_cases, dv_field_categories, gwet_ac, insufficient_note, load_resolutions, pool_gwet, record_resolution, resolution_value
from screening_lib import ai_screen_decision, human_screen_decision, load_screening_decisions, save_screening_decisions, screening_index
from studio_lib import _values, atomic_replace, load_extraction, safe_token
from studio_state import S

bp = Blueprint("agreement", __name__)


@bp.get("/api/agreement/reliability")
def api_agreement_reliability():
    exclude = {x.strip() for x in (request.args.get("exclude") or "").split(",") if x.strip()}
    data = _agreement_index()
    rows = []
    wsum1 = wsum2 = acc1 = acc2 = 0.0

    def _add_row(f, ac1, ac2, extra=None):
        nonlocal acc1, acc2, wsum1, wsum2
        if ac1 and ac1.get("coeff") is not None:
            acc1 += ac1["coeff"] * ac1.get("n2", 0)
            wsum1 += ac1.get("n2", 0)
        if ac2 and ac2.get("coeff") is not None:
            acc2 += ac2["coeff"] * ac2.get("n2", 0)
            wsum2 += ac2.get("n2", 0)
        row = {
            "name": f["name"], "label": f["label"], "group": f["group"], "kind": f["kind"],
            "multi": f["multi"], "ordered": f["ordered"],
            "n_levels": len(f["levels"]), "n_items": 0, "n_multi": 0, "n_raters": 0,
            "ac1": ac1["coeff"] if ac1 else None, "ac1_pa": ac1.get("pa") if ac1 else None,
            "ac1_pe": ac1.get("pe") if ac1 else None,
            "ac2": ac2["coeff"] if ac2 else None, "ac2_pa": ac2.get("pa") if ac2 else None,
            "ac2_pe": ac2.get("pe") if ac2 else None,
            "matched_pairs": None, "unmatched": None, "coverage": None,
            "insufficient": False, "note": None,
        }
        if extra:
            row.update(extra)
        rows.append(row)
        return row

    # ---- experiment-level labels (case = one experiment, position-independent)
    for f in data["fields"]:
        if f["kind"] != "experiment":
            continue
        cmap = _field_case_map(data["raters"], f["name"], exclude)
        observed = set()
        case_ratings = []
        raters_seen = set()
        for vals in (v for cases in cmap.values() for v in cases.values()):
            observed.update(vals.values())
            raters_seen.update(vals.keys())
            case_ratings.append(list(vals.values()))
        if f["multi"]:
            cats = sorted(observed)
        else:
            cats = list(f["levels"]) + [v for v in sorted(observed) if v not in f["levels"]]
        ac1 = ac2 = None
        if 2 <= len(cats) < _LABEL_MAX_LEVELS and sum(1 for v in case_ratings if len(v) >= 2):
            res1 = gwet_ac(case_ratings, cats)
            if res1:
                ac1 = res1
            if f["ordered"] and all(re.fullmatch(r"-?\d+", c) for c in cats):
                res2 = gwet_ac(case_ratings, cats, _linear_weights(len(cats)))
                if res2:
                    ac2 = res2
        row = _add_row(f, ac1, ac2)
        row["n_levels"] = len(cats)
        row["n_items"] = len(case_ratings)
        row["n_multi"] = sum(1 for v in case_ratings if len(v) >= 2)
        row["n_raters"] = len(raters_seen)

    # ---- DV-level labels (canonical two-stage alignment, closed labels only)
    blocks, studies = _dv_alignment_for(data, exclude)
    summary = dv_alignment_summary(blocks, studies)
    study_pass = [s for s in studies if s.get("gate")]
    passing_keys = {s["key"] for s in study_pass}
    passing_blocks = [b for b in blocks if b["key"] in passing_keys]
    if passing_blocks:
        pass_summary = dv_alignment_summary(passing_blocks, study_pass)
        pooled_cov = pass_summary["coverage"]
        pooled_unmatched = pass_summary["unmatched"]
    else:
        pooled_cov = summary["coverage"]
        pooled_unmatched = summary["unmatched"]
    for f in _dv_agreement_defs():
        cases_by_study = {}
        for block in passing_blocks:
            cases = dv_field_cases(block, f["name"])
            if cases:
                cases_by_study.setdefault(block["key"], []).extend(cases)
        cats = dv_field_categories(f, cases_by_study)
        res1 = res2 = None
        if 2 <= len(cats) < _LABEL_MAX_LEVELS and cases_by_study:
            pooled = pool_gwet(cases_by_study, cats)
            if pooled["coeff"] is not None:
                res1 = pooled
            if f["ordered"] and all(re.fullmatch(r"-?\d+", c) for c in cats):
                pooled2 = pool_gwet(cases_by_study, cats, _linear_weights(len(cats)))
                if pooled2["coeff"] is not None:
                    res2 = pooled2
        matched_pairs = res1["n_multi"] if res1 else 0
        insufficient = bool(matched_pairs < DV_MIN_MATCHED or pooled_cov < DV_MIN_COVERAGE)
        note = insufficient_note(pooled_cov, matched_pairs) if insufficient else None
        if insufficient:
            res1 = res2 = None
        row = _add_row(f, res1, res2, extra={
            "n_levels": len(cats),
            "n_items": (res1["n_items"] if res1 else
                        sum(len(c) for c in cases_by_study.values())),
            "n_multi": matched_pairs,
            "n_raters": len({r["rater"] for b in passing_blocks for r in b["raters"]}),
            "matched_pairs": matched_pairs,
            "unmatched": pooled_unmatched,
            "coverage": pooled_cov,
            "insufficient": insufficient,
            "note": note,
        })
        row["n_levels"] = len(cats)

    overview = {
        "ac1": (acc1 / wsum1) if wsum1 else None,
        "ac2": (acc2 / wsum2) if wsum2 else None,
        "n_fields": len([r for r in rows if r["ac1"] is not None]),
        "n_items_total": sum(r["n_multi"] for r in rows),
    }
    rater_summary = data.get("rater_summary") or []
    return jsonify({"reference": AGREEMENT_REFERENCE, "exclude": sorted(exclude),
                    "raters": data["rater_names"], "rater_summary": rater_summary,
                    "fields": rows, "overall": overview,
                    "dv_alignment": summary})

@bp.get("/api/agreement/conflicts")
def api_agreement_conflicts():
    master = (request.args.get("master") or "").strip()
    field_filter = (request.args.get("field") or "").strip()
    kind_filter = (request.args.get("kind") or "").strip()
    study_filter = (request.args.get("study") or "").strip()
    status = (request.args.get("status") or "open").strip().lower()
    try:
        limit = max(1, min(2000, int(request.args.get("limit", 300))))
    except (TypeError, ValueError):
        limit = 300
    try:
        offset = max(0, int(request.args.get("offset", 0)))
    except (TypeError, ValueError):
        offset = 0
    data = _agreement_index()
    resolutions = load_resolutions(master) if master else {}

    cases = []
    field_order = []
    count_defs = {}

    def _note_field(fdef):
        if fdef["name"] not in count_defs:
            field_order.append(fdef["name"])
            count_defs[fdef["name"]] = fdef

    def _case(fdef, key, study_id, case_id, values, master_value=None,
              dv_index=None, dv_name=None):
        return {"fdef": fdef, "key": key, "study_id": study_id, "case_id": case_id,
                "values": values, "master_value": master_value,
                "dv_index": dv_index, "dv_name": dv_name}

    # ---- experiment-level labels: one case per Study_ID (position-free already)
    if not kind_filter or kind_filter == "experiment":
        for f in data["fields"]:
            if f["kind"] != "experiment":
                continue
            if field_filter and f["name"] != field_filter:
                continue
            _note_field(f)
            cmap = _field_case_map(data["raters"], f["name"], {master} if master else ())
            for key, by_case in cmap.items():
                for case, vals in by_case.items():
                    if len(set(vals.values())) < 2:
                        continue
                    ordered = {r: vals[r] for r in sorted(
                        vals, key=lambda r: _rater_sort_key(r, AGREEMENT_REFERENCE))}
                    cases.append(_case(f, key, case[0], "exp:" + str(case[0]), ordered))

    # ---- DV-level labels: aligned exactly like the reliability coefficients, so
    #      two raters listing their DVs in a different order cannot fabricate
    #      hundreds of phantom conflicts.  The Master is a resolver, not a rater,
    #      so their own (often cloned) coding never distorts the alignment.
    if not kind_filter or kind_filter == "dv":
        blocks = data.get("dv_blocks") or []
        if master:
            blocks, _ = _dv_alignment_for(data, {master})
        dv_defs = {f["name"]: f for f in _dv_agreement_defs()}
        for c in dv_conflict_cases({"dv_blocks": blocks}, master,
                                   field_names=[field_filter] if field_filter else None):
            fdef = dv_defs.get(c["field"]) or {
                "name": c["field"], "label": c["field_label"], "group": c["group"],
                "kind": "dv", "multi": c["multi"], "levels": c["levels"],
                "labels": c["labels"],
            }
            _note_field(fdef)
            cases.append(_case(fdef, c["key"], c["study_id"], c["case_id"],
                               c["values"], c["master_value"],
                               c["dv_index"], c["dv_name"]))

    counts = {name: {"name": name, "label": count_defs[name]["label"],
                     "kind": count_defs[name]["kind"], "group": count_defs[name]["group"],
                     "multi": count_defs[name]["multi"], "open": 0, "settled": 0}
              for name in field_order}
    rows, studies = [], {}
    for fc in cases:
        f = fc["fdef"]
        name = f["name"]
        res = resolution_value(resolutions, fc["key"], fc["case_id"], name)
        settled = bool(res)
        counts[name]["settled" if settled else "open"] += 1
        st = studies.setdefault(fc["key"], {"key": fc["key"], "label": fc["key"],
                                            "n_open": 0, "n_settled": 0, "fields": set()})
        st["n_settled" if settled else "n_open"] += 1
        st["fields"].add(name)
        if status == "open" and settled:
            continue
        if status == "settled" and not settled:
            continue
        if study_filter and fc["key"] != study_filter:
            continue
        rows.append({
            "key": fc["key"], "study_id": fc["study_id"], "case_id": fc["case_id"],
            "dv_index": fc["dv_index"], "dv_name": fc["dv_name"],
            "field": name, "field_label": f["label"], "group": f["group"],
            "kind": f["kind"], "multi": f["multi"],
            "levels": f.get("levels") or [], "labels": f.get("labels") or {},
            "values": fc["values"], "master_value": fc["master_value"],
            "resolution": res or None, "settled": settled,
        })
    total = len(rows)
    study_list = [{"key": k, "label": v["label"], "n_open": v["n_open"],
                   "n_settled": v["n_settled"], "n_fields": len(v["fields"])}
                  for k, v in studies.items()]
    study_list.sort(key=lambda s: (-s["n_open"], str(s["label"]).lower()))
    return jsonify({"total": total, "offset": offset, "limit": limit,
                    "rows": rows[offset:offset + limit],
                    "fields": [counts[name] for name in field_order],
                    "studies": study_list, "study": study_filter,
                    "master": master, "raters": data["rater_names"]})

@bp.post("/api/agreement/resolve")
def api_agreement_resolve():
    payload = request.get_json(force=True, silent=True) or {}
    master = (payload.get("master") or "").strip()
    key = payload.get("key")
    study_id = str(payload.get("study_id") or "").strip()
    field_name = payload.get("field")
    value = payload.get("value")
    dv_index = payload.get("dv_index")
    dv_name = str(payload.get("dv_name") or "").strip()
    case_id = str(payload.get("case_id") or "").strip()
    if not master:
        return jsonify({"ok": False, "error": "master name is required"}), 400
    if key not in S.KEYS:
        return jsonify({"ok": False, "error": "unknown paper key"}), 400
    fdef = next((f for f in _agreement_field_defs() if f["name"] == field_name), None)
    if fdef is None:
        return jsonify({"ok": False, "error": "not a deterministic label"}), 400
    path = os.path.join(S.CODINGS, f"{key} - {safe_token(master)}.json")
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                records = json.load(fh)
        except Exception:
            records = None
    else:
        records = None
    if not isinstance(records, list):
        records = json.loads(json.dumps(load_extraction(key) or []))
    rec = next((r for r in records if str(r.get("Study_ID") or "").strip() == study_id), None)
    if rec is None:
        return jsonify({"ok": False, "error": "study id not found"}), 400
    if fdef["multi"]:
        if isinstance(value, str):
            parts = [p.strip() for p in value.split("+")]
        else:
            parts = _values(value)
        clean = []
        for v in parts:
            c = _canon_level(v, fdef)
            if c and c not in clean:
                clean.append(c)
        newval = clean or None
    else:
        if value is None or str(value).strip() == "":
            newval = None
        else:
            newval = _canon_level(value, fdef) or None
            if fdef["ordered"] and newval is not None:
                try:
                    newval = int(newval)
                except (TypeError, ValueError):
                    pass
    if fdef["kind"] == "experiment":
        rec[field_name] = newval
    else:
        dvs = rec.get("DV_Metrics") or []
        # Prefer matching the aligned DV row by name; fall back to the index in
        # the Master's own file (which, when cloned from the canonical record,
        # matches the reference row the conflict was built from).
        target = None
        if dv_name:
            want = _norm_dv_name(dv_name)
            for j, dv in enumerate(dvs):
                if isinstance(dv, dict) and _norm_dv_name(dv.get("dv_name")) == want:
                    target = j
                    break
        if target is None:
            try:
                di = int(dv_index)
            except (TypeError, ValueError):
                di = -1
            if di < 0 or di >= len(dvs):
                return jsonify({"ok": False, "error": "dv_index is required for DV fields"}), 400
            target = di
        dvs[target][field_name] = newval
    fd, tmp = tempfile.mkstemp(suffix=".json", dir=S.TEMP)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(records, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        atomic_replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    # The decision is tracked in the resolution store (not inferred from the
    # coding file, which carries the value on every cell once cloned).
    stored = _field_value(newval, fdef)
    if case_id:
        record_resolution(master, key, case_id, field_name, stored)
    _invalidate_agreement()
    return jsonify({"ok": True, "path": os.path.relpath(path, S.ROOT).replace("\\", "/"),
                    "value": newval, "settled": bool(stored)})

@bp.get("/api/agreement/abstract/reliability")
def api_agreement_abstract_reliability():
    exclude = {x.strip() for x in (request.args.get("exclude") or "").split(",") if x.strip()}
    index = _abstract_agreement_index()
    maps = _abstract_case_maps(index)
    fields = []
    wsum = acc = 0.0
    for lab, name in abstract_fields():
        cmap = maps[lab]
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
    master = (request.args.get("master") or "").strip()
    field_filter = (request.args.get("field") or "").strip()
    study_filter = (request.args.get("study") or "").strip()
    status = (request.args.get("status") or "open").strip().lower()
    try:
        limit = max(1, min(2000, int(request.args.get("limit", 300))))
    except (TypeError, ValueError):
        limit = 300
    try:
        offset = max(0, int(request.args.get("offset", 0)))
    except (TypeError, ValueError):
        offset = 0
    title_of = {}
    try:
        for rec in (screening_index().get("rows") or []):
            title_of[str(rec.get("id"))] = rec.get("title") or str(rec.get("id"))
    except Exception:
        title_of = {}
    index = _abstract_agreement_index()
    maps = _abstract_case_maps(index)
    master_dec = load_screening_decisions(master) if master else {}
    rows, counts, studies = [], [], {}
    for lab, name in abstract_fields():
        if field_filter and lab != field_filter:
            continue
        open_n = settled_n = 0
        for uid, vals in maps[lab].items():
            if len(set(vals.values())) < 2:
                continue
            mval = (((master_dec.get(uid) or {}).get("criteria") or {}).get(lab) or {}).get("verdict", "")
            settled = bool(mval)
            if settled:
                settled_n += 1
            else:
                open_n += 1
            if status == "open" and mval:
                continue
            if status == "settled" and not mval:
                continue
            st = studies.setdefault(uid, {"key": uid, "label": title_of.get(uid, uid),
                                          "n_open": 0, "n_settled": 0, "fields": set()})
            if settled:
                st["n_settled"] += 1
            else:
                st["n_open"] += 1
            st["fields"].add(lab)
            if study_filter and uid != study_filter:
                continue
            rows.append({"key": uid, "study_id": uid, "dv_index": None, "field": lab,
                         "field_label": name, "group": "Abstract screening", "kind": "exp",
                         "multi": False, "levels": list(_ABSTRACT_CATS),
                         "labels": {v: v for v in _ABSTRACT_CATS},
                         "values": dict(vals), "master_value": mval or None,
                         "settled": bool(mval)})
        counts.append({"name": lab, "label": name, "kind": "exp",
                       "group": "Abstract screening", "multi": False,
                       "open": open_n, "settled": settled_n})
    total = len(rows)
    study_list = [{"key": k, "label": v["label"], "n_open": v["n_open"],
                   "n_settled": v["n_settled"], "n_fields": len(v["fields"])}
                  for k, v in studies.items()]
    study_list.sort(key=lambda s: (-s["n_open"], str(s["label"]).lower()))
    return jsonify({"total": total, "offset": offset, "limit": limit,
                    "rows": rows[offset:offset + limit], "fields": counts,
                    "studies": study_list, "study": study_filter,
                    "master": master, "raters": sorted(index.keys())})

@bp.post("/api/agreement/abstract/resolve")
def api_agreement_abstract_resolve():
    payload = request.get_json(force=True, silent=True) or {}
    master = (payload.get("master") or "").strip()
    uid = str(payload.get("key") or payload.get("study_id") or "").strip()
    lab = str(payload.get("field") or "").strip()
    value = payload.get("value")
    if not master:
        return jsonify({"ok": False, "error": "master name is required"}), 400
    if lab not in [l for l, _ in abstract_fields()]:
        return jsonify({"ok": False, "error": "unknown criterion"}), 400
    if not uid:
        return jsonify({"ok": False, "error": "record id is required"}), 400
    dec = load_screening_decisions(master)
    rec = dec.get(uid) or {"id": uid, "an": uid, "coder": master, "criteria": {},
                           "decision": "",
                           "saved_at": datetime.now().isoformat(timespec="seconds")}
    crits = rec.setdefault("criteria", {})
    entry = dict(crits.get(lab) or {})
    entry["verdict"] = "" if value in (None, "") else str(value).strip()
    crits[lab] = entry
    rec["decision"] = human_screen_decision(rec)
    rec["saved_at"] = datetime.now().isoformat(timespec="seconds")
    dec[uid] = rec
    save_screening_decisions(master, dec)
    return jsonify({"ok": True, "window": "abstract"})

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
