"""Find the keys where the canonical route and the agent routes disagree on
*which* experiment a record holds.

The triage already counts how many keys have a different number of
experiments.  That count mixes two very different things:

* the canonical route deliberately **merges** two of the paper's experiments
  into one record (a coding choice, not a defect), and
* the canonical route holds the paper's **main** experiment under the
  ``Exp1`` id that the agent routes use for the paper's **pilot** (a
  misalignment that breaks the IRR join key and hides a study from the
  analysis).

The second kind is detectable: the routes agree on the *number* of
experiments, but the per-experiment sample sizes do not line up, so the
canonical record must be holding a different experiment than the id says.
Where the canonical route has fewer records than the agent routes, and the
sample sizes disagree, the canonical route is missing an experiment.

Products
--------
``Results/experiment_alignment.csv``   the mismatches
"""
import csv
import json
import os
import sys
from collections import defaultdict

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS = os.path.join(ROOT, "Results")
OUT = os.path.join(RESULTS, "experiment_alignment.csv")


def load(path):
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
    except Exception:
        return None
    return d if isinstance(d, list) else d.get("experiments", [])


def sample(recs):
    """{experiment id: (N analyzed, N recruited)} per route."""
    out = {}
    for rec in recs or []:
        sid = str(rec.get("Study_ID") or "")
        n = rec.get("N_Analyzed")
        nr = rec.get("N_Recruited")
        try:
            n = int(n)
        except Exception:
            n = None
        try:
            nr = int(nr)
        except Exception:
            nr = None
        out[sid] = (n, nr)
    return out


def main():
    import glob
    rows = []
    for p in sorted(glob.glob(os.path.join(ROOT, "Extraction", "*.json"))):
        key = os.path.basename(p)[:-5]
        can = sample(load(p))
        sb = sample(load(os.path.join(ROOT, "Codings", key + " - SB.json")))
        mm = sample(load(os.path.join(ROOT, "Codings", key + " - MM.json")))
        agents = sb or mm
        if not can or not agents:
            continue
        if len(can) == len(agents):
            # same number of experiments: do the sample sizes line up per id?
            for sid, vals in can.items():
                a = agents.get(sid)
                if not a:
                    continue
                if vals[0] is not None and a[0] is not None and vals[0] != a[0]:
                    rows.append(dict(
                        study_key=key, kind="same_id_different_sample",
                        canonical_id=sid, canonical_n=vals[0], agent_n=a[0],
                        canonical_exps=len(can), agent_exps=len(agents)))
            continue
        # different number of experiments: the canonical route may be missing
        # one, or may have merged two on purpose
        canon_ns = {v[0] for v in can.values() if v[0] is not None}
        agent_ns = {v[0] for v in agents.values() if v[0] is not None}
        missing = agent_ns - canon_ns
        if missing:
            rows.append(dict(
                study_key=key, kind="canonical_missing_an_experiment",
                canonical_id=";".join(sorted(can)),
                canonical_n=";".join(str(x) for x in sorted(canon_ns)),
                agent_n=";".join(str(x) for x in sorted(missing)),
                canonical_exps=len(can), agent_exps=len(agents)))

    with open(OUT, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["study_key", "kind", "canonical_id",
                                           "canonical_n", "agent_n",
                                           "canonical_exps", "agent_exps"])
        w.writeheader()
        w.writerows(rows)

    kinds = defaultdict(int)
    for r in rows:
        kinds[r["kind"]] += 1
    print("experiment-alignment mismatches: %d" % len(rows))
    for k, v in sorted(kinds.items()):
        print("   %-34s %d" % (k, v))
    print()
    for r in rows[:12]:
        print("   %-38s %-32s canonical=%s agent=%s"
              % (r["study_key"][:38], r["kind"][:32], r["canonical_n"], r["agent_n"]))
    print("\nwrote", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
