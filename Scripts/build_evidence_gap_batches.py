"""Build briefs for the human-queue keys the evidence pass has not read.

``Scripts/draft_human_decisions.py`` reports how many items still have no
evidence.  This script lists the keys and writes one brief per group, so
the remaining papers get read with the same rigour as the first ones.

The brief asks the questions at **study level**, not at item level.  The
human queue's items are mostly one flag on many rows of the same paper
("which contrast carries ``eligible_primary``?"), so a study-level answer
settles dozens of rows at once, and a paper is read once rather than once
per row.
"""
import csv
import json
import os
import sys
from collections import defaultdict

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS = os.path.join(ROOT, "Results")
EVID = os.path.join(RESULTS, "human_evidence")
QUEUE = os.path.join(RESULTS, "coder_disagreement_human_queue.csv")
OUT = os.path.join(ROOT, "temp", "mm_route",
                   os.environ.get("EVID_GAP_DIR", "evidence_gap_batches"))

HEAD = """You are gathering evidence for a decision the human master has to make.

Workspace: {root}

The three coding routes disagree about fields the paper does not settle on
its own.  The human master decides; your job is to read the paper and
report **what it shows**, with the quote and the page, so the decision is
a confirmation rather than a re-read.

The questions are asked at STUDY level: one answer settles many rows of
the same paper, and the paper is read once.

FIELDS AND WHAT THE ROUTES DISAGREE ABOUT
{fields}

Q1 - THE ELIGIBLE CONTRAST  (field `eligible_primary`)
  Which contrast in this study is the intervention against a control that
  received **no misinformation intervention**?  Read the paper's design:
  the arms, what each arm received, and what the control arm was.
  - If the study has such a contrast, name it with the paper's own words
    for the two arms, e.g. "correction vs misinformation only" - the
    label must be recognisable against the rows listed above.
  - If the study has **no** such contrast (a single arm, a pre/post
    change with no control, an exposure contrast), say so.
  - If the paper reports several such contrasts, name the one the paper
    itself treats as its primary test, and say why.

Q2 - THE DIRECTION OF EACH MEASURE  (field `dv_polarity`)
  For every measure listed above, say whether a HIGHER score is better or
  worse, with the page that fixes it.  The measures are listed per item
  below.

Q3 - THE SCREENING CRITERION  (field `Fulltext_Exclusion_Criterion`)
  Which of these apply, per experiment?
    C1  no misinformation intervention
    C2  no control / comparison group
    C3  not English
    C4  not a primary experimental study
  The field is recorded PER EXPERIMENT, so give the set per experiment.

FOR EACH KEY
  - Read `Papers/<Key>.pdf` and `Text/<Key>.txt`.
  - Write `Results/human_evidence/<Key>.md` in EXACTLY this format:

```
# <Key> - evidence for the human master

Sources read: Papers/<Key>.pdf , Text/<Key>.txt
Design: <one or two sentences: the arms, the sample, the outcomes>

## eligible_primary
Recommendation: <the contrast, in the paper's own words, or "none", or "no basis to set it">
Evidence: <one or two sentences, with the page>
Relevant quote: "<verbatim from the paper>"

## <measure name> (dv_polarity)
Recommendation: <0 or 1 - 0 means a higher score is worse, 1 means a higher score is better>
Evidence: <one or two sentences, with the page>
Relevant quote: "<verbatim from the paper>"

## Fulltext_Exclusion_Criterion
Recommendation: <C1,C2 - the criteria that apply, per experiment, or "none">
Evidence: <one or two sentences, with the page>
Relevant quote: "<verbatim from the paper>"
```

  - Use one `## <measure name> (dv_polarity)` block for each measure the
    items below name.  Copy the measure name exactly as the items give it.
  - If the paper does not support any value, say so in the recommendation
    and say what it does support instead.
  - Verify every quote verbatim against the paper text.

RULES
  - Do NOT edit any `Extraction/` or `Codings/` file.  You are producing
    evidence; the decision is the human's.
  - Do not read other keys' `Extraction/` or `Codings/` files, and do not
    grep across those directories.
  - Never invent a quote or a page number.
  - Scratch under `temp/mm_route/evgap/<your own subfolder>/` only, never
    delete anything under `temp/`, and never write into a shared file: the
    batches run at the same time and one agent overwrote another's
    scratch script.

KEYS IN THIS BATCH
{keys}

Reply with one line per key: key | the eligible contrast (or "none") | the
measures whose direction you settled | the criteria that apply.
"""

FIELDS_NOTE = """  eligible_primary   - which contrast is the intervention against a
                       control that received no misinformation intervention
  dv_polarity        - whether a higher score on the named measure is
                       better (1) or worse (0)
  Fulltext_Exclusion_Criterion
                    - which of C1 (no misinformation intervention),
                       C2 (no control group), C3 (not English),
                       C4 (not a primary experimental study) apply
"""


def main():
    rows = list(csv.DictReader(open(QUEUE, encoding="utf-8-sig", newline="")))
    have = set()
    if os.path.isdir(EVID):
        have = {fn[:-3] for fn in os.listdir(EVID) if fn.endswith(".md")}
    by_key = defaultdict(list)
    for r in rows:
        by_key[r["study_key"]].append(r)
    missing = [k for k in sorted(by_key) if k not in have]
    # a key another agent is reading right now must not be handed out a
    # second time: two agents writing the same evidence file race
    excl_file = os.environ.get("EVID_GAP_EXCLUDE")
    if excl_file and os.path.isfile(excl_file):
        excl = {ln.strip() for ln in open(excl_file, encoding="utf-8")
                if ln.strip()}
        missing = [k for k in missing if k not in excl]
        print("excluded %d keys another agent is reading" % len(excl))
    print("keys in the queue: %d | with evidence: %d | to read: %d"
          % (len(by_key), len(by_key) - len(missing), len(missing)))

    os.makedirs(OUT, exist_ok=True)
    for old in os.listdir(OUT):
        os.remove(os.path.join(OUT, old))
    per = int(os.environ.get("EVID_GAP_BATCH", "4"))
    n = 0
    for i in range(0, len(missing), per):
        chunk = missing[i:i + per]
        body, kb = [], []
        for k in chunk:
            kb.append("  KEY: %s" % k)
            measures, criteria = [], []
            for r in by_key[k]:
                if r["field"] == "dv_polarity" and r["dv"]:
                    if r["dv"] not in measures:
                        measures.append(r["dv"])
                if r["field"] == "Fulltext_Exclusion_Criterion":
                    criteria.append("C1-C4")
            if measures:
                body.append("  - %s: the measures below, and which one "
                            "carries the disputed flag" % k)
                for m in measures:
                    body.append("      measure: %s" % m)
            if criteria:
                body.append("  - %s: the screening criterion, per experiment"
                            % k)
        with open(os.path.join(OUT, "%03d.txt" % n), "w", encoding="utf-8") as fh:
            fh.write(HEAD.format(root=ROOT, keys="\n".join(kb),
                                 fields=FIELDS_NOTE + "\n" + "\n".join(body)))
        n += 1
    print("wrote %d briefs to %s" % (n, OUT))
    for k in missing:
        print("   ", k)
    return 0


if __name__ == "__main__":
    sys.exit(main())
