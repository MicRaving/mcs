"""Rebuild the derived master workbook in the background.

After a screening or full-text decision is saved the master workbook has to be
regenerated, because it is a **derived** product (``Scripts/compile_master.py``,
plan §11).  Recompiling on every keystroke would be wasteful, so requests are
*debounced*: a save marks the project dirty, and one background thread waits
``DEBOUNCE_SECS`` after the last request, compiles to a temp file and replaces
the workbook atomically.  The PRISMA tab therefore always shows a workbook that
matches the decision files.

Only a project with ``Project/master_base.xlsx`` compiles (a legacy project
keeps writing the workbook directly).  In an online project only the **owner**
compiles: another member's mirror does not hold the other coders' decision
files (blinding), so their compile would compute wrong counts - and the
workbook is ``derived``, i.e. local-only for them anyway.

Status: :func:`status` (``GET /api/master/status``); trigger:
:func:`schedule`; synchronous build: :func:`compile_now`
(``POST /api/master/compile``).
"""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime

from studio_state import S, log

#: Seconds to wait after the last request before compiling.
DEBOUNCE_SECS = 5.0

_STATUS = {
    "running": False,
    "scheduled": False,
    "reason": "",
    "ok": None,
    "error": "",
    "last": None,
    "last_seconds": None,
    "out": None,
    "summary": {},
    "compiles": 0,
}

_LOCK = threading.Lock()
_COMPILE_LOCK = threading.Lock()
_WAITER = {"thread": None}


def compiled_path(root=None):
    return os.path.join(root or S.ROOT, "master_dataset.xlsx")


def _may_compile():
    """Owner-only in an online project (blinding: the mirror lacks other tags)."""
    if not S.online:
        return True
    try:
        from routes_online import current_store

        store = current_store()
        if store is None:
            return True
        return (store.me() or {}).get("role") == "owner"
    except Exception:  # noqa: BLE001 - an unknown role must not block a local build
        return False


def _denial():
    """A readable reason this mirror must not compile the master (None = it may).

    ``master_dataset.xlsx`` is a derived consensus file: it carries the AI's and
    every coder's screening/full-text verdicts, so it is not in a blinded
    coder's manifest at all (protocol ``blind_stages``) and the mirror holds
    only that member's own decision files.  Compiling there would silently
    compute wrong counts, so the job reports *why* it skipped instead of
    failing on a missing workbook or writing a partial one.
    """
    if _may_compile():
        return None
    if not S.online:
        return None
    return ("the master workbook is a derived consensus file compiled by the "
            "project owner: in an online project a coder's mirror does not hold "
            "the other coders' decision files (blinding), so it is not compiled here")


def enabled(root=None):
    """Whether this project builds its master from decision files."""
    try:
        import compile_master

        root = root or S.ROOT
        return bool(root) and compile_master.enabled(root) and _may_compile()
    except Exception:  # noqa: BLE001
        return False


def status():
    with _LOCK:
        payload = dict(_STATUS)
    payload["enabled"] = enabled()
    payload["blocked"] = _denial()
    payload["debounce_secs"] = DEBOUNCE_SECS
    payload["path"] = compiled_path() if S.ROOT else None
    return payload


def schedule(reason="decision", delay=None):
    """Ask for a recompile; returns False when the project is not compiled."""
    if not enabled():
        return False
    delay = DEBOUNCE_SECS if delay is None else max(0.0, float(delay))
    with _LOCK:
        _STATUS["scheduled"] = True
        _STATUS["reason"] = str(reason or "decision")
        thread = _WAITER.get("thread")
        if _STATUS["running"] or (thread is not None and thread.is_alive()):
            return True                   # the running/pending pass picks this up
        thread = threading.Thread(target=_waiter, args=(delay,), daemon=True,
                                  name="master-compile")
        _WAITER["thread"] = thread
        thread.start()
    return True


def _waiter(delay):
    time.sleep(delay)
    while True:
        with _LOCK:
            _STATUS["scheduled"] = False
        run_once()
        with _LOCK:
            if not _STATUS["scheduled"]:
                return
        time.sleep(delay)                 # a request arrived during the compile


def run_once():
    """Compile now, updating the status; never raises."""
    import compile_master

    denial = _denial()
    if denial:
        with _LOCK:
            _STATUS.update(ok=False, error=denial)
        log(f"master compile skipped: {denial}")
        return {"ok": False, "skipped": True, "error": denial}
    acquired = _COMPILE_LOCK.acquire(blocking=False)
    if not acquired:
        return {"ok": False, "busy": True}
    with _LOCK:
        _STATUS.update(running=True, error="")
    started = time.time()
    try:
        import screening_lib as SL

        with SL.MASTER_WRITE_LOCK:
            summary = compile_master.compile_master(S.ROOT, compiled_path())
        seconds = round(time.time() - started, 2)
        with _LOCK:
            _STATUS.update(ok=True, summary=summary, out=compiled_path(),
                           last=datetime.now().isoformat(timespec="seconds"),
                           last_seconds=seconds, compiles=_STATUS["compiles"] + 1)
        log(f"master compiled in {seconds}s ({summary.get('rows')} rows, "
            f"peak {summary.get('peak_rss_mb')} MB)")
        return {"ok": True, "summary": summary, "seconds": seconds}
    except Exception as exc:  # noqa: BLE001 - a failed compile must not break the app
        with _LOCK:
            _STATUS.update(ok=False, error=str(exc),
                           last=datetime.now().isoformat(timespec="seconds"))
        log(f"master compile failed: {exc}")
        return {"ok": False, "error": str(exc)}
    finally:
        with _LOCK:
            _STATUS["running"] = False
        _COMPILE_LOCK.release()


def compile_now():
    """Compile immediately (blocking) and return the result."""
    denial = _denial()
    if denial:
        with _LOCK:
            _STATUS.update(ok=False, error=denial)
        log(f"master compile skipped: {denial}")
        return {"ok": False, "skipped": True, "error": denial}
    if not enabled():
        return {"ok": False, "error": "this project does not use a derived master"}
    return run_once()
