"""Apply the confirmed same-trial exclusions (decision D4, 2026-10-21).

The campaign owner's decision was to exclude **only the confirmed**
same-trial pairs, so every candidate pair had to be checked against the
paper first.  The verification agents' verdicts are consolidated in
``Results/duplicate_trial_verdicts_consolidated.csv``; this script turns
the ``SAME TRIAL`` rows into entries in ``Scripts/study_exclusions.py``.

Safety rules
------------
* a pair the verifiers disagree about (``CONTESTED``) is **not**
  excluded - the owner said *confirmed only*, and a contested pair is not
  confirmed;
* a pair with no paper named as the one to keep is not excluded either;
* a paper that two confirmed pairs both want to keep is reported and
  **not** dropped, because dropping it would lose a study;
* the exclusion is written as a ``"*"`` experiment label, which
  ``Scripts/study_exclusions.py`` reads as "every experiment of this
  study key" - a confirmed duplicate is the same trial, so no experiment
  of it may be pooled again;
* every write goes to a fresh dict in ``Scripts/study_exclusions.py``
  and the file is backed up first.

Usage
-----
    python Scripts/apply_duplicate_verdicts.py            # dry run
    python Scripts/apply_duplicate_verdicts.py --apply
"""
import argparse
import csv
import os
import sys
from collections import Counter
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS = os.path.join(ROOT, "Results")
VERDICTS = os.path.join(RESULTS, "duplicate_trial_verdicts_consolidated.csv")
EXCLUSIONS = os.path.join(ROOT, "Scripts", "study_exclusions.py")
OUT = os.path.join(RESULTS, "duplicate_trial_decisions.csv")

LIST_NAME = "CONFIRMED_DUPLICATE_TRIALS"
MARKER = ("    # --- same trial confirmed against the paper "
          "(decision D4, 2026-10-21)\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    if not os.path.isfile(VERDICTS):
        print("no consolidated verdicts: %s" % VERDICTS, file=sys.stderr)
        return 1
    rows = list(csv.DictReader(open(VERDICTS, encoding="utf-8-sig", newline="")))
    if not rows:
        print("the consolidated verdicts file is empty")
        return 1

    counts = Counter(r["verdict"].strip().upper() for r in rows)
    print("pairs recorded: %d" % len(rows))
    for v, n in counts.most_common():
        print("   %-14s %d" % (v, n))

    entries = {}
    skipped = []
    for r in rows:
        verdict = r["verdict"].strip().upper()
        keep = (r.get("keep") or "").strip()
        drop = (r.get("drop") or "").strip()
        scope = [s for s in (r.get("drop_experiments") or "").split(";") if s]
        if verdict != "SAME TRIAL":
            if verdict == "CONTESTED":
                skipped.append((r["key_a"], r["key_b"],
                                "the verifiers disagree - not confirmed"))
            continue
        if not keep or not drop:
            skipped.append((r["key_a"], r["key_b"],
                            "no paper named as the one to keep"))
            continue
        if keep == drop:
            skipped.append((r["key_a"], r["key_b"], "keep and drop are the "
                                                      "same paper"))
            continue
        if drop in entries:
            skipped.append((r["key_a"], r["key_b"],
                            "%s is already dropped by another confirmed pair"
                            % drop))
            continue
        labels = scope or ["*"]
        for label in labels:
            if (drop, label) in entries:
                skipped.append((r["key_a"], r["key_b"],
                                "%s / %s is already dropped" % (drop, label)))
                continue
            entries[(drop, label)] = dict(
                duplicate_of=keep,
                reason=("Same trial as %s, confirmed against the paper "
                        "(decision D4, 2026-10-21): %s"
                        % (keep, (r.get("note") or "").strip()[:400])),
                kept=keep)

    print("\nexperiment entries to drop: %d over %d papers"
          % (len(entries), len({k for k, _ in entries})))
    for (drop, label), e in sorted(entries.items()):
        print("   drop %-38s / %-22s  keep %s"
              % (drop[:38], label[:22], e["duplicate_of"]))
    if skipped:
        print("\nnot excluded: %d" % len(skipped))
        for a, b, why in skipped:
            print("   %-36s <-> %-36s  %s" % (a[:36], b[:36], why))

    with open(OUT, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=[
            "key_a", "key_b", "verdict", "dropped", "dropped_experiments",
            "kept", "reason"])
        w.writeheader()
        for r in rows:
            drop = (r.get("drop") or "").strip()
            applied = []
            for label in (r.get("drop_experiments") or "").split(";"):
                if label and (drop, label) in entries:
                    applied.append(label)
            if drop and not applied and (drop, "*") in entries:
                applied = ["*"]
            w.writerow(dict(
                key_a=r["key_a"], key_b=r["key_b"], verdict=r["verdict"],
                dropped=drop if applied else "",
                dropped_experiments=";".join(applied),
                kept=(r.get("keep") or ""), reason=r.get("note") or ""))
    print("\nwrote", OUT)

    if not args.apply:
        print("dry run - pass --apply to write Scripts/study_exclusions.py")
        return 0
    if not entries:
        print("\nnothing confirmed to exclude; no write")
        return 0

    src = open(EXCLUSIONS, encoding="utf-8").read()
    if LIST_NAME in src:
        print("\n%s is already in study_exclusions.py - refusing to write "
              "twice." % LIST_NAME)
        return 1
    bak = os.path.join(ROOT, "archive",
                        "study_exclusions_%s.py"
                        % datetime.now().strftime("%Y-%m-%d-%H%M%S"))
    with open(bak, "w", encoding="utf-8") as fh:
        fh.write(src)
    print("backup:", os.path.relpath(bak, ROOT))

    block = [MARKER, "%s = {\n" % LIST_NAME]
    for (drop, label), e in sorted(entries.items()):
        block.append("    (%r, %r): dict(\n" % (drop, label))
        block.append("        duplicate_of=%r,\n" % e["duplicate_of"])
        block.append("        reason=(\n            %r),\n" % e["reason"])
        block.append("        kept=%r),\n" % e["kept"])
    block.append("}\n\n")

    anchor = "ALL_EXCLUSIONS = {}"
    if anchor not in src:
        print("could not find the assembly point", file=sys.stderr)
        return 1
    src = src.replace(anchor, "\n".join(block) + "\n" + anchor, 1)
    src = src.replace(
        "ALL_EXCLUSIONS.update(CODING_EXCLUDED_STUDIES)",
        "ALL_EXCLUSIONS.update(CODING_EXCLUDED_STUDIES)\n"
        "ALL_EXCLUSIONS.update(%s)" % LIST_NAME)
    with open(EXCLUSIONS, "w", encoding="utf-8") as fh:
        fh.write(src)
    print("wrote", os.path.relpath(EXCLUSIONS, ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
