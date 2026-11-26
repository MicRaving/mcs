"""The one-product entrypoint: the browser-mode toggle (:mod:`studio_server.run`).

Three things are checked:

* the switch and its defaults (``STUDIO_BROWSER_MODE``, the browser port);
* **off** costs nothing: no browser module is imported and ``/app/`` answers a
  page that says browser mode is switched off;
* **on** starts the gateway as a child and restarts it when it crashes.

The process-level checks are POSIX-only: they rely on SIGTERM and on the
gateway's own ``/app/health``; the unit checks run everywhere.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parents[1]

from studio_server import browser, run  # noqa: E402
from studio_server.app import create_app  # noqa: E402
from studio_server.config import Config  # noqa: E402

POSIX = pytest.mark.skipif(os.name != "posix", reason="POSIX process semantics")


# ── the switch ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("value,expected", [
    (None, False), ("", False), ("0", False), ("off", False), ("no", False),
    ("1", True), ("true", True), ("yes", True), ("on", True), (" TRUE ", True),
])
def test_browser_mode_switch(value, expected):
    env = {} if value is None else {"STUDIO_BROWSER_MODE": value}
    assert browser.browser_mode_enabled(env) is expected


def test_browser_port_default_and_override():
    assert browser.browser_port({}) == 8780
    assert browser.browser_port({"STUDIO_BROWSER_PORT": "9000"}) == 9000
    assert browser.browser_port({"STUDIO_BROWSER_PORT": "nope"}) == 8780


def test_load_config_reads_the_toggle(monkeypatch):
    from studio_server.config import load_config

    monkeypatch.setenv("STUDIO_BROWSER_MODE", "1")
    monkeypatch.setenv("STUDIO_BROWSER_PORT", "9100")
    cfg = load_config(temp_dir())
    assert cfg.browser_mode is True and cfg.browser_port == 9100


def temp_dir():
    import tempfile

    return tempfile.mkdtemp(prefix="studio-run-")


# ── off: no browser stack, /app/ says so ──────────────────────────────────


def test_off_never_imports_the_browser_stack():
    """A fresh interpreter: importing the server must not import studio_web."""
    code = (
        "import sys; sys.path.insert(0, %r);"
        "import studio_server.app, studio_server.run;"
        "print(int('studio_web' in sys.modules))"
    ) % str(SERVER_DIR)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         timeout=120, cwd=str(SERVER_DIR))
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "0", out.stderr


def test_off_app_page_and_health(client):
    response = client.get("/app/")
    assert response.status_code == 200
    assert "Browser mode is switched off" in response.text
    assert "desktop app" in response.text
    assert response.headers["cache-control"] == "no-store"
    # every /app/* path is the same friendly page (the proxy config is unchanged)
    assert "Browser mode is switched off" in client.get("/app/p/any/").text
    assert client.get("/api/health").json()["browser_mode"] == "off"
    assert client.get("/app/health").json()["browser_mode"] == "off"


def test_on_health_reports_workers(monkeypatch, tmp_path):
    monkeypatch.setattr(browser, "probe_gateway", lambda url=None, timeout=0.8: {
        "ok": True, "workers": 3, "rss_mb": 812.5, "lane": {"slots": 1, "waiting": 0}})
    app = create_app(Config(data_dir=tmp_path / "data", browser_mode=True, browser_port=8780))
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        body = c.get("/api/health").json()
        assert body["browser_mode"] == "on" and body["workers"] == 3
        assert body["rss_mb"] == 812.5 and body["gateway_ok"] is True
        assert c.get("/app/").status_code == 404          # the gateway owns /app/


def test_on_health_reports_an_unreachable_gateway(monkeypatch, tmp_path):
    monkeypatch.setattr(browser, "probe_gateway", lambda url=None, timeout=0.8: {
        "ok": False, "error": "ConnectionRefusedError"})
    app = create_app(Config(data_dir=tmp_path / "data", browser_mode=True))
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        body = c.get("/api/health").json()
        assert body["browser_mode"] == "on" and body["gateway_ok"] is False
        assert "ConnectionRefusedError" in body["gateway_error"]


# ── the child supervisor ──────────────────────────────────────────────────


class _ExitingGateway(run.GatewayProcess):
    """A gateway whose command exits at once, so the restart path is testable."""

    def command(self):
        return [sys.executable, "-c", "import sys; sys.exit(3)"]


def test_gateway_is_restarted_with_backoff(monkeypatch):
    monkeypatch.setattr(run, "BACKOFF", (0.05,))
    monkeypatch.setattr(run, "HEALTHY_AFTER", 0.0)
    gw = _ExitingGateway("127.0.0.1", 1, log_level="critical")
    try:
        assert gw.start() is True
        deadline = time.monotonic() + 10
        while gw.restarts < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert gw.restarts >= 2
    finally:
        gw.stop()
        count = gw.restarts
    time.sleep(0.2)
    assert gw.restarts == count          # stop() ends the restart loop


# ── the real product, POSIX ───────────────────────────────────────────────


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _get(url: str, timeout: float = 3.0):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.status, resp.read()


def _wait(url: str, timeout: float = 60.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            return _get(url)
        except (urllib.error.URLError, ConnectionError, OSError):
            time.sleep(0.15)
    raise AssertionError(f"not reachable: {url}")


@POSIX
def test_product_off_answers_app_on_the_browser_port(tmp_path):
    import tempfile

    port, bport = _free_port(), _free_port()
    env = dict(os.environ, STUDIO_BROWSER_MODE="0", STUDIO_BROWSER_PORT=str(bport),
               PYTHONPATH=str(SERVER_DIR), PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.Popen(
        [sys.executable, "-m", "studio_server", "run", "--host", "127.0.0.1",
         "--port", str(port), "--data", str(tmp_path / "data")],
        cwd=str(SERVER_DIR), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        _wait(f"http://127.0.0.1:{port}/api/health")
        status, body = _get(f"http://127.0.0.1:{bport}/app/")
        assert status == 200 and b"Browser mode is switched off" in body
        assert json.loads(_get(f"http://127.0.0.1:{port}/api/health")[1])["browser_mode"] == "off"
    finally:
        proc.terminate()
        try:
            proc.wait(20)
        except subprocess.TimeoutExpired:
            proc.kill()


@POSIX
def test_product_on_starts_and_restarts_the_gateway(tmp_path):
    port, bport = _free_port(), _free_port()
    env = dict(os.environ, STUDIO_BROWSER_MODE="1", STUDIO_BROWSER_PORT=str(bport),
               PYTHONPATH=str(SERVER_DIR), PYTHONDONTWRITEBYTECODE="1",
               STUDIO_WEB_DATA=str(tmp_path / "web"), STUDIO_WEB_COOKIE_SECURE="0",
               STUDIO_WEB_LAUNCHER="subprocess")
    proc = subprocess.Popen(
        [sys.executable, "-m", "studio_server", "run", "--host", "127.0.0.1",
         "--port", str(port), "--data", str(tmp_path / "data")],
        cwd=str(SERVER_DIR), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        _wait(f"http://127.0.0.1:{bport}/app/health")
        body = json.loads(_get(f"http://127.0.0.1:{port}/api/health")[1])
        assert body["browser_mode"] == "on" and body["gateway_ok"] is True
        pid = json.loads(_get(f"http://127.0.0.1:{bport}/app/health")[1]).get("pid")

        # crash the gateway: the supervisor restarts it (same port again)
        import signal

        os.kill(int(pid), signal.SIGKILL)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                new = json.loads(_get(f"http://127.0.0.1:{bport}/app/health")[1]).get("pid")
                if new and int(new) != int(pid):
                    break
            except (urllib.error.URLError, ConnectionError, OSError):
                pass
            time.sleep(0.3)
        else:
            raise AssertionError("the gateway was not restarted")
        assert int(new) != int(pid)
    finally:
        proc.terminate()
        try:
            proc.wait(60)
        except subprocess.TimeoutExpired:
            proc.kill()
