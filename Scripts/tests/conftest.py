"""Make the campaign modules importable for the tests in this folder.

``Scripts/`` holds the pipeline modules and ``Scripts/coding_web`` the app
modules (``screening_lib``, ``fulltext_lib``); both go on ``sys.path`` the same
way the app and the other test suites do it.
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.dirname(_HERE)
for _path in (_SCRIPTS, os.path.join(_SCRIPTS, "coding_web")):
    if _path not in sys.path:
        sys.path.insert(0, _path)
