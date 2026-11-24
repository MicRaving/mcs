"""Gateway behaviour with fake workers (PLAN_online_only §4, §6, §9; test plan T1)."""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from studio_web.app import create_app
from studio_web.config import WebConfig
from studio_web.heavy import classify

from .conftest import FakeAuth, FakeLauncher

ORIGIN = "http://gw.test"


def make(tmp_path, **over):
    cfg = WebConfig(data_dir=str(tmp_path / "web"), server="http://studio.invalid",
                    worker_script="unused", monitor_secs=3600, cookie_secure=True, **over)
    launcher = FakeLauncher()
    auth = FakeAuth()
    app = create_app(cfg, launcher=launcher, auth=auth)
    return app, launcher, auth


def run(app, scenario):
    async def main():
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, client=("10.0.0.5", 50000))
            async with httpx.AsyncClient(transport=transport, base_url=ORIGIN,
                                         follow_redirects=False) as c:
                return await scenario(c)
    return asyncio.run(main())


async def login(c, user="anna", pw=None):
    from .conftest import USERS

    r = await c.post("/app/login", data={"username": user, "password": pw or USERS[user]["password"]},
                     headers={"Origin": ORIGIN})
    assert r.status_code == 303, r.text
    return r


def sup_of(app):
    return app.state.web["sup"]


# ── login / session ───────────────────────────────────────────────────────

def test_login_sets_a_strict_httponly_cookie_and_lists_projects(tmp_path):
    app, _l, auth = make(tmp_path)

    async def scenario(c):
        r = await c.get("/app/")
        assert r.status_code == 200 and "Sign in" in r.text
        r = await login(c, "bo")
        cookie = r.headers["set-cookie"].lower()
        for part in ("studio_session=", "httponly", "samesite=strict", "path=/app", "secure"):
            assert part in cookie
        assert auth.logins == [("bo", "10.0.0.5")]
        c.cookies.set("studio_session", r.cookies["studio_session"])
        r = await c.get("/app/")
        assert "Demo" in r.text and "Other" in r.text           # two projects: a list
    run(app, scenario)


def test_single_project_redirects_and_wrong_password_is_401(tmp_path):
    app, _l, _a = make(tmp_path)

    async def scenario(c):
        r = await c.post("/app/login", data={"username": "anna", "password": "nope"},
                         headers={"Origin": ORIGIN})
        assert r.status_code == 401 and "Wrong username or password" in r.text
        r = await c.post("/app/login", json={"username": "anna", "password": "pw-anna"},
                         headers={"Origin": ORIGIN})
        assert r.status_code == 200 and r.json()["projects"][0]["id"] == "p1"
        c.cookies.set("studio_session", r.cookies["studio_session"])
        r = await c.get("/app/")
        assert r.status_code == 302 and r.headers["location"] == "/app/p/p1/"
    run(app, scenario)


def test_no_session_means_401_for_api_and_redirect_for_pages(tmp_path):
    app, launcher, _a = make(tmp_path)

    async def scenario(c):
        r = await c.get("/app/p/p1/api/papers", headers={"Accept": "application/json"})
        assert r.status_code == 401
        r = await c.get("/app/p/p1/", headers={"Accept": "text/html"})
        assert r.status_code == 302 and r.headers["location"] == "/app/"
        c.cookies.set("studio_session", "forged")
        r = await c.get("/app/p/p1/api/papers")
        assert r.status_code == 401
        assert launcher.spawned == []
    run(app, scenario)


def test_cross_site_writes_are_refused(tmp_path):
    app, _l, _a = make(tmp_path)

    async def scenario(c):
        r = await c.post("/app/login", data={"username": "anna", "password": "pw-anna"})
        assert r.status_code == 403                                   # no Origin at all
        r = await c.post("/app/login", data={"username": "anna", "password": "pw-anna"},
                         headers={"Origin": "http://evil.test"})
        assert r.status_code == 403
        r = await login(c)
        c.cookies.set("studio_session", r.cookies["studio_session"])
        r = await c.post("/app/p/p1/api/save", json={}, headers={"Origin": "http://evil.test"})
        assert r.status_code == 403
        r = await c.post("/app/p/p1/api/save", json={}, headers={"Referer": ORIGIN + "/app/p/p1/"})
        assert r.status_code == 200
    run(app, scenario)


