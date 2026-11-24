"""Find pooled effects that come from a record the coding marks as excluded.

``Fulltext_Exclusion_Criterion`` is the campaign's screening layer, and it
is recorded **per experiment**: a paper can have one experiment that
qualifies and another that does not, and the criterion is set on the
experiment that does not.

The pooled models select on ``eligible_primary == 1`` and never read the
criterion, so an excluded experiment still contributes its effects unless
its key is listed by hand in ``Scripts/study_exclusions.py``.

The comparison is therefore made **per record**, not per study key: the
criterion that applies is the one on the record the row belongs to.  A
study whose *supplementary* record carries C2 does not exclude its three
proper experiments.

Products
--------
``Results/excluded_but_pooled.csv``   the records and their pooled effects
"""
import csv
import json
import os
import re
import sys
from collections import Counter

import pandas as pd

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS = os.path.join(ROOT, "Results")
OUT = os.path.join(RESULTS, "excluded_but_pooled.csv")


def criteria(value):
    if value in (None, "", []):
        return set()
    if isinstance(value, str):
        value = [value]
    out = set()
    for v in value:
        for m in re.finditer(r"\bC([1-4])\b", str(v)):
            out.add("C" + m.group(1))
    return out


def main():
    df = pd.read_excel(os.path.join(RESULTS, "metaanalysis.xlsx"),
                       sheet_name="DV_Metrics")
    df = df[(df["eligible_primary"] == 1) & df["yi"].notna()]
    df = df.set_index(["study_key", "experiment_id"], drop=False)

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
        for rec in recs:
            crit = criteria(rec.get("Fulltext_Exclusion_Criterion"))
            if not crit:
                continue
            sid = str(rec.get("Study_ID"))
            try:
                sub = df.loc[(key, sid)]
            except KeyError:
                continue
            if isinstance(sub, pd.DataFrame):
                frame = sub
            elif isinstance(sub, pd.Series):
                frame = sub.to_frame().T
            else:
                continue
            for _, r in frame.iterrows():
                rows.append(dict(
                    study_key=key, experiment_id=sid,
                    criteria=";".join(sorted(crit)),
                    dv_name=str(r.get("dv_name"))[:70],
                    yi=r.get("yi"), vi=r.get("vi"),
                    contrast=str(r.get("contrast"))[:60],
                    n_studies=r.get("n_studies")))

    with open(OUT, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=[
            "study_key", "experiment_id", "criteria", "dv_name", "yi", "vi",
            "contrast", "n_studies"])
        w.writeheader()
        w.writerows(rows)

    keys = Counter(r["study_key"] for r in rows)
    exps = Counter((r["study_key"], r["experiment_id"]) for r in rows)
    print("pooled effects from a record the coding marks as EXCLUDED: %d"
          % len(rows))
    print("   across %d records in %d study keys"
          % (len(exps), len(keys)))
    print("   by criterion:",
          dict(Counter(c for r in rows for c in r["criteria"].split(";"))))
    print()
    for (key, exp), n in exps.most_common(30):
        crit = {r["criteria"] for r in rows
                if r["study_key"] == key and r["experiment_id"] == exp}
        print("   %-38s / %-30s %2d  %s"
              % (key[:38], exp[:30], n, ";".join(sorted(crit))))
    print("\nwrote", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
