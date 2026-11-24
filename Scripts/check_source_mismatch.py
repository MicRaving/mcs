"""Find study keys whose coding does not match the paper it points at.

Two mismatches of this kind have already bitten the campaign: the O'Rourke
key's source was a different paper (Rozendaal 2016), and ``Cho et al.
(2023)`` still carries the **Litt et al. (2015)** record after its PDF was
replaced.  The second is invisible unless you compare the coding with the
paper.

This script does that comparison mechanically: it takes the distinctive
words of the record's title and asks whether the paper's own text contains
them.  A key whose title is not in its own paper is a source mismatch.

Products
--------
``Results/source_mismatch.csv``
"""
import csv
import glob
import json
import os
import re
import sys
from collections import Counter

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS = os.path.join(ROOT, "Results")
OUT = os.path.join(RESULTS, "source_mismatch.csv")

STOP = set("""a an the of and or in on for with to from at by as is are was were be
this that these those it its their our we you they he she them using use used
based study studies effect effects new more less between among what which who
""".split())


def title_words(title):
    ws = [w for w in re.findall(r"[A-Za-z][A-Za-z\-']{3,}", title or "")
          if w.lower() not in STOP]
    return ws


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
        if not recs:
            continue
        rec = recs[0]
        title = str(rec.get("Title") or "")
        ws = title_words(title)
        if len(ws) < 4:
            continue
        # the paper's own text, first ~40k characters
        txt = ""
        tp = os.path.join(ROOT, "Text", key + ".txt")
        if os.path.isfile(tp):
            try:
                txt = open(tp, encoding="utf-8", errors="replace").read()[:40000]
            except Exception:
                txt = ""
        if not txt:
            continue
        low = txt.lower()
        hit = sum(1 for w in ws if w.lower() in low)
        frac = hit / len(ws)
        if frac < 0.5:
            rows.append(dict(
                study_key=key,
                title=title[:140],
                doi=rec.get("DOI") or "",
                study_id=rec.get("Study_ID") or "",
                n_analyzed=rec.get("N_Analyzed"),
                title_words_in_paper="%d of %d" % (hit, len(ws)),
                fraction=round(frac, 2)))
    rows.sort(key=lambda r: r["fraction"])
    with open(OUT, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=[
            "study_key", "title", "doi", "study_id", "n_analyzed",
            "title_words_in_paper", "fraction"])
        w.writeheader()
        w.writerows(rows)
    print("keys whose title is largely absent from their own paper: %d"
          % len(rows))
    print()
    for r in rows[:25]:
        print("   %-42s %-14s doi=%-34s %s"
              % (r["study_key"][:42], r["title_words_in_paper"],
                 str(r["doi"])[:34], r["title"][:60]))
    print("\nwrote", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
