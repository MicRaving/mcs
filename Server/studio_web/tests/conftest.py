"""Fixtures for the gateway tests: a fake auth backend and a fake launcher
whose "workers" are tiny HTTP servers in threads (echoing what they receive)."""

from __future__ import annotations

import json
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parents[2]
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import pytest  # noqa: E402

from studio_web.auth import AuthFailed  # noqa: E402

USERS = {
    "anna": {"password": "pw-anna", "user": {"id": 2, "username": "anna", "display_name": "Anna"},
             "projects": [{"id": "p1", "name": "Demo", "role": "coder"}]},
    "bo": {"password": "pw-bo", "user": {"id": 3, "username": "bo", "display_name": "Bo"},
           "projects": [{"id": "p1", "name": "Demo", "role": "coder"},
                        {"id": "p2", "name": "Other", "role": "owner"}]},
}


class FakeAuth:
    def __init__(self):
        self.tokens: dict[str, str] = {}
        self.logins: list[tuple[str, str | None]] = []
        self.logged_out: list[str] = []

    async def login(self, username, password, forwarded_for):
        self.logins.append((username, forwarded_for))
        u = USERS.get(username)
        if not u or u["password"] != password:
            raise AuthFailed(401, "unauthorized", "bad credentials")
        token = f"tok-{username}-{len(self.tokens)}"
        self.tokens[token] = username
        return {"token": token, "user": u["user"]}

    async def me(self, token):
        name = self.tokens.get(token)
        if not name:
            raise AuthFailed(401, "unauthorized")
        u = USERS[name]
        return {"user": u["user"], "projects": u["projects"]}

    async def logout(self, token):
        self.logged_out.append(token)
        self.tokens.pop(token, None)

    def forget(self, token):
        pass

    async def close(self):
        pass


class FakeWorkerProcess:
    _next_pid = 50000

    def __init__(self, env):
        FakeWorkerProcess._next_pid += 1
        self.pid = FakeWorkerProcess._next_pid
        self.env = dict(env)
        self.secret = env["STUDIO_WORKER_SECRET"]
        self.drained = 0
        self.received: list[dict] = []
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()
        proc = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _do(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                if self.headers.get("X-Studio-Worker-Secret") != proc.secret:
                    self.send_response(403)
                    self.end_headers()
                    return
                parsed = urllib.parse.urlsplit(self.path)
                with proc._lock:
                    proc.active += 1
                    proc.max_active = max(proc.max_active, proc.active)
                try:
                    if parsed.path == "/__worker/drain":
                        proc.drained += 1
                        out = {"ok": True, "pending": 0}
                    else:
                        q = urllib.parse.parse_qs(parsed.query)
                        if "sleep" in q:
                            time.sleep(float(q["sleep"][0]))
                        out = {"method": self.command, "path": parsed.path, "raw": self.path,
                               "query": parsed.query, "body": body.decode("utf-8", "replace"),
                               "headers": {k.lower(): v for k, v in self.headers.items()},
                               "worker_pid": proc.pid}
                        proc.received.append(out)
                finally:
                    with proc._lock:
                        proc.active -= 1
                data = json.dumps(out).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Set-Cookie", "worker=1")
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _do

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self._alive = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def alive(self):
        return self._alive

    def terminate(self):
        if self._alive:
            self._alive = False
            self.httpd.shutdown()
            self.httpd.server_close()

    kill = terminate

    def wait(self, timeout):
        return not self._alive


class FakeLauncher:
    name = "fake"

    def __init__(self):
        self.spawned: list[FakeWorkerProcess] = []
        self.delay = 0.0
        #: spawns that happened while a process for the same mirror was alive
        self.overlaps = 0

    def spawn(self, env, log_path, timeout):
        if self.delay:
            time.sleep(self.delay)
        mine = (env["STUDIO_HOME"], env["STUDIO_PROJECT"])
        if any(p.alive() and (p.env["STUDIO_HOME"], p.env["STUDIO_PROJECT"]) == mine for p in self.spawned):
            self.overlaps += 1
        p = FakeWorkerProcess(env)
        self.spawned.append(p)
        return p


@pytest.fixture
def fake_rss(monkeypatch):
    """``{pid: bytes}`` the supervisor reads instead of /proc."""
    table: dict[int, int] = {}
    import studio_web.supervisor as sup

    monkeypatch.setattr(sup, "rss_bytes", lambda pid: table.get(pid, 100 * 1024 * 1024))
    monkeypatch.setattr(sup, "pss_bytes", lambda pid: table.get(pid, 100 * 1024 * 1024))
    return table
