"""Summary routes: schema dimensions, observed levels, variables and summary edits."""

from flask import Blueprint, jsonify, request
import json
import os
import tempfile
from studio_lib import _CODING_INDEX, _field_by_name, _field_options, _norm, _observed_levels, _values, atomic_replace, coding_index, load_extraction, safe_token, validate_records
from studio_state import S

bp = Blueprint("summary", __name__)


@bp.get("/api/summary/dimensions")
def api_summary_dimensions():
    data = coding_index()
    out = []
    for kind, fields in (("experiment", data["common_fields"]), ("dv", data["dv_fields"])):
        for f in fields:
            n = len(_observed_levels(
                data["experiments"] if kind == "experiment" else data["dvs"], [f["name"]]).get(f["name"], []))
            out.append({"dim": f["name"], "label": f.get("label") or f["name"],
                        "group": ("DV metrics" if kind == "dv" else (f.get("group") or "Other")),
                        "kind": kind, "n_levels": n})
    group_order = []
    for x in out:
        if x["group"] not in group_order:
            group_order.append(x["group"])
    return jsonify({"total_experiments": len(data["experiments"]),
                    "total_dvs": len(data["dvs"]),
                    "total_studies": len({r["study_key"] for r in data["experiments"]}),
                    "n_fields": len(out), "dimensions": out, "group_order": group_order})

@bp.get("/api/summary/levels")
def api_summary_levels():
    dim = (request.args.get("dim") or "").strip()
    data = coding_index()
    field, kind = _field_by_name(dim)
    if field is None:
        return jsonify({"dim": dim, "label": dim, "kind": "", "n_levels": 0, "levels": []})
    rows = data["experiments"] if kind == "experiment" else data["dvs"]
    agg = {}
    for r in rows:
        for val in _values(r.get(field["name"])):
            a = agg.setdefault(val, {"value": val, "n_records": 0, "studies": set()})
            a["n_records"] += 1
            a["studies"].add(r["study_key"])
    labels = {_norm(o.get("value")): o.get("label") for o in (field.get("options") or [])}
    levels = [{"value": k, "label": labels.get(k) or (None if k == "" else k),
               "n_records": v["n_records"], "n_studies": len(v["studies"])}
              for k, v in agg.items()]
    levels.sort(key=lambda x: (-x["n_records"], str(x["value"])))
    return jsonify({"dim": field["name"], "label": field.get("label") or field["name"],
                    "kind": kind, "n_levels": len(levels), "levels": levels})

@bp.get("/api/summary/variables")
def api_summary_variables():
    dim = (request.args.get("dim") or "").strip()
    value = request.args.get("value")  # None = no level filter; "" = the blank level
    q = (request.args.get("q") or "").strip().lower()
    data = coding_index()
    field, kind = _field_by_name(dim)
    if dim == "":
        rows, kind = data["experiments"], "experiment"
    elif field is None:
        rows = []
    else:
        rows = data["experiments"] if kind == "experiment" else data["dvs"]
        if value is not None:
            target = _norm(value)
            rows = [r for r in rows if target in _values(r.get(dim))]
    if q:
        rows = [r for r in rows
                if q in str(r["study_key"]).lower()
                or q in str(r.get("experiment_id") or "").lower()
                or q in str(r.get("dv_name") or "").lower()]
    total = len(rows)
    try:
        limit = max(1, min(4000, int(request.args.get("limit", 3000))))
    except (TypeError, ValueError):
        limit = 3000
    fields = (data["common_fields"] if kind == "experiment" else data["dv_fields"]) if kind else []
    observed = (data["common_observed"] if kind == "experiment" else data["dv_observed"]) if kind else {}
    meta = [{"name": f["name"], "label": f.get("label") or f["name"],
             "group": f.get("group") or "",
             "options": _field_options(f, observed.get(f["name"], []))} for f in fields]
    return jsonify({"total": total, "rows": rows[:limit], "truncated": total > limit,
                    "dim": dim, "value": value, "kind": kind, "fields": meta,
                    "n_studies": len({r["study_key"] for r in rows})})

@bp.post("/api/summary/save")
def api_summary_save():
    payload = request.get_json(force=True, silent=True) or {}
    coder = payload.get("coder")
    edits = payload.get("edits")
    if not coder or not str(coder).strip():
        return jsonify({"ok": False, "error": "coder name is required"}), 400
    if not isinstance(edits, dict):
        return jsonify({"ok": False, "error": "no edits to save"}), 400
    data = coding_index()
    common_names, dv_names = data["common_names"], data["dv_names"]
    saved = n_fields = 0
    warnings = []
    for key, byexp in edits.items():
        if key not in S.KEYS or not isinstance(byexp, dict):
            continue
        target = os.path.join(S.CODINGS, f"{key} - {safe_token(coder)}.json")
        records = None
        if os.path.exists(target):
            try:
                with open(target, encoding="utf-8") as fh:
                    records = json.load(fh)
            except Exception:
                records = None
        if not isinstance(records, list):
            records = json.loads(json.dumps(load_extraction(key) or []))
        index = {str(r.get("Study_ID")): r for r in records if isinstance(r, dict)}
        for exp_id, fields in byexp.items():
            rec = index.get(str(exp_id))
            if rec is None or not isinstance(fields, dict):
                continue
            for f, v in fields.items():
                if f in common_names:
                    rec[f] = v
                    n_fields += 1
                elif f == "__dvs" and isinstance(v, dict):
                    dvs = rec.get("DV_Metrics") or []
                    for i, dvf in v.items():
                        try:
                            di = int(i)
                        except (TypeError, ValueError):
                            continue
                        if 0 <= di < len(dvs) and isinstance(dvf, dict):
                            for df, dv in dvf.items():
                                if df in dv_names:
                                    dvs[di][df] = dv
                                    n_fields += 1
        os.makedirs(S.CODINGS, exist_ok=True)
        fd, tmp = tempfile.mkstemp(suffix=".json", dir=S.TEMP)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(records, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
            atomic_replace(tmp, target)
            saved += 1
            # M3 (audit): warn (never block) on codings that would fail B_validate.
            try:
                warnings.extend(f"{key}: {w}" for w in validate_records(records))
            except Exception:
                pass
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
    _CODING_INDEX["data"] = None
    return jsonify({"ok": True, "studies": saved, "fields": n_fields,
                    "warnings": warnings})
