# -*- coding: utf-8 -*-
"""Audit ``dv_polarity`` in every harmonized sidecar under the W5-POL precedence.

For every ``Datasets/<Key>/<Key> - harmonized_variables.csv`` this recomputes the
resolved orientation with the *same* code path the runners use --
``dv_polarity._apply_resolution_file`` (study-coded sources + reproduction
arbitration) -- and reports where the stored ``dv_polarity`` differs from

* the **chosen** value (``mismatch`` -- a real inconsistency), and
* the pure veracity **rule** (``rule_mismatch`` -- expected whenever the
  study-coded direction wins).

It also flags an out-of-vocabulary stored value (anything other than
``higher_is_better`` / ``higher_is_worse`` / blank).

Output columns: ``key, uniform_variable, dv_domain, veracity, current_polarity,
rule, study_coded, study_coded_source, chosen, reproduction_check, reason,
mismatch, rule_mismatch``.

Run:  python Scripts/audit_polarity.py [--out Results/polarity_audit.csv]
Exit: 0 (the audit reports, it does not fail on findings).
"""
import argparse
import glob
import os
import sys
from collections import Counter

B = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(B, "Scripts"))
import dv_polarity as DP  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ALLOWED = {DP.HIGHER, DP.LOWER, ""}


def sidecar_paths():
    return sorted(glob.glob(os.path.join(B, "Datasets", "*", "* - harmonized_variables.csv")))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=os.path.join(B, "Results", "polarity_audit.csv"))
    args = ap.parse_args(argv)

    import pandas as pd

    rows = []
    files = 0
    unreadable = []
    per_file_mismatch = Counter()
    for p in sidecar_paths():
        study = os.path.basename(os.path.dirname(p))
        folder = os.path.dirname(p)
        try:
            d = pd.read_csv(p, encoding="utf-8-sig", low_memory=False)
        except Exception as e:  # pragma: no cover
            unreadable.append((study, repr(e)))
            continue
        if "uniform_variable" not in d.columns:
            continue
        files += 1
        try:
            _resolution, _sc, details = DP._apply_resolution_file(d, folder, study)
        except Exception:
            continue
        cur = {DP._as_str(u): DP._clean(v)
               for u, v in zip(d["uniform_variable"], d.get("dv_polarity", ""))}
        for uv, det in details.items():
            c = cur.get(uv, "")
            chosen = det["chosen"]
            rule = det["rule"]
            mismatch = bool(chosen and c != chosen)
            rule_mismatch = bool(rule and c != rule)
            rows.append(dict(key=study, uniform_variable=uv,
                             dv_domain=det["dv_domain"], veracity=det["veracity"],
                             current_polarity=c, rule=rule,
                             study_coded=det["study_coded"],
                             study_coded_source=det["study_coded_source"],
                             chosen=chosen,
                             reproduction_check=det["reproduction_check"],
                             reason=det["reason"],
                             mismatch=mismatch, rule_mismatch=rule_mismatch))
            if mismatch:
                per_file_mismatch[study] += 1

    cols = ["key", "uniform_variable", "dv_domain", "veracity", "current_polarity",
            "rule", "study_coded", "study_coded_source", "chosen",
            "reproduction_check", "reason", "mismatch", "rule_mismatch"]
    out = pd.DataFrame(rows, columns=cols)
    out.to_csv(args.out, index=False, encoding="utf-8-sig")

    n_mismatch = int(out["mismatch"].sum()) if len(out) else 0
    n_rule_mismatch = int(out["rule_mismatch"].sum()) if len(out) else 0
    n_invalid = int((~out["current_polarity"].isin(ALLOWED)).sum()) if len(out) else 0
    print(f"audit_polarity (precedence): {files} sidecars, {len(out)} dv rows -> {args.out}")
    print(f"  mismatches vs the resolved choice: {n_mismatch}")
    print(f"  rows where the study-coded direction overrides the rule: {n_rule_mismatch}")
    print(f"  invalid_polarity (not higher_is_better/higher_is_worse/blank): {n_invalid}")
    if per_file_mismatch:
        print("  mismatches by study:")
        for k, v in per_file_mismatch.most_common(20):
            print(f"    {k}: {v}")
    if unreadable:
        print(f"  unreadable sidecars: {len(unreadable)}")
        for k, e in unreadable[:10]:
            print(f"    {k}: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
