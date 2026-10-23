"""Dispatch the human-evidence pass.

The evidence pass asks an agent to read the paper for the items in the human
master's queue and report **what the paper shows** — without deciding.  The
evidence lands in ``Results/human_evidence/<Key>.md``.

It is resumable by construction: a batch is done when every key in it has an
evidence file, so a crashed session or a rate limit costs at most one batch.

Usage
-----
    python Scripts/human_evidence.py --status
    python Scripts/human_evidence.py --next          # print the next batch
    python Scripts/human_evidence.py --next --all
"""
import argparse
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS = os.path.join(ROOT, "Results")
OUT_DIR = os.path.join(RESULTS, "human_evidence")
BATCH_DIR = os.path.join(ROOT, "temp", "mm_route", "evidence_batches")


def batch_keys(path):
    keys = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("Study key: "):
                keys.append(line[len("Study key: "):].strip())
    return keys


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true", dest="show_status")
    ap.add_argument("--next", action="store_true")
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()

    if not os.path.isdir(BATCH_DIR):
        print("no evidence batches; run build_human_evidence_queue.py first",
              file=sys.stderr)
        return 1
    os.makedirs(OUT_DIR, exist_ok=True)
    files = sorted(f for f in os.listdir(BATCH_DIR) if f.endswith(".txt"))

    done, todo = [], []
    for f in files:
        bid = f[:-4]
        keys = batch_keys(os.path.join(BATCH_DIR, f))
        have = [k for k in keys
                if os.path.isfile(os.path.join(OUT_DIR, k + ".md"))]
        (done if len(have) == len(keys) and keys else todo).append(bid)

    if args.show_status:
        print("evidence batches: %d | done %d | todo %d"
              % (len(files), len(done), len(todo)))
        n_keys = len(os.listdir(OUT_DIR)) if os.path.isdir(OUT_DIR) else 0
        print("evidence files: %d" % n_keys)
        if todo:
            print("todo:", ", ".join(todo))
        return 0

    if args.next:
        picked = todo if args.all else todo[:1]
        for b in picked:
            path = os.path.join(BATCH_DIR, b + ".txt")
            print("=" * 72)
            print("EVIDENCE BATCH %s" % b)
            print("=" * 72)
            with open(path, encoding="utf-8") as fh:
                print(fh.read())
        return 0

    print("evidence batches: %d | done %d | todo %d"
          % (len(files), len(done), len(todo)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
