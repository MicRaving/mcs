"""Forkserver preload: import the Flask app (and its heavy deps) once.

The forkserver imports this module **before** any worker is forked, so the
interpreter, Flask, numpy, pandas, PyMuPDF and the whole CodingStudio import
graph are mapped once and shared copy-on-write by every worker.  A worker's
own private memory is then its project state, not a fresh 100 MB import.

``studio_state`` configures a root at import time, so this module points it at
an empty scratch directory.  The child re-points the app at its own project
(:func:`browser_worker.build_worker` calls ``S.set_online``), which runs the
app's re-root hooks exactly as a desktop folder switch does.

``studio_web._freeze`` must stay the last preload module: it calls
``gc.freeze()`` so a worker's cyclic GC never touches (and un-shares) these
pages.  :mod:`studio_web.launcher` keeps that order in ``PRELOAD``.

Never import this module in the sync server: it is part of the browser stack.
"""

from __future__ import annotations

import os
import sys
import tempfile


def _neutral_root() -> str:
    root = os.path.join(tempfile.gettempdir(), "studio-forkserver")
    os.makedirs(root, exist_ok=True)
    # the import-time root of studio_state: empty on purpose (the child
    # reconfigures to its user's mirror before it serves a request)
    os.environ["CODING_STUDIO_ROOT"] = root
    # keep an import-time "recent project" write out of the gateway user's config
    os.environ.setdefault("XDG_CONFIG_HOME", os.path.join(root, "config"))
    return root


def preload() -> None:
    """Import the app in this (forkserver) process; never fatal."""
    _neutral_root()
    try:
        from . import ensure_worker_importable

        ensure_worker_importable()
        import app  # noqa: F401  the desktop Flask app, routes and all
    except Exception as exc:  # noqa: BLE001 - the worker imports it again and reports
        sys.stderr.write(f"studio-web preload: app import failed: {exc!r}\n")
        sys.stderr.flush()


preload()
