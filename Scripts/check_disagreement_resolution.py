"""Check that the resolvable coder disagreements are resolved.

The disagreement triage sorts every difference between the three coding
routes into three classes (see ``Results/CODER_DISAGREEMENTS.md``):

* **rule** — two of the three routes carry the same value, so a script can
  settle it.  This class must be **closed**: every such value written into
  the analysis route, none pending.
* **paper** — the paper's wording settles it, so a master agent reads the
  paper.  This class is open until the master-agent queue is empty.
* **human** — a judgement the paper does not dictate.  This class is open
  by design: it is the human master's queue.

This script reports the state of each class and **fails** when the rule
class still holds a pending write, i.e. when a disagreement that a rule can
settle has been left unresolved.

Usage
-----
    python Scripts/check_disagreement_resolution.py
"""
import csv
import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS = os.path.join(ROOT, "Results")


def jload(name, default=None):
    path = os.path.join(RESULTS, name)
    if not os.path.isfile(path):
        return default
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def main():
    tiers = jload("coder_disagreement_tiers_summary.json", {}) or {}
    split = jload("coder_disagreement_split_summary.json", {}) or {}
    scope = jload("coder_master_scope.json", {}) or {}
    work = jload("coder_master_worklist.json", {}) or {}
    irr = jload("irr_baseline_comparison.json", {}) or {}

    applied = pending = 0
    fx = os.path.join(RESULTS, "coder_majority_fixes.csv")
    if os.path.isfile(fx):
        with open(fx, encoding="utf-8-sig", newline="") as fh:
            for r in csv.DictReader(fh):
                if r.get("pending") == "1":
                    pending += 1
                else:
                    applied += 1

    master_batches = master_done = 0
    state = os.path.join(ROOT, "temp", "mm_route", "master_state.json")
    if os.path.isfile(state):
        try:
            with open(state, encoding="utf-8") as fh:
                st = json.load(fh)
            b = st.get("batches") or {}
            master_batches = len(b)
            master_done = sum(1 for v in b.values()
                              if v.get("status") == "done")
        except Exception:
            pass

    irr_moved = sum(v.get("keys_with_a_moved_measure", 0)
                     for v in (irr.get("routes") or {}).values())

    classes = [
        ("rule", applied, "a script",
         "closed" if pending == 0 else "OPEN",
         "record-level field values settled by a 2-of-3 majority"),
        ("paper", (scope.get("pool_jobs", 0) or 0) + (scope.get("coding_jobs", 0) or 0),
         "a master agent",
         "closed" if master_batches and master_done >= master_batches else "open",
         "items the paper settles; %d of %d batches done" % (master_done, master_batches)),
        ("human", split.get("human_items", 0), "a human master",
         "open by design",
         "judgements the paper does not dictate (%d studies)"
         % split.get("human_keys", 0)),
        ("prose", (tiers.get("counts") or {}).get("prose_items", 0),
         "not adjudicated", "closed",
         "free-text differences: the same thing said differently"),
    ]

    print("coder-disagreement resolution state")
    print("-" * 78)
    for name, count, who, status, what in classes:
        print("  %-7s %8s  %-14s  %-16s  %s"
              % (name, count, who, status, what))
    print("-" * 78)
    print("  rule class: %d applied, %d pending" % (applied, pending))
    print("  paper class: %d items over %d study keys"
          % ((scope.get("pool_jobs", 0) or 0) + (scope.get("coding_jobs", 0) or 0),
             len(work)))
    print("  human class: %d items over %d study keys"
          % (split.get("human_items", 0), split.get("human_keys", 0)))
    print("  inter-rater reliability measures moved by the reconciliation: %d"
          % irr_moved)

    # a dependence the clustering cannot catch: the same trial published more
    # than once.  Not a rule question - the campaign owner decides which
    # outcomes are unique - but it must be visible in the gate.
    sm = os.path.join(RESULTS, "source_mismatch.csv")
    if os.path.isfile(sm):
        try:
            with open(sm, encoding="utf-8-sig", newline="") as fh:
                srows = list(csv.DictReader(fh))
        except Exception:
            srows = []
        if srows:
            print("  keys whose CODING is for a different paper: %d (%s)"
                  % (len(srows), ", ".join(r["study_key"] for r in srows)))
            print("     a pooled effect may sit under a key whose paper does "
                  "not report it (Results/source_mismatch.csv)")

    dup = os.path.join(RESULTS, "possible_duplicate_trials.csv")
    if os.path.isfile(dup):
        try:
            with open(dup, encoding="utf-8-sig", newline="") as fh:
                drows = list(csv.DictReader(fh))
        except Exception:
            drows = []
        if drows:
            dkeys = {r["key_a"] for r in drows} | {r["key_b"] for r in drows}
            print("  candidate same-trial publication pairs: %d over %d study "
                  "keys (Results/possible_duplicate_trials.csv) -- needs a "
                  "campaign-owner decision, not a rule"
                  % (len(drows), len(dkeys)))
    print()

    if pending:
        print("FAIL: %d rule-settable disagreement(s) are still pending."
              % pending)
        return 1
    print("PASS: every disagreement a rule can settle is resolved "
          "(%d applied, 0 pending)." % applied)
    if irr_moved:
        print("NOTE: %d inter-rater reliability measure(s) moved - the frozen "
              "baseline in Results/irr_baseline_<date>/ is the honest figure."
              % irr_moved)
    return 0


if __name__ == "__main__":
    sys.exit(main())
