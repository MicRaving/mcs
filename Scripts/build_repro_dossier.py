# -*- coding: utf-8 -*-
"""Generate the per-study reproduction dossier (interface §7).

Products (all in ``Results/``):

  ``repro_dossier/<Key>.json``            schema_version 1, full dossier
  ``repro_dossier/<Key> - results.csv``   flat twin, one row per contrast
  ``repro_dossier_index.csv``             one row per study (the master-sheet source)

Merge priority and gap semantics live in ``repro_dossier_lib``.  Dossier JSON is
written for every dataset-backed study (a ``Datasets/<Key>/<Key> - reproduction.py``
exists); the index covers the union of dataset-backed, report and Extraction keys so
the master ``Reproduction`` sheet can be a projection without losing report-only rows.

Run:  python Scripts/build_repro_dossier.py
      python Scripts/build_repro_dossier.py --only "Adamus,Arechar"
"""
import argparse
import json
import os
import sys
import time

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


def enumerate_keys(only=None):
    """dict key -> {'dataset_backed':bool,'report':path,'extraction':path}."""
    out = {}
    onlys = [s.strip().lower() for s in (only or "").split(",") if s.strip()]

    def _want(k):
        return (not onlys) or any(o in k.lower() for o in onlys)

    ds = L.DATASETS
    if os.path.isdir(ds):
        for d in sorted(os.listdir(ds)):
            folder = os.path.join(ds, d)
            if not os.path.isdir(folder) or d.startswith("_") or d in ("temp",):
                continue
            script = os.path.join(folder, d + " - reproduction.py")
            if os.path.isfile(script) and _want(d):
                out.setdefault(d, {"dataset_backed": False, "report": "", "extraction": ""})
                out[d]["dataset_backed"] = True
    if os.path.isdir(L.REPRO):
        for fn in sorted(os.listdir(L.REPRO)):
            if not fn.endswith(".md") or fn.startswith("_BATCH") or fn.endswith(" - SB.md"):
                continue
            k = os.path.splitext(fn)[0]
            if _want(k):
                e = out.setdefault(k, {"dataset_backed": False, "report": "", "extraction": ""})
                e["report"] = os.path.relpath(os.path.join(L.REPRO, fn), ROOT).replace("\\", "/")
    if os.path.isdir(L.EXTRACTION):
        for fn in sorted(os.listdir(L.EXTRACTION)):
            if not fn.endswith(".json"):
                continue
            k = os.path.splitext(fn)[0]
            if _want(k):
                e = out.setdefault(k, {"dataset_backed": False, "report": "", "extraction": ""})
                e["extraction"] = os.path.relpath(os.path.join(L.EXTRACTION, fn),
                                                  ROOT).replace("\\", "/")
    return out


def _load_domains():
    p = os.path.join(L.RESULTS, "reproduction_summary.xlsx")
    if not os.path.isfile(p):
        return None
    try:
        return pd.read_excel(p, sheet_name="Domains")
    except Exception:
        return None


def _load_report_maps():
    md_status, audit = {}, {}
    try:
        import parse_reproduction_reports as P
        for r in P.parse_all(include_text=False):
            audit[r["key"]] = r
            if r.get("status") and r["status"] != "unknown":
                md_status[r["key"]] = r["status"]
    except Exception:
        pass
    return md_status, audit


def build(only=None, write=True):
    keys = enumerate_keys(only)
    domains_df = _load_domains()
    woe = L.load_woe()
    prov = L.load_provenance()
    md_status, audit = _load_report_maps()
    os.makedirs(L.DOSSIER_DIR, exist_ok=True)
    generated = time.strftime("%Y-%m-%d %H:%M:%S")

    index_rows = []
    n_dossier = 0
    for key, meta in sorted(keys.items()):
        dossier = L.build_study(key, domains_df, woe, prov, md_status, audit)
        dossier["generated_at"] = generated
        dossier["dataset_backed"] = bool(meta["dataset_backed"])
        dataset_path = (os.path.relpath(os.path.join(L.DATASETS, key), ROOT).replace("\\", "/")
                        if os.path.isdir(os.path.join(L.DATASETS, key)) else "")
        json_path = os.path.join(L.DOSSIER_DIR, key + ".json")
        if write and meta["dataset_backed"]:
            with open(json_path, "w", encoding="utf-8") as fh:
                json.dump(dossier, fh, ensure_ascii=False, indent=1, default=str)
            flat = [L.strip_internal(r) for r in dossier["results"]]
            df = pd.DataFrame(flat, columns=L.RESULT_COLUMNS)
            df.to_csv(os.path.join(L.DOSSIER_DIR, key + " - results.csv"), index=False,
                      encoding="utf-8-sig")
            n_dossier += 1
        index_rows.append(L.summarize_for_index(
            dossier, json_path if meta["dataset_backed"] else "",
            dataset_path, meta["report"]))

    idx = pd.DataFrame(index_rows, columns=L.INDEX_COLUMNS)
    idx_path = os.path.join(L.RESULTS, "repro_dossier_index.csv")
    if write:
        idx.to_csv(idx_path, index=False, encoding="utf-8-sig")

    # ---- coverage report -------------------------------------------------
    n_pair = int(((idx["n_reproduced_numeric"] > 0)).sum())
    n_domrec = int((idx["n_domain_recalc_numeric"] > 0).sum())
    n_side = int((idx["n_reproduced_side_numeric"] > 0).sum())
    n_woe_full = int((idx["n_woe_full"] > 0).sum())
    n_log = int(idx["log_path"].fillna("").astype(str).str.strip().ne("").sum())
    n_prov = int((idx["n_exclusion_provenance_rules"] > 0).sum())
    n_rows_pair = int((idx["numeric_pair_studies_marker"] == 1).sum())
    print(f"repro dossier: {len(idx)} study rows, {n_dossier} dossier JSON written "
          f"-> {os.path.relpath(L.DOSSIER_DIR, ROOT)}/")
    print(f"  studies with >=1 numeric original<->reproduced pair: {n_pair}")
    print(f"  studies with >=1 numeric domain recalc (recalc_rows.d): {n_domrec}")
    print(f"  studies with >=1 numeric reproduced-side value (pair OR recalc): {n_side}")
    print(f"  studies with numeric woe d_on AND d_off:               {n_woe_full}")
    print(f"  studies with a durable run log_path:                   {n_log}")
    print(f"  studies with per-rule exclusion provenance:            {n_prov}")
    print(f"  index -> {os.path.relpath(idx_path, ROOT)}")
    print(f"  status counts: {idx['reproduced_status'].value_counts().to_dict()}")
    return idx, n_pair, n_prov


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", default=None, help="comma-separated key substrings")
    ap.add_argument("--dry-run", action="store_true", help="do not write products")
    args = ap.parse_args(argv)
    build(only=args.only, write=not args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
