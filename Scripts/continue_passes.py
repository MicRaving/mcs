"""Hand out the next master-agent and evidence batches, in one go.

The two passes that resolve the coder disagreements are long, and the
provider's free tier rate-limits, so they are meant to be run in
bursts.  This script claims the next batches of both, marks them as
running, and prints the prompt for each, so a session can dispatch them
and pick up where the last one stopped.

    python Scripts/continue_passes.py                 # 2 master + 2 evidence
    python Scripts/continue_passes.py --master 4 --evidence 4
    python Scripts/continue_passes.py --evidence-only

A batch that the provider refuses is released again with
``--set-status todo``; it is not a failed batch, it is a batch nobody
worked on.
"""
import argparse
import os
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MASTER_DIR = os.path.join(ROOT, "temp", "mm_route", "master_batches")
EVID_DIR = os.path.join(ROOT, "temp", "mm_route", "evidence_gap_batches_v2")
MASTER = os.path.join(ROOT, "Scripts", "master_agent.py")


def run(*args):
    subprocess.check_call([sys.executable] + list(args), cwd=ROOT)


def hand_out_master(n):
    """Claim n master batches and print their prompts."""
    import json
    state_path = os.path.join(ROOT, "temp", "mm_route", "master_state.json")
    state = json.load(open(state_path, encoding="utf-8"))
    batches = state.setdefault("batches", {})
    ids = sorted(os.path.basename(p)[:-4] for p in os.listdir(MASTER_DIR)
                 if p.endswith(".txt"))
    done = {b for b, v in batches.items() if v.get("status") == "done"}
    running = {b for b, v in batches.items() if v.get("status") == "running"}
    todo = [b for b in ids if b not in done and b not in running]
    picked = todo[:n]
    for b in picked:
        run(MASTER, "--mark", b, "--set-status", "running")
    print("=" * 72)
    print("MASTER BATCHES  (%d of %d todo)" % (len(picked), len(todo)))
    print("=" * 72)
    for b in picked:
        path = os.path.join(MASTER_DIR, b + ".txt")
        print("\n--- dispatch this prompt to a master agent ---")
        print("Read the file `%s` and follow it exactly. It is your complete "
              "brief: the study keys, the disputed items, the field "
              "vocabularies, the rules, and the output format. The brief is "
              "the only place you may learn the routes' values for these "
              "keys - do not open `Results/coder_*queue*.csv` or any other "
              "queue file.\n"
              "When you land, mark the batch done: "
              "`python Scripts/master_agent.py --mark %s --set-status done`."
              % (path, b))
    return len(picked)


def hand_out_evidence(n):
    """Claim n evidence briefs and print their prompts."""
    import json
    state_path = os.path.join(ROOT, "temp", "mm_route", "evidence_state.json")
    state = {}
    if os.path.isfile(state_path):
        state = json.load(open(state_path, encoding="utf-8"))
    done = set(state.get("done") or [])
    files = sorted(f for f in os.listdir(EVID_DIR) if f.endswith(".txt"))
    todo = [f for f in files if f[:-4] not in done]
    picked = todo[:n]
    state.setdefault("done", [])
    for f in picked:
        state["done"].append(f[:-4])
    with open(state_path, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=1)
    print()
    print("=" * 72)
    print("EVIDENCE BRIEFS  (%d of %d todo)" % (len(picked), len(todo)))
    print("=" * 72)
    for f in picked:
        path = os.path.join(EVID_DIR, f)
        print("\n--- dispatch this prompt to an evidence agent ---")
        print("Read the file `%s` and follow it exactly. It is your complete "
              "brief: the keys, the questions, the output format, and the "
              "rules." % path)
    return len(picked)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--master", type=int, default=2)
    ap.add_argument("--evidence", type=int, default=2)
    ap.add_argument("--evidence-only", action="store_true")
    ap.add_argument("--master-only", action="store_true")
    args = ap.parse_args()
    if not args.evidence_only:
        hand_out_master(args.master)
    if not args.master_only:
        hand_out_evidence(args.evidence)
    return 0


if __name__ == "__main__":
    sys.exit(main())
