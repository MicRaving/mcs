# -*- coding: utf-8 -*-
"""Validate the reproduction dossier (interface §7).

Checks
------
* ``repro_dossier_index.csv`` has every ``INDEX_COLUMNS`` column.
* every dataset-backed index row has a dossier JSON that exists and parses;
* ``schema_version`` / ``generated_at`` present on every dossier;
* every flat results CSV has exactly ``RESULT_COLUMNS``;
* no duplicate ``contrast_id`` inside a dossier; every ``contrast_id`` starts with its key;
* every result row has an ORIGINAL (with an ``extraction_ref``) or a gap reason;
* every numeric value has a provenance pointer (original -> extraction_ref;
  reproduced -> reproduced_source; without-exclusions -> estimand; exclusions -> JSON).

Writes one row per problem to ``Results/repro_dossier_validation.csv`` and exits 1 when
any ERROR is found.

Run:  python Scripts/validate_repro_dossier.py
"""
import json
import os
import sys

import pandas as pd

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _HERE)

import repro_dossier_lib as L  # noqa: E402

OUT_CSV = os.path.join(L.RESULTS, "repro_dossier_validation.csv")
INDEX_CSV = os.path.join(L.RESULTS, "repro_dossier_index.csv")


def _problem(rows, key, cid, check, detail, severity="ERROR"):
    rows.append({"study_key": key, "contrast_id": cid, "check": check,
                 "detail": detail, "severity": severity})


def validate():
    problems = []
    if not os.path.isfile(INDEX_CSV):
        _problem(problems, "", "", "MISSING_INDEX", f"{INDEX_CSV} not found")
        return problems
    idx = pd.read_csv(INDEX_CSV, encoding="utf-8-sig")
    for col in L.INDEX_COLUMNS:
        if col not in idx.columns:
            _problem(problems, "", "", "MISSING_INDEX_COLUMN", col)

    n_rows = 0
    for _, r in idx.iterrows():
        key = str(r["study_key"])
        if str(r.get("dataset_backed", "")).lower() != "yes":
            continue
        dp = str(r.get("dossier_path", "") or "")
        if not dp:
            _problem(problems, key, "", "NO_DOSSIER_PATH", "dataset-backed row has no dossier")
            continue
        path = os.path.join(ROOT, dp)
        if not os.path.isfile(path):
            _problem(problems, key, "", "NO_DOSSIER_FILE", dp)
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                d = json.load(fh)
        except Exception as e:
            _problem(problems, key, "", "DOSSIER_UNREADABLE", f"{type(e).__name__}: {e}")
            continue
        if int(d.get("schema_version") or 0) != L.SCHEMA_VERSION:
            _problem(problems, key, "", "BAD_SCHEMA_VERSION", str(d.get("schema_version")))
        if not d.get("generated_at"):
            _problem(problems, key, "", "MISSING_GENERATED_AT", "")
        csv_path = os.path.join(L.DOSSIER_DIR, key + " - results.csv")
        if os.path.isfile(csv_path):
            try:
                df = pd.read_csv(csv_path, encoding="utf-8-sig")
                missing = [c for c in L.RESULT_COLUMNS if c not in df.columns]
                if missing:
                    _problem(problems, key, "", "MISSING_RESULT_COLUMN", ";".join(missing))
            except Exception as e:
                _problem(problems, key, "", "RESULTS_CSV_UNREADABLE",
                         f"{type(e).__name__}: {e}")
        seen = set()
        for row in d.get("results") or []:
            n_rows += 1
            cid = str(row.get("contrast_id", ""))
            if cid in seen:
                _problem(problems, key, cid, "DUPLICATE_CONTRAST_ID", "")
            seen.add(cid)
            if cid and not cid.startswith(key + "|"):
                _problem(problems, key, cid, "CONTRAST_ID_MISMATCH",
                         f"does not start with {key}|")
            gaps = []
            try:
                gaps = json.loads(row.get("gaps_json") or "[]")
            except Exception:
                _problem(problems, key, cid, "BAD_GAPS_JSON", str(row.get("gaps_json"))[:80])
            has_orig = row.get("original_value") is not None
            if has_orig:
                if not str(row.get("extraction_ref") or "").strip():
                    _problem(problems, key, cid, "ORIGINAL_WITHOUT_PROVENANCE", "")
            else:
                if not gaps:
                    _problem(problems, key, cid, "ROW_WITHOUT_ORIGINAL_OR_GAP", "")
            if row.get("reproduced_value") is not None:
                if not str(row.get("reproduced_source") or "").strip():
                    _problem(problems, key, cid, "REPRODUCED_WITHOUT_PROVENANCE", "")
            woe_nums = [row.get(k) for k in ("woe_d_on", "woe_d_off", "woe_delta")]
            if any(v is not None for v in woe_nums):
                if not str(row.get("without_exclusions_estimand") or "").strip():
                    _problem(problems, key, cid, "WOE_WITHOUT_PROVENANCE", "")
            ex = str(row.get("exclusions_applied_json") or "")
            if ex:
                try:
                    json.loads(ex)
                except Exception:
                    _problem(problems, key, cid, "BAD_EXCLUSIONS_JSON", ex[:80])
    return problems


def main():
    problems = validate()
    os.makedirs(L.RESULTS, exist_ok=True)
    df = pd.DataFrame(problems, columns=["study_key", "contrast_id", "check", "detail",
                                         "severity"])
    df.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    n_err = int((df["severity"] == "ERROR").sum()) if len(df) else 0
    n_warn = int((df["severity"] == "WARNING").sum()) if len(df) else 0
    print(f"validate_repro_dossier: {len(df)} problem(s) "
          f"({n_err} ERROR, {n_warn} WARNING) -> {os.path.relpath(OUT_CSV, ROOT)}")
    if len(df):
        for check, n in df["check"].value_counts().items():
            print(f"  {check:<34} {n}")
    return 1 if n_err else 0


if __name__ == "__main__":
    sys.exit(main())
