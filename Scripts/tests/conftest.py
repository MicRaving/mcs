"""Make the campaign modules importable for the tests in this folder.

``Scripts/`` holds the pipeline modules and ``Scripts/coding_web`` the app
modules (``screening_lib``, ``fulltext_lib``); both go on ``sys.path`` the same
way the app and the other test suites do it.
"""

from __future__ import annotations

# --- test isolation (owner report 2026-11-25): never the real app data -------
import os as _os  # noqa: E402
import sys as _sys  # noqa: E402

_GUARD_DIR = _os.path.abspath(_os.path.join(_os.path.dirname(__file__), '..'))
if _GUARD_DIR not in _sys.path:
    _sys.path.insert(0, _GUARD_DIR)
import testing_guard as _testing_guard  # noqa: E402

_testing_guard.isolate()
_testing_guard.pytest_hooks(globals())
# ------------------------------------------------------------------------------

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.dirname(_HERE)
for _path in (_SCRIPTS, os.path.join(_SCRIPTS, "coding_web")):
    if _path not in sys.path:
        sys.path.insert(0, _path)