# ── proxy ────────────────────────────────────────────────────────────────

def test_proxy_forwards_raw_path_and_query_strips_credentials(tmp_path):
    app, launcher, auth = make(tmp_path)

    async def scenario(c):
        r = await login(c)
        tok = r.cookies["studio_session"]
        c.cookies.set("studio_session", tok)
        r = await c.get("/app/p/p1/api/coding/AMAR%20et%20al.%20%282026%29/AN?x=1&y=%C3%A4",
                        headers={"Authorization": "Bearer stolen", "X-Studio-Worker-Secret": "guess"})
        assert r.status_code == 200, r.text
        got = r.json()
        assert got["raw"] == "/api/coding/AMAR%20et%20al.%20%282026%29/AN?x=1&y=%C3%A4"
        h = got["headers"]
        assert "cookie" not in h and "authorization" not in h
        assert h["x-studio-worker-secret"] == launcher.spawned[0].secret != "guess"
        assert "set-cookie" not in r.headers                        # the worker cannot set cookies
        assert r.headers["x-content-type-options"] == "nosniff"
        env = launcher.spawned[0].env
        assert env["STUDIO_TOKEN"] == tok and env["STUDIO_PROJECT"] == "p1"
        assert env["STUDIO_HOME"].endswith("u2")
        r = await c.post("/app/p/p1/api/save", json={"coder": "AN"}, headers={"Origin": ORIGIN})
        assert r.json()["body"] == '{"coder":"AN"}'
    run(app, scenario)


@pytest.mark.parametrize("path", [
    "/app/p/p1/../p2/api/papers",
    "/app/p/p1/api/%2e%2e/%2e%2e/etc/passwd",
    "/app/p/p1/paper/..%2fsecret",
    "/app/p/p1/paper/a%5c..%5cb",
    "/app/p/p1/x/%00",
])
def test_traversal_is_refused(tmp_path, path):
    app, launcher, _a = make(tmp_path)

    async def scenario(c):
        r = await login(c)
        c.cookies.set("studio_session", r.cookies["studio_session"])
        r = await c.get(path)
        assert r.status_code in (400, 404)
        for w in launcher.spawned:
            assert not any("etc/passwd" in x["path"] or "secret" in x["path"] for x in w.received)
    run(app, scenario)


def test_foreign_project_is_404_and_workers_are_per_user(tmp_path):
    app, launcher, _a = make(tmp_path)

    async def scenario(c):
        r = await login(c, "anna")
        c.cookies.set("studio_session", r.cookies["studio_session"])
        assert (await c.get("/app/p/p2/api/papers")).status_code == 404
        a = (await c.get("/app/p/p1/api/papers")).json()["worker_pid"]
        r = await login(c, "bo")
        c.cookies.set("studio_session", r.cookies["studio_session"])
        b = (await c.get("/app/p/p1/api/papers")).json()["worker_pid"]
        assert a != b and len(launcher.spawned) == 2
        assert (await c.get("/app/p/p1/api/papers")).json()["worker_pid"] == b   # reused
    run(app, scenario)


def test_navigation_shows_preparing_page_while_the_worker_starts(tmp_path):
    app, launcher, _a = make(tmp_path)
    launcher.delay = 0.3

    async def scenario(c):
        r = await login(c)
        c.cookies.set("studio_session", r.cookies["studio_session"])
        r = await c.get("/app/p/p1/", headers={"Accept": "text/html"})
        assert r.status_code == 200 and "Preparing your workspace" in r.text
        await asyncio.sleep(0.6)
        r = await c.get("/app/p/p1/", headers={"Accept": "text/html"})
        assert r.json()["path"] == "/"
        assert len(launcher.spawned) == 1
    run(app, scenario)


