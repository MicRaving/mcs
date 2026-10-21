"""MM (MiniMax) coding-route dispatcher: queue, prompts, state and sweep.

State lives in ``temp/mm/state.json`` (key -> {status, attempts, updated, started}).
The parent session spawns one ``opencode/minimax-m3.1-flash-free#max`` subagent per
queued key and marks it as it completes; ``--next N`` returns (and atomically
**claims**) up to N queued keys, clamped to the pool cap.

Concurrency
-----------
Several dispatchers (a scheduled nudge plus an interactive session) can call this
script at the same time.  Every state read-modify-write happens under an exclusive
lock on ``temp/mm/state.lock``, and ``--next`` claims the keys it returns in the
same locked transaction, so two dispatchers never receive the same study.

Usage
  python Scripts/mm_route.py --status
  python Scripts/mm_route.py --next 20          # claims what it returns
  python Scripts/mm_route.py --peek 20          # read-only
  python Scripts/mm_route.py --prompt "<Key>"
  python Scripts/mm_route.py --mark "<Key>" --status done|failed
  python Scripts/mm_route.py --sweep            # validate finished work, recover
  python Scripts/mm_route.py --housekeeping     # sweep + prune scratch + status
  python Scripts/mm_route.py --dispatch-prompt
"""
import argparse
import contextlib
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: The agent bundle: the generated contract an agent reads instead of the
#: legacy schema.json.  It is written by ``Scripts/convert_legacy_schema.py
#: --bundle`` (and by ``GET /api/scheme/bundle``).
# forward slashes: the path is read by an agent, not by the OS
BUNDLE_DIR = "Project/bundles/misinformation"
TMP = os.path.join(ROOT, "temp", "mm")
#: The router's own bookkeeping lives OUTSIDE the agents' scratch namespace: an
#: agent that wipes ``temp/mm`` must not take the queue state with it.
ROUTE = os.path.join(ROOT, "temp", "mm_route")
STATE = os.path.join(ROUTE, "state.json")
BAK = os.path.join(ROUTE, "state.bak.json")
LIVE = os.path.join(ROUTE, "live.json")
LOCK = os.path.join(ROUTE, "state.lock")

#: Target number of concurrently in-flight MM subagents.
POOL = 20

REPLACE_RETRIES = 8
REPLACE_BACKOFF = 0.05

#: A running key with no output after this many seconds is assumed dead.
STALE_RUNNING = 4 * 3600


def mm_path(key):
    return os.path.join(ROOT, "Codings", key + " - MM.json")


def md_path(key):
    return os.path.join(ROOT, "Codings", key + " - MM.md")


def excluded_keys():
    """Study keys whose every experiment is excluded as a duplicate/non-independent report."""
    try:
        sys.path.insert(0, os.path.join(ROOT, "Scripts"))
        import study_exclusions as se
    except Exception:
        return set()
    per_key = {}
    for (key, exp) in se.NONINDEPENDENT_EXCLUSIONS:
        per_key.setdefault(key, set()).add(exp)
    out = set()
    for key, excluded in per_key.items():
        path = os.path.join(ROOT, "Extraction", key + ".json")
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            ids = {r.get("Study_ID") for r in data if isinstance(r, dict)}
        except Exception:
            continue
        if ids and ids <= excluded:
            out.add(key)
    return out


@contextlib.contextmanager
def state_lock():
    os.makedirs(ROUTE, exist_ok=True)
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
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()


def load_state():
    # The workspace lives in a synced folder: os.replace occasionally leaves the
    # destination briefly absent and a caller then saved a near-empty state.  Prefer
    # the primary file, fall back to the rolling backup, then the mirror outside
    # temp/, and never silently return {} while a backup exists.
    mirror = os.path.join(ROOT, "temp", "mm_route_state_mirror.json")
    for path in (STATE, BAK, mirror):
        if not os.path.isfile(path):
            continue
        last = "unknown"
        for _ in range(5):
            try:
                with open(path, encoding="utf-8") as fh:
                    st = json.load(fh)
                if isinstance(st, dict):
                    return st
                last = "top-level JSON is %s, not an object" % type(st).__name__
            except Exception as exc:  # transient read during a concurrent replace
                last = repr(exc)
            time.sleep(0.05)
        if path == mirror:
            raise RuntimeError(
                "could not read %s after 5 attempts (%s); refusing to continue" % (path, last))
    return {}


