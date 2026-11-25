"""Keep every test run away from the user's real CodingStudio data.

Owner report 2026-11-25: test runs had left mirrors of fake servers
(``127.0.0.1_<port>``) in the real ``%LOCALAPPDATA%\\CodingStudio\\online`` -
a test went through ``routes_online.get_store()``, whose store uses the
default mirror base.  Every suite's ``conftest.py`` calls :func:`isolate` at
import time (before any app module reads the environment):

* ``CODINGSTUDIO_ONLINE_DIR`` -> a per-session temp dir (the mirrors),
* ``CODINGSTUDIO_CONFIG``     -> a temp config file (recent projects, saved
  connections),
* ``CODINGSTUDIO_NO_KEYRING=1`` -> no test writes to the OS credential store.

and registers :func:`check` at the end of the session: when anything new
appeared under the real online folder during the run, the session fails and
names it (:class:`RealDataTouched`).  ``test_zz_isolation.py`` asserts the
same from inside the suite.
"""

from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile

_STATE: dict = {}


def real_online_dir() -> str:
    """The real mirror base (what ``mirror.default_base_dir`` would pick
    without the override)."""
    if os.name == "nt":
        root = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
    elif sys.platform == "darwin":
        root = os.path.join(os.path.expanduser("~"), "Library", "Application Support")
    else:
        root = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(root, "CodingStudio", "online")


def snapshot(base: str | None = None) -> dict:
    """``{relpath: mtime_ns}`` of the first two levels (servers, projects)."""
    base = base or real_online_dir()
    out: dict[str, int] = {}
    try:
        servers = list(os.scandir(base))
    except OSError:
        return out
    for s in servers:
        try:
            out[s.name] = s.stat().st_mtime_ns
        except OSError:
            continue
        if not s.is_dir():
            continue
        try:
            for p in os.scandir(s.path):
                out[s.name + "/" + p.name] = -1          # names only below level 1
        except OSError:
            pass
    return out


class RealDataTouched(AssertionError):
    pass


def isolate() -> dict:
    """Point the app's data at a session temp dir (idempotent)."""
    if _STATE.get("tmp"):
        return _STATE
    tmp = tempfile.mkdtemp(prefix="cs-tests-")
    _STATE["tmp"] = tmp
    _STATE["before"] = snapshot()
    os.environ["CODINGSTUDIO_ONLINE_DIR"] = os.path.join(tmp, "online")
    os.environ["CODINGSTUDIO_CONFIG"] = os.path.join(tmp, "codingstudio.config.json")
    os.environ["CODINGSTUDIO_NO_KEYRING"] = "1"
    atexit.register(shutil.rmtree, tmp, True)
    return _STATE


def changes() -> list[str]:
    """What appeared/changed under the real online folder since :func:`isolate`."""
    before = _STATE.get("before")
    if before is None:
        return []
    now = snapshot()
    out = []
    for name, stamp in now.items():
        if name not in before:
            out.append(f"new: {name}")
        elif stamp != -1 and before[name] != stamp:
            out.append(f"changed: {name}")
    return sorted(out)


def check() -> None:
    found = changes()
    if found:
        raise RealDataTouched(
            "the test run wrote under the real " + real_online_dir() + ": " + "; ".join(found[:10]))


def pytest_hooks(namespace: dict) -> None:
    """Install the session-end check into a conftest's namespace."""

    def pytest_sessionfinish(session, exitstatus):     # noqa: ARG001
        found = changes()
        if found:
            sys.stderr.write("\nTEST ISOLATION FAILED - written under the real "
                             f"{real_online_dir()}:\n  " + "\n  ".join(found) + "\n")
            session.exitstatus = 1

    namespace.setdefault("pytest_sessionfinish", pytest_sessionfinish)
