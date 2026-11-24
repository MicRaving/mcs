"""Unit tests of the browser worker's own logic (the full worker is covered by
``Server/studio_web/tests/test_e2e_browser.py``)."""

from __future__ import annotations

import os
import sys

import pytest

import browser_policy
import browser_worker as bw


def _endpoint(cls):
    return next(e for e, c in browser_policy.CLASSIFICATION.items() if c == cls)


def test_policy_allows_reads_and_refuses_desktop_only_routes():
    allowed = _endpoint(browser_policy.ALLOW)
    status, body = bw.check_route(allowed, "GET", "coder")
    assert status == 200 and body == {}
    desktop = _endpoint(browser_policy.DESKTOP_ONLY)
    status, body = bw.check_route(desktop, "POST", "owner")
    assert status == 403 and body["error"] == "desktop_only"


def test_owner_only_routes_follow_the_project_role():
    owner_only = _endpoint(browser_policy.OWNER_ONLY)
    assert bw.check_route(owner_only, "POST", "owner")[0] == 200
    assert bw.check_route(owner_only, "POST", "coder")[0] == 403


def test_unknown_endpoints_are_refused():
    status, body = bw.check_route("nope.not_a_route", "GET", "owner")
    assert status == 403


def test_a_missing_policy_fails_closed(monkeypatch):
    monkeypatch.setitem(sys.modules, "browser_policy", None)      # import -> ImportError
    status, body = bw.check_route(_endpoint(browser_policy.ALLOW), "GET", "owner")
    assert status == 503 and body["error"] == "desktop_only"


def test_prepare_home_points_every_per_user_location_at_the_home(tmp_path, monkeypatch):
    keys = ("HOME", "LOCALAPPDATA", "APPDATA", "XDG_CACHE_HOME", "XDG_CONFIG_HOME",
            "XDG_DATA_HOME", "CODING_STUDIO_ROOT", "TMPDIR")
    for k in keys:
        monkeypatch.setenv(k, os.environ.get(k, ""))           # restored after the test
    monkeypatch.setenv("CODING_STUDIO_RUN_ANALYSIS", "1")
    paths = bw.prepare_home(str(tmp_path / "u7"))
    home = str(tmp_path / "u7")
    for k in keys:
        assert os.environ[k].startswith(home), k
    assert os.path.isdir(paths["CODING_STUDIO_ROOT"])
    assert "CODING_STUDIO_RUN_ANALYSIS" not in os.environ        # never the pipeline on the server


def test_worker_config_requires_the_secret_and_token():
    with pytest.raises(SystemExit):
        bw.WorkerConfig.from_env({"STUDIO_SERVER": "http://s", "STUDIO_PROJECT": "p", "STUDIO_HOME": "/h"})
    cfg = bw.WorkerConfig.from_env({"STUDIO_SERVER": "http://s", "STUDIO_TOKEN": "t", "STUDIO_PROJECT": "p",
                                    "STUDIO_HOME": "/h", "STUDIO_WORKER_SECRET": "x"})
    assert cfg.sync_interval == 5.0 and cfg.push_debounce == 1.0 and cfg.local_blobs is None
