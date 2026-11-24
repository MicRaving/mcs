"""Find DV rows whose ``effect_size_type_reported`` is "none" although the row
carries a usable standardised effect.

The standardisation guard reads a reported type of "none" as *the paper
reported no effect size* and refuses the row.  But many rows carry a
``d_val``/``yi``/``vi`` that the coder derived from the paper's cell
means/SDs, while leaving the reported-type field at "none".  The row then
carries an effect the pipeline will not pool, and nothing in the logs says
why the row is missing.

This is the defect class the master pass found three times by hand
(``Bråten et al. (2019)``, ``Deng et al. (2025)``, ``Cook et al. (2017)``).
This script finds every instance, so it can be swept in one pass.

Products
--------
``Results/effect_type_none_rows.csv``
"""
import csv
import json
import os
import sys
from collections import Counter

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS = os.path.join(ROOT, "Results")
OUT = os.path.join(RESULTS, "effect_type_none_rows.csv")


def num(v):
    try:
        return float(v)
    except Exception:
        return None


def main():
    rows = []
    for fn in sorted(os.listdir(os.path.join(ROOT, "Extraction"))):
        if not fn.endswith(".json"):
            continue
        key = fn[:-5]
        try:
            d = json.load(open(os.path.join(ROOT, "Extraction", fn),
                               encoding="utf-8"))
        except Exception:
            continue
        recs = d if isinstance(d, list) else d.get("experiments", [])
        for i, rec in enumerate(recs):
            for j, dv in enumerate(rec.get("DV_Metrics") or []):
                if not isinstance(dv, dict):
                    continue
                if str(dv.get("effect_size_type_reported") or "").strip() != "none":
                    continue
                has_d = num(dv.get("d_val")) is not None
                has_yi = num(dv.get("yi")) is not None
                if not (has_d or has_yi):
                    continue
                has_cells = all(num(dv.get(f)) is not None
                                for f in ("m1i", "m2i", "sd1i", "sd2i"))
                if has_cells:
                    # the paper reported per-arm means/SDs, so the reported
                    # type cannot be "none": "means" (or "d" if it also gave
                    # a standardised effect) is what the paper reported.
                    verdict = "type should be means (the paper reports cell means/SDs)"
                elif has_d:
                    verdict = "type should be d (the row carries a standardised d)"
                else:
                    verdict = "check: the row carries yi/vi but no d and no cells"
                rows.append(dict(
                    study_key=key, record=i, dv_index=j,
                    dv_name=str(dv.get("dv_name"))[:70],
                    d_val=dv.get("d_val"), yi=dv.get("yi"), vi=dv.get("vi"),
                    eligible_primary=dv.get("eligible_primary"),
                    contrast_type=dv.get("contrast_type"),
                    has_d=int(has_d), has_yi=int(has_yi),
                    has_cells=int(has_cells), verdict=verdict,
                    n1i=dv.get("n1i"), n2i=dv.get("n2i"),
                    m1i=dv.get("m1i"), m2i=dv.get("m2i"),
                    sd1i=dv.get("sd1i"), sd2i=dv.get("sd2i")))

    with open(OUT, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=[
            "study_key", "record", "dv_index", "dv_name", "d_val", "yi", "vi",
            "eligible_primary", "contrast_type", "has_d", "has_yi",
            "has_cells", "verdict",
            "n1i", "n2i", "m1i", "m2i", "sd1i", "sd2i"])
        w.writeheader()
        w.writerows(rows)

    keys = {r["study_key"] for r in rows}
    elig = sum(1 for r in rows if str(r["eligible_primary"]) == "1")
    vc = Counter(r["verdict"] for r in rows)
    print("rows with effect_size_type_reported = 'none' but a usable effect: %d"
          % len(rows))
    print("   across %d study keys; %d of them are eligible_primary = 1"
          % (len(keys), elig))
    print()
    for k, n in vc.most_common():
        print("   %-58s %d" % (k[:58], n))
    print()
    for k, n in Counter(r["study_key"] for r in rows).most_common(12):
        print("   %-44s %d" % (k[:44], n))
    print("\nwrote", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
