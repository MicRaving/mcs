"""studio-web — the browser gateway of CodingStudio's online-only mode (phase 8).

``Results/PLAN_online_only.md`` §4/§6: logs browser users in with the studio
server's credentials, keeps the session cookie, and proxies
``/app/p/<project>/…`` to one headless CodingStudio worker per (user, project)
(``Scripts/coding_web/browser_worker.py``).  The supervisor starts workers on
demand, stops idle ones, restarts oversized ones and lets only one memory-heavy
corpus computation run at a time.

The gateway never reads project data itself: visibility, tags, tasks and locks
are decided by the studio server and the worker's unchanged desktop code.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

WEB_NAME = "studio-web"
WEB_VERSION = "0.1.0"


def repo_scripts() -> Path | None:
    """``<repo>/Scripts`` in a dev checkout, ``/app`` layout in the image, else None."""
    here = Path(__file__).resolve()
    for cand in (here.parents[2] / "Scripts", here.parents[1]):
        if (cand / "coding_web" / "browser_worker.py").is_file():
            return cand
    return None


def default_worker_script() -> str:
    env = os.environ.get("STUDIO_WEB_WORKER_SCRIPT")
    if env:
        return env
    scripts = repo_scripts()
    if scripts is None:
        return ""
    return str(scripts / "coding_web" / "browser_worker.py")


def ensure_worker_importable() -> None:
    """Make ``browser_worker`` importable (forkserver children run it in-process)."""
    script = default_worker_script()
    if not script:
        return
    coding_web = os.path.dirname(script)
    for path in (coding_web, os.path.dirname(coding_web)):
        if path and path not in sys.path:
            sys.path.insert(0, path)
