"""Test isolation for this suite (see ``Scripts/testing_guard.py``)."""

from __future__ import annotations

# --- test isolation (owner report 2026-11-25): never the real app data -------
import os as _os  # noqa: E402
import sys as _sys  # noqa: E402

_GUARD_DIR = _os.path.abspath(_os.path.join(_os.path.dirname(__file__), '../..'))
if _GUARD_DIR not in _sys.path:
    _sys.path.insert(0, _GUARD_DIR)
import testing_guard as _testing_guard  # noqa: E402

_testing_guard.isolate()
_testing_guard.pytest_hooks(globals())
# ------------------------------------------------------------------------------
