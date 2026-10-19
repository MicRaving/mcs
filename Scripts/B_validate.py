"""Validate campaign coding JSONs for schema v3.0 completeness.
Usage: python B_validate.py <file.json> [more.json ...]
       python B_validate.py --dir <coding_dir>
Exit code 0 = all valid, 1 = issues found.
"""
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import contract as CT  # noqa: E402  (ONE DV-completeness rule, shared with the app)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REQUIRED = ["Study_ID", "Year", "Authors", "Title", "Study_Description",
            "Intervention_Type", "DV_Metrics"]
DETAIL_TYPES = {"other"}
DESC = ("n1i", "m1i", "sd1i", "n2i", "m2i", "sd2i")
# Schema v3.1 fields that are OPTIONAL at validation time (required-with-coverage
# in validate_extraction.py only): source_location / source_quote.
OPTIONAL_DV_FIELDS = ("source_location", "source_quote")


def has_descriptives(dv):
    return all(dv.get(k) is not None for k in DESC)


def has_fallback(dv):
    """True when a DV carries any usable coded effect (contract.dv_has_effect)."""
    return CT.dv_has_effect(dv)


def check_file(path):
    issues = []
    try:
        data = json.load(open(path, encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        return [f"JSON parse error: {e}"]
    if not isinstance(data, list) or not data:
        return ["top level must be a non-empty JSON array"]
    for i, rec in enumerate(data):
        if not isinstance(rec, dict):
            issues.append(f"[{i}] not an object")
            continue
        for k in REQUIRED:
            if k not in rec or rec[k] in (None, "", []):
                issues.append(f"[{i}] missing required field {k}")
        it = rec.get("Intervention_Type")
        it_codes = it if isinstance(it, list) else ([it] if it not in (None, "") else [])
        if any(str(c) in DETAIL_TYPES for c in it_codes) and not rec.get("Intervention_Type_Detail"):
            issues.append(f"[{i}] Intervention_Type={it} requires Intervention_Type_Detail")
        dvs = rec.get("DV_Metrics")
        if not isinstance(dvs, list):
            issues.append(f"[{i}] DV_Metrics must be a list")
            continue
        for j, dv in enumerate(dvs):
            if not isinstance(dv, dict):
                issues.append(f"[{i}].DV_Metrics[{j}] not an object")
                continue
            if dv.get("dv_name") in (None, ""):
                issues.append(f"[{i}].DV_Metrics[{j}] missing dv_name")
            if dv.get("contrast_type") is None:
                issues.append(f"[{i}].DV_Metrics[{j}] missing contrast_type")
            if dv.get("eligible_primary") is None:
                issues.append(f"[{i}].DV_Metrics[{j}] missing eligible_primary")
            reason = dv.get("missing_metrics_reason")
            if not (CT.dv_has_effect(dv) or (reason and str(reason).strip())):
                issues.append(
                    f"[{i}].DV_Metrics[{j}] no descriptives/fallback and empty missing_metrics_reason")
    return issues


def main():
    args = sys.argv[1:]
    files = []
    if args and args[0] == "--dir":
        files = sorted(glob.glob(os.path.join(args[1], "*.json")))
    else:
        files = args
    bad = 0
    for f in files:
        issues = check_file(f)
        if issues:
            bad += 1
            print(f"INVALID {os.path.basename(f)}")
            for it in issues[:12]:
                print("   -", it)
            if len(issues) > 12:
                print(f"   ... {len(issues) - 12} more")
        else:
            print(f"OK      {os.path.basename(f)}")
    print(f"\n{len(files) - bad}/{len(files)} valid")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
