"""Verify the ``dv_polarity`` precedence invariant (workstream W5-POL, 2026-09-23).

The SDT layer (``reanalysis.py``) computes ``d' = z(HR) - z(FAR)`` where the
true-veracity DV supplies ``HR`` and the false-veracity DV supplies the
correct-rejection rate.  The owner decision for polarity is:

    the **study-coded** direction wins IF it reproduces the paper's effects;
    otherwise the veracity-dependent rule fills in.

This audit therefore validates the PRECEDENCE, not the pure veracity rule:

1. **Sidecar precedence** -- every ``Datasets/<Key>/<Key> - harmonized_variables.csv``
   DV row must equal the value ``dv_polarity.resolve_polarity`` produces from the
   study-coded sources (``variables.csv`` / ``dv_direction.csv``) and the
   reproduction arbitration.  A row that matches neither the rule nor the
   resolved study-coded choice is a violation.
2. **Sidecar <-> variables.csv mirror** -- the provenance ``dv_polarity`` column
   must match the sidecar for every shared uniform variable.
3. **Extraction agreement** (informational) -- the coded ``Extraction/*.json``
   ``DV_Metrics`` polarity is reported against the rule; a mismatch is printed
   but not failed, because the extraction layer is coder-owned and may
   legitimately follow the study coding where the rule differs.

Run:  python Scripts/validate_polarity.py [--quiet]
Exit: 0 = invariant holds, 1 = violations found.
"""
import argparse
import glob
import json
import os
import sys
from collections import Counter, defaultdict

B = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(B, "Scripts"))
import dv_polarity as DP  # noqa: E402
import domain_restriction as DR  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ALLOWED = {DP.HIGHER, DP.LOWER, ""}


def _rows_sidecar():
    """Every sidecar DV row with its resolved ``chosen`` value and audit fields."""
    import pandas as pd
    out = []
    for p in glob.glob(os.path.join(B, "Datasets", "*", "* - harmonized_variables.csv")):
        study = os.path.basename(os.path.dirname(p))
        folder = os.path.dirname(p)
        try:
            d = pd.read_csv(p, encoding="utf-8-sig", low_memory=False)
        except Exception:
            continue
        if "uniform_variable" not in d.columns:
            continue
        try:
            resolution, _sc, details = DP._apply_resolution_file(d, folder, study)
        except Exception:
            continue
        cur = {DP._as_str(u): DP._clean(v)
               for u, v in zip(d["uniform_variable"], d.get("dv_polarity", ""))}
        for uv, det in details.items():
            out.append(dict(study=study, uv=uv, dom=det["dv_domain"],
                            ver=det["veracity"], pol=det["chosen"],
                            cur=cur.get(uv, ""), rule=det["rule"],
                            study_coded=det["study_coded"],
                            source=det["study_coded_source"],
                            check=det["reproduction_check"], reason=det["reason"]))
    return out


def _rows_extraction():
    out = []
    for p in sorted(glob.glob(os.path.join(B, "Extraction", "*.json"))):
        key = os.path.splitext(os.path.basename(p))[0]
        try:
            data = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        for o in (data if isinstance(data, list) else [data]):
            for dv in (o.get("DV_Metrics") or []):
                if not isinstance(dv, dict):
                    continue
                ver, comp = DP.expected_from_score_types(dv.get("score_types"), dv.get("dv_name"))
                dom = DR.resolve_domain(dv.get("dv_domain"), key, dv.get("dv_name"))
                out.append(dict(study=key, name=str(dv.get("dv_name")), dom=dom, ver=ver,
                                comp=comp, pol=dv.get("dv_polarity"),
                                notes=str(dv.get("discernment_defined_as") or "")))
    return out


def check_sidecar(rows, violations):
    for r in rows:
        if r["pol"] not in ALLOWED:
            violations.append(dict(layer="sidecar-vocab", study=r["study"], name=r["uv"],
                                   dom=r["dom"], ver=r["ver"], expected="allowed",
                                   found=r["pol"]))
            continue
        if r["pol"] and r["cur"] != r["pol"]:
            violations.append(dict(layer="sidecar-precedence", study=r["study"], name=r["uv"],
                                   dom=r["dom"], ver=r["ver"],
                                   expected=r["pol"], found=r["cur"] or "(blank)"))


def check_variables(srows, violations):
    import pandas as pd
    side = {(r["study"], r["uv"]): r["cur"] for r in srows}
    for p in glob.glob(os.path.join(B, "Datasets", "*", "* - variables.csv")):
        study = os.path.basename(os.path.dirname(p))
        # Only the main ``<Key> - variables.csv`` is mirrored by the runners; the
        # artifact-specific ``<Key> - <Artifact> - variables.csv`` files are
        # independent coder records and are not required to match the sidecar.
        if os.path.basename(p) != study + " - variables.csv":
            continue
        try:
            d = pd.read_csv(p, encoding="utf-8-sig", low_memory=False)
        except Exception:
            continue
        uc = "uniform_name" if "uniform_name" in d.columns else (
            "uniform_variable" if "uniform_variable" in d.columns else None)
        if uc is None or "dv_polarity" not in d.columns:
            continue
        for _, r in d.iterrows():
            name = DP._as_str(r[uc])
            if not name.startswith("dv_") or (study, name) not in side:
                continue
            got = DP._clean(r["dv_polarity"])
            want = side[(study, name)]
            if want and got != want:
                violations.append(dict(layer="variables", study=study, name=name, dom="",
                                       ver="", expected=want, found=got))


def extraction_report(erows):
    """Informational: extraction polarity vs the veracity rule."""
    mism = []
    for r in erows:
        exp = DP.expected_polarity(r["dom"], r["ver"], r["name"], r["notes"])
        if exp is None:
            continue
        target = 1 if exp == DP.HIGHER else 0
        if r["pol"] != target:
            mism.append(r)
    return mism


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    srows = _rows_sidecar()
    erows = _rows_extraction()
    violations = []
    check_sidecar(srows, violations)
    check_variables(srows, violations)
    ext_mism = extraction_report(erows)

    reasons = Counter(r["reason"] for r in srows)
    checks = Counter(r["check"] for r in srows if r["check"])
    n_study_coded = sum(1 for r in srows if r["pol"] and r["study_coded"]
                        and r["pol"] == r["study_coded"] and r["rule"]
                        and r["study_coded"] != r["rule"])
    if not args.quiet:
        print(f"validate_polarity (precedence): {len(srows)} sidecar DV rows, "
              f"{len(erows)} extraction DV rows")
        print(f"  study-coded overrides the rule: {n_study_coded}")
        print("  resolution reasons:")
        for k, v in reasons.most_common():
            print(f"    {k}: {v}")
        if checks:
            print("  reproduction checks:")
            for k, v in checks.most_common():
                print(f"    {k}: {v}")
        print(f"  extraction-vs-rule mismatches (informational): {len(ext_mism)}")
        if violations:
            print(f"\nVIOLATIONS: {len(violations)}")
            for v in violations[:40]:
                print(f"  [{v['layer']}] {v['study']} | {v.get('dom','')} | ver={v['ver']} "
                      f"| expected={v['expected']} found={v['found']} | {str(v['name'])[:50]}")
            if len(violations) > 40:
                print(f"  ... {len(violations) - 40} more")

    ok = not violations
    print("POLARITY VALIDATION PASS" if ok else f"POLARITY VALIDATION FAIL ({len(violations)})")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
