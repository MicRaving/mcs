"""Space Bunny (SB) coding-route dispatcher: queue, prompts and state.

State lives in ``temp/sb/state.json`` (key -> {status, updated}).  The parent session
spawns one ``opencode-go/space-bunny-free`` subagent per queued key and marks it as it
completes; ``--next N`` returns (and atomically **claims**) up to N queued keys.

Concurrency
-----------
Several dispatchers (the ``SB pool refill`` cron plus any interactive session) can run
this script at the same time.  Every state read-modify-write therefore happens under an
exclusive lock on ``temp/sb/state.lock``, and ``--next`` *claims* the keys it returns in
the same locked transaction.  Without that, two dispatchers each read a stale state,
each write the whole file back, and each silently loses the other's marks -- which
resurrects keys that are already in flight and makes two agents write the same study's
`` - SB`` artefacts over each other.  ``--next`` also clamps to the pool size, so a
second dispatcher racing the first gets an empty list instead of a duplicate.

Usage
  python Scripts/sb_route.py --status
  python Scripts/sb_route.py --next 24          # claims what it returns
  python Scripts/sb_route.py --peek 24          # read-only, claims nothing
  python Scripts/sb_route.py --prompt "<Key>"
  python Scripts/sb_route.py --mark "<Key>" --status running|done|failed
  python Scripts/sb_route.py --dispatch-prompt
"""
import argparse
import contextlib
import glob
import json
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: The agent bundle: the generated contract an agent reads instead of the
#: legacy schema.json.  It is written by ``Scripts/convert_legacy_schema.py
#: --bundle`` (and by ``GET /api/scheme/bundle``).
# forward slashes: the path is read by an agent, not by the OS
# the agent bundle travels with the CodingStudio app, in
# <app dir>/Schemes/<scheme id>/bundles/
BUNDLE_DIR = "Scripts/coding_web/Schemes/misinformation/bundles"
TMP = os.path.join(ROOT, "temp", "sb")
STATE = os.path.join(TMP, "state.json")
LOCK = os.path.join(TMP, "state.lock")

#: Target number of concurrently in-flight SB subagents.
POOL = 24

#: Windows needs ``os.replace`` retried when the destination is momentarily locked.
REPLACE_RETRIES = 8
REPLACE_BACKOFF = 0.05


@contextlib.contextmanager
def state_lock():
    """Exclusive cross-process lock around a state read-modify-write."""
    os.makedirs(TMP, exist_ok=True)
    fh = open(LOCK, "a+b")
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            if os.name == "nt":
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()


def load_state():
    """Read the state, tolerating a concurrent writer.

    ``save_state`` replaces the file atomically, so a read should never observe a
    half-written file.  Historically it could, and this function answered "{}", which
    is catastrophic: any caller that then saved (e.g. ``--mark``) would write back a
    near-empty state and wipe every other key.  So retry briefly, and raise rather
    than return a plausible-looking empty dict if the file is genuinely unreadable.
    """
    if not os.path.isfile(STATE):
        return {}
    last = "unknown"
    for _ in range(5):
        try:
            with open(STATE, encoding="utf-8") as fh:
                st = json.load(fh)
            if isinstance(st, dict):
                return st
            last = "top-level JSON is %s, not an object" % type(st).__name__
        except Exception as exc:  # transient read during a concurrent replace
            last = repr(exc)
        time.sleep(0.05)
    raise RuntimeError(
        "could not read %s after 5 attempts (%s); refusing to continue, because "
        "saving a state that failed to load would erase every other key"
        % (STATE, last))


