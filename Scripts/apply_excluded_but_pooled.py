"""Apply the campaign owner's decision: exclude the 26 studies the coding
marks as excluded but the pool still uses.

The decision (2026-10-21) was *exclude all 26 from the pool*.  The
exclusion mechanism in ``Scripts/study_exclusions.py`` is keyed by
``(study_key, experiment)``, so the generator works out which experiments of
each key have to go:

* a key that fails **C1** (no misinformation intervention) has *no* eligible
  experiment - no arm of it can supply a misinformation-intervention effect
  - so **all** of its experiments are excluded;
* a key that fails only **C2** or **C4** keeps the experiments that are
  genuine experiments with a control, and loses the ones whose rows were
  pooling an effect without one.

The script writes the new entries into ``Scripts/study_exclusions.py`` (with
a backup), then re-checks that the pooled effects really are gone.

Usage
-----
    python Scripts/apply_excluded_but_pooled.py            # dry run
    python Scripts/apply_excluded_but_pooled.py --apply
"""
import argparse
import csv
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime

import pandas as pd

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS = os.path.join(ROOT, "Results")
EXCLUSIONS = os.path.join(ROOT, "Scripts", "study_exclusions.py")
CSV_IN = os.path.join(RESULTS, "excluded_but_pooled.csv")
CSV_OUT = os.path.join(RESULTS, "excluded_but_pooled_decisions.csv")

MARKER = "    # --- excluded because the coding marks the study as excluded\n"
LIST_NAME = "CODING_EXCLUDED_STUDIES"


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
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--merge", action="store_true",
                    help="rewrite the list even when it is already there, so "
                         "records that have appeared since the last run are "
                         "added and ones that no longer qualify are dropped")
    args = ap.parse_args()

    rows = list(csv.DictReader(open(CSV_IN, encoding="utf-8-sig", newline="")))
    df = pd.read_excel(os.path.join(RESULTS, "metaanalysis.xlsx"),
                       sheet_name="DV_Metrics")

    entries = {}
    detail = []
    for key in sorted({r["study_key"] for r in rows}):
        d = json.load(open(os.path.join(ROOT, "Extraction", key + ".json"),
                           encoding="utf-8"))
        recs = d if isinstance(d, list) else d.get("experiments", [])
        sids = [str(r.get("Study_ID")) for r in recs]
        # the records of this key that the coding marks as excluded AND
        # that actually contribute pooled effects
        excluded = [r for r in rows if r["study_key"] == key]
        by_exp = defaultdict(list)
        for r in excluded:
            by_exp[r["experiment_id"]].append(r)
        for exp, exrows in by_exp.items():
            crit = set()
            for rec in recs:
                if str(rec.get("Study_ID")) == exp:
                    crit = criteria(rec.get("Fulltext_Exclusion_Criterion"))
                    break
            if "C1" in crit:
                rule = ("C1: the experiment delivers no misinformation "
                        "intervention, so it cannot supply a "
                        "misinformation-intervention effect")
            else:
                rule = ("%s: the experiment's rows were pooling an effect "
                        "without a valid comparison"
                        % ", ".join(sorted(crit)) or "excluded by the coding")
            entries[(key, exp)] = dict(
                criterion=", ".join(sorted(crit)) or "excluded by the coding",
                reason=("Excluded from the pooled analysis on the campaign "
                        "owner's decision (2026-10-21): %s. The study remains "
                        "fully coded, harmonised and reproduced; the criterion "
                        "is the screening layer and the pooled models select "
                        "on eligible_primary without reading it, so without "
                        "this entry the experiment kept contributing its "
                        "effects." % rule),
                kept=";".join(s for s in sids if s != exp) or "")
            detail.append(dict(
                study_key=key, experiment_id=exp,
                criteria=",".join(sorted(crit)),
                pooled_effects_removed=len(exrows),
                other_experiments_kept=";".join(s for s in sids if s != exp)))

    print("study keys: %d" % len({k for k, _ in entries}))
    print("experiments to exclude: %d" % len(entries))
    print("pooled effects removed: %d"
          % sum(d["pooled_effects_removed"] for d in detail))
    for (key, exp), e in sorted(entries.items()):
        print("   %-40s / %-30s %s" % (key[:40], exp[:30], e["criterion"]))
    print()

    with open(CSV_OUT, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=[
            "study_key", "experiment_id", "criteria", "pooled_effects_removed",
            "other_experiments_kept"])
        w.writeheader()
        w.writerows(detail)
    print("wrote", CSV_OUT)

    if not args.apply:
        print("\ndry run - pass --apply to write Scripts/study_exclusions.py")
        return 0

    src = open(EXCLUSIONS, encoding="utf-8").read()
    if LIST_NAME in src and not args.merge:
        print("\n%s is already in %s - refusing to write twice.\n"
              "Use --merge to add the records that have appeared since."
              % (LIST_NAME, os.path.basename(EXCLUSIONS)))
        return 1

    stamp = datetime.now().strftime("%Y-%m-%d")
    bak = os.path.join(ROOT, "archive",
                        "study_exclusions_%s.py" % stamp)
    with open(bak, "w", encoding="utf-8") as fh:
        fh.write(src)
    print("backup:", os.path.relpath(bak, ROOT))

    block = [MARKER, "%s = {\n" % LIST_NAME]
    for (key, exp), e in sorted(entries.items()):
        block.append('    (%r, %r): dict(\n' % (key, exp))
        block.append('        criterion=%r,\n' % e["criterion"])
        block.append('        reason=(\n            %r),\n'
                     % e["reason"])
        block.append('        kept=%r),\n' % e["kept"])
    block.append("}\n\n")
    # The list itself carries NO `ALL_EXCLUSIONS.update(...)` line: the
    # assembly happens once, after `ALL_EXCLUSIONS = {}`.  A list that
    # updates before the dict exists raises a NameError on import.

    # The list is derived from the audit product, so it is rewritten
    # whole each time: a record that has appeared since the last run is
    # added, and one that no longer qualifies is dropped.  That keeps the
    # list and the product in step.
    start = src.find("%s = {" % LIST_NAME)
    if start >= 0:
        end = src.find("\n}\n", start)
        if end < 0:
            print("could not find the end of %s" % LIST_NAME, file=sys.stderr)
            return 1
        end += 3
        head = src[:start]
        tail = src[end:]
        # drop the stray update line that the block may carry
        tail = tail.replace("ALL_EXCLUSIONS.update(%s)\n" % LIST_NAME, "", 1)
        src = head + "\n".join(block) + tail
    else:
        anchor = "ALL_EXCLUSIONS = {}"
        if anchor not in src:
            print("could not find the assembly point in study_exclusions.py",
                  file=sys.stderr)
            return 1
        src = src.replace(anchor, "\n".join(block) + "\n" + anchor, 1)

    # the assembly: the list must be added to the dict AFTER the dict
    # exists, and exactly once
    anchor = "ALL_EXCLUSIONS = {}"
    if anchor not in src:
        print("could not find the assembly point in study_exclusions.py",
              file=sys.stderr)
        return 1
    update = "ALL_EXCLUSIONS.update(%s)" % LIST_NAME
    if update not in src:
        src = src.replace(anchor, anchor + "\n" + update, 1)
    with open(EXCLUSIONS, "w", encoding="utf-8") as fh:
        fh.write(src)
    print("wrote", os.path.relpath(EXCLUSIONS, ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
