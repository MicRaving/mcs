"""Build the human-evidence pass.

The 166 items in the human master's queue are judgements the paper does not
dictate — most of them ``Fulltext_Exclusion_Criterion`` (does the study
meet every inclusion criterion?) and ``eligible_primary`` (is this contrast
the primary eligible one?).  The master agent reads the paper for these
items but must NOT write the answer: the call is the human's.

What it *can* do is gather the evidence, so the human spends a minute
confirming rather than an hour re-reading.  This script builds one prompt
per study key: read the paper, state what it shows for each item, quote the
page, and write the finding to ``Results/human_evidence/<Key>.md`` — a
**separate file**, leaving the canonical coding untouched.

Products
--------
``temp/mm_route/evidence_batches/<NN>.txt``   the prompts
``Results/human_evidence/<Key>.md``            the evidence (written by the agents)
"""
import csv
import json
import os
import sys
from collections import defaultdict

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS = os.path.join(ROOT, "Results")
OUT_DIR = os.path.join(RESULTS, "human_evidence")
BATCH_DIR = os.path.join(ROOT, "temp", "mm_route", "evidence_batches")
QUEUE = os.path.join(RESULTS, "coder_disagreement_human_queue.csv")

HEAD = """You are gathering EVIDENCE for the human master of the
misinformation-intervention meta-analysis.

Workspace: {root}

The corpus was coded three times independently (canonical / SB / MM).  Some
disagreements are judgements the paper does not settle on its own, so a person
decides them.  Your job is NOT to decide.  Your job is to read the paper and
report **what it shows**, so the human can confirm or override in a minute.

STUDY KEYS IN THIS BATCH
{keys}

FOR EACH KEY, FOR EACH ITEM BELOW
  1. Read the paper: `Papers/<Key>.pdf` and `Text/<Key>.txt`.
  2. Work out what the paper shows for that item, and quote the page.
  3. Write a short finding to `Results/human_evidence/<Key>.md`, using EXACTLY
     this format (one block per item, under a `## <field>` heading):

```
## <field>
Recommendation: <the value you would set, or "no basis to set it">
Evidence: <one or two sentences, with the page>
Relevant quote: "<verbatim from the paper>"
```

     Use the item's own field name as the heading.  For a DV item, use
     `## <dv name> (<field>)`.

  4. If the paper does not support any value, say so in the recommendation
     and say what it does support instead.

RULES
  - Do NOT edit `Extraction/<Key>.json`.  These are the human's decisions;
    your file is evidence only.
  - Do NOT read any other key's `Extraction/` or `Codings/` file, and do not
    grep across those directories.
  - Never invent a quote.  If you cannot find the text, say so.
  - Scratch under `temp/mm_route/evidence/` only; never delete anything
    under `temp/`.

FIELD NOTES
  - `Fulltext_Exclusion_Criterion`: leave EMPTY when the study meets every
    inclusion criterion.  Otherwise list the unmet criterion(s): C1 = no
    misinformation intervention; C2 = no control/comparison group; C3 = not
    English; C4 = not a primary experimental study.  A study that fails a
    criterion is still fully coded — the field only keeps the screening layer
    consistent.  Note that a record-level field is per *experiment*: a paper
    can have one experiment that qualifies and another that does not.
  - `eligible_primary`: is this contrast the one intervention-vs-control
    comparison that qualifies for the primary pooled effect?  Report what
    the paper's table shows, and whether only one contrast per experiment
    qualifies.
  - `dv_polarity`: 1 = a higher score is better/protective, 0 = a higher score
    is worse.  Report which way the measure runs.

Reply with one line per key: `<key> | items covered | the single most important
thing the human should look at`.
"""

ITEMS = {
    "eligible_primary":
        "is this contrast the one intervention-vs-control comparison that "
        "qualifies for the primary pooled effect?",
    "dv_polarity":
        "which way does the measure run? (1 = higher is better/protective, "
        "0 = higher is worse)",
    "Fulltext_Exclusion_Criterion":
        "does the study meet every inclusion criterion? (leave empty if yes; "
        "otherwise C1 no misinformation intervention, C2 no control group, "
        "C3 not English, C4 not a primary experimental study)",
}


def main():
    if not os.path.isfile(QUEUE):
        print("no human queue; run split_coder_disagreements.py first",
              file=sys.stderr)
        return 1
    rows = list(csv.DictReader(open(QUEUE, encoding="utf-8-sig", newline="")))
    by_key = defaultdict(list)
    for r in rows:
        by_key[r["study_key"]].append(r)

    os.makedirs(OUT_DIR, exist_ok=True)
    os.makedirs(BATCH_DIR, exist_ok=True)
    for old in os.listdir(BATCH_DIR):
        os.remove(os.path.join(BATCH_DIR, old))

    keys = sorted(by_key)
    per = int(os.environ.get("EVIDENCE_BATCH", "5"))
    n = 0
    for i in range(0, len(keys), per):
        chunk = keys[i:i + per]
        parts = [HEAD.format(root=ROOT,
                             keys="\n".join("  - " + k for k in chunk))]
        for key in chunk:
            parts.append("=" * 72)
            parts.append("Study key: %s" % key)
            parts.append("Sources : Papers/%s.pdf , Text/%s.txt" % (key, key))
            parts.append("Output  : Results/human_evidence/%s.md" % key)
            parts.append("")
            for r in by_key[key]:
                label = r["field"] or r["dv"]
                parts.append("  - item: %s" % label)
                parts.append("    question: %s"
                             % ITEMS.get(r["field"], "what the paper shows"))
                parts.append("    competing values: %s" % r["routes"])
        with open(os.path.join(BATCH_DIR, "%03d.txt" % n), "w",
                  encoding="utf-8") as fh:
            fh.write("\n".join(parts))
        n += 1

    print("human items: %d across %d study keys" % (len(rows), len(keys)))
    print("wrote %d evidence prompts to %s" % (n, BATCH_DIR))
    print("evidence lands in %s" % OUT_DIR)
    return 0


if __name__ == "__main__":
    sys.exit(main())