def save_state(st):
    """Write the state atomically: temp file in the same dir, then os.replace.

    ``os.replace`` is atomic, but on Windows it fails with ``PermissionError`` when
    *any* other handle has the destination open without ``FILE_SHARE_DELETE``.  Two
    things do that here: the unlocked readers below (``--status``/``--peek`` call
    ``load_state`` without taking the lock), and the OneDrive/Nextcloud sync client,
    since this workspace lives inside a synced folder.  Either can turn a claim into
    a lost slot -- ``claim`` picks keys, then crashes before saving, so they are never
    marked running.  Retry briefly rather than propagate.
    """
    os.makedirs(TMP, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=TMP, prefix=".state-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(st, fh, indent=2, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        last = None
        for attempt in range(REPLACE_RETRIES):
            try:
                os.replace(tmp, STATE)
                return
            except PermissionError as exc:  # destination momentarily locked
                last = exc
                if attempt == REPLACE_RETRIES - 1:
                    break
                time.sleep(REPLACE_BACKOFF * (attempt + 1))
        raise last
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def mark(st, key, status):
    st[key] = {"status": status, "updated": int(time.time())}
    return st


def in_flight(st):
    return sorted(k for k, v in st.items() if v.get("status") == "running")


def queue(st=None):
    """Study keys with an Extraction coding and no completed SB coding, oldest first."""
    st = load_state() if st is None else st
    keys = sorted(os.path.basename(p)[:-5] for p in glob.glob(os.path.join(ROOT, "Extraction", "*.json")))
    out = []
    for k in keys:
        if os.path.isfile(os.path.join(ROOT, "Codings", k + " - SB.json")):
            continue
        if st.get(k, {}).get("status") in ("done", "running"):
            continue
        out.append(k)
    return out


def claim(n):
    """Claim up to ``n`` queued keys for dispatch, atomically and within the pool cap.

    Returns the claimed keys (already marked ``running``), so a dispatcher that loses the
    race gets ``[]`` rather than a key another dispatcher already owns.
    """
    with state_lock():
        st = load_state()
        room = max(0, POOL - len(in_flight(st)))
        picked = queue(st)[:max(0, min(n, room))]
        if picked:
            for k in picked:
                mark(st, k, "running")
            save_state(st)
        return picked


def prompt(key):
    return "\n".join([
        "You are a Space Bunny Free coding agent on the misinformation-intervention meta-analysis.",
        "Workspace: " + ROOT,
        "Study key: " + key,
        "",
        "Read first:",
        "  Scripts/SB_ROUTE.md  and  Scripts/SB_CODING_INSTRUCTIONS.md",
        "  " + BUNDLE_DIR + "/prompt.md  and  " + BUNDLE_DIR + "/scheme.json",
        "  " + BUNDLE_DIR + "/json-schema.json  and  " + BUNDLE_DIR + "/criteria.json",
        "  (the bundle is the authoritative field list; it is generated from the",
        "   project's scheme Project/scheme.json)",
        "Sources:  Papers/" + key + ".pdf   Text/" + key + ".txt   Datasets/" + key + "/ (raw deposit)",
        "",
        "Deliverables (ONLY for this study; never touch another study or a canonical artefact):",
        "  Read Scripts/SB_SHARED_CODE_TRAPS.md (settled pool-wide conventions, incl. the no-deposit rule).",
        "  1. Codings/" + key + " - SB.json  (+ Codings/" + key + " - SB.md) — code every experiment per",
        "     SB_CODING_INSTRUCTIONS.md; no invented numbers (sourced effect or missing_metrics_reason);",
        "     never a multi-df omnibus as the eligible_primary==1 effect.",
        "     Also set the common field Fulltext_Exclusion_Criterion: leave EMPTY when the study meets",
        "     all inclusion criteria; otherwise list the unmet one(s) — C1 = no misinformation",
        "     intervention, C2 = no control/comparison group, C3 = not English, C4 = not a primary",
        "     experimental study — and STILL complete the harmonisation and reproduction as normal.",
        "  2. Datasets/" + key + "/harmonizer - SB.py, run it to write the four ` - SB` products",
        "     (<Key> - harmonized - SB.csv, - harmonized_variables - SB.csv, - exclusions - SB.csv,",
        "     - conditions - SB.csv); label ALL DVs and covariates with the correct dv_polarity;",
        "     read ONLY the raw deposit; deterministic.",
        "  3. Datasets/" + key + "/<Key> - reproduction - SB.py + Reproduction/" + key + " - SB.md —",
        "     recompute the paper's reported results from the ` - SB` harmonized data (after the",
        "     ` - SB` exclusions); verdict yes|partial|no|not reproducible.",
        "",
        "Keep scratch in temp/sb/" + key + "/ only. Verify with",
        "  python Scripts/B_validate.py \"Codings/" + key + " - SB.json\"",
        "Reply with: study key, verdict, and the artefacts written.",
    ])


DISPATCH = "\n".join([
    "SB DISPATCH: top up the Space Bunny coding pool to 24 in-flight subagents.",
    "1. Run: python Scripts/sb_route.py --status   (see in_flight / room).",
    "2. Run: python Scripts/sb_route.py --next 4    (it claims the keys it prints and clamps",
    "   itself to the pool cap, so a dispatcher racing another one gets an empty list).",
    "   Do not re-derive N yourself; a second dispatcher would double-dispatch a key and two",
    "   agents would overwrite each other's ` - SB` artefacts.",
    "3. For each returned key, spawn an opencode-go/space-bunny-free `general` subagent",
    "   (background) with the prompt from `python Scripts/sb_route.py --prompt \"<Key>\"`.",
    "   `--next` already marked the key running, so no extra --mark is needed.",
    "4. When a subagent finishes, `--mark \"<Key>\" --status done` (or failed) and claim the",
    "   next queued key with `--next 1` so 24 stay in flight, until the queue is empty.",
])


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--next", type=int, default=0,
                    help="claim and print up to N queued keys (clamped to the pool cap)")
    ap.add_argument("--peek", type=int, default=0,
                    help="print up to N queued keys without claiming them")
    ap.add_argument("--mark")
    ap.add_argument("--status", dest="status", nargs="?", const="__show__",
                    help="show the summary, or (with --mark) the new status")
    ap.add_argument("--prompt", metavar="KEY")
    ap.add_argument("--dispatch-prompt", action="store_true")
    args = ap.parse_args()

    # Study keys contain non-cp1252 characters (e.g. "Bulut & Karanfiloğlu (2026)"),
    # which raise UnicodeEncodeError when printed to a legacy Windows console.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    if args.dispatch_prompt:
        print(DISPATCH)
        return 0
    if args.prompt:
        print(prompt(args.prompt))
        return 0

    st = load_state()
    if args.mark:
        s = args.status if (args.status and args.status != "__show__") else "done"
        with state_lock():
            st = load_state()
            mark(st, args.mark, s)
            save_state(st)
        print("marked", args.mark, "->", s)
        return 0

    if args.peek:
        for k in queue()[:args.peek]:
            print(k)
        return 0

    if args.next:
        for k in claim(args.next):
            print(k)
        return 0

    running = in_flight(st)
    done = sorted(k for k, v in st.items() if v.get("status") == "done")
    failed = sorted(k for k, v in st.items() if v.get("status") == "failed")
    print(json.dumps({"queue": len(queue(st)), "in_flight": len(running), "room": max(0, POOL - len(running)),
                      "done": len(done), "failed": len(failed),
                      "next_queued": queue(st)[:12]}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
