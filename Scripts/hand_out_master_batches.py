"""Hand out master-agent batches and mark them running.

``Scripts/master_agent.py --next`` prints one batch's brief; this script
prints the *ids* of the next N todo batches and marks them running, so a
supervisor can dispatch them to agents in one go.

Two rules it enforces:

* a batch already marked running is never handed out again - two agents
  editing the same study key race, and each overwrites the other's field
  values;
* the batch is marked running **before** it is handed out, so a crash
  costs at most one batch, and the batch file carries the reminder to
  mark it done when the agent lands.
"""
import argparse
import json
import os
import subprocess
import sys
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BATCH_DIR = os.path.join(ROOT, "temp", "mm_route", "master_batches")
MASTER = os.path.join(ROOT, "Scripts", "master_agent.py")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=4)
    ap.add_argument("--claim", action="store_true",
                    help="mark the batches running (do this BEFORE dispatching)")
    ap.add_argument("--release", nargs="*", default=None,
                    help="release these batch ids back to the todo pool")
    args = ap.parse_args()

    if args.release:
        for b in args.release:
            subprocess.check_call([sys.executable, MASTER, "--mark", b,
                                   "--set-status", "failed"])
            print("released", b)
        return 0

    state = json.load(open(os.path.join(ROOT, "temp", "mm_route",
                                        "master_state.json"), encoding="utf-8"))
    batches = state.get("batches", {})
    ids = sorted(os.path.basename(p)[:-4] for p in os.listdir(BATCH_DIR)
                 if p.endswith(".txt"))
    done = [b for b in ids if batches.get(b, {}).get("status") == "done"]
    running = [b for b in ids if batches.get(b, {}).get("status") == "running"]
    todo = [b for b in ids
            if batches.get(b, {}).get("status") not in ("done", "running")]

    picked = todo[:args.n]
    print("batches: %d | done %d | running %d | todo %d"
          % (len(ids), len(done), len(running), len(todo)))
    for b in picked:
        path = os.path.join(BATCH_DIR, b + ".txt")
        size = os.path.getsize(path)
        print("  %-14s %6.1f KB  %s" % (b, size / 1000,
                                         os.path.relpath(path, ROOT)))
    if args.claim:
        for b in picked:
            subprocess.check_call([sys.executable, MASTER, "--mark", b,
                                   "--set-status", "running"])
        print("marked %d batch(es) running at %s"
              % (len(picked), datetime.now().isoformat(timespec="seconds")))
    else:
        print("\n(pass --claim to mark them running before dispatching)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
