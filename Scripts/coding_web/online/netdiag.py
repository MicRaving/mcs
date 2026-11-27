"""Step-by-step reachability check for a WebDAV root (the join/connect dialog).

"the project cannot be reached right now" is the honest answer when nothing is
known, and a useless one when the cause is a certificate, a proxy or a slow
listing.  Each step reports its own outcome and timing: the CA bundle in use,
name resolution, TCP, TLS (with the peer certificate), an authenticated
``PROPFIND Depth: 0`` and a ``Depth: 1`` listing.

Nothing here writes to the share.  A failure in one step does not stop the
others that can still run (DNS may fail, TLS cannot; a 401 still proves the
server answered).
"""

from __future__ import annotations

import socket
import ssl
import time
import urllib.parse

from . import tls
from .errors import OfflineError
from .webdav import WebDAV

STEP_TIMEOUT = 15.0


def _step(name: str, ok: bool, **detail) -> dict:
    return {"step": name, "ok": bool(ok), **detail}


def run(url: str, username: str | None = None, password: str | None = None) -> dict:
    """Check *url*; returns ``{"ok", "url", "host", "tls", "steps", "hint"}``."""
    raw = str(url or "").strip()
    parts = urllib.parse.urlsplit(raw if "://" in raw else "https://" + raw)
    https = (parts.scheme or "https").lower() == "https"
    host = parts.hostname or ""
    port = parts.port or (443 if https else 80)
    out: dict = {"ok": False, "url": raw, "host": host, "port": port,
                 "tls": tls.describe(), "steps": []}
    if not host:
        out["steps"].append(_step("address", False, error="the URL has no host name"))
        out["hint"] = "Ask for a new invitation - the address in this one is incomplete."
        return out
    out["steps"].append(_step("address", True, detail=f"{parts.scheme}://{host}:{port}"))

    started = time.perf_counter()
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        out["steps"].append(_step("dns", True, ms=round((time.perf_counter() - started) * 1000),
                                  addresses=sorted({i[4][0] for i in infos})[:6]))
    except OSError as exc:
        out["steps"].append(_step("dns", False, error=f"{type(exc).__name__}: {exc}"))
        out["hint"] = ("This computer cannot find the server's name. Check the internet "
                       "connection (VPN, campus network) and try again.")
        return out

    started = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=STEP_TIMEOUT):
            pass
        out["steps"].append(_step("tcp", True, ms=round((time.perf_counter() - started) * 1000)))
    except OSError as exc:
        out["steps"].append(_step("tcp", False, error=f"{type(exc).__name__}: {exc}"))
        out["hint"] = ("The server answered neither on port 443 nor otherwise: a firewall, a "
                       "proxy or the network blocks the connection.")
        return out

    if https:
        started = time.perf_counter()
        try:
            ctx = tls.context()
            with socket.create_connection((host, port), timeout=STEP_TIMEOUT) as sock:
                with ctx.wrap_socket(sock, server_hostname=host) as tls_sock:
                    cert = tls_sock.getpeercert() or {}
                    out["steps"].append(_step(
                        "tls", True, ms=round((time.perf_counter() - started) * 1000),
                        protocol=tls_sock.version(),
                        issuer=", ".join(v for part in cert.get("issuer", ()) for _, v in part),
                        not_after=cert.get("notAfter")))
        except ssl.SSLCertVerificationError as exc:
            out["steps"].append(_step("tls", False, error=f"{type(exc).__name__}: {exc}",
                                      verify_message=getattr(exc, "verify_message", "")))
            out["hint"] = ("The certificate of the server cannot be checked on this computer "
                           f"(the app uses {tls.describe().get('ca_file')}). A VPN or a "
                           "security program that inspects connections is the usual cause.")
            return out
        except (ssl.SSLError, OSError) as exc:
            out["steps"].append(_step("tls", False, error=f"{type(exc).__name__}: {exc}"))
            out["hint"] = "The encrypted connection could not be set up."
            return out

    dav = WebDAV(raw, username, password, timeout=STEP_TIMEOUT,
                 allow_insecure_http=not https, retries=0)
    try:
        started = time.perf_counter()
        try:
            dav.propfind("", depth="0")
            out["steps"].append(_step("webdav", True,
                                      ms=round((time.perf_counter() - started) * 1000)))
        except OfflineError as exc:
            out["steps"].append(_step("webdav", False, error=str(exc)))
            out["hint"] = "The server could not be reached after the connection was established."
            return out
        except Exception as exc:            # noqa: BLE001 - 401/404 are answers too
            out["steps"].append(_step("webdav", False, status=getattr(exc, "status", 0),
                                      error=f"{type(exc).__name__}: {exc}"))
            out["hint"] = ("The server answered, but refused this address. With an invitation "
                           "that usually means the share link was deleted - ask for a new one.")
            return out
        started = time.perf_counter()
        try:
            rows = dav.propfind("", depth="1")
            files = [p for p in rows if not p.is_dir]
            out["steps"].append(_step("listing", True, entries=len(rows), files=len(files),
                                      ms=round((time.perf_counter() - started) * 1000)))
        except Exception as exc:            # noqa: BLE001
            out["steps"].append(_step("listing", False, error=f"{type(exc).__name__}: {exc}"))
    finally:
        dav.close()
    out["ok"] = all(s["ok"] for s in out["steps"])
    return out
