"""Compare the coded ``Fulltext_Exclusion_Criterion`` with the screen's own reason.

``master_dataset.xlsx`` records, per record, the reason the full text was
excluded (one reason per record).  The coding records the *criteria* that are
unmet (C1-C4).  Where the two disagree, one of them is wrong, and the
disagreement is mechanically findable — no paper needs to be re-read to
notice it.

The evidence pass found this on `Sheng et al. (2021)`: the screen says
"Not a primary experimental study (C4)" while the coding says
``['C2','C4']``.

Products
--------
``Results/screen_criterion_agreement.csv``   the disagreements
"""
import csv
import os
import re
import sys
from collections import defaultdict

import pandas as pd

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS = os.path.join(ROOT, "Results")
OUT = os.path.join(RESULTS, "screen_criterion_agreement.csv")

#: the screen's reason text -> the criterion it implies
REASON_TO_CRIT = [
    (r"no misinformation intervention|not a misinformation",
     "C1"),
    (r"no control|no comparison group|control group",
     "C2"),
    (r"not english|non-english|language",
     "C3"),
    (r"not a primary experimental|not primary|not an experiment",
     "C4"),
]


def screen_criteria(reason):
    """The criteria the screen's reason implies."""
    text = str(reason or "").lower()
    out = set()
    for pat, crit in REASON_TO_CRIT:
        if re.search(pat, text):
            out.add(crit)
    return out


def coded_criteria(value):
    """The criteria a value names.

    Accepts both the bare code ("C2", or a list of them) and the screen's
    reason string ("No control/comparison group (C2)").
    """
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
    path = os.path.join(ROOT, "master_dataset.xlsx")
    if not os.path.isfile(path):
        print("no master_dataset.xlsx", file=sys.stderr)
        return 1
    df = pd.read_excel(path, sheet_name=0)
    col_rew = next((c for c in df.columns
                     if "Fulltext" in str(c) and "Reason" in str(c)), None)
    col_crit = next((c for c in df.columns
                     if "Fulltext_Exclusion_Criterion" == str(c)), None)
    col_doi = next((c for c in df.columns if str(c).lower() == "doi"), None)
    print("screening columns: reason=%r criterion=%r doi=%r"
          % (col_rew, col_crit, col_doi))
    if not col_rew or not col_crit:
        print("no full-text reason/criterion column found; columns:",
              list(df.columns)[:40], file=sys.stderr)
        return 1

    # the screening sheet is keyed by DOI; a record can appear more than once
    by_doi = defaultdict(list)
    for _, r in df.iterrows():
        if col_doi:
            doi = str(r.get(col_doi) or "").strip().lower()
            if doi and doi != "nan":
                by_doi[doi].append(r)

    rows = []
    compared = 0
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
        if not recs:
            continue
        doi = str(recs[0].get("DOI") or "").strip().lower()
        cands = by_doi.get(doi) if doi else []
        if not cands:
            continue
        for i, rec in enumerate(recs):
            coded = coded_criteria(rec.get("Fulltext_Exclusion_Criterion"))
            for srow in cands:
                sval = srow.get(col_crit)
                if sval in (None, "") or str(sval).strip().lower() == "nan":
                    continue
                scr = coded_criteria(sval)
                compared += 1
                if coded != scr:
                    rows.append(dict(
                        study_key=key, record=i,
                        screen_reason=str(srow.get(col_rew))[:200],
                        screen_says=";".join(sorted(scr)) or "(none)",
                        coded=";".join(sorted(coded)) or "(empty)",
                        overlap=";".join(sorted(coded & scr)) or "(none)",
                        screen_excluded=int(bool(scr)),
                        coded_excluded=int(bool(coded))))
                break

    with open(OUT, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=[
            "study_key", "record", "screen_reason", "screen_says", "coded",
            "overlap", "screen_excluded", "coded_excluded"])
        w.writeheader()
        w.writerows(rows)

    only_screen = sum(1 for r in rows if r["overlap"] == "(none)"
                      and r["screen_excluded"])
    only_coded = sum(1 for r in rows if r["overlap"] == "(none)"
                     and r["coded_excluded"])
    print("screening rows compared: %d" % compared)
    print("   the coding and the screen DISAGREE: %d" % len(rows))
    print("   the screen excluded it but the coding does not: %d" % only_screen)
    print("   the coding excluded it but the screen does not: %d" % only_coded)
    print()
    for r in rows[:18]:
        print("   %-36s screen=%-10s coded=%-10s overlap=%s"
              % (r["study_key"][:36], r["screen_says"], r["coded"],
                 r["overlap"]))
    print("\nwrote", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
