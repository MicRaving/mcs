# -*- coding: utf-8 -*-
"""Coverage routes: the harmonizer **triage screen** (interface §6 / plan §4 B3).

Serves the derived harmonization coverage products

* ``Results/harmonization_coverage_summary.csv`` -- one row per study with the
  per-slot rollup statuses, missing counts, blocking flags and the
  poolability-weighted missingness score;
* ``Results/harmonization_coverage.csv`` -- the per-``(study, slot[, variable])``
  matrix the chips and the assignment workspace read.

Endpoints
---------
GET /api/coverage                     ranked study list + per-slot chips + the
                                      readiness funnel + the assignment-audit
                                      counters (parity / conflicts / needs-veracity
                                      and the non-parity findings).
GET /api/coverage/study/<key>         the coverage matrix rows for one study plus
                                      the assignment targets (open coverage gaps +
                                      unmapped raw columns from ``variables.csv``).

The endpoint is read-only with respect to ``Datasets/``: it never writes a
dataset, only serves the products that ``Scripts/harmonization_coverage.py`` generated.
"""

from __future__ import annotations

from flask import Blueprint, abort, jsonify, request
import csv
import glob
import os
import re

from studio_state import S

bp = Blueprint("coverage", __name__)

# --- slot vocabulary (mirrors Scripts/harmonization_coverage.py; interface §6 is frozen) ----
SLOT_ORDER = ["S1", "S2", "S3", "S4", "S5", "S6", "S6cell", "S7", "S8",
              "S9", "S10", "S11"]
SLOT_NAMES = {
    "S1": "id", "S2": "condition_col", "S3": "arm_roles", "S4": "dv_pairs",
    "S5": "dv_domain", "S6": "veracity", "S6cell": "veracity_cell",
    "S7": "timepoint", "S8": "dv_polarity", "S9": "exclusions",
    "S10": "covariates", "S11": "provenance",
}
SLOT_WEIGHT = {
    "S1": 2.0, "S2": 5.0, "S3": 4.0, "S4": 2.0, "S5": 4.0, "S6": 5.0,
    "S6cell": 5.0, "S7": 1.0, "S8": 3.0, "S9": 2.0, "S10": 1.0, "S11": 1.0,
}

_FILTERS = ("all", "blocks_sdt", "blocks_did", "blocks_exclusions_on",
            "needs_na_verdict", "has_dataset", "paper_only")

_BLANK = {"", "nan", "none", "nat", "<na>"}


