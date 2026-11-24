"""End-to-end: browser users through the REAL gateway, REAL workers, REAL server.

Nothing is faked: ``python -m studio_server`` on a throw-away data dir, the
gateway ``python -m studio_web`` in front of it, and the gateway's own worker
processes (``browser_worker.py``: the desktop app in online mode).  The tests
talk to the gateway like a browser does (cookie, Origin header) and check
that blinding, the coder-tag gate, the route policy, locks, "Finish paper" and
the write path behave like on the desktop (PLAN_online_only §5, test plan T2/T3).

Blob linking: the gateway points the workers at the server's blob directory;
where the OS user may not create symlinks (Windows without developer mode) the
workers fall back to downloading - both paths end in the same mirror.

Run with::

    $env:STUDIO_E2E="1"; python -m pytest Server/studio_web/tests/test_e2e_browser.py -q
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("STUDIO_E2E") != "1",
                                reason="set STUDIO_E2E=1 to run against the real services")

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER_DIR = os.path.abspath(os.path.join(HERE, "..", ".."))
REPO = os.path.abspath(os.path.join(SERVER_DIR, ".."))
SCRIPTS = os.path.join(REPO, "Scripts")
CODING_WEB = os.path.join(SCRIPTS, "coding_web")
for _p in (SCRIPTS, CODING_WEB):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from online.client import ServerClient  # noqa: E402
from online.tests.e2e_real_server import (  # noqa: E402
    CODER, CODER_TAG, KEY, KEYS, OTHER, OTHER_TAG, OWNER, Http, Server, build_source, free_port,
    wait_until,
)
from online_shared import protocol  # noqa: E402

SCRATCH_ROOT = os.path.join(REPO, "temp", "online", "e2e_browser")


class Browser:
    """A browser session against the gateway: cookie jar + same-origin header."""

    def __init__(self, base: str):
        self.base = base
        self.jar = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar),
                                              _NoRedirect())

    def req(self, method, path, body=None, origin=True, accept="application/json"):
        data = None
        headers = {"Accept": accept}
        if origin:
            headers["Origin"] = self.base
        if isinstance(body, dict):
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        elif isinstance(body, str):
            data = body.encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        r = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with self.op.open(r, timeout=120) as resp:
                return resp.status, resp.read(), resp.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers

    def json(self, method, path, body=None, **kw):
        st, raw, _h = self.req(method, path, body, **kw)
        try:
            return st, json.loads(raw or b"null")
        except ValueError:
            return st, {"raw": raw[:300]}

    def login(self, username, password):
        form = urllib.parse.urlencode({"username": username, "password": password})
        st, raw, headers = self.req("POST", "/app/login", form, accept="text/html")
        assert st == 303, (st, raw[:300])
        return headers


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


@pytest.fixture(scope="module")
def bw():
    os.makedirs(SCRATCH_ROOT, exist_ok=True)
    scratch = tempfile.mkdtemp(prefix="run-", dir=SCRATCH_ROOT)
    data_dir = os.path.join(scratch, "server-data")
    source = os.path.join(scratch, "source-project")
    build_source(source)
    server = Server(data_dir, os.path.join(scratch, "server.log"))
    gw_proc = None

    class W:
        pass

    w = W()
    w.scratch = scratch
    try:
        server.create_admin(*OWNER)
        server.start()
        admin = Http(server.base)
        token = admin.ok("POST", "/api/auth/login", {"username": OWNER[0], "password": OWNER[1]})["token"]
        for (u, p), disp in ((CODER, "Anna"), (OTHER, "Bo")):
            admin.ok("POST", "/api/admin/users", {"username": u, "display_name": disp, "password": p,
                                                  "is_admin": False}, token=token)
        pid = admin.ok("POST", "/api/projects", {"name": "Browser E2E"}, token=token)["id"]
        users = {u["username"]: u for u in admin.ok("GET", "/api/admin/users", token=token)["users"]}
        admin.ok("PUT", f"/api/p/{pid}/members/{users[CODER[0]]['id']}",
                 {"role": "coder", "tags": [CODER_TAG], "tasks": ["coding", "settle"]}, token=token)
        admin.ok("PUT", f"/api/p/{pid}/members/{users[OTHER[0]]['id']}",
                 {"role": "coder", "tags": [OTHER_TAG], "tasks": ["coding", "settle"]}, token=token)
        # publish the synthetic project straight through the sync API (owner writes all)
        c = ServerClient(server.base, token=token)
        entries = []
        for d, _ds, fs in os.walk(source):
            for f in fs:
                full = os.path.join(d, f)
                rel = os.path.relpath(full, source).replace(os.sep, "/")
                with open(full, "rb") as fh:
                    data = fh.read()
                h = protocol.sha256_bytes(data)
                if c.blobs_missing(pid, [h]):
                    c.put_blob(pid, h, data)
                entries.append({"path": rel, "base_hash": None, "new_hash": h, "size": len(data)})
        res = c.push(pid, "e2e-publisher", entries)
        assert all(r["status"] == "accepted" for r in res["results"]), res

        port = free_port()
        env = dict(os.environ, PYTHONPATH=SERVER_DIR, STUDIO_WEB_DATA=os.path.join(scratch, "web"),
                   STUDIO_WEB_SERVER=server.base,
                   STUDIO_WEB_LOCAL_BLOBS=os.path.join(data_dir, "blobs"),
                   STUDIO_WEB_COOKIE_SECURE="0", STUDIO_WEB_MONITOR_SECS="1", PYTHONUNBUFFERED="1")
        env.pop("STUDIO_DATA_DIR", None)
        gw_log = open(os.path.join(scratch, "gateway.log"), "ab")
        gw_proc = subprocess.Popen([sys.executable, "-m", "studio_web", "serve", "--host", "127.0.0.1",
                                    "--port", str(port)], cwd=SERVER_DIR, env=env,
                                   stdout=gw_log, stderr=gw_log)
        base = f"http://127.0.0.1:{port}"
        wait_until(lambda: urllib.request.urlopen(base + "/app/health", timeout=2).status == 200,
                   timeout=60, what="gateway")
        w.server, w.admin, w.token, w.pid, w.base, w.users = server, admin, token, pid, base, users
        w.web = os.path.join(scratch, "web")
        w.anna = Browser(base)
        w.bo = Browser(base)
        yield w
    finally:
        if gw_proc is not None:
            gw_proc.terminate()
            try:
                gw_proc.wait(30)
            except subprocess.TimeoutExpired:
                gw_proc.kill()
        server.stop()


def app_path(w, rest):
    return f"/app/p/{w.pid}/{rest.lstrip('/')}"


def mirror_of(w, username):
    uid = w.users[username]["id"]
    base = os.path.join(w.web, f"u{uid}", "mirrors")
    for d, ds, _fs in os.walk(base):
        if w.pid in ds:
            return os.path.join(d, w.pid)
    return None


def server_head(w, path):
    man = w.admin.ok("GET", f"/api/p/{w.pid}/manifest?since=0", token=w.token)
    for f in man["files"]:
        if f["path"] == path and not f.get("deleted"):
            return f["hash"]
    return None


# ---------------------------------------------------------------------------

def test_b01_login_cookie_and_project_redirect(bw):
    st, raw, _h = bw.anna.req("GET", "/app/", accept="text/html")
    assert st == 200 and b"Sign in" in raw
    headers = bw.anna.login(*CODER)
    cookie = headers.get("Set-Cookie", "").lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "path=/app" in cookie
    st, _raw, headers = bw.anna.req("GET", "/app/", accept="text/html")
    assert st == 302 and headers["Location"] == f"/app/p/{bw.pid}/"


def test_b02_worker_starts_in_online_browser_mode(bw):
    st, status = bw.anna.json("GET", app_path(bw, "api/online/status"))
    assert st == 200, status
    assert status["active"] is True and status["browser"] is True
    assert status["me"]["tags"] == [CODER_TAG] and status["me"]["role"] == "coder"
    assert status["mode"] == "online"
    st, raw, _h = bw.anna.req("GET", app_path(bw, ""), accept="text/html")
    assert st == 200 and (b"browser.js" in raw or b"Preparing" in raw)


def test_b03_papers_pdfs_and_links(bw):
    st, data = bw.anna.json("GET", app_path(bw, "api/papers"))
    assert st == 200
    assert {p["key"] for p in data["papers"]} == set(KEYS)
    st, raw, _h = bw.anna.req("GET", app_path(bw, "paper/" + urllib.parse.quote(KEY, safe="")))
    assert st == 200 and raw.startswith(b"%PDF-1.4")
    m = mirror_of(bw, CODER[0])
    text = os.path.join(m, "Text", KEY + ".txt")
    assert os.path.isfile(text)
    if os.path.islink(text):            # Linux: linked from the server's blob store
        assert os.path.realpath(text).startswith(os.path.realpath(os.path.dirname(bw.server.data_dir)))


def test_b04_blinding_and_the_coder_tag_gate(bw):
    m = mirror_of(bw, CODER[0])
    assert os.path.isfile(os.path.join(m, "Codings", f"{KEY} - {CODER_TAG}.json"))
    assert not os.path.exists(os.path.join(m, "Codings", f"{KEY} - {OTHER_TAG}.json"))
    assert not os.path.exists(os.path.join(m, "Screening", f"{OTHER_TAG}.json"))
    q = urllib.parse.quote(KEY, safe="")
    # the foreign file is simply not in the mirror (as on the desktop) ...
    st, body = bw.anna.json("GET", app_path(bw, f"api/coding/{q}/{OTHER_TAG}"))
    assert st == 404, body
    # ... and a request that names the foreign tag is refused by the coder gate
    st, body = bw.anna.json("GET", app_path(bw, f"api/screening/list?coder={OTHER_TAG}"))
    assert st == 403, body
    st, body = bw.anna.json("POST", app_path(bw, "api/save"),
                            {"key": KEY, "coder": OTHER_TAG, "records": [{"Study_ID": "X"}]})
    assert st == 403, body


def test_b05_route_policy_refuses_desktop_only_routes(bw):
    st, body = bw.anna.json("POST", app_path(bw, "api/root"), {"root": "/"})
    assert st == 403 and body.get("error") in ("desktop_only", "forbidden"), body
    st, body = bw.anna.json("POST", app_path(bw, "api/online/connect"),
                            {"server": "http://evil.invalid", "username": "x", "password": "y"})
    assert st == 403, body


def test_b06_save_reaches_the_server_with_the_users_identity(bw):
    path = f"Codings/{KEY} - {CODER_TAG}.json"
    before = server_head(bw, path)
    rec = [{"Study_ID": "ALPHA2020", "Title": KEY, "Coder": CODER_TAG, "N_Analyzed": 999}]
    st, body = bw.anna.json("POST", app_path(bw, "api/save"),
                            {"key": KEY, "coder": CODER_TAG, "records": rec})
    assert st == 200 and body.get("ok") is not False, body
    wait_until(lambda: server_head(bw, path) not in (None, before), timeout=20,
               what="the browser save on the server")
    # one history with the desktop: the change row carries Anna's account
    import sqlite3

    con = sqlite3.connect(f"file:{os.path.join(bw.server.data_dir, 'studio.db')}?mode=ro", uri=True)
    try:
        row = con.execute("SELECT user_id, kind FROM changes WHERE project_id=? AND path=? "
                          "ORDER BY seq DESC LIMIT 1", (bw.pid, path)).fetchone()
    finally:
        con.close()
    assert row is not None and row[0] == bw.users[CODER[0]]["id"] and row[1] in ("push", "merge"), row


def test_b07_finish_paper_lifts_blinding_for_that_paper_only(bw):
    st, body = bw.anna.json("POST", app_path(bw, "api/online/done"),
                            {"key": KEY, "stage": "coding", "tag": CODER_TAG})
    assert st == 200, body
    bw.anna.json("POST", app_path(bw, "api/online/sync"), {})
    m = mirror_of(bw, CODER[0])
    wait_until(lambda: os.path.isfile(os.path.join(m, "Codings", f"{KEY} - {OTHER_TAG}.json")),
               timeout=30, what="the other tag's file after Finish paper")
    assert not os.path.exists(os.path.join(m, "Codings", f"{KEYS[1]} - {OTHER_TAG}.json"))


def test_b08_locks_are_shared_with_desktop_clients(bw):
    q = urllib.parse.quote(KEY, safe="")
    st, body = bw.anna.json("POST", app_path(bw, f"api/online/locks/{q}"), {})
    assert st == 200, body
    # Bo is a desktop client here: the plain sync API with his own token
    bo_tok = bw.admin.ok("POST", "/api/auth/login", {"username": OTHER[0], "password": OTHER[1]})["token"]
    st, body = bw.admin.request("POST", f"/api/p/{bw.pid}/locks/{q}", {"client_id": "bo-desktop"},
                                token=bo_tok)
    assert st == 409, body
    # and Bo in the browser sees the same lock
    bw.bo.login(*OTHER)
    st, body = bw.bo.json("POST", app_path(bw, f"api/online/locks/{q}"), {})
    assert st == 409, body
    st, body = bw.anna.json("DELETE", app_path(bw, f"api/online/locks/{q}"))
    assert st == 200, body


def test_b09_cross_site_and_traversal_refused(bw):
    st, _ = bw.anna.json("POST", app_path(bw, "api/save"), {"key": KEY}, origin=False)
    assert st == 403
    st, _r, _h = bw.anna.req("GET", app_path(bw, "paper/..%2f..%2fserver-data%2fstudio.db"))
    assert st == 400


def test_b10_two_users_two_workers_then_logout_stops_the_worker(bw):
    health = json.loads(urllib.request.urlopen(bw.base + "/app/health").read())
    assert health["workers"] == 2
    st, _r, _h = bw.anna.req("POST", "/app/logout", "", accept="text/html")
    assert st == 303
    wait_until(lambda: json.loads(urllib.request.urlopen(bw.base + "/app/health").read())["workers"] == 1,
               timeout=40, what="anna's worker to stop")
    st, _ = bw.anna.json("GET", app_path(bw, "api/online/status"))
    assert st == 401