# ── heavy lane ───────────────────────────────────────────────────────────

def test_heavy_classification():
    assert classify("GET", "/api/agreement/reliability") == (True, "agreement")
    assert classify("POST", "/api/screening/merge") == (True, None)
    assert classify("GET", "/api/papers") == (False, None)
    assert classify("POST", "/api/save") == (False, None)
    assert classify("GET", "/pageimg/K/1.png") == (False, None)


def test_heavy_requests_of_different_users_run_one_at_a_time(tmp_path):
    app, launcher, _a = make(tmp_path)

    async def scenario(c):
        cookies = {}
        for user in ("anna", "bo"):
            r = await login(c, user)
            cookies[user] = r.cookies["studio_session"]

        async def call(user, path):
            t0 = time.monotonic()
            r = await c.get(path, cookies={"studio_session": cookies[user]})
            return r, time.monotonic() - t0

        # start both workers first
        await call("anna", "/app/p/p1/api/online/status")
        await call("bo", "/app/p/p1/api/online/status")
        t0 = time.monotonic()
        (ra, _), (rb, _), (rl, tl) = await asyncio.gather(
            call("anna", "/app/p/p1/api/agreement/reliability?sleep=0.5"),
            call("bo", "/app/p/p1/api/summary/variables?sleep=0.5"),
            call("anna", "/app/p/p1/api/coding/K/AN"))            # light: not queued
        total = time.monotonic() - t0
        assert ra.status_code == rb.status_code == rl.status_code == 200
        assert total >= 0.95, total                                # serialised
        assert tl < 0.45, tl
        queued = sorted(float(r.headers["x-studio-queued"]) for r in (ra, rb))
        assert queued[0] < 0.2 and queued[1] >= 0.4
        assert ra.json()["headers"].get("x-studio-heavy") == "1"
        # the family is warm in anna's worker now: no lane any more
        r, _ = await call("anna", "/app/p/p1/api/agreement/reliability")
        assert "x-studio-queued" not in r.headers
        assert "agreement" in sup_of(app).workers[(2, "p1")].warm
    run(app, scenario)


# ── lifecycle ────────────────────────────────────────────────────────────

def test_idle_soft_cap_hard_cap_and_pressure(tmp_path, fake_rss):
    app, launcher, _a = make(tmp_path, idle_secs=1000, soft_cap_mb=500, hard_cap_mb=900,
                             pressure_mb=1000)
    MB = 1024 * 1024

    async def scenario(c):
        cookies = {}
        for user in ("anna", "bo"):
            r = await login(c, user)
            cookies[user] = r.cookies["studio_session"]
            await c.get("/app/p/p1/api/online/status", cookies={"studio_session": cookies[user]})
        sup = sup_of(app)
        wa, wb = sup.workers[(2, "p1")], sup.workers[(3, "p1")]
        pa, pb = wa.proc, wb.proc

        # nothing to do
        fake_rss.update({pa.pid: 200 * MB, pb.pid: 200 * MB})
        await sup.enforce()
        await asyncio.sleep(0.05)
        assert pa.alive() and pb.alive()

        # soft cap: anna's worker is drained and stopped, restarted on the next request
        fake_rss[pa.pid] = 600 * MB
        await sup.enforce()
        await asyncio.sleep(0.2)
        assert not pa.alive() and pa.drained == 1
        r = await c.get("/app/p/p1/api/papers", cookies={"studio_session": cookies["anna"]})
        assert r.status_code == 200 and wa.proc is not pa and wa.proc.alive()

        # idle: bo's worker stops after idle_secs without requests
        sup.cfg.idle_secs = 0.0
        wb.last_used -= 10
        wa.last_used = time.monotonic() + 3600
        await sup.enforce()
        await asyncio.sleep(0.2)
        assert not pb.alive() and pb.drained == 1
        sup.cfg.idle_secs = 1000

        # hard cap: stopped even with a request in flight (no drain then)
        pa2 = wa.proc
        fake_rss[pa2.pid] = 950 * MB
        wa.inflight = 1
        await sup.enforce()
        await asyncio.sleep(0.2)
        assert not pa2.alive() and pa2.drained == 0
        wa.inflight = 0

        # pressure: the least recently used idle worker goes first
        r = await c.get("/app/p/p1/api/papers", cookies={"studio_session": cookies["anna"]})
        r = await c.get("/app/p/p1/api/papers", cookies={"studio_session": cookies["bo"]})
        pa3, pb3 = wa.proc, wb.proc
        fake_rss.update({pa3.pid: 450 * MB, pb3.pid: 450 * MB})
        wa.last_used, wb.last_used = time.monotonic() - 50, time.monotonic() - 5
        sup.cfg.pressure_mb = 800
        await sup.enforce()
        await asyncio.sleep(1.2)            # the stop event is logged once the process is gone
        assert not pa3.alive() and pb3.alive()
        events = [e["event"] + ":" + str(e.get("reason", "")) for e in sup.events]
        for want in ("stopped:soft_cap", "stopped:idle", "stopped:hard_cap", "stopped:pressure"):
            assert want in events, "|".join(events)
    run(app, scenario)