def _s(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if type(v).__name__ == "bool_":
        return "true" if bool(v) else "false"
    if v is None:
        return ""
    try:
        import pandas as pd
        if pd.isna(v):
            return ""
    except (TypeError, ValueError):
        pass
    s = str(v).strip()
    return "" if s.lower() in _BLANK else s


def _results_dir():
    return os.path.join(S.ROOT, "Results")


def _path(name):
    return os.path.join(_results_dir(), name)


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


# ---------------------------------------------------------------------------
# cached CSV reads (the matrix is ~5 MB; one load per product revision)
# ---------------------------------------------------------------------------
_CACHE = {}


def _read_csv_cached(path):
    import pandas as pd
    key = os.path.abspath(path)
    mt = _mtime(path)
    hit = _CACHE.get(key)
    if hit is not None and hit[0] == mt:
        return hit[1]
    if not os.path.isfile(path):
        _CACHE[key] = (mt, None)
        return None
    try:
        df = pd.read_csv(path, encoding="utf-8-sig", low_memory=False)
        df.columns = [str(c).strip().lstrip("\ufeff") for c in df.columns]
    except Exception:  # noqa: BLE001  (never take the app down)
        df = None
    _CACHE[key] = (mt, df)
    return df


# ---------------------------------------------------------------------------
# derived views over the matrix / summary
# ---------------------------------------------------------------------------
_DERIVED = {"stamp": None, "data": None}


def _derive():
    """Load + index the coverage products once per (summary, matrix) revision."""
    summary_path = _path("harmonization_coverage_summary.csv")
    matrix_path = _path("harmonization_coverage.csv")
    stamp = (_mtime(summary_path), _mtime(matrix_path))
    if _DERIVED["data"] is not None and _DERIVED["stamp"] == stamp:
        return _DERIVED["data"]

    summary = _read_csv_cached(summary_path)
    matrix = _read_csv_cached(matrix_path)

    slot_counts = {}
    open_by_study = {}
    if matrix is not None and len(matrix):
        cols = set(matrix.columns)
        if {"study_key", "slot", "status"} <= cols:
            g = matrix.groupby(["study_key", "slot", "status"], dropna=False).size()
            for (k, slot, st), n in g.items():
                k, slot, st = _s(k), _s(slot), _s(st)
                if not k or not slot:
                    continue
                slot_counts.setdefault(k, {}).setdefault(slot, {})
                slot_counts[k][slot][st] = slot_counts[k][slot].get(st, 0) + int(n)
            open_rows = matrix[matrix["status"].astype(str).str.strip().isin(
                ("missing", "ambiguous"))]
            for r in open_rows.to_dict("records"):
                k = _s(r.get("study_key"))
                if not k:
                    continue
                open_by_study.setdefault(k, []).append({
                    "slot": _s(r.get("slot")),
                    "scope": _s(r.get("scope")),
                    "target": _s(r.get("target")),
                    "status": _s(r.get("status")),
                    "evidence": _s(r.get("evidence")),
                    "auto_detail": _s(r.get("auto_detail")),
                })

    for k, rows in open_by_study.items():
        rows.sort(key=lambda d: (-SLOT_WEIGHT.get(d["slot"], 0.0),
                                 SLOT_ORDER.index(d["slot"]) if d["slot"] in SLOT_ORDER else 99,
                                 d["target"]))
    data = {"summary": summary, "matrix": matrix, "slot_counts": slot_counts,
            "open_by_study": open_by_study}
    _DERIVED.update(stamp=stamp, data=data)
    return data


def _summary_records():
    d = _derive()
    s = d["summary"]
    if s is None or not len(s):
        return []
    out = []
    for _, r in s.iterrows():
        rec = {c: r.get(c) for c in s.columns}
        out.append(rec)
    return out


def _audit_block():
    """Aggregate ``Results/assignment_audit.csv`` into counters + findings."""
    df = _read_csv_cached(_path("assignment_audit.csv"))
    checks, findings = [], []
    if df is not None and len(df):
        for chk, sub in df.groupby("check"):
            ok = sub["ok"].map(lambda v: str(v).strip().lower() in ("true", "1", "yes")).sum()
            bad = len(sub) - int(ok)
            checks.append({"check": str(chk), "ok": int(ok), "bad": int(bad),
                           "n": int(sub["n"].sum())})
            for _, r in sub.iterrows():
                if str(r.get("ok")).strip().lower() not in ("true", "1", "yes"):
                    findings.append({"study_key": _s(r.get("study_key")),
                                     "check": str(chk), "n": int(r.get("n") or 0),
                                     "detail": _s(r.get("detail"))})
    checks.sort(key=lambda c: c["check"])
    parity = next((c for c in checks if c["check"] == "parity"), {"ok": 0, "bad": 0, "n": 0})
    return {"checks": checks, "parity": parity, "findings": findings,
            "conflicts": _count_conflicts(),
            "needs_veracity": _count_needs_veracity()}


def _count_conflicts():
    """Assignment rows stamped ``conflict`` across every study file."""
    n = 0
    for p in glob.glob(os.path.join(S.DATASETS or "", "*",
                                    "* - variable_assignments.csv")):
        try:
            import pandas as pd
            df = pd.read_csv(p, encoding="utf-8-sig", low_memory=False)
            if "status" in df.columns:
                n += int(df["status"].astype(str).str.strip().str.lower().eq("conflict").sum())
        except Exception:  # noqa: BLE001
            continue
    return n


def _count_needs_veracity():
    """Studies whose sidecar still carries a blank veracity (S6 missing)."""
    d = _derive()
    s = d["summary"]
    if s is None or "S6_missing" not in s.columns:
        return 0
    try:
        return int((s["S6_missing"].fillna(0).astype(float) > 0).sum())
    except Exception:  # noqa: BLE001
        return 0


def _paper_keys():
    return set(getattr(S, "KEYS", set()) or set())


def _study_record(rec, slot_counts, open_by_study, paper_keys):
    key = _s(rec.get("study_key"))
    out = {"study_key": key, "has_dataset": True,
           "is_paper": key in paper_keys}
    for c in ("n_dv_rows", "n_dv_cols", "missingness_score",
              "blocks_sdt", "blocks_did", "blocks_exclusions_on",
              "needs_na_verdict"):
        v = rec.get(c)
        try:
            if v is not None and str(v) != "":
                f = float(v)
                out[c] = int(f) if f == int(f) else f
            else:
                out[c] = None
        except (TypeError, ValueError):
            out[c] = v
    for slot in SLOT_ORDER:
        out[slot] = _s(rec.get(slot))
        try:
            out[slot + "_missing"] = int(float(rec.get(slot + "_missing") or 0))
        except (TypeError, ValueError):
            out[slot + "_missing"] = 0
    counts = slot_counts.get(key, {})
    out["slot_counts"] = {slot: dict(counts.get(slot, {})) for slot in SLOT_ORDER}
    opens = list(open_by_study.get(key, []))
    out["open_slots"] = opens
    out["first_open_slot"] = opens[0] if opens else None
    return out


# ---------------------------------------------------------------------------
# endpoints
# ---------------------------------------------------------------------------
@bp.get("/api/coverage")
def api_coverage():
    d = _derive()
    summary = d["summary"]
    paper_keys = _paper_keys()
    studies = []
    if summary is not None and len(summary):
        for rec in summary.to_dict("records"):
            studies.append(_study_record(rec, d["slot_counts"], d["open_by_study"],
                                         paper_keys))
    have = {s["study_key"] for s in studies}
    for k in sorted(paper_keys - have):
        rec = {"study_key": k, "missingness_score": None, "blocks_sdt": None,
               "blocks_did": None, "blocks_exclusions_on": None,
               "needs_na_verdict": None, "n_dv_rows": 0, "n_dv_cols": 0}
        for slot in SLOT_ORDER:
            rec[slot] = ""
            rec[slot + "_missing"] = 0
        studies.append(_study_record(rec, {}, {}, paper_keys) | {"has_dataset": False})

    # readiness funnel (true+false cell -> + condition column -> + explicit roles)
    def _filled(slot):
        return {s["study_key"] for s in studies
                if s.get("has_dataset") and s.get(slot) == "filled"}

    cell = _filled("S6cell")
    cond = _filled("S2")
    roles = _filled("S3")
    funnel = {"true_false_cell": len(cell), "with_condition": len(cell & cond),
              "explicit_roles": len(cell & cond & roles),
              "harmonized": len(have), "papers": len(paper_keys)}

    # filters
    flt = (request.args.get("filter") or "all").strip().lower()
    if flt not in _FILTERS:
        flt = "all"
    q = (request.args.get("q") or "").strip().lower()
    sort = (request.args.get("sort") or "missingness_score").strip()
    sort_key = sort[1:] if sort.startswith("-") else sort
    direction = (request.args.get("dir") or "").strip().lower()
    if direction not in ("asc", "desc"):
        direction = "desc" if sort_key in (
            "missingness_score", "blocks_sdt", "blocks_did",
            "blocks_exclusions_on", "needs_na_verdict",
            "n_dv_rows", "n_dv_cols") else "asc"

    def _keep(s):
        if flt == "paper_only":
            return not s["has_dataset"]
        if not s["has_dataset"]:
            return False
        if flt == "blocks_sdt":
            return s.get("blocks_sdt") == 1
        if flt == "blocks_did":
            return s.get("blocks_did") == 1
        if flt == "blocks_exclusions_on":
            return s.get("blocks_exclusions_on") == 1
        if flt == "needs_na_verdict":
            return s.get("needs_na_verdict") == 1
        return True

    studies = [s for s in studies if _keep(s) and (not q or q in s["study_key"].lower())]

    def _val(s):
        v = s.get(sort_key)
        return None if v in (None, "") else v

    have = [s for s in studies if _val(s) is not None]
    miss = [s for s in studies if _val(s) is None]

    def _key(s):
        v = _val(s)
        return float(v) if isinstance(v, (int, float)) else str(v).lower()

    try:
        have.sort(key=_key, reverse=(direction == "desc"))
    except TypeError:
        have.sort(key=lambda s: str(_val(s)).lower(), reverse=(direction == "desc"))
    studies = have + miss

    counts = {"total": len(studies), "has_dataset": sum(1 for s in studies if s["has_dataset"]),
              "paper_only": sum(1 for s in studies if not s["has_dataset"]),
              "blocks_sdt": sum(1 for s in studies if s.get("blocks_sdt") == 1),
              "blocks_did": sum(1 for s in studies if s.get("blocks_did") == 1),
              "blocks_exclusions_on": sum(1 for s in studies if s.get("blocks_exclusions_on") == 1),
              "needs_na_verdict": sum(1 for s in studies if s.get("needs_na_verdict") == 1)}

    return jsonify({
        "generated_at": _iso(_mtime(_path("harmonization_coverage_summary.csv"))),
        "slots": [{"code": s, "name": SLOT_NAMES[s], "weight": SLOT_WEIGHT[s]}
                  for s in SLOT_ORDER],
        "funnel": funnel,
        "counts": counts,
        "filter": flt,
        "sort": sort_key,
        "dir": direction,
        "audit": _audit_block(),
        "studies": studies,
        "filters": list(_FILTERS),
    })


@bp.get("/api/coverage/study/<path:key>")
def api_coverage_study(key):
    d = _derive()
    if d["matrix"] is None:
        return jsonify({"key": key, "rows": [], "targets": [],
                        "warning": "harmonization_coverage.csv not found — run "
                                   "python Scripts/coverage.py"})
    m = d["matrix"]
    rows = m[m["study_key"].astype(str) == key]
    out = [{c: _s(r.get(c)) for c in m.columns} for _, r in rows.iterrows()]
    targets = _assignment_targets(key, d)
    return jsonify({"key": key, "rows": out, "targets": targets,
                    "open_slots": d["open_by_study"].get(key, [])})


def _assignment_targets(key, derived):
    """Open coverage gaps + unmapped raw columns for the workspace's (L) pane."""
    targets = []
    seen = set()

    # 1) open coverage gaps (per-variable slots point at a sidecar uniform name)
    for gap in derived["open_by_study"].get(key, []):
        t = {"kind": "coverage", "slot": gap["slot"], "scope": gap["scope"],
             "target": gap["target"], "status": gap["status"],
             "evidence": gap["evidence"], "detail": gap["auto_detail"],
             "uniform_variable": "", "original_variable": "", "artifact": "",
             "item_scope": "", "keyable": True}
        if gap["scope"] == "variable":
            t["uniform_variable"] = gap["target"]
        targets.append(t)

    # 2) unmapped raw columns from the study's variables.csv
    folder = os.path.join(S.DATASETS or "", key)
    var_path = os.path.join(folder, f"{key} - variables.csv")
    orig_seen = set()
    if os.path.isfile(var_path):
        try:
            import pandas as pd
            v = pd.read_csv(var_path, encoding="utf-8-sig", low_memory=False)
            v.columns = [str(c).strip().lstrip("\ufeff") for c in v.columns]
            ucol = "uniform_variable" if "uniform_variable" in v.columns else (
                "uniform_name" if "uniform_name" in v.columns else None)
            for _, r in v.iterrows():
                orig = _s(r.get("original_variable"))
                if orig:
                    orig_seen.add(orig)
                role = _s(r.get("role")).lower()
                uni = _s(r.get(ucol)) if ucol else ""
                if role not in ("other", "unmapped") or not orig:
                    continue
                t = {"kind": "raw", "slot": "", "scope": "variable",
                     "target": orig, "status": "unmapped", "evidence": f"variables.csv:{orig}",
                     "detail": _s(r.get("notes")) or f"role={role}",
                     "uniform_variable": uni, "original_variable": orig,
                     "artifact": "", "item_scope": "",
                     "original_values": _s(r.get("original_values")),
                     "mapped_values": _s(r.get("mapped_values")),
                     "keyable": True}
                targets.append(t)
        except Exception:  # noqa: BLE001
            pass

    # 3) raw headers not present in variables.csv
    for p in sorted(glob.glob(os.path.join(folder, f"{key} - prepared*.csv"))):
        try:
            with open(p, encoding="utf-8-sig", newline="") as fh:
                hdr = [str(c).strip().lstrip("\ufeff") for c in next(csv.reader(fh))]
        except (OSError, StopIteration):
            continue
        art = "main"
        m = re.search(r" - prepared (\d+|[A-Za-z0-9]+)\.csv$", os.path.basename(p))
        if m:
            art = m.group(1)
        for c in hdr:
            if not c or c in orig_seen or c.lower() in ("pid", "study_key", "artifact"):
                continue
            t = {"kind": "raw", "slot": "", "scope": "variable", "target": c,
                 "status": "unmapped", "evidence": f"prepared:{art}",
                 "detail": "raw column with no variables.csv row",
                 "uniform_variable": "", "original_variable": c, "artifact": art,
                 "item_scope": "", "keyable": True}
            targets.append(t)

    # de-dup on (kind, original/target, slot)
    out = []
    for t in targets:
        k = (t["kind"], t.get("original_variable") or t["target"], t["slot"])
        if k in seen:
            continue
        seen.add(k)
        out.append(t)
    return out


def _iso(mt):
    if mt is None:
        return None
    from datetime import datetime
    return datetime.fromtimestamp(mt).isoformat(timespec="seconds")
