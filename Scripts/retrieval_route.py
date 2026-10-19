"""Retrieval-route pool: queue / dispatch helpers for the DV+dataset recovery campaign.

One subagent per paper; brief = Scripts/RETRIEVAL_BRIEF.md.  Queue = every Extraction key
that has uncodeable DV rows OR is missing its dataset (498 keys); state lives in
``temp/retrieval/state.json``.

    python Scripts/retrieval_route.py --status        # queue / in_flight / done
    python Scripts/retrieval_route.py --next 24       # JSON list of the next 24 keys
    python Scripts/retrieval_route.py --mark "<Key>" --status running|done|failed
    python Scripts/retrieval_route.py --pool 24       # refill REPORT for a dispatcher (does NOT reserve)
    python Scripts/retrieval_route.py --claim 24      # atomically reserve + report: USE THIS to dispatch

Conventions: every artefact is additive and suffixed " - retrieval"; canonical files are never
touched.  A study counts as done when ``Results/<Key> - retrieval.md`` exists.

Concurrency: ``--pool`` only *reports* which keys are free — it does not reserve them, so two
dispatchers running it concurrently hand out the same keys (this silently ran one key twice and
caused an artefact overwrite).  Use ``--claim N`` to reserve atomically.  State is written via
temp-file + ``os.replace`` so a concurrent reader never observes truncated JSON.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
B = os.path.dirname(HERE)
TEMP = os.path.join(B, "temp", "retrieval")
STATE = os.path.join(TEMP, "state.json")


def _load_state():
    """Read state.  A corrupt/partial read must NOT silently return {} — that would make the
    whole queue look pending and re-dispatch every running key."""
    if not os.path.isfile(STATE):
        return {}
    for _ in range(5):                      # a concurrent atomic replace may race the open
        try:
            with open(STATE, encoding="utf-8") as fh:
                return json.load(fh)
        except FileNotFoundError:
            return {}
        except json.JSONDecodeError:
            time.sleep(0.05)
        except OSError:
            time.sleep(0.05)
    return {}


def _save_state(st):
    """Atomic write: temp file in the same dir + os.replace, so readers see old or new, never a
    truncated file.  os.replace is atomic on Windows and POSIX."""
    os.makedirs(TEMP, exist_ok=True)
    tmp = STATE + ".tmp." + str(os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(st, fh, indent=1, ensure_ascii=False)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, STATE)                  # atomic on both platforms


class _StateLock:
    """Cross-process mutex around read-modify-write of state.json (msvcrt on Windows, fcntl on
    POSIX).  Prevents two concurrent --mark/--claim calls from clobbering each other."""

    def __init__(self, timeout=30.0):
        self.timeout = timeout
        self.path = STATE + ".lock"
        self.fh = None

    def __enter__(self):
        os.makedirs(TEMP, exist_ok=True)
        deadline = time.time() + self.timeout
        while True:
            try:
                self.fh = open(self.path, "a+")
                break
            except OSError:
                if time.time() > deadline:
                    raise
                time.sleep(0.05)
        if os.name == "nt":                 # pragma: no cover - platform specific
            import msvcrt
            while True:
                try:
                    self.fh.seek(0)
                    msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.time() > deadline:
                        raise
                    time.sleep(0.05)
        else:                               # pragma: no cover - platform specific
            import fcntl
            while True:
                try:
                    fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.time() > deadline:
                        raise
                    time.sleep(0.05)
        return self

    def __exit__(self, *exc):
        try:
            if os.name == "nt":             # pragma: no cover - platform specific
                import msvcrt
                self.fh.seek(0)
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:                           # pragma: no cover - platform specific
                import fcntl
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
        finally:
            self.fh.close()
        return False


def queue_keys():
    """The target set: uncodeable DV rows OR a missing dataset."""
    st_missing = set()
    try:
        st_missing = set(json.load(open(os.path.join(B, "Results", "state.json"),
                                        encoding="utf-8")).get("datasets", {}).get("missing", []))
    except Exception:
        pass
    out = []
    for p in glob.glob(os.path.join(B, "Extraction", "*.json")):
        key = os.path.basename(p)[:-5]
        has_gap = False
        try:
            j = json.load(open(p, encoding="utf-8"))
            recs = j if isinstance(j, list) else j.get("experiments", [j])
            has_gap = any(str(d.get("missing_metrics_reason") or "").strip()
                          for r in recs for d in (r.get("DV_Metrics") or []))
        except Exception:
            pass
        if has_gap or key in st_missing:
            out.append(key)
    return sorted(out)


def done(key):
    """True when ANY retrieval report for this key exists.

    Subagents write suffixed variants (``<Key> - retrieval.md``,
    ``<Key> - retrieval - <tag>.md``, ``... - pass 2.md``, ...), so an exact-name
    test silently under-detects a finished key and the refill cron re-dispatches it
    -- which is why the same study appeared with several passes.  Match the family.
    """
    return bool(glob.glob(os.path.join(B, "Results", key + " - retrieval*.md")))


def status(st=None, keys=None):
    st = _load_state() if st is None else st
    keys = queue_keys() if keys is None else keys
    running = [k for k in keys if st.get(k, {}).get("status") == "running" and not done(k)]
    finished = [k for k in keys if done(k) or st.get(k, {}).get("status") == "done"]
    failed = [k for k in keys if st.get(k, {}).get("status") == "failed" and not done(k)]
    pending = [k for k in keys if k not in set(running) | set(finished) | set(failed)]
    return {"queue_total": len(keys), "pending": len(pending), "in_flight": len(running),
            "done": len(finished), "failed": len(failed), "next_queued": pending[:5]}


def claim(pool):
    """Atomically reserve up to `pool` slots and return the keys to dispatch.

    Selection, state update and write all happen under one lock, so two concurrent
    dispatchers can never be handed the same key."""
    with _StateLock():
        st = _load_state()
        keys = queue_keys()
        s = status(st, keys)
        room = max(0, pool - s["in_flight"])
        done_set = {k for k in keys
                    if done(k) or st.get(k, {}).get("status") in ("running", "done")}
        nxt = [k for k in keys if k not in done_set][:room]
        now = int(time.time())
        for k in nxt:
            st[k] = {"status": "running", "updated": now}
        if nxt:
            _save_state(st)
    return {"room": room, "in_flight": s["in_flight"] + len(nxt), "dispatch": nxt,
            "reserved": True}


def _out(obj, indent=None):
    """Print JSON as UTF-8 regardless of the console code page.

    Study keys contain non-ASCII characters (e.g. "Kaňková", "Hämäläinen").  On a cp1252
    console a plain print() raises UnicodeEncodeError *after* --claim has already reserved the
    keys, so the dispatcher never learns what it reserved.  Writing UTF-8 to the real stdout
    buffer avoids that and keeps the keys recoverable.
    """
    s = json.dumps(obj, ensure_ascii=False, indent=indent)
    try:
        sys.stdout.write(s + "\n")
        sys.stdout.flush()
    except UnicodeEncodeError:
        buf = getattr(sys.stdout, "buffer", None)
        if buf is not None:
            buf.write((s + "\n").encode("utf-8"))
            buf.flush()
        else:
            print(s.encode("ascii", "backslashreplace").decode("ascii"))


def main(argv=None):
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="backslashreplace")
        except Exception:
            pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", nargs="?", const="__show__",
                    help="no value: show status. With --mark: running|done|failed")
    ap.add_argument("--next", type=int)
    ap.add_argument("--pool", type=int,
                    help="how many slots to refill to (REPORT ONLY, does not reserve)")
    ap.add_argument("--claim", type=int,
                    help="atomically reserve + report how many slots to refill to")
    ap.add_argument("--mark")
    a = ap.parse_args(argv)

    if a.mark:
        val = a.status if (a.status and a.status != "__show__") else "done"
        if val not in ("running", "done", "failed"):
            print("--status must be running|done|failed", file=sys.stderr)
            return 2
        with _StateLock():
            st = _load_state()
            st[a.mark] = {"status": val, "updated": int(time.time())}
            _save_state(st)
        print("marked", a.mark, "->", val, flush=True)
        return 0
    if a.status and a.status != "__show__":
        print("a --status value requires --mark", file=sys.stderr)
        return 2
    if a.next:
        s = status()
        done_set = {k for k in queue_keys()
                    if done(k) or _load_state().get(k, {}).get("status") in ("running", "done")}
        nxt = [k for k in queue_keys() if k not in done_set][: a.next]
        _out(nxt)
        return 0
    if a.claim:
        _out(claim(a.claim))
        return 0
    if a.pool:
        s = status()
        room = max(0, a.pool - s["in_flight"])
        done_set = {k for k in queue_keys()
                    if done(k) or _load_state().get(k, {}).get("status") in ("running", "done")}
        nxt = [k for k in queue_keys() if k not in done_set][:room]
        _out({"room": room, "in_flight": s["in_flight"], "dispatch": nxt,
              "reserved": False})
        return 0
    _out(status(), indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