def test_a_request_during_a_stop_waits_one_process_per_mirror(tmp_path):
    app, launcher, _a = make(tmp_path)

    async def scenario(c):
        r = await login(c)
        c.cookies.set("studio_session", r.cookies["studio_session"])
        await c.get("/app/p/p1/api/papers")
        sup = sup_of(app)
        w = sup.workers[(2, "p1")]
        first = w.proc
        stops = [asyncio.create_task(sup.stop(w, "soft_cap")) for _ in range(3)]
        await asyncio.sleep(0)                                    # the stop has begun
        r = await c.get("/app/p/p1/api/papers")                   # arrives mid-stop
        await asyncio.gather(*stops)
        assert r.status_code == 200
        assert not first.alive() and w.proc is not first and w.proc.alive()
        assert launcher.overlaps == 0 and len(launcher.spawned) == 2
        assert [e["event"] for e in sup.events].count("stopped") == 1   # three calls, one stop
    run(app, scenario)


def test_logout_stops_the_users_workers_and_clears_the_cookie(tmp_path):
    app, launcher, auth = make(tmp_path)

    async def scenario(c):
        r = await login(c)
        tok = r.cookies["studio_session"]
        c.cookies.set("studio_session", tok)
        await c.get("/app/p/p1/api/papers")
        p = launcher.spawned[0]
        r = await c.post("/app/logout", headers={"Origin": ORIGIN})
        assert r.status_code == 303
        assert "studio_session=" in r.headers["set-cookie"] and "max-age=0" in r.headers["set-cookie"].lower()
        assert not p.alive() and p.drained == 1
        assert auth.logged_out == [tok]
    run(app, scenario)


def test_health_detail_only_from_loopback_and_admin_drain(tmp_path):
    app, launcher, _a = make(tmp_path)

    async def scenario(c):
        r = await login(c)
        c.cookies.set("studio_session", r.cookies["studio_session"])
        await c.get("/app/p/p1/api/papers")
        r = await c.get("/app/health")
        assert r.json()["workers"] == 1 and "detail" not in r.json()
        r = await c.post("/app/admin/drain", headers={"Origin": ORIGIN})
        assert r.status_code == 403
        local = httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))
        async with httpx.AsyncClient(transport=local, base_url=ORIGIN) as lc:
            r = await lc.get("/app/health")
            assert r.json()["detail"][0]["user"] == 2
            r = await lc.post("/app/admin/drain", headers={"Origin": ORIGIN})
            assert r.status_code == 200 and r.json()["ok"] is True
        assert launcher.spawned[0].drained == 1
    run(app, scenario)
