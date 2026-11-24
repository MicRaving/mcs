"""Find publications that may report the same experiment.

The meta-analysis clusters non-independent contrasts by
``study_key | experiment_id | control_arm`` (``suite_common.contrast_cluster_id``),
so it can cluster *within* a paper but **not across papers**: when one trial
is published several times, each publication becomes its own cluster and the
same participants are counted more than once.

``Scripts/study_exclusions.py`` can drop a duplicate experiment, but a human
has to identify it first.  This script finds the candidates: study keys whose
analysed N coincides and whose title or intervention text overlaps, which is
what a re-report of the same trial looks like.

The output is a **candidate list for review**, not a decision: whether two
publications are the same trial, and which outcomes to keep, is a
substantive judgement for the campaign owner.

Products
--------
``Results/possible_duplicate_trials.csv``
"""
import csv
import glob
import json
import os
import re
import sys
from collections import defaultdict

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS = os.path.join(ROOT, "Results")
OUT = os.path.join(RESULTS, "possible_duplicate_trials.csv")

STOP = set("""a an the of and or in on for with to from at by as is are was were be
this that these those it its their our we you they he she them effects effect
study trial experiment participants participant results result using use across
among between new evidence media literacy training intervention information""".split())


def words(text):
    toks = re.findall(r"[a-z]{4,}", (text or "").lower())
    return {t for t in toks if t not in STOP}


def main():
    recs = []
    for p in sorted(glob.glob(os.path.join(ROOT, "Extraction", "*.json"))):
        key = os.path.basename(p)[:-5]
        try:
            d = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        for r in (d if isinstance(d, list) else d.get("experiments", [])):
            n = r.get("N_Analyzed")
            try:
                n = int(n)
            except Exception:
                n = None
            title = str(r.get("Title") or "")
            authors = str(r.get("Authors") or "")
            itype = r.get("Intervention_Type")
            recs.append(dict(
                key=key, sid=str(r.get("Study_ID") or ""), n=n, title=title,
                authors=authors, itype=itype,
                w=words(title + " " + authors + " " + str(itype))))

    by_n = defaultdict(list)
    for r in recs:
        if r["n"] and r["n"] >= 20:
            by_n[r["n"]].append(r)

    rows = []
    for n, group in sorted(by_n.items()):
        if len(group) < 2:
            continue
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = group[i], group[j]
                if a["key"] == b["key"]:
                    continue
                inter = a["w"] & b["w"]
                if len(inter) < 3:
                    continue
                jac = len(inter) / len(a["w"] | b["w"]) if (a["w"] | b["w"]) else 0
                # same first author surname counts as a strong hint
                fa = a["authors"].split(";")[0].split(",")[0].strip().lower()
                fb = b["authors"].split(";")[0].split(",")[0].strip().lower()
                same_author = bool(fa) and fa == fb
                if jac < 0.18 and not same_author:
                    continue
                rows.append(dict(
                    n_analyzed=n, key_a=a["key"], id_a=a["sid"],
                    key_b=b["key"], id_b=b["sid"],
                    same_first_author=int(same_author),
                    title_word_jaccard=round(jac, 3),
                    shared_words=";".join(sorted(inter)[:14]),
                    title_a=a["title"][:110], title_b=b["title"][:110]))

    with open(OUT, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["n_analyzed", "key_a", "id_a", "key_b",
                                           "id_b", "same_first_author",
                                           "title_word_jaccard", "shared_words",
                                           "title_a", "title_b"])
        w.writeheader()
        w.writerows(rows)

    keys = {r["key_a"] for r in rows} | {r["key_b"] for r in rows}
    print("candidate same-trial publication pairs: %d" % len(rows))
    print("study keys involved: %d" % len(keys))
    print()
    for r in sorted(rows, key=lambda x: (-x["same_first_author"],
                                          -x["title_word_jaccard"]))[:20]:
        print("   N=%-6s %-34s %-34s sameauthor=%d jac=%.2f"
              % (r["n_analyzed"], r["key_a"][:34], r["key_b"][:34],
                 r["same_first_author"], r["title_word_jaccard"]))
    print("\nwrote", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
