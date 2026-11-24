"""Reconcile the duplicate-trial verdicts.

The verification agents worked in parallel, so two of them sometimes
judged the same pair differently, and a pair can be judged in opposite
directions (``A`` and ``B`` both keep themselves).  Both are the owner's
call, so this script does not decide: it finds the conflicts, checks that
the confirmed pairs are consistent, and writes the summary the owner
needs to apply the exclusions.

Products
--------
``Results/duplicate_trial_verdicts_conflicts.csv``  the conflicting pairs
``Results/duplicate_trial_summary.csv``            every pair, one row
"""
import csv
import os
import re
import sys
from collections import Counter, defaultdict

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS = os.path.join(ROOT, "Results")
VERDICTS = os.path.join(RESULTS, "duplicate_trial_verdicts.csv")
CONFLICTS = os.path.join(RESULTS, "duplicate_trial_verdicts_conflicts.csv")
SUMMARY = os.path.join(RESULTS, "duplicate_trial_summary.csv")

SAME = ("SAME TRIAL", "SAME", "SAME_TRIAL")
DIFFERENT = ("DIFFERENT", "DISTINCT")
UNSURE = ("UNSURE", "UNCLEAR", "UNKNOWN")


def norm_verdict(text):
    t = (text or "").strip().upper()
    for s in SAME:
        if s in t:
            return "SAME TRIAL"
    for d in DIFFERENT:
        if d in t:
            return "DIFFERENT"
    for u in UNSURE:
        if u in t:
            return "UNSURE"
    return t or "UNKNOWN"


def norm_pair(a, b):
    x, y = sorted([(a or "").strip(), (b or "").strip()])
    return (x, y)


def kept_paper(text, a, b):
    """The paper the verdict says to keep.

    The verifiers write "keep <paper>, drop <paper>" or "KEEP: <paper>
    (DROP: <paper>)", so the paper named after a *keep* word and not
    after a *drop* word is the one to keep.
    """
    low = (text or "").lower()
    keeps, drops = set(), set()
    for name in (a, b):
        n = name.lower()
        for m in re.finditer(r"keep[^.;|]{0,40}", low):
            if n in m.group(0):
                keeps.add(name)
        for m in re.finditer(r"drop[^.;|]{0,40}", low):
            if n in m.group(0):
                drops.add(name)
    return (keeps - drops).pop() if len(keeps - drops) == 1 else ""


def main():
    if not os.path.isfile(VERDICTS):
        print("no verdicts file")
        return 1
    rows = list(csv.DictReader(open(VERDICTS, encoding="utf-8-sig", newline="")))
    if not rows:
        print("the verdicts file is empty")
        return 1

    by_pair = defaultdict(list)
    for r in rows:
        a = r.get("key_a") or r.get("Key_A") or ""
        b = r.get("key_b") or r.get("Key_B") or ""
        by_pair[norm_pair(a, b)].append(r)

    conflicts = []
    for pair, rs in sorted(by_pair.items()):
        verdicts = {norm_verdict(r.get("verdict")) for r in rs}
        if len(verdicts) > 1:
            conflicts.append(dict(
                key_a=pair[0], key_b=pair[1],
                problem="the verifiers disagree",
                detail=" | ".join(
                    "%s: %s (%s)" % (r.get("verdict", "").strip()[:40],
                                     r.get("reason", "")[:120],
                                     r.get("evidence_page", "")[:40])
                    for r in rs)))
            continue
        v = verdicts.pop()
        if v != "SAME TRIAL":
            continue
        keeps = {kept_paper(" ".join(str(r.get(k) or "") for k in
                                      ("verdict", "reason")), pair[0], pair[1])
                 for r in rs}
        keeps.discard("")
        if len(keeps) > 1:
            conflicts.append(dict(
                key_a=pair[0], key_b=pair[1],
                problem="the verifiers keep different papers",
                detail=" | ".join(
                    "%s: %s" % (r.get("verdict", "")[:40],
                               r.get("reason", "")[:140]) for r in rs)))
        elif not keeps:
            conflicts.append(dict(
                key_a=pair[0], key_b=pair[1],
                problem="no paper is named as the one to keep",
                detail=" | ".join(
                    "%s: %s" % (r.get("verdict", "")[:40],
                               r.get("reason", "")[:140]) for r in rs)))


    with open(CONFLICTS, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["key_a", "key_b", "problem",
                                           "detail"])
        w.writeheader()
        w.writerows(conflicts)

    summary = []
    for pair, rs in sorted(by_pair.items()):
        verdicts = [norm_verdict(r.get("verdict")) for r in rs]
        summary.append(dict(
            key_a=pair[0], key_b=pair[1],
            verdict=Counter(verdicts).most_common(1)[0][0],
            n_verdicts=len(rs),
            agreeing=len(set(verdicts)) == 1,
            evidence=" || ".join(r.get("reason", "")[:300] for r in rs),
            pages=" || ".join(r.get("evidence_page", "") for r in rs)))
    with open(SUMMARY, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["key_a", "key_b", "verdict",
                                           "n_verdicts", "agreeing",
                                           "evidence", "pages"])
        w.writeheader()
        w.writerows(summary)

    print("verdict rows: %d over %d distinct pairs"
          % (len(rows), len(by_pair)))
    counts = Counter(norm_verdict(r.get("verdict")) for r in rows)
    for v, n in counts.most_common():
        print("   %-14s %d" % (v, n))
    print("\npairs with a conflict: %d" % len(conflicts))
    for c in conflicts:
        print("   %-40s <-> %-40s  %s"
              % (c["key_a"][:40], c["key_b"][:40], c["problem"]))
    print("\nwrote", CONFLICTS)
    print("wrote", SUMMARY)
    return 0


if __name__ == "__main__":
    sys.exit(main())
