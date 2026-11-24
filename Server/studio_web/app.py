"""The gateway application (PLAN_online_only §4).

Routes::

    GET  /                      -> 302 /app/
    GET  /app/                  login page, or the project list / redirect
    POST /app/login             form or JSON {username, password}
    POST /app/logout
    GET  /app/health            {ok, workers, rss_mb, lane}; detail from loopback only
    POST /app/admin/drain       loopback only: push every worker's outbox (backup hook)
    ANY  /app/p/<pid>/<rest>    proxied to the user's worker as /<rest>

Every non-GET request must carry an ``Origin`` (or ``Referer``) of this site;
the session cookie is ``HttpOnly; SameSite=Strict; Path=/app``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import urllib.parse

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse

from . import WEB_NAME, WEB_VERSION
from .auth import AuthFailed, ServerAuth
from .config import WebConfig
from .heavy import classify
from .pages import login_page, preparing_page, projects_page
from .supervisor import SECRET_HEADER, Supervisor

log = logging.getLogger("studio_web")

HEAVY_HEADER = "X-Studio-Heavy"
COOKIE_MAX_AGE = 30 * 24 * 3600      # default when STUDIO_WEB_SESSION_DAYS is unset
_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer",
        "trailers", "transfer-encoding", "upgrade", "host", "content-length"}
_DROP_REQ = _HOP | {"cookie", "authorization", SECRET_HEADER.lower(), HEAVY_HEADER.lower()}
_DROP_RESP = (_HOP - {"content-length"}) | {"set-cookie", "server"}
_SAFE = {"GET", "HEAD", "OPTIONS"}
_BAD_RAW = ("%2e%2e", "%2f", "%5c", "%00", "\\")
_SEC_HEADERS = {"X-Content-Type-Options": "nosniff", "Referrer-Policy": "same-origin",
                "X-Frame-Options": "DENY"}


def _json_err(status: int, code: str, message: str = "", **extra) -> JSONResponse:
    body = {"error": code, "message": message or code}
    body.update(extra)
    return JSONResponse(body, status_code=status)


def _is_loopback(request: Request) -> bool:
    host = request.client.host if request.client else ""
    return host in ("127.0.0.1", "::1", "localhost") and "x-forwarded-for" not in request.headers


def _client_ip(request: Request) -> str | None:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff
    return request.client.host if request.client else None


def _wants_html(request: Request) -> bool:
    return request.method == "GET" and "text/html" in (request.headers.get("accept") or "")


def _path_is_safe(request: Request) -> bool:
    raw = request.scope.get("raw_path") or b""
    raw_s = raw.decode("latin-1").lower()
    if any(bad in raw_s for bad in _BAD_RAW):
        return False
    segs = request.url.path.split("/")
    return ".." not in segs and "." not in segs[1:]


def create_app(cfg: WebConfig | None = None, *, launcher=None, auth: ServerAuth | None = None) -> FastAPI:
    cfg = cfg or WebConfig.from_env()
    cfg.ensure_dirs()
    state: dict = {}

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        sup = Supervisor(cfg, launcher=launcher)
        state["sup"] = sup
        state["auth"] = auth or ServerAuth(cfg.server, cfg.auth_ttl)
        state["http"] = httpx.AsyncClient(timeout=httpx.Timeout(None, connect=10.0))
        sup.start()
        try:
            yield
        finally:
            await sup.close()
            await state["http"].aclose()
            if auth is None:
                await state["auth"].close()

    app = FastAPI(title=WEB_NAME, version=WEB_VERSION, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.cfg = cfg
    app.state.web = state

    # ── CSRF: same-origin writes only ────────────────────────────────────
    @app.middleware("http")
    async def same_origin(request: Request, call_next):
        if request.method not in _SAFE:
            origin = request.headers.get("origin") or request.headers.get("referer") or ""
            expected = cfg.public_origin
            if expected:
                ok = origin == expected or origin.startswith(expected + "/")
            else:
                host = request.headers.get("host") or ""
                netloc = urllib.parse.urlsplit(origin).netloc if origin else ""
                ok = bool(netloc) and netloc == host
            if not ok:
                return _json_err(403, "forbidden", "cross-site request refused")
        response = await call_next(request)
        for k, v in _SEC_HEADERS.items():
            response.headers.setdefault(k, v)
        return response

    # ── session helpers ──────────────────────────────────────────────────
    def _token(request: Request) -> str:
        return request.cookies.get(cfg.cookie_name) or ""

    def _set_cookie(resp: Response, token: str) -> None:
        max_age = int(cfg.session_days * 86400) if cfg.session_days else COOKIE_MAX_AGE
        resp.set_cookie(cfg.cookie_name, token, max_age=max_age, path="/app",
                        httponly=True, secure=cfg.cookie_secure, samesite="strict")

    def _clear_cookie(resp: Response) -> None:
        resp.delete_cookie(cfg.cookie_name, path="/app", httponly=True,
                           secure=cfg.cookie_secure, samesite="strict")

    async def _session(request: Request) -> tuple[str, dict] | None:
        token = _token(request)
        if not token:
            return None
        try:
            return token, await state["auth"].me(token)
        except AuthFailed:
            return None

    def _unauth(request: Request) -> Response:
        if _wants_html(request):
            resp: Response = RedirectResponse("/app/", status_code=302)
        else:
            resp = _json_err(401, "unauthorized", "please sign in again")
        _clear_cookie(resp)
        return resp

    # ── pages ────────────────────────────────────────────────────────────
    @app.get("/")
    async def root():
        return RedirectResponse("/app/", status_code=302)

    @app.get("/app")
    async def app_noslash():
        return RedirectResponse("/app/", status_code=302)

    @app.get("/app/")
    async def home(request: Request):
        sess = await _session(request)
        if sess is None:
            resp = HTMLResponse(login_page())
            if _token(request):
                _clear_cookie(resp)
            return resp
        _token_, me = sess
        projects = list(me.get("projects") or [])
        if len(projects) == 1:
            return RedirectResponse(f"/app/p/{urllib.parse.quote(str(projects[0]['id']))}/", status_code=302)
        return HTMLResponse(projects_page(me.get("user") or {}, projects))

    @app.get("/app/login")
    async def login_get():
        return HTMLResponse(login_page())

    @app.post("/app/login")
    async def login_post(request: Request):
        raw = await request.body()
        ctype = request.headers.get("content-type") or ""
        is_json = "json" in ctype
        if is_json:
            try:
                data = json.loads(raw or b"{}")
            except ValueError:
                data = {}
        else:
            data = {k: v[0] for k, v in urllib.parse.parse_qs(raw.decode("utf-8", "replace")).items()}
        username = str(data.get("username") or "").strip()
        password = str(data.get("password") or "")
        try:
            res = await state["auth"].login(username, password, _client_ip(request))
        except AuthFailed as exc:
            msg = {401: "Wrong username or password.",
                   429: "Too many failed attempts - wait a few minutes."}.get(exc.status, exc.message)
            if is_json:
                return _json_err(exc.status if exc.status in (401, 429) else 502, exc.code, msg)
            return HTMLResponse(login_page(msg, username), status_code=exc.status if exc.status in (401, 429) else 502)
        except httpx.HTTPError:
            msg = "The project server is not reachable right now."
            return _json_err(503, "offline", msg) if is_json else HTMLResponse(login_page(msg, username), 503)
        token = res["token"]
        if is_json:
            me = await state["auth"].me(token)
            resp: Response = JSONResponse({"ok": True, "user": res.get("user"),
                                           "projects": me.get("projects") or []})
        else:
            resp = RedirectResponse("/app/", status_code=303)
        _set_cookie(resp, token)
        return resp

    @app.post("/app/logout")
    async def logout(request: Request):
        token = _token(request)
        if token:
            try:
                me = await state["auth"].me(token)
                uid = (me.get("user") or {}).get("id")
                if uid is not None:
                    await state["sup"].stop_user(int(uid), "logout")
            except AuthFailed:
                pass
            await state["auth"].logout(token)
        resp: Response = (JSONResponse({"ok": True}) if "json" in (request.headers.get("accept") or "")
                          else RedirectResponse("/app/", status_code=303))
        _clear_cookie(resp)
        return resp

    @app.get("/app/health")
    async def health(request: Request):
        snap = state["sup"].snapshot(detail=_is_loopback(request))
        return {"ok": True, "version": WEB_VERSION, **snap}

    @app.post("/app/admin/drain")
    async def drain_all(request: Request):
        if not _is_loopback(request):
            return _json_err(403, "forbidden", "local only")
        sup = state["sup"]
        results = {}
        for w in list(sup.workers.values()):
            if w.alive():
                results[f"{w.user_id}/{w.project_id}"] = await sup.drain(w)
        return {"ok": all(r.get("ok") for r in results.values()), "workers": results}

    # ── the app proxy ────────────────────────────────────────────────────
    @app.get("/app/p/{pid}")
    async def project_noslash(pid: str):
        return RedirectResponse(f"/app/p/{urllib.parse.quote(pid)}/", status_code=302)

    @app.api_route("/app/p/{pid}/{rest:path}",
                   methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"])
    async def proxy(pid: str, rest: str, request: Request):
        if not _path_is_safe(request):
            return _json_err(400, "bad_request", "invalid path")
        sess = await _session(request)
        if sess is None:
            return _unauth(request)
        token, me = sess
        project = next((p for p in (me.get("projects") or []) if str(p.get("id")) == pid), None)
        if project is None:
            return _json_err(404, "not_found", "no such project")
        user_id = int((me.get("user") or {}).get("id"))
        sup: Supervisor = state["sup"]
        w = sup.workers.get((user_id, pid))
        if (w is None or not w.alive()) and _wants_html(request) and rest in ("", "index.html"):
            # navigation: answer at once, start the worker in the background
            if not sup.is_starting(user_id, pid):
                task = asyncio.create_task(sup.get(user_id, pid, token))
                task.add_done_callback(lambda t: t.exception())
            return HTMLResponse(preparing_page(str(project.get("name") or "")))
        try:
            w = await sup.get(user_id, pid, token)
        except Exception as exc:  # noqa: BLE001 - SpawnError, OSError
            log.warning("worker start failed: %s", exc)
            return _json_err(503, "worker_unavailable", "your workspace could not be started")

        path = "/" + rest
        heavy, family = classify(request.method, path)
        # forward the path exactly as the browser encoded it (keys hold spaces,
        # parentheses ...): everything after "/app/p/<pid>"
        raw = (request.scope.get("raw_path") or b"").decode("latin-1")
        parts = raw.split("/", 4)          # ["", "app", "p", "<pid>", "<rest>"]
        raw_rest = "/" + (parts[4] if len(parts) > 4 else "")
        use_lane = heavy and (family is None or family not in w.warm)
        body = await request.body()
        headers = {k: v for k, v in request.headers.items() if k.lower() not in _DROP_REQ}
        headers[SECRET_HEADER] = w.secret
        if heavy:
            headers[HEAVY_HEADER] = "1"
        url = w.base_url + raw_rest
        if request.url.query:
            url += "?" + request.url.query

        w.inflight += 1
        w.last_used = time.monotonic()
        acquired = False
        queued = 0.0
        resp = None
        try:
            if use_lane:
                sup.lane_waiting += 1
                t0 = time.monotonic()
                try:
                    await sup.lane.acquire()
                    acquired = True
                finally:
                    sup.lane_waiting -= 1
                queued = time.monotonic() - t0
            req = state["http"].build_request(request.method, url, headers=headers, content=body)
            resp = await state["http"].send(req, stream=True)
        except Exception as exc:  # noqa: BLE001 - worker died / restarted
            w.inflight -= 1
            if acquired:
                sup.lane.release()
            log.info("proxy to worker failed: %r", exc)
            return _json_err(503, "worker_restarted",
                             "your workspace was restarted; please retry")

        out_headers = {k: v for k, v in resp.headers.items() if k.lower() not in _DROP_RESP}
        if use_lane:
            out_headers["X-Studio-Queued"] = f"{queued:.1f}"
        status = resp.status_code

        async def body_iter():
            try:
                async for chunk in resp.aiter_raw():
                    yield chunk
            finally:
                await resp.aclose()
                w.inflight -= 1
                w.last_used = time.monotonic()
                if acquired:
                    sup.lane.release()
                if family and status < 500:
                    w.warm.add(family)
                if heavy:
                    asyncio.create_task(sup.enforce())

        return StreamingResponse(body_iter(), status_code=status, headers=out_headers)

    return app


__all__ = ["create_app"]
