"""Summarise one merged screening batch: verdicts, decisions and the include list.

Reads ``master_dataset.xlsx`` (the authoritative store) and writes

* ``Results/screening_batch_<stamp>.csv``  - one row per record of the batch
  (an, title, year, source, duplicate status, C1-C4 verdicts, intervention,
  decision, rationale of the decisive criterion);
* ``Results/screening_batch_<stamp>_summary.json`` - the counts the PRISMA
  narrative needs, including the per-criterion exclusion tally.

Usage
-----
    python Scripts/summarize_screening_batch.py --source new_hit_2026-11-04
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MASTER = os.path.join(ROOT, "master_dataset.xlsx")
RESULTS = os.path.join(ROOT, "Results")

CRITERIA = ["C1", "C2", "C3", "C4"]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", default="new_hit_2026-11-04")
    ap.add_argument("--stamp", default=None)
    args = ap.parse_args(argv)

    stamp = args.stamp or args.source.replace("new_hit_", "")
    df = pd.read_excel(MASTER, dtype=str).fillna("")
    batch = df[df["Record_Source"].str.startswith(args.source)].copy()
    if batch.empty:
        print(f"no rows with Record_Source {args.source!r}", file=sys.stderr)
        return 1

    def decisive(row):
        for c in CRITERIA:
            if str(row.get(f"{c}_verdict") or "").strip() == "No":
                return c
        return ""

    batch["Decisive_Criterion"] = batch.apply(decisive, axis=1)
    batch["Decision"] = batch.apply(
        lambda r: "Exclude" if any(str(r.get(f"{c}_verdict") or "").strip() == "No"
                                   for c in CRITERIA)
        else ("Include" if all(str(r.get(f"{c}_verdict") or "").strip() == "Yes"
                               for c in CRITERIA)
              else "Unsure"), axis=1)

    cols = ["an", "title", "year", "source", "shortDBName", "doi",
            "Duplicate", "Duplicate_Group", "Duplicate_Of",
            "C1_verdict", "C2_verdict", "C3_verdict", "C4_verdict",
            "Intervention_verdict", "Decision", "Decisive_Criterion",
            "Effective_Verdict"]
    out = batch[[c for c in cols if c in batch.columns]].copy()
    os.makedirs(RESULTS, exist_ok=True)
    csv_path = os.path.join(RESULTS, f"screening_batch_{stamp}.csv")
    out.to_csv(csv_path, index=False, encoding="utf-8-sig")

    summary = {
        "source": args.source,
        "records": int(len(batch)),
        "unique_reports": int((batch["Duplicate"] != "duplicate").sum()),
        "duplicates": int((batch["Duplicate"] == "duplicate").sum()),
        "decisions": dict(Counter(batch["Decision"])),
        "effective_verdict": dict(Counter(batch["Effective_Verdict"])),
        "excluded_by_criterion": dict(Counter(
            c for c in batch["Decisive_Criterion"] if c)),
        "verdicts": {c: dict(Counter(batch[f"{c}_verdict"])) for c in CRITERIA},
        "interventions": dict(Counter(
            batch.loc[batch["Decision"] != "Exclude", "Intervention_verdict"])),
        "included_records": int((batch["Decision"] == "Include").sum()),
        "unsure_records": int((batch["Decision"] == "Unsure").sum()),
    }
    json_path = os.path.join(RESULTS, f"screening_batch_{stamp}_summary.json")
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=1)
        fh.write("\n")

    print(json.dumps(summary, ensure_ascii=False, indent=1))
    print(f"\n-> {os.path.relpath(csv_path, ROOT)}")
    print(f"-> {os.path.relpath(json_path, ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
