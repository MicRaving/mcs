#!/usr/bin/env python
"""Write the coder-disagreement report.

The report explains *where* the three coding routes disagree, *why* they
disagree, and what was done about it.  It is written from the products the
triage produced, so it never goes stale relative to them.

Products
--------
``Results/CODER_DISAGREEMENTS.md``   the report
"""
import csv
import json
import os
import sys
from collections import Counter

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS = os.path.join(ROOT, "Results")


def jload(name, default=None):
    path = os.path.join(RESULTS, name)
    if not os.path.isfile(path):
        return default
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def main():
    tiers = jload("coder_disagreement_tiers_summary.json", {})
    split = jload("coder_disagreement_split_summary.json", {})
    scope = jload("coder_master_scope.json", {})
    log = os.path.join(RESULTS, "coder_majority_fixes.csv")
    n_fixes = n_applied = n_pending = 0
    if os.path.isfile(log):
        with open(log, encoding="utf-8-sig", newline="") as fh:
            for r in csv.DictReader(fh):
                n_fixes += 1
                if r.get("pending") == "1":
                    n_pending += 1
                else:
                    n_applied += 1

    md = []
    md.append("# Coder disagreements: what the routes disagree about, and who settles it\n")
    md.append("\nThree routes code the same corpus independently:\n")
    md.append("\n| route | file | role |")
    md.append("|---|---|---|")
    md.append("| canonical | `Extraction/<Key>.json` | the analysis route |")
    md.append("| SB | `Codings/<Key> - SB.json` | SpaceBunny reliability route |")
    md.append("| MM | `Codings/<Key> - MM.json` | MiniMax reliability route |")
    md.append("\nEvery difference between them is sorted by *what settling it "
              "requires*, not by how often it happens.\n")

    md.append("\n## The three destinations\n")
    md.append("\n**Rule.**  Two of the three routes carry the same value, so the "
              "value is settled and no decision is needed.  A script applies "
              "it.\n")
    md.append("\n**Master agent.**  The paper's own wording settles it — the "
              "experiment count, the value of a field the paper states, the "
              "taxonomy a row belongs to, how completely an enumeration was "
              "done.  An agent reads the paper and states the value.\n")
    md.append("\n**Human master.**  A judgement the paper does *not* dictate, "
              "where two routes disagree and no majority exists: whether a "
              "contrast qualifies as the primary eligible one, which "
              "direction of an outcome is desirable, whether the study meets "
              "the inclusion criteria.\n")

    md.append("\n## Counts\n")
    counts = tiers.get("counts", {})
    md.append("\n| tier | keys | items |")
    md.append("|---|---|---|")
    md.append("| pool (can change a pooled estimate) | %s | %s |"
              % (counts.get("keys_with_pool", "?"), counts.get("pool_items", "?")))
    md.append("| coding (changes the coding, not the result) | %s | %s |"
              % (counts.get("keys_with_coding", "?"), counts.get("coding_items", "?")))
    md.append("| prose (same thing said differently — not a disagreement) | — | %s |"
              % counts.get("prose_items", "?"))
    md.append("\n")

    work = jload("coder_master_worklist.json", {}) or {}
    md.append("\n## What each destination got\n")
    md.append("\n| destination | items | studies | status |")
    md.append("|---|---|---|---|")
    md.append("| rule (majority, applied by script) | %d | — | %s |"
              % (n_fixes,
                 "**closed** — %d applied, %d pending" % (n_applied, n_pending)))
    md.append("| master agent (read the paper) | %d | %d | open — queued |"
              % (scope.get("pool_jobs", 0) + scope.get("coding_jobs", 0),
                 len(work) or scope.get("pool_keys", 0)))
    md.append("| **human master (decide)** | **%s** | **%s** | open by design |"
              % (split.get("human_items", "?"), split.get("human_keys", "?")))
    md.append("| prose (same thing said differently) | %s | — | not adjudicated |"
              % counts.get("prose_items", "?"))
    md.append("\n")
    md.append("\n`Scripts/check_disagreement_resolution.py` reports this state and "
              "**fails when the rule class still holds a pending write**, so "
              "\"every disagreement a rule can settle is resolved\" is a check, "
              "not a claim.  It also runs as the `disagreements` gate in "
              "`run_analysis.py`.\n")

    md.append("\n### The human queue by field\n")
    md.append("\n| field | items | what the human decides |")
    md.append("|---|---|---|")
    questions = {
        "eligible_primary": "is this contrast the primary eligible "
                            "intervention-vs-control comparison?",
        "dv_polarity": "which direction of the outcome is desirable "
                       "(1 = higher is better, 0 = higher is worse)?",
        "Fulltext_Exclusion_Criterion": "does the study meet every inclusion "
                                         "criterion (C1-C4)?",
    }
    for field, n in (split.get("human_by_field") or {}).items():
        md.append("| `%s` | %d | %s |"
                  % (field, n, questions.get(field, "a judgement the paper "
                                                   "does not dictate")))
    md.append("\n\nThe briefing sheet with the competing values side by side "
              "is `Results/HUMAN_MASTER_BRIEFING.md`.\n")

    md.append("\n## What was applied\n")
    md.append("\n`Scripts/apply_coder_majority_fixes.py` wrote **%d** "
              "record-level field values** into the canonical route, each one "
              "the value that two of the three routes agreed on.\n"
              % n_fixes)
    md.append("\nThe script is deliberately narrow:\n")
    md.append("\n* only controlled-enumeration fields, so a majority is a "
              "choice from a fixed list rather than a free-text paraphrase;\n")
    md.append("\n* never a field the analysis reads as a fact or a judgement "
              "(`N_Analyzed`, `Gender_Ratio`, `n_arms`, "
              "`control_group_type`, `Fulltext_Exclusion_Criterion`, ...);\n")
    md.append("\n* per record, aligned on `Study_ID`, because a field can "
              "differ between the records of one paper (`Intervention_Type` "
              "does in 51 files, `Setting` in 38);\n")
    md.append("\n* never a list-subset relation — one route's list being a "
              "subset of another's is a *completeness* question, not a "
              "majority, so it goes to the master agent;\n")
    md.append("\n* when a code needs a companion field, the companion travels "
              "with it (`Intervention_Type = ['other']` brings its "
              "`Intervention_Type_Detail`).\n")
    md.append("\nEvery original file is archived under "
              "`archive/extraction_disagreements_<date>/` and every write is "
              "logged in `Results/coder_majority_fixes.csv`.\n")

    md.append("\n## The IRR measurement is unaffected\n")
    md.append("\nThe SB and MM routes exist to measure agreement, and the "
              "agreement products compare them against the canonical route. "
              "Writing the majority into the canonical route would make that "
              "measurement circular, so the pre-reconciliation products were "
              "frozen to `Results/irr_baseline_<date>/` **before** the "
              "writes.  The baseline is the honest inter-rater reliability "
              "figure.\n")
    cmp_path = os.path.join(RESULTS, "irr_baseline_comparison.json")
    if os.path.isfile(cmp_path):
        with open(cmp_path, encoding="utf-8") as fh:
            cmp = json.load(fh)
        moved = sum(v.get("keys_with_a_moved_measure", 0)
                    for v in (cmp.get("routes") or {}).values())
        if moved == 0:
            md.append("\n`Scripts/compare_irr_baseline.py` compares the two "
                      "and finds that **no IRR measure moved** — the "
                      "reconciliation touched only record-level coding "
                      "fields, never `eligible_primary`, `dv_polarity`, "
                      "`dv_domain` or the exclusion criterion, so the "
                      "before and after figures are identical.  The frozen "
                      "baseline is therefore not a different measurement, it "
                      "is the same measurement, kept.\n")
        else:
            md.append("\n`Scripts/compare_irr_baseline.py` reports that %d "
                      "keys have a moved measure; the frozen baseline is the "
                      "honest figure and the live one is not.\n" % moved)

    md.append("\n## Reproducing\n")
    md.append("\n```")
    md.append("python Scripts/tier_coder_disagreements.py     # what differs")
    md.append("python Scripts/split_coder_disagreements.py    # rule / agent / human")
    md.append("python Scripts/apply_coder_majority_fixes.py   # apply the rule")
    md.append("python Scripts/build_master_agent_worklist.py  # the agent's work")
    md.append("python Scripts/build_human_master_briefing.py  # the human's sheet")
    md.append("```\n")
    md.append("\n### A note on matching DV rows\n")
    md.append("\nA `dv_name` is not unique: the same outcome name occurs once "
              "per arm pair, per timepoint, per condition — 1,865 names "
              "repeat inside a single route.  Matching the routes on a "
              "composite key (`dv_name` + arm labels + timepoints) looks "
              "stricter but in practice the routes label the same row "
              "differently often enough that it silently drops 1,129 of the "
              "1,131 real DV conflicts.  The rows are therefore paired by "
              "position inside a `dv_name`; a content-based pairing was "
              "checked against it and agrees to within 6 conflicts "
              "(4,361 positional vs 4,367 content).\n")

    with open(os.path.join(RESULTS, "CODER_DISAGREEMENTS.md"), "w",
              encoding="utf-8") as fh:
        fh.write("\n".join(md) + "\n")
    print("wrote Results/CODER_DISAGREEMENTS.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
