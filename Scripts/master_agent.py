"""Dispatch the master agent's work.

The disagreement triage leaves the master agent a list of items the *paper*
settles (see ``Scripts/build_master_agent_worklist.py``).  This script
turns that list into a resumable queue: it hands out the next batch, records
what was done, and skips the batches that are already finished.

The queue state lives in ``temp/mm_route/master_state.json`` and is mirrored
to ``temp/mm_route_master_state_mirror.json``.  A batch is marked done by
its agent; the pass therefore survives a restart, a rate limit, or a session
that ends half-way.

Usage
-----
    python Scripts/master_agent.py --status
    python Scripts/master_agent.py --next            # print the next prompt
    python Scripts/master_agent.py --next --all      # print every prompt
    python Scripts/master_agent.py --mark 000 --set-status done
    python Scripts/master_agent.py --mark 000 --set-status failed
    python Scripts/master_agent.py --rebuild         # rebuild the worklist
"""
import argparse
import json
import os
import sys
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(ROOT, "temp", "mm_route", "master_state.json")
MIRROR = os.path.join(ROOT, "temp", "mm_route_master_state_mirror.json")
BATCH_DIR = os.path.join(ROOT, "temp", "mm_route", "master_batches")


def load_state():
    if not os.path.isfile(STATE):
        return {"batches": {}, "created": datetime.now().isoformat(timespec="seconds")}
    try:
        with open(STATE, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {"batches": {}, "created": datetime.now().isoformat(timespec="seconds")}


def save_state(state):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=1, ensure_ascii=False)
    with open(MIRROR, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=1, ensure_ascii=False)


def batch_ids():
    if not os.path.isdir(BATCH_DIR):
        return []
    return sorted(os.path.basename(p)[:-4]
                  for p in os.listdir(BATCH_DIR) if p.endswith(".txt"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true", dest="show_status")
    ap.add_argument("--next", action="store_true")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--mark", metavar="BATCH")
    ap.add_argument("--set-status", dest="status_value", default="done",
                    choices=["done", "failed", "running"],
                    help="the status to set for --mark (default: done)")
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--include-running", action="store_true",
                    help="also hand out batches already marked running (only "
                         "after checking no agent is still on them)")
    args = ap.parse_args()

    if args.rebuild:
        import subprocess
        subprocess.check_call([sys.executable,
                                os.path.join(ROOT, "Scripts",
                                             "build_master_agent_worklist.py")])
        return 0

    state = load_state()
    ids = batch_ids()
    batches = state.setdefault("batches", {})

    if args.mark:
        if args.mark not in ids:
            print("unknown batch: %s" % args.mark, file=sys.stderr)
            return 1
        batches[args.mark] = {"status": args.status_value,
                               "at": datetime.now().isoformat(timespec="seconds")}
        save_state(state)
        print("%s -> %s" % (args.mark, batches[args.mark]["status"]))
        return 0

    # a state entry for a batch file that no longer exists is history: the
    # worklist rebuild re-queues a finished batch's keys when they gain new
    # items, and the finished batch file is then removed.
    stale = [b for b in batches if b not in ids]
    done = [b for b in ids if batches.get(b, {}).get("status") == "done"]
    failed = [b for b in ids if batches.get(b, {}).get("status") == "failed"]
    running = [b for b in ids if batches.get(b, {}).get("status") == "running"]
    # A batch that is already running must NOT be handed out again: two
    # agents editing the same study key at the same time race, and each
    # overwrites the other's field values.
    todo = [b for b in ids if b not in done and b not in running]
    if args.include_running:
        todo = [b for b in ids if b not in done]

    if args.show_status:
        print("batches: %d | done %d | failed %d | running %d | todo %d"
              % (len(ids), len(done), len(failed), len(running), len(todo)))
        if stale:
            print("superseded (keys re-queued): %s" % ", ".join(sorted(stale)))
        if failed:
            print("failed:", ", ".join(failed))
        return 0

    if args.next:
        picked = todo if args.all else todo[:1]
        for b in picked:
            path = os.path.join(BATCH_DIR, b + ".txt")
            print("=" * 72)
            print("BATCH %s  (%s)" % (b, os.path.relpath(path, ROOT)))
            print("=" * 72)
            with open(path, encoding="utf-8") as fh:
                print(fh.read())
        return 0

    print("batches: %d | done %d | todo %d" % (len(ids), len(done), len(todo)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
