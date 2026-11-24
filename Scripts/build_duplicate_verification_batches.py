"""Build the briefs for the duplicate-trial verification pass.

Decision 4 (2026-10-21) was: exclude only the *confirmed* same-trial
pairs, so every candidate pair has to be checked against the paper before
it is excluded.  ``Scripts/find_duplicate_trials.py`` produces the
candidates; this script turns them into one brief per batch of papers.

The brief asks a single question per pair — *are these the same
participants, the same trial, or two different studies that happen to share
an N?* — and the answer decides whether the pair is excluded.
"""
import csv
import json
import os
import sys
from collections import defaultdict

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS = os.path.join(ROOT, "Results")
OUT = os.path.join(ROOT, "temp", "mm_route", "dup_batches")

HEAD = """You are verifying whether pairs of papers in a meta-analysis are the
SAME TRIAL, so that the same participants are not counted twice.

Workspace: {root}

The meta-analysis clusters non-independent contrasts by
`study_key | experiment_id | control_arm`, so it can cluster within a paper
but NOT across papers.  Where one trial is published more than once, or a
later paper re-analyses an earlier one, each becomes its own cluster and
the participants are counted more than once.

A candidate pair is only a candidate: two papers can share an analysed N, a
first author and a title vocabulary and still be different studies.

FOR EACH PAIR BELOW, read BOTH papers and decide:
  SAME TRIAL      - the same participants, measured in the same trial
                    (a duplicate publication, a thesis and its article, a
                    conference paper and its journal version), or a later
                    paper that RE-ANALYSES the earlier one's data
  DIFFERENT        - different participants, different recruitment, or
                    the second paper merely re-uses the same sample for a
                    genuinely new analysis
  UNSURE           - the papers do not say

Evidence to gather for every pair:
  - the analysed N of each paper, and whether it is the same N
  - the recruitment route and the sample frame
  - the intervention and the design: are they the same manipulation of the
    same people?
  - the date: a later paper reporting the same trial is a duplicate; a
    later paper on a DIFFERENT sample that cites the earlier one is not

THEN WRITE, for each pair, one record in
`Results/duplicate_trial_verdicts.csv` with the header:
  key_a, key_b, verdict, reason, evidence_page
where verdict is one of SAME TRIAL / DIFFERENT / UNSURE, the reason is one
or two sentences, and evidence_page names the page(s) in each paper that
carry the sample description.

ALSO: for every pair you judged SAME TRIAL, note which of the two should
be KEPT and which DROPPED, and say why in the reason (the one with the
full report, the peer-reviewed article rather than a thesis, the paper that
is not a re-analysis, and so on).

RULES
  - Do NOT edit any `Extraction/` or `Codings/` file and do NOT edit
    `Scripts/study_exclusions.py`.  You are producing evidence; the
    decision is applied afterwards.
  - Read only the papers listed here.  Do not grep across `Extraction/`
    or `Codings/`.
  - Never invent a quote or a page number.
  - Scratch under `temp/mm_route/dup/` only; never delete anything under
    `temp/`.

PAIRS IN THIS BATCH
{pairs}

Reply with one line per pair: key_a | key_b | verdict | kept | reason.
"""


def main():
    path = os.path.join(RESULTS, "possible_duplicate_trials.csv")
    if not os.path.isfile(path):
        print("no candidate list; run find_duplicate_trials.py first",
              file=sys.stderr)
        return 1
    rows = list(csv.DictReader(open(path, encoding="utf-8-sig", newline="")))
    pairs = sorted({(r["key_a"], r["key_b"]) for r in rows})
    keys = sorted({k for p in pairs for k in p})
    print("candidate pairs: %d over %d keys" % (len(pairs), len(keys)))

    os.makedirs(OUT, exist_ok=True)
    for old in os.listdir(OUT):
        os.remove(os.path.join(OUT, old))
    per = int(os.environ.get("DUP_BATCH", "6"))
    n = 0
    for i in range(0, len(pairs), per):
        chunk = pairs[i:i + per]
        body = []
        for a, b in chunk:
            body.append("  PAIR: %s   <->   %s" % (a, b))
            body.append("     papers: Papers/%s.pdf , Papers/%s.pdf" % (a, b))
        with open(os.path.join(OUT, "%03d.txt" % n), "w", encoding="utf-8") as fh:
            fh.write(HEAD.format(root=ROOT, pairs="\n".join(body)))
        n += 1
    print("wrote %d briefs to %s" % (n, OUT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