def load_live():
    """Keys with a live subagent session (written by the dispatcher)."""
    try:
        with open(LIVE, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return {k for k, v in data.items() if v}
        if isinstance(data, list):
            return set(data)
    except Exception:
        pass
    return set()


def save_live(keys):
    os.makedirs(ROUTE, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=ROUTE, prefix=".live-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({k: True for k in sorted(keys)}, fh, indent=2, ensure_ascii=False)
    last = None
    for attempt in range(REPLACE_RETRIES):
        try:
            os.replace(tmp, LIVE)
            return
        except PermissionError as exc:
            last = exc
            if attempt == REPLACE_RETRIES - 1:
                break
            time.sleep(REPLACE_BACKOFF * (attempt + 1))
    if os.path.exists(tmp):
        os.unlink(tmp)
    raise last


def active_keys(st):
    """Keys with a live agent: the live ledger plus any state-marked running key.

    A live-ledger key whose MM coding is already on disk is *not* in flight: the
    file is the deliverable, so its presence means the work is finished.  This
    makes the ledger self-healing against a restored (stale) live.json — the
    workspace lives in a synced folder, so a synced copy of an older live.json
    can otherwise re-add finished keys and starve the pool of room.  A key the
    state still marks as running always counts as in flight, so a file that an
    agent is still iterating on is never handed to a second agent.
    """
    running = set(in_flight(st))
    out = set(running)
    for k in load_live():
        if k in running:
            continue
        if os.path.isfile(mm_path(k)):
            continue
        out.add(k)
    return out


def sync_done(st):
    """Self-heal: any Coding present on disk is done, whatever the state says."""
    for p in glob.glob(os.path.join(ROOT, "Codings", "* - MM.json")):
        key = os.path.basename(p)[:-10]
        slot = st.setdefault(key, {})
        if slot.get("status") != "done":
            slot.update({"status": "done", "updated": int(time.time())})
    return st


def save_state(st):
    os.makedirs(ROUTE, exist_ok=True)
    # Rolling backup first, so a vanished primary file can always be recovered.
    try:
        if os.path.isfile(STATE):
            with open(STATE, "rb") as src, open(BAK, "wb") as dst:
                dst.write(src.read())
    except OSError:
        pass
    fd, tmp = tempfile.mkstemp(dir=ROUTE, prefix=".state-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(st, fh, indent=2, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        # Mirror outside temp/ as well: an agent that wipes a scratch tree must not
        # be able to take the queue state with it.
        try:
            mirror = os.path.join(ROOT, "temp", "mm_route_state_mirror.json")
            fd2, tmp2 = tempfile.mkstemp(dir=os.path.dirname(mirror),
                                         prefix=".mirror-", suffix=".json")
            with os.fdopen(fd2, "w", encoding="utf-8") as fh2:
                json.dump(st, fh2, indent=2, ensure_ascii=False)
            os.replace(tmp2, mirror)
        except OSError:
            pass
        last = None
        for attempt in range(REPLACE_RETRIES):
            try:
                os.replace(tmp, STATE)
                return
            except PermissionError as exc:
                last = exc
                if attempt == REPLACE_RETRIES - 1:
                    break
                time.sleep(REPLACE_BACKOFF * (attempt + 1))
        raise last
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def mark(st, key, status, **extra):
    slot = st.setdefault(key, {})
    slot["status"] = status
    slot["updated"] = int(time.time())
    slot.update(extra)
    return st


def in_flight(st):
    return sorted(k for k, v in st.items() if v.get("status") == "running")


def queue(st=None):
    """Study keys with an Extraction coding, a paper, and no completed MM coding."""
    st = load_state() if st is None else st
    keys = sorted(os.path.basename(p)[:-5]
                  for p in glob.glob(os.path.join(ROOT, "Extraction", "*.json")))
    skip = excluded_keys()
    live = load_live()
    out = []
    for k in keys:
        if k in skip or k in live:
            continue
        if os.path.isfile(mm_path(k)):
            continue
        if not os.path.isfile(os.path.join(ROOT, "Papers", k + ".pdf")):
            continue
        v = st.get(k, {})
        if v.get("status") in ("running", "done", "excluded"):
            continue
        if v.get("status") == "failed" and v.get("attempts", 0) >= 3:
            continue
        out.append(k)
    return out


def claim(n):
    with state_lock():
        st = sync_done(load_state())
        room = max(0, POOL - len(active_keys(st)))
        picked = queue(st)[:max(0, min(n, room))]
        now = int(time.time())
        for k in picked:
            slot = st.setdefault(k, {})
            slot.update({"status": "running", "started": now, "updated": now,
                         "attempts": int(slot.get("attempts", 0)) + 1})
        if picked:
            save_state(st)
        return picked


def _validate(key):
    """Return (ok, detail) for a finished coding."""
    path = mm_path(key)
    if not os.path.isfile(path):
        return False, "missing file"
    try:
        if os.path.getsize(path) < 200:
            return False, "file too small"
    except OSError as exc:
        return False, repr(exc)
    py = sys.executable
    checks = [
        [py, os.path.join(ROOT, "Scripts", "B_validate.py"), path],
        [py, os.path.join(ROOT, "Scripts", "mark_source.py"),
         "--verify", path, "--fields"],
    ]
    for cmd in checks:
        try:
            r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                               timeout=600, encoding="utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001
            return False, repr(exc)
        if r.returncode != 0:
            return False, (r.stdout or r.stderr or "")[-500:]
    return True, "ok"


def sweep(clean=True):
    """Validate running/done work, recover finished files, fail stale runs."""
    st = load_state()
    now = int(time.time())
    changes = []
    for key in sorted(st):
        v = st[key]
        status = v.get("status")
        if status not in ("running", "done", "failed"):
            continue
        if status == "running":
            if os.path.isfile(mm_path(key)):
                ok, detail = _validate(key)
                if ok:
                    mark(st, key, "done", verified=now)
                    changes.append((key, "running->done"))
                # else: leave it running; the agent may still be iterating
            elif now - int(v.get("started") or v.get("updated") or now) > STALE_RUNNING:
                mark(st, key, "failed", error="stale: no output")
                changes.append((key, "running->failed(stale)"))
        elif status == "done":
            if not os.path.isfile(mm_path(key)):
                mark(st, key, "failed", error="done but file missing")
                changes.append((key, "done->failed(missing)"))
        elif status == "failed":
            if os.path.isfile(mm_path(key)):
                ok, _ = _validate(key)
                if ok:
                    mark(st, key, "done", verified=now)
                    changes.append((key, "failed->done(recovered)"))
    if changes:
        with state_lock():
            # re-apply changes onto a fresh read, but only where the key still carries
            # the status this sweep observed (a concurrent dispatcher may have moved it)
            fresh = load_state()
            for key, what in changes:
                from_status = what.split("->", 1)[0]
                if fresh.get(key, {}).get("status") == from_status:
                    fresh[key] = st[key]
            save_state(fresh)
    if clean:
        # Only prune scratch that is a day old: a file can appear before the agent
        # has finished iterating, and deleting its scratch mid-task corrupts the run.
        cutoff = time.time() - 86400
        for key in list(st):
            if st.get(key, {}).get("status") == "done":
                d = os.path.join(TMP, key)
                if os.path.isdir(d):
                    try:
                        if os.path.getmtime(d) < cutoff:
                            shutil.rmtree(d, ignore_errors=True)
                    except OSError:
                        pass
    return changes


def prompt(key):
    return "\n".join([
        "You are an MM (MiniMax M3.1 Flash, Max Thinking) coding agent on the",
        "misinformation-intervention meta-analysis.",
        "",
        "Workspace: " + ROOT,
        "Study key: " + key,
        "",
        "Read first, in this order:",
        "  Scripts/MM_CODING_INSTRUCTIONS.md   (your full task and rules)",
        "  " + BUNDLE_DIR + "/prompt.md        (the task, the field list, the rules)",
        "  " + BUNDLE_DIR + "/scheme.json       (the machine contract: fields, roles,",
        "                                         conditions, vocabularies)",
        "  " + BUNDLE_DIR + "/json-schema.json  (the shape of the JSON you must write)",
        "  " + BUNDLE_DIR + "/criteria.json     (the inclusion criteria and their keys)",
        "",
        "The bundle is generated from the project's scheme (Project/scheme.json);",
        "it is the authoritative field list.  Scripts/coding_web/schema.json is the",
        "legacy view of the same scheme and is kept only for compatibility.",
        "",
        "Sources: Papers/" + key + ".pdf and Text/" + key + ".txt.",
        "You may read raw files in Datasets/" + key + "/ only; never its derived products.",
        "",
        "Do NOT read Extraction/" + key + ".*, any Codings/" + key + " - *.json,",
        "Datasets/" + key + "/* - *.csv, Datasets/" + key + "/repro_facts.json or",
        "Reproduction/" + key + ".* — they contain another coder's answers.",
        "",
        "Deliverables (write ONLY these):",
        "  1. Codings/" + key + " - MM.json  — JSON array, one object per experiment,",
        "     every DV with source_span/source_quote/source_location, and a record-level",
        "     source_spans map covering EVERY non-empty field except the documented skip list.",
        "  2. Codings/" + key + " - MM.md    — short design/eligibility/DV/marking notes.",
        "",
        "No invented numbers: every effect is sourced, else a non-empty missing_metrics_reason.",
        "Never use a multi-df omnibus F/chi2 as the eligible_primary == 1 effect.",
        "",
        "Before finishing, both must report zero problems:",
        "  python Scripts/B_validate.py \"Codings/" + key + " - MM.json\"",
        "  python Scripts/mark_source.py --verify \"Codings/" + key + " - MM.json\" --fields",
        "",
        "Keep scratch under temp/mm/" + key + "/ and delete ONLY that folder when done.",
        "NEVER delete temp/mm itself, temp/mm_route, or any other session's scratch:",
        "temp/mm_route holds the dispatcher's queue state.",
        "Reply with: study key, experiments, DVs marked/total, fields marked/total,",
        "any missing effect and why, estimation assumptions, and the two paths.",
    ])


DISPATCH = "\n".join([
    "MM POOL REFILL: top up the MiniMax coding pool to 20 in-flight subagents.",
    "1. Run: python Scripts/mm_route.py --housekeeping",
    "   (validates finished codings, recovers files, prunes scratch, prints status).",
    "2. Run: python Scripts/mm_route.py --next 20",
    "   It claims the keys it prints and clamps itself to the pool cap, so a second",
    "   dispatcher racing this one gets an empty list. Never re-derive N yourself.",
    "3. For each returned key, spawn an opencode/minimax-m3.1-flash-free#max `general`",
    "   subagent (background, max thinking) whose prompt says: follow",
    "   Scripts/MM_CODING_INSTRUCTIONS.md for study \"<Key>\", and paste the prompt from",
    "   `python Scripts/mm_route.py --prompt \"<Key>\"`.",
    "4. When a subagent finishes, run `python Scripts/mm_route.py --mark \"<Key>\" --status done`",
    "   (or failed), then claim the next key with `--next 1`, keeping 20 in flight until",
    "   the queue is empty.",
    "5. If a wave has no in-flight work and the queue is non-empty, repeat from 1.",
])


def status(st=None):
    st = sync_done(load_state() if st is None else st)
    running = sorted(active_keys(st))
    done = sorted(set(
        [k for k, v in st.items() if v.get("status") == "done"]
        + [os.path.basename(p)[:-10]
           for p in glob.glob(os.path.join(ROOT, "Codings", "* - MM.json"))]))
    failed = sorted(k for k, v in st.items() if v.get("status") == "failed")
    q = queue(st)
    blocked = sorted(k for k in
                     (os.path.basename(p)[:-5]
                      for p in glob.glob(os.path.join(ROOT, "Extraction", "*.json")))
                     if not os.path.isfile(os.path.join(ROOT, "Papers", k + ".pdf"))
                     and not os.path.isfile(mm_path(k)))
    return {"pool": POOL, "queue": len(q), "in_flight": len(running),
            "room": max(0, POOL - len(running)), "done": len(done),
            "failed": len(failed), "blocked_no_pdf": blocked,
            "excluded_duplicates": sorted(excluded_keys()),
            "next_queued": q[:12], "failed_keys": failed[:20]}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--next", type=int, default=0)
    ap.add_argument("--peek", type=int, default=0)
    ap.add_argument("--mark")
    ap.add_argument("--live", metavar="KEY", help="record a dispatched key as live")
    ap.add_argument("--unlive", metavar="KEY", help="drop a finished key from the live ledger")
    ap.add_argument("--status", dest="status", nargs="?", const="__show__")
    ap.add_argument("--prompt", metavar="KEY")
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--housekeeping", action="store_true")
    ap.add_argument("--dispatch-prompt", action="store_true")
    args = ap.parse_args()

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

    if args.live:
        with state_lock():
            keys = load_live()
            keys.add(args.live)
            save_live(keys)
        print("live +", args.live)
        return 0
    if args.unlive:
        with state_lock():
            keys = load_live()
            keys.discard(args.unlive)
            save_live(keys)
        print("live -", args.unlive)
        return 0

    if args.mark:
        s = args.status if (args.status and args.status != "__show__") else "done"
        with state_lock():
            st = load_state()
            mark(st, args.mark, s)
            save_state(st)
        print("marked", args.mark, "->", s)
        return 0

    if args.sweep or args.housekeeping:
        changes = sweep(clean=args.housekeeping)
        for key, what in changes:
            print(key, what)
        print(json.dumps(status(), indent=2, ensure_ascii=False))
        return 0

    if args.peek:
        for k in queue()[:args.peek]:
            print(k)
        return 0

    if args.next:
        for k in claim(args.next):
            print(k)
        return 0

    print(json.dumps(status(), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
