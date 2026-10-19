# -*- coding: utf-8 -*-
"""Harmonization **coverage matrix** (interface §6).

Derives, for every harmonized study, the status of the 11 essential slots the
analysis layer actually needs, so coders can see what is missing and triage it:

    S1 id              study_key + artifact + participant id (`pid`)
    S2 condition_col   a `condition` / `condition_raw` column
    S3 arm_roles       an explicit `<Key> - conditions.csv` role map
    S4 dv_pairs        every `dv_*` data column has its `_01` 0-1 twin
    S5 dv_domain       a controlled `dv_domain` on every sidecar DV row
    S6 veracity        a true/false label (+ >=1 true and >=1 false per cell)
    S7 timepoint       a timepoint (blank = posttest by convention)
    S8 dv_polarity     an orientation on every sidecar DV row
    S9 exclusions      `<Key> - exclusions.csv` rules for the study's `flag_*`
    S10 covariates     at least one participant `cov_*` column
    S11 provenance     a `<Key> - variables.csv` with `original_variable`s

Products
--------
* ``Results/harmonization_coverage.csv`` -- one row per ``(study, slot[, variable])``
  with ``status in {filled, missing, ambiguous, na, confirmed}`` and an
  ``evidence`` pointer.
* ``Results/harmonization_coverage_summary.csv`` -- one row per study: per-slot
  rollup statuses, missing counts, blocking flags and a **missingness score
  weighted by poolability impact**.

Machine statuses are only ``filled``/``missing``/``ambiguous``; ``na`` and
``confirmed`` come from coder assignments (:mod:`Scripts.variable_assignments`)
-- ``na`` requires a coder verdict (B1) and a confirmed assignment stamps the
slot ``confirmed``.  ``ambiguous`` means "needs a human decision" (e.g. a blank
veracity on a composite/difference DV, or a blank timepoint under the
posttest-by-convention rule).

Run:  python Scripts/harmonization_coverage.py
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

try:
    import contract as _contract
    HAS_CONTRACT = True
except Exception:  # noqa: BLE001  (authored by the parallel workstream)
    _contract = None
    HAS_CONTRACT = False

try:
    import dv_polarity as _dvp
except Exception:  # noqa: BLE001
    _dvp = None

try:
    import variable_assignments as _va
except Exception:  # noqa: BLE001
    _va = None

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

DS = os.path.join(ROOT, "Datasets")
EXTRACTION = os.path.join(ROOT, "Extraction")
RESULTS = os.path.join(ROOT, "Results")

SLOT_ORDER = ["S1", "S2", "S3", "S4", "S5", "S6", "S6cell", "S7", "S8",
              "S9", "S10", "S11"]
SLOT_NAMES = {
    "S1": "id", "S2": "condition_col", "S3": "arm_roles", "S4": "dv_pairs",
    "S5": "dv_domain", "S6": "veracity", "S6cell": "veracity_cell",
    "S7": "timepoint", "S8": "dv_polarity", "S9": "exclusions",
    "S10": "covariates", "S11": "provenance",
}
SLOT_CODES = set(SLOT_ORDER)

#: poolability-impact weight per slot (missing veracity on susceptibility DVs >
#: missing control roles > missing polarity > missing exclusion rules > provenance)
SLOT_WEIGHT = {
    "S1": 2.0, "S2": 5.0, "S3": 4.0, "S4": 2.0, "S5": 4.0, "S6": 5.0,
    "S6cell": 5.0, "S7": 1.0, "S8": 3.0, "S9": 2.0, "S10": 1.0, "S11": 1.0,
}

_BLANK = {"", "nan", "none", "nat", "<na>"}


def _s(v):
    # pandas parses a `veracity` column of "true"/"false" into booleans
    if isinstance(v, bool):
        return "true" if v else "false"
    if type(v).__name__ == "bool_":
        return "true" if bool(v) else "false"
    if v is None:
        return ""
    try:
        if pd.isna(v):
            return ""
    except (TypeError, ValueError):
        pass
    s = str(v).strip()
    return "" if s.lower() in _BLANK else s


def _wide_cols(path):
    """Column names of the wide CSV (header only -- cheap)."""
    if not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8-sig", newline="") as fh:
            row = next(csv.reader(fh))
        return [str(c).strip().lstrip("\ufeff") for c in row]
    except (OSError, StopIteration):
        return []


def _read(path):
    df = pd.read_csv(path, encoding="utf-8-sig", low_memory=False)
    df.columns = [str(c).strip().lstrip("\ufeff") for c in df.columns]
    return df


def _is_composite(name, notes=""):
    if _dvp is not None:
        try:
            return bool(_dvp.is_composite(name, notes))
        except Exception:  # noqa: BLE001
            pass
    return bool(re.search(r"discern|difference|\bdiff\b|minus|\bnet\b|d_prime",
                          str(name or ""), re.I))


def _extraction_records(key):
    p = os.path.join(EXTRACTION, f"{key}.json")
    if not os.path.isfile(p):
        return []
    try:
        j = json.load(open(p, encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if isinstance(j, list):
        return [r for r in j if isinstance(r, dict)]
    return [j] if isinstance(j, dict) else []


def _assignment_index(key, folder):
    """raw key -> {field: (value,status,coder,decided_at,evidence)} plus flags."""
    out = {}
    slot_fixes = set()
    if _va is None:
        return out, slot_fixes
    try:
        a = _va.read_verified(key, folder)
    except Exception:  # noqa: BLE001
        return out, slot_fixes
    if a is None or a.empty:
        return out, slot_fixes
    for _, r in a.iterrows():
        st = _s(r.get("status")).lower()
        if st != "confirmed":
            continue
        k = _va._norm_key(r.get("artifact"), r.get("original_variable"),
                          r.get("item_scope"))
        d = out.setdefault(k, {})
        for f in _va.LABEL_FIELDS:
            v = _s(r.get(f))
            if v:
                d[f] = (v, "confirmed", _s(r.get("coder")), _s(r.get("decided_at")),
                        _s(r.get("evidence")))
        if _va._is_na_row(r):
            d["_na"] = ("na", "na", _s(r.get("coder")), _s(r.get("decided_at")),
                        _s(r.get("evidence")))
        sf = _s(r.get("slot_fix")).upper()
        if sf in SLOT_CODES:
            slot_fixes.add(sf)
    return out, slot_fixes


def _lookup(idx, artifact, original, item_scope=""):
    if not original:
        return None
    for (a, o, it), d in idx.items():
        if o != original:
            continue
        if item_scope and it and it != item_scope:
            continue
        if a != "*" and artifact != "*" and a != artifact:
            continue
        return d
    return None


def _side_key_cols(side):
    for c in ("uniform_variable", "role", "dv_domain", "veracity", "timepoint",
              "dv_polarity", "original_variable", "notes"):
        if c not in side.columns:
            side[c] = ""
    return side


def _row(study_key, slot, scope, target, status, evidence="", auto_detail="",
         coder="", decided_at=""):
    return {"study_key": study_key, "slot": slot, "scope": scope,
            "target": target, "status": status, "evidence": evidence,
            "auto_detail": auto_detail, "coder": coder, "decided_at": decided_at}


def coverage_for_study(key, folder):
    """Return (list_of_matrix_rows, summary_dict) for one study."""
    rows = []
    side_path = os.path.join(folder, f"{key} - harmonized_variables.csv")
    wide_path = os.path.join(folder, f"{key} - harmonized.csv")
    var_path = os.path.join(folder, f"{key} - variables.csv")
    excl_path = os.path.join(folder, f"{key} - exclusions.csv")
    cond_path = os.path.join(folder, f"{key} - conditions.csv")
    dir_path = os.path.join(folder, f"{key} - dv_direction.csv")

    # legacy per-DV direction (include/direction) -- context for a blank polarity
    dir_map = {}
    if os.path.isfile(dir_path):
        try:
            dd = _read(dir_path)
            if "uniform_variable" in dd.columns:
                for _, r in dd.iterrows():
                    d = _s(r.get("direction"))
                    if d:
                        dir_map.setdefault(_s(r.get("uniform_variable")), d)
        except Exception:  # noqa: BLE001
            dir_map = {}

    side = _side_key_cols(_read(side_path)) if os.path.isfile(side_path) else pd.DataFrame()
    wide_cols = _wide_cols(wide_path)
    idx, slot_fixes = _assignment_index(key, folder)

    def _confirm(slot):
        return slot in slot_fixes

    def _push(slot, scope, target, status, evidence="", auto_detail="",
              coder="", decided_at=""):
        if slot in slot_fixes and status != "na":
            status, evidence, coder, decided_at = "confirmed", "assignments", coder, decided_at
        rows.append(_row(key, slot, scope, target, status, evidence,
                         auto_detail, coder, decided_at))

    # -- S1 id -------------------------------------------------------------
    has_pid = "pid" in wide_cols
    _push("S1", "study", "pid", "filled" if has_pid else "missing",
          "wide:pid" if has_pid else "wide:(absent)",
          "" if has_pid else "no participant id column -> paired d' unavailable")

    # -- S2 condition column ----------------------------------------------
    cond_cols = [c for c in ("condition", "condition_raw") if c in wide_cols]
    _push("S2", "study", "condition|condition_raw",
          "filled" if cond_cols else "missing",
          "wide:" + ",".join(cond_cols) if cond_cols else "wide:(absent)",
          "" if cond_cols else "no condition column -> no arms")

    # -- S3 arm roles ------------------------------------------------------
    s3_status, s3_ev, s3_detail = "missing", "conditions.csv:(absent)", ""
    if os.path.isfile(cond_path):
        try:
            cm = _read(cond_path)
        except Exception:  # noqa: BLE001
            cm = pd.DataFrame()
        if "role" in cm.columns and len(cm) and cm["role"].map(_s).ne("").any():
            s3_status = "filled"
            s3_ev = f"conditions.csv:{len(cm)} rows"
        else:
            s3_status = "ambiguous"
            s3_ev = "conditions.csv:(no role)"
            s3_detail = "conditions.csv present but carries no usable role mapping"
    else:
        s3_detail = "no explicit control/intervention role map -> flagged heuristic"
    _push("S3", "study", "conditions.csv", s3_status, s3_ev, s3_detail)

    # -- S4 dv pairs -------------------------------------------------------
    dv_cols = [c for c in wide_cols if str(c).startswith("dv_") and not str(c).endswith("_01")]
    if not wide_cols:
        _push("S4", "study", "dv_*_01", "missing", "wide:(absent)",
              "no wide table")
    else:
        for c in dv_cols:
            twin = c + "_01"
            if twin in wide_cols:
                _push("S4", "variable", c, "filled", f"wide:{twin}")
            else:
                _push("S4", "variable", c, "missing", f"wide:{c}",
                      "no _01 0-1 twin")
    if not dv_cols and wide_cols:
        _push("S4", "study", "dv_*_01", "missing", "wide:(no dv_*)",
              "wide has no dv_* column")

    # -- per-DV sidecar slots S5-S8 ---------------------------------------
    isdv = side["uniform_variable"].map(lambda v: str(v).startswith("dv_")) if len(side) else pd.Series([], dtype=bool)
    dvside = side[isdv] if len(side) else side
    explicit_timepoints = set()
    if len(dvside):
        explicit_timepoints = {_s(v) for v in dvside["timepoint"] if _s(v)}
    multi_wave = len(explicit_timepoints) >= 2

    ver_true = ver_false = 0
    for pos, (_, r) in enumerate(dvside.iterrows()):
        uv = _s(r.get("uniform_variable"))
        orig = _s(r.get("original_variable"))
        notes = _s(r.get("notes"))
        art = _s(r.get("artifact")) if "artifact" in side.columns else "*"
        a = _lookup(idx, _va._norm_artifact(art) if _va else art, orig,
                    _s(r.get("item_scope")) if "item_scope" in side.columns else "")
        a = a or {}
        ev_row = f"sidecar:{pos}"
        ver = _s(r.get("veracity"))
        dom = _s(r.get("dv_domain"))
        tp = _s(r.get("timepoint"))
        pol = _s(r.get("dv_polarity"))

        # S5 dv_domain
        if "dv_domain" in a:
            _push("S5", "variable", uv, "confirmed", "assignments:" + orig,
                  coder=a["dv_domain"][2], decided_at=a["dv_domain"][3])
        elif "_na" in a:
            _push("S5", "variable", uv, "na", "assignments:" + orig,
                  coder=a["_na"][2], decided_at=a["_na"][3])
        elif dom:
            _push("S5", "variable", uv, "filled", ev_row)
        else:
            _push("S5", "variable", uv, "missing", ev_row, "blank dv_domain")

        # S6 veracity
        if "veracity" in a:
            _push("S6", "variable", uv, "confirmed", "assignments:" + orig,
                  coder=a["veracity"][2], decided_at=a["veracity"][3])
        elif "_na" in a:
            _push("S6", "variable", uv, "na", "assignments:" + orig,
                  coder=a["_na"][2], decided_at=a["_na"][3])
        elif ver:
            _push("S6", "variable", uv, "filled", ev_row)
        elif _is_composite(uv, notes):
            _push("S6", "variable", uv, "ambiguous", ev_row,
                  "composite/difference DV carries no stimulus veracity; "
                  "needs a coder N/A verdict")
        else:
            _push("S6", "variable", uv, "missing", ev_row, "blank veracity")
        if ver.lower() == "true":
            ver_true += 1
        elif ver.lower() == "false":
            ver_false += 1

        # S7 timepoint
        if "timepoint" in a:
            _push("S7", "variable", uv, "confirmed", "assignments:" + orig,
                  coder=a["timepoint"][2], decided_at=a["timepoint"][3])
        elif "_na" in a:
            _push("S7", "variable", uv, "na", "assignments:" + orig,
                  coder=a["_na"][2], decided_at=a["_na"][3])
        elif tp:
            _push("S7", "variable", uv, "filled", ev_row)
        elif multi_wave:
            _push("S7", "variable", uv, "missing", ev_row,
                  "multi-wave study needs an explicit timepoint")
        else:
            _push("S7", "variable", uv, "ambiguous", ev_row,
                  "blank timepoint -> posttest by convention; needs a coder verdict "
                  "only for multi-wave studies")

        # S8 dv_polarity
        if "dv_polarity" in a:
            _push("S8", "variable", uv, "confirmed", "assignments:" + orig,
                  coder=a["dv_polarity"][2], decided_at=a["dv_polarity"][3])
        elif "_na" in a:
            _push("S8", "variable", uv, "na", "assignments:" + orig,
                  coder=a["_na"][2], decided_at=a["_na"][3])
        elif pol:
            _push("S8", "variable", uv, "filled", ev_row)
        else:
            det = "blank dv_polarity"
            if uv in dir_map:
                det += f"; dv_direction.csv direction={dir_map[uv]} (legacy, not a polarity label)"
            _push("S8", "variable", uv, "missing", ev_row, det)

    # -- S6 cell (>=1 true + >=1 false) -----------------------------------
    if ver_true and ver_false:
        _push("S6cell", "study", "true+false cell", "filled",
              f"sidecar:true={ver_true},false={ver_false}")
    elif ver_true or ver_false:
        _push("S6cell", "study", "true+false cell", "ambiguous",
              f"sidecar:true={ver_true},false={ver_false}",
              "only one veracity present -> no SDT discrimination")
    else:
        _push("S6cell", "study", "true+false cell", "missing",
              "sidecar:(no veracity)", "no true/false veracity labels")

    # -- S9 exclusions -----------------------------------------------------
    n_flags = sum(1 for c in wide_cols if str(c).startswith("flag_"))
    n_rules = 0
    if os.path.isfile(excl_path):
        try:
            ex = _read(excl_path)
            n_rules = len(ex)
        except Exception:  # noqa: BLE001
            n_rules = 0
    if n_rules:
        _push("S9", "study", "exclusions.csv", "filled",
              f"exclusions.csv:{n_rules} rules")
    elif n_flags:
        _push("S9", "study", "exclusions.csv", "missing",
              f"wide:flag_* x{n_flags}", "flag_* columns but no exclusion rules")
    else:
        _push("S9", "study", "exclusions.csv", "na", "wide:(no flag_*)",
              "no flag_* columns -> no author exclusion check to encode")

    # -- S10 covariates ----------------------------------------------------
    cov_cols = [c for c in wide_cols if str(c).startswith("cov_") and not str(c).endswith("_01")]
    cov_declared = any(_s(c.get("covariate")) for rec in _extraction_records(key)
                       for c in (rec.get("Covariates") or []) if isinstance(c, dict))
    if cov_cols:
        _push("S10", "study", "cov_*", "filled", f"wide:{len(cov_cols)} cov columns")
    elif cov_declared:
        _push("S10", "study", "cov_*", "missing", "Extraction:Covariates",
              "coded covariates but no cov_* column in the wide")
    else:
        _push("S10", "study", "cov_*", "na", "Extraction:Covariates:(empty)",
              "study has no participant covariates")

    # -- S11 provenance ----------------------------------------------------
    if os.path.isfile(var_path):
        try:
            v = _read(var_path)
            orig_ok = "original_variable" in v.columns and \
                v["original_variable"].map(_s).ne("").any()
        except Exception:  # noqa: BLE001
            orig_ok = False
        if orig_ok:
            _push("S11", "study", "variables.csv", "filled", f"variables.csv:{len(v)} rows")
        else:
            _push("S11", "study", "variables.csv", "ambiguous", "variables.csv",
                  "variables.csv present but no non-blank original_variable")
    else:
        _push("S11", "study", "variables.csv", "missing", "variables.csv:(absent)",
              "no provenance variable map")

    summary = _summarize(key, rows, len(dvside), len(dv_cols))
    return rows, summary


def _rollup(statuses):
    st = {s for s in statuses if s}
    if not st:
        return "na"
    if st <= {"filled", "confirmed", "na"}:
        return "filled"
    if st == {"missing"}:
        return "missing"
    if "missing" in st and not (st & {"filled", "confirmed"}):
        return "missing"
    return "partial"


def _severity(statuses):
    if not statuses:
        return 0.0
    vals = []
    for s in statuses:
        vals.append({"missing": 1.0, "ambiguous": 0.5}.get(s, 0.0))
    return sum(vals) / len(vals)


def _summarize(key, rows, n_dv_rows, n_dv_cols):
    by_slot = {}
    for r in rows:
        by_slot.setdefault(r["slot"], []).append(r["status"])
    out = {"study_key": key, "n_dv_rows": n_dv_rows, "n_dv_cols": n_dv_cols}
    score = 0.0
    for slot in SLOT_ORDER:
        sts = by_slot.get(slot, [])
        out[slot] = _rollup(sts)
        out[f"{slot}_missing"] = sum(1 for s in sts if s == "missing")
        score += SLOT_WEIGHT[slot] * _severity(sts)
    out["missingness_score"] = round(score, 2)
    # blocking flags
    s6_states = set(by_slot.get("S6", []))
    out["blocks_sdt"] = int(out["S3"] == "missing" or out["S6cell"] in ("missing", "ambiguous")
                            or out["S4"] in ("missing", "partial")
                            or "missing" in s6_states)
    out["blocks_did"] = int(out["S1"] == "missing" or out["S2"] == "missing"
                            or out["S4"] in ("missing", "partial"))
    out["blocks_exclusions_on"] = int(out["S9"] == "missing")
    out["needs_na_verdict"] = int("ambiguous" in set(by_slot.get("S7", []))
                                  or "ambiguous" in set(by_slot.get("S6", [])))
    return out


def discover_studies(base=None):
    base = base or DS
    out = []
    for name in sorted(os.listdir(base)):
        folder = os.path.join(base, name)
        if not os.path.isdir(folder):
            continue
        if os.path.isfile(os.path.join(folder, f"{name} - harmonized_variables.csv")):
            out.append((name, folder))
    return out


SUMMARY_COLS = ["study_key", "n_dv_rows", "n_dv_cols", "missingness_score",
                "blocks_sdt", "blocks_did", "blocks_exclusions_on",
                "needs_na_verdict"] + \
    [s for sl in SLOT_ORDER for s in (sl, f"{sl}_missing")]

MATRIX_COLS = ["study_key", "slot", "scope", "target", "status", "evidence",
               "auto_detail", "coder", "decided_at"]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--datasets", default=DS)
    ap.add_argument("--out", default=os.path.join(RESULTS, "harmonization_coverage.csv"))
    ap.add_argument("--summary-out",
                    default=os.path.join(RESULTS, "harmonization_coverage_summary.csv"))
    args = ap.parse_args(argv)

    studies = discover_studies(args.datasets)

    matrix, summaries = [], []
    for key, folder in studies:
        try:
            rows, summ = coverage_for_study(key, folder)
        except Exception as e:  # noqa: BLE001  (never abort the whole matrix)
            rows, summ = [_row(key, "S1", "study", "pid", "ambiguous", "",
                               f"coverage error: {e!r}")], \
                {"study_key": key, "n_dv_rows": 0, "n_dv_cols": 0,
                 "missingness_score": 0.0}
        matrix.extend(rows)
        summaries.append(summ)

    os.makedirs(RESULTS, exist_ok=True)
    m = pd.DataFrame(matrix, columns=MATRIX_COLS)
    m.to_csv(args.out, index=False, encoding="utf-8-sig")
    s = pd.DataFrame(summaries)
    for c in SUMMARY_COLS:
        if c not in s.columns:
            s[c] = ""
    s = s[SUMMARY_COLS].sort_values("missingness_score", ascending=False)
    s.to_csv(args.summary_out, index=False, encoding="utf-8-sig")

    _print_summary(m, s, args.out, args.summary_out)
    return 0


def _count_studies(m, slot, statuses):
    sub = m[(m["slot"] == slot) & (m["status"].isin(statuses))]
    return sub["study_key"].nunique(), len(sub)


def _print_summary(m, s, out_path, summ_path):
    n_studies = m["study_key"].nunique()
    print(f"coverage: {n_studies} harmonized studies -> {out_path}")
    print(f"          summary -> {summ_path}")

    def _st(slot):
        sub = m[m["slot"] == slot]
        return {k: int(v) for k, v in sub["status"].value_counts().items()}

    # raw gap summary, comparable to i2 §4 (S6 raw = missing + ambiguous,
    # because a blank veracity on a composite/difference DV is `ambiguous`)
    n_v, r_v = _count_studies(m, "S6", ["missing", "ambiguous"])
    n_p, r_p = _count_studies(m, "S8", ["missing", "ambiguous"])
    n_d, r_d = _count_studies(m, "S5", ["missing", "ambiguous"])
    print("-" * 74)
    print("raw gaps (blank sidecar fields, comparable to i2 §4)")
    print(f"  S6 veracity  blank : {r_v:>5} rows / {n_v:>3} studies")
    print(f"  S8 polarity  blank : {r_p:>5} rows / {n_p:>3} studies")
    print(f"  S5 dv_domain blank : {r_d:>5} rows / {n_d:>3} studies")
    for slot, label in (("S1", "no pid"), ("S2", "no condition column"),
                        ("S3", "no conditions.csv"), ("S9", "flags but no rules"),
                        ("S11", "no variables.csv"), ("S10", "no covariates")):
        sub = m[(m["slot"] == slot) & (m["status"] == "missing")]
        print(f"  {slot} {label:<22}: {sub['study_key'].nunique():>3} studies")
    amb = m[(m["slot"] == "S7") & (m["status"] == "ambiguous")]["study_key"].nunique()
    print(f"  S7 timepoint blank (ambiguous, convention=post): {amb:>3} studies")

    # readiness funnel
    cell = set(m[(m["slot"] == "S6cell") & (m["status"] == "filled")]["study_key"])
    cond = set(m[(m["slot"] == "S2") & (m["status"] == "filled")]["study_key"])
    roles = set(m[(m["slot"] == "S3") & (m["status"] == "filled")]["study_key"])
    print("-" * 74)
    print(f"readiness funnel: true+false cell {len(cell)} -> with condition column "
          f"{len(cell & cond)} -> explicit roles {len(cell & cond & roles)}")

    # per-slot status distribution
    print("-" * 74)
    print(f"  {'slot':<6} {'name':<16} {'filled':>7} {'missing':>7} {'ambig':>6} "
          f"{'na':>5} {'conf':>5}")
    for slot in SLOT_ORDER:
        c = _st(slot)
        print(f"  {slot:<6} {SLOT_NAMES[slot]:<16} {c.get('filled', 0):>7} "
              f"{c.get('missing', 0):>7} {c.get('ambiguous', 0):>6} "
              f"{c.get('na', 0):>5} {c.get('confirmed', 0):>5}")

    top = s.head(8)[["study_key", "missingness_score", "blocks_sdt", "blocks_did"]]
    print("-" * 74)
    print("worst studies by poolability-weighted missingness score:")
    for _, r in top.iterrows():
        print(f"  {r['missingness_score']:>7.2f}  sdt={int(r['blocks_sdt'])} "
              f"did={int(r['blocks_did'])}  {r['study_key']}")


if __name__ == "__main__":
    sys.exit(main())
