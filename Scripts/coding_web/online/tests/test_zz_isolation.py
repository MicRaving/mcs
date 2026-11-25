"""Guard: the suite never writes the user's real CodingStudio data.

Owner report 2026-11-25: test runs had left mirrors of fake servers in the real
``%LOCALAPPDATA%\\CodingStudio\\online\\127.0.0.1_*``.  ``conftest.py`` points
the mirrors, the app config and the credential store at a session temp dir
(``Scripts/testing_guard.py``); this module runs last in this folder
(``zz``) and fails when anything new appeared under the real folder meanwhile
- the session-end hook checks the same for the whole run.
"""

from __future__ import annotations

import os

import testing_guard
from online import keyring_store
from online.mirror import default_base_dir


def test_mirrors_config_and_keyring_point_at_the_session_temp_dir():
    tmp = testing_guard.isolate()["tmp"]
    real = os.path.normcase(os.path.abspath(testing_guard.real_online_dir()))
    base = os.path.normcase(os.path.abspath(default_base_dir()))
    assert base != real
    assert base.startswith(os.path.normcase(os.path.abspath(tmp)))
    assert os.environ["CODINGSTUDIO_CONFIG"].startswith(tmp)
    assert keyring_store.backend_name() == "file"       # no test reaches the OS store


def test_the_app_config_is_the_session_one():
    from studio_state import config_path

    tmp = testing_guard.isolate()["tmp"]
    assert os.path.normcase(config_path()).startswith(os.path.normcase(tmp))


def test_nothing_was_written_under_the_real_online_folder():
    found = testing_guard.changes()
    assert not found, (f"the suite wrote under {testing_guard.real_online_dir()}: {found}")
