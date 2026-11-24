"""Compare the IRR measurement before and after the reconciliation.

The reconciliation writes the majority value into the canonical route, and
the agreement products compare the agent routes against the canonical route.
If the reconciliation had touched anything the agreement measures look at,
the post-reconciliation figures would be circular.

This script compares the frozen baseline (``Results/irr_baseline_<date>/``)
with the live products and reports whether any measure moved.

Products
--------
``Results/irr_baseline_comparison.json``   the comparison
"""
import csv
import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS = os.path.join(ROOT, "Results")

#: the measures the agreement products report
MEASURES = ("can_exp", "mm_exp", "can_dv", "mm_dv", "can_elig", "mm_elig",
            "domain_jaccard", "polarity_same", "dvname_jaccard",
            "criteria_same", "exp_delta", "elig_delta", "dv_delta")


def baseline_dir():
    cands = [d for d in os.listdir(RESULTS)
             if d.startswith("irr_baseline_") and
             os.path.isdir(os.path.join(RESULTS, d))]
    if not cands:
        return None
    return os.path.join(RESULTS, sorted(cands)[-1])


def load_csv(path):
    if not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return {r["study_key"]: r for r in csv.DictReader(fh)}


def main():
    bdir = baseline_dir()
    if not bdir:
        print("no baseline directory found", file=sys.stderr)
        return 1
    out = {"baseline_dir": os.path.relpath(bdir, RESULTS), "routes": {}}
    for name, fname in (("MM", "mm_coding_agreement.csv"),
                        ("SB", "sb_coding_agreement.csv")):
        b = load_csv(os.path.join(bdir, fname))
        l = load_csv(os.path.join(RESULTS, fname))
        moved, changed = [], {}
        for k in sorted(set(b) & set(l)):
            diffs = [m for m in MEASURES
                     if m in b[k] and m in l[k] and b[k][m] != l[k][m]]
            if diffs:
                moved.append(k)
                for m in diffs:
                    changed.setdefault(m, []).append(k)
        out["routes"][name] = {
            "keys_compared": len(set(b) & set(l)),
            "keys_with_a_moved_measure": len(moved),
            "measures_moved": {m: len(v) for m, v in changed.items()},
            "examples": {m: v[:8] for m, v in changed.items()},
        }
        print("%s: %d keys compared, %d with a moved measure"
              % (name, len(set(b) & set(l)), len(moved)))
        for m, n in (changed or {}).items():
            print("    %-18s %d keys" % (m, n))

    out["verdict"] = (
        "the reconciliation did not change any IRR measure"
        if not any(v["keys_with_a_moved_measure"]
                   for v in out["routes"].values())
        else "the reconciliation moved at least one IRR measure - the "
             "baseline is the honest figure, the live one is not")
    with open(os.path.join(RESULTS, "irr_baseline_comparison.json"), "w",
              encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, ensure_ascii=False)
    print("\n" + out["verdict"])
    print("wrote Results/irr_baseline_comparison.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
