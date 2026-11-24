"""Release master-agent batches that are marked ``running`` but have no
live agent on them.

A batch is marked ``running`` when it is handed out, so a crashed session
or a restart leaves batches stuck behind.  Two agents editing the same
study key race, and each overwrites the other's field values, so a stuck
batch is a batch that must not be re-dispatched silently either - it has to
be *released* explicitly, and only after checking that no agent is still
working on it.

The script therefore:
  - lists the batches marked running, with the time they were handed out;
  - refuses to release anything unless the caller passes ``--release``;
  - removes the state entry, which returns the batch to the todo pool;
  - keeps a copy of the previous state in ``temp/mm_route/``.

It does NOT touch any ``Extraction/`` file, and it never releases a batch
that is not named in ``--only`` when that option is used.
"""
import argparse
import json
import os
import shutil
import sys
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(ROOT, "temp", "mm_route", "master_state.json")
BATCH_DIR = os.path.join(ROOT, "temp", "mm_route", "master_batches")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--release", action="store_true",
                    help="actually release them (default: list only)")
    ap.add_argument("--only", nargs="*", default=None,
                    help="release only these batch ids")
    args = ap.parse_args()

    state = json.load(open(STATE, encoding="utf-8"))
    batches = state.get("batches", {})
    ids = {os.path.basename(p)[:-4] for p in os.listdir(BATCH_DIR)
           if p.endswith(".txt")} if os.path.isdir(BATCH_DIR) else set()

    running = [b for b in sorted(batches)
               if batches[b].get("status") == "running" and b in ids]
    print("batches marked running (and still present): %d" % len(running))
    for b in running:
        print("   %-14s handed out at %s" % (b, batches[b].get("at")))

    if not args.release:
        print("\nlist only - pass --release to return them to the todo pool")
        return 0

    targets = running if not args.only else [
        b for b in running if b in set(args.only)]
    skipped = [b for b in running if b not in targets]
    if skipped:
        print("\nNOT released (not named in --only):", ", ".join(skipped))
    if not targets:
        print("\nnothing to release")
        return 0

    bak = STATE + ".bak.%s" % datetime.now().strftime("%Y%m%d%H%M%S")
    shutil.copy2(STATE, bak)
    for b in targets:
        batches.pop(b, None)
    with open(STATE, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, ensure_ascii=False)
    print("\nreleased %d batch(es): %s" % (len(targets), ", ".join(targets)))
    print("state backup:", os.path.relpath(bak, ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
