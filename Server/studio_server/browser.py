"""Browser mode: the switch, the "switched off" page and the gateway probe.

The sync server and the browser gateway are one product (:mod:`studio_server.run`):
``STUDIO_BROWSER_MODE=1`` starts the gateway as a supervised child, ``0`` (the
default) does not, and then the server itself answers ``/app/`` with a small
page that says so.  The reverse proxy therefore points ``/app`` at the browser
port either way (see ``Server/deploy/caddy-browser-mode.txt``).

This module is deliberately **stdlib-only**: :mod:`studio_server.app` imports it
without pulling in any browser-mode module (``studio_web`` and the worker stack
are never imported in the sync-server process).
"""

from __future__ import annotations

import http.client
import json
import os
import threading
import time

DEFAULT_BROWSER_PORT = 8780
_TRUE = ("1", "true", "yes", "on")

#: /app/health (and therefore the health probe) is cached for this long
STATUS_TTL = 2.0
_probe_lock = threading.Lock()
_probe_cache: dict[str, object] = {"at": 0.0, "url": "", "value": None}


def browser_mode_enabled(env=None) -> bool:
    """``STUDIO_BROWSER_MODE``: ``1`` starts the browser stack, ``0`` does not."""
    env = os.environ if env is None else env
    return str(env.get("STUDIO_BROWSER_MODE", "0")).strip().lower() in _TRUE


def browser_port(env=None) -> int:
    env = os.environ if env is None else env
    try:
        return int(str(env.get("STUDIO_BROWSER_PORT", DEFAULT_BROWSER_PORT)).strip())
    except (TypeError, ValueError):
        return DEFAULT_BROWSER_PORT


def gateway_health_url(port: int | None = None) -> str:
    return f"http://127.0.0.1:{browser_port() if port is None else int(port)}/app/health"


def probe_gateway(url: str | None = None, timeout: float = 0.8) -> dict:
    """The gateway's ``/app/health`` answer, or ``{ok: False, ...}`` (never raises).

    Cached for :data:`STATUS_TTL` seconds so a health poll from the reverse
    proxy cannot turn into a request per probe.  Called from FastAPI's sync
    endpoint threadpool, so a slow/unreachable gateway cannot block the loop.
    """
    url = url or gateway_health_url()
    now = time.monotonic()
    with _probe_lock:
        if _probe_cache["url"] == url and now - float(_probe_cache["at"]) < STATUS_TTL:
            return dict(_probe_cache["value"] or {})  # type: ignore[arg-type]
    value: dict
    try:
        parts = url.split("/", 3)             # http://127.0.0.1:8780/app/health
        conn = http.client.HTTPConnection(parts[2], timeout=timeout)
        try:
            conn.request("GET", "/" + (parts[3] if len(parts) > 3 else ""))
            resp = conn.getresponse()
            body = resp.read()
            value = json.loads(body or b"{}") if resp.status == 200 else {
                "ok": False, "status": resp.status}
        finally:
            conn.close()
        if not isinstance(value, dict):
            value = {"ok": False, "error": "unexpected gateway answer"}
    except Exception as exc:  # noqa: BLE001 - a health probe must never raise
        value = {"ok": False, "error": type(exc).__name__}
    with _probe_lock:
        _probe_cache.update({"at": time.monotonic(), "url": url, "value": dict(value)})
    return value


def health_summary(port: int | None = None, mode: bool | None = None) -> dict:
    """The browser part of ``/api/health`` and ``/app/health`` on the sync port."""
    on = browser_mode_enabled() if mode is None else bool(mode)
    if not on:
        return {"browser_mode": "off"}
    gw = probe_gateway(gateway_health_url(port))
    out = {"browser_mode": "on", "workers": gw.get("workers", 0),
           "rss_mb": gw.get("rss_mb", 0), "gateway_ok": bool(gw.get("ok"))}
    lane = gw.get("lane")
    if lane:
        out["lane"] = lane
    if not out["gateway_ok"]:
        out["gateway_error"] = gw.get("error") or f"status {gw.get('status', '?')}"
    return out


_STYLE = """
:root{color-scheme:light dark;--fg:#1d2330;--muted:#677085;--bg:#f5f6f8;--card:#fff;--accent:#3559d6}
@media (prefers-color-scheme:dark){:root{--fg:#e7eaf0;--muted:#98a1b3;--bg:#15181f;--card:#1e222b;--accent:#7f9bff}}
*{box-sizing:border-box}body{margin:0;font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;
background:var(--bg);color:var(--fg);display:grid;place-items:center;min-height:100vh}
main{background:var(--card);padding:28px 32px;border-radius:12px;width:min(520px,92vw);
box-shadow:0 2px 16px rgba(0,0,0,.08)}h1{font-size:1.25rem;margin:0 0 10px}
p{color:var(--muted);margin:0 0 12px}code{font:inherit;background:rgba(127,127,127,.12);padding:1px 5px;border-radius:5px}
"""


def switched_off_page() -> str:
    """The page ``/app/`` answers when this server runs without browser mode."""
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>CodingStudio — browser mode is switched off</title>"
        f"<style>{_STYLE}</style></head><body><main>"
        "<h1>Browser mode is switched off on this server</h1>"
        "<p>This CodingStudio server stores and syncs projects, but it does not run "
        "the browser app.</p>"
        "<p>Use the CodingStudio desktop app and connect it to this server "
        "(<em>Online project</em>).</p>"
        "<p>The person who runs the server can enable browser mode with "
        "<code>STUDIO_BROWSER_MODE=1</code>.</p>"
        "</main></body></html>"
    )


def switched_off_text() -> str:
    return "Browser mode is switched off on this server. Use the CodingStudio desktop app."


def page_headers() -> dict[str, str]:
    return {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}


__all__ = [
    "DEFAULT_BROWSER_PORT",
    "browser_mode_enabled",
    "browser_port",
    "gateway_health_url",
    "health_summary",
    "page_headers",
    "probe_gateway",
    "switched_off_page",
    "switched_off_text",
]
