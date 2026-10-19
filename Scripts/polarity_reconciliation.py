# -*- coding: utf-8 -*-
"""Per-DV polarity reconciliation (workstream W5-POL, 2026-09-23).

Owner decision: **the study-coded direction wins IF it reproduces the paper's
effects; otherwise fall back to the veracity-dependent rule.**

For every ``Datasets/<Key>/<Key> - harmonized_variables.csv`` DV row this
compares the two candidate polarities

* **study-coded** -- precedence (a) ``<Key> - variables.csv`` ``dv_polarity`` /
  ``polarity``, then (b) ``<Key> - dv_direction.csv`` ``direction`` (with the
  rule-mirror guard documented in ``dv_polarity.study_coded_polarity``);
* **rule** -- ``dv_polarity.expected_polarity`` (veracity-dependent);

runs the reproduction arbitration (``dv_polarity.repro_check_for_domain`` on the
study's ``repro_facts.json``) and resolves the final ``chosen`` value via
``dv_polarity.resolve_polarity``.  Where a study cannot be checked the
study-coded value is kept and marked ``study_coded_unverified`` (never silently
dropped).

Products
--------
* ``Results/polarity_reconciliation.csv`` -- the full per-DV table
  (``key, uniform_variable, dv_domain, veracity, study_coded, rule, chosen,
  reproduction_check, reason``).
* with ``--apply``: rewrites only the ``dv_polarity`` column of each affected
  sidecar (and mirrors it into ``variables.csv`` via
  ``dv_polarity.normalize_variables_file``), archiving the original files to
  ``archive/ws_w5pol_backup_2026-09-23/`` first.  No other data is touched.

Run:
  python Scripts/polarity_reconciliation.py            # report only
  python Scripts/polarity_reconciliation.py --apply    # repair the sidecars
"""
import argparse
import glob
import os
import shutil
import sys
from collections import Counter

B = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(B, "Scripts"))
import dv_polarity as DP  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BACKUP = os.path.join(B, "archive", "ws_w5pol_backup_2026-09-23")
OUT = os.path.join(B, "Results", "polarity_reconciliation.csv")
COLUMNS = ["key", "uniform_variable", "dv_domain", "veracity", "study_coded",
           "study_coded_source", "rule", "chosen", "reproduction_check", "reason"]


def sidecar_paths():
    return sorted(glob.glob(os.path.join(B, "Datasets", "*", "* - harmonized_variables.csv")))


def build_rows():
    """Return the per-DV reconciliation rows (no files written)."""
    import pandas as pd
    rows = []
    for p in sidecar_paths():
        key = os.path.basename(os.path.dirname(p))
        folder = os.path.dirname(p)
        try:
            side = pd.read_csv(p, encoding="utf-8-sig", low_memory=False)
        except Exception:
            continue
        if "uniform_variable" not in side.columns:
            continue
        _resolution, _sc, details = DP._apply_resolution_file(side, folder, key)
        for uv, d in details.items():
            rows.append({"key": key, "uniform_variable": uv,
                         "dv_domain": d["dv_domain"], "veracity": d["veracity"],
                         "study_coded": d["study_coded"],
                         "study_coded_source": d["study_coded_source"],
                         "rule": d["rule"],
                         "chosen": d["chosen"],
                         "reproduction_check": d["reproduction_check"],
                         "reason": d["reason"]})
    return pd.DataFrame(rows, columns=COLUMNS)


def _archive(path):
    """Copy a file to the W5-POL backup tree, preserving its dataset folder."""
    if not os.path.isfile(path):
        return
    rel = os.path.relpath(path, B)
    dst = os.path.join(BACKUP, rel)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if not os.path.isfile(dst):
        shutil.copy2(path, dst)


def apply_resolution(table):
    """Rewrite only dv_polarity in affected sidecars (+ variables.csv mirror)."""
    import pandas as pd
    n_files = n_rows = 0
    affected = []
    for p in sidecar_paths():
        key = os.path.basename(os.path.dirname(p))
        folder = os.path.dirname(p)
        try:
            side = pd.read_csv(p, encoding="utf-8-sig", low_memory=False)
        except Exception:
            continue
        if "uniform_variable" not in side.columns:
            continue
        resolution, _sc, details = DP._apply_resolution_file(side, folder, key)
        cur = {str(u): DP._clean(v) for u, v in
               zip(side["uniform_variable"], side.get("dv_polarity", ""))}
        changed = {uv: d["chosen"] for uv, d in details.items()
                   if d["chosen"] and cur.get(uv, "") != d["chosen"]}
        if not changed:
            continue
        _archive(p)
        var = os.path.join(folder, key + " - variables.csv")
        _archive(var)
        side2, n, _ = DP.normalize_sidecar(side, resolution=resolution)
        side2.to_csv(p, index=False, encoding="utf-8-sig")
        DP.normalize_variables_file(var, p)
        n_files += 1
        n_rows += n
        affected.append((key, n))
    return n_files, n_rows, affected


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--apply", action="store_true",
                    help="rewrite the affected sidecars (archived first)")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    table = build_rows()
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    table.to_csv(args.out, index=False, encoding="utf-8-sig")

    def _n(mask):
        return int(mask.sum()) if len(table) else 0

    n_dv = len(table)
    n_conflict = _n((table["study_coded"] != "") & (table["rule"] != "")
                    & (table["study_coded"] != table["rule"])) if n_dv else 0
    n_study_only = _n((table["study_coded"] != "") & (table["rule"] == "")) if n_dv else 0
    n_rule_only = _n((table["study_coded"] == "") & (table["rule"] != "")) if n_dv else 0
    n_agree = _n((table["study_coded"] != "") & (table["study_coded"] == table["rule"])) if n_dv else 0
    reasons = Counter(table["reason"]) if n_dv else Counter()
    checks = Counter(table["reproduction_check"]) if n_dv else Counter()

    print(f"polarity_reconciliation: {n_dv} DV rows -> {args.out}")
    print(f"  study-coded conflicts with the rule : {n_conflict}")
    print(f"  study-coded only (rule declines)    : {n_study_only}")
    print(f"  rule only (no study coding)         : {n_rule_only}")
    print(f"  study-coded agrees with the rule    : {n_agree}")
    print("  resolution reasons:")
    for k, v in reasons.most_common():
        print(f"    {k}: {v}")
    print("  reproduction checks (conflicts only):")
    for k, v in checks.most_common():
        print(f"    {k}: {v}")

    if args.apply:
        n_files, n_rows, affected = apply_resolution(table)
        print(f"\napplied: {n_rows} rows rewritten in {n_files} sidecar(s); "
              f"originals archived to {os.path.relpath(BACKUP, B)}")
        if affected and not args.quiet:
            for k, n in affected[:40]:
                print(f"    {k}: {n}")
            if len(affected) > 40:
                print(f"    ... {len(affected) - 40} more")
    return 0


if __name__ == "__main__":
    sys.exit(main())
