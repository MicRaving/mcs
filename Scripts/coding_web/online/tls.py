"""One HTTPS context for every outbound request (the updater and WebDAV modes).

Why this exists: the app is a frozen bundle, and on macOS Python's OpenSSL
looks for its trust store at the *building* machine's path (a python.org
install).  On a Mac without that Python every HTTPS request then fails with
``CERTIFICATE_VERIFY_FAILED`` - the in-app update ("could not reach GitHub")
and joining an online project ("the project cannot be reached") at the same
time, while browsers work.  The bundle therefore ships its own CA file
(``certifi``) and this module hands one cached context to both paths.

Order: ``CODINGSTUDIO_CA_FILE`` (support/testing) -> the bundled/installed
``certifi`` file -> the platform default store.  A context is only cached once
it actually holds certificates, so a broken bundle keeps using the default.
"""

from __future__ import annotations

import os
import ssl
import threading

_LOCK = threading.Lock()
_CONTEXT: ssl.SSLContext | None = None
SOURCE = ""


def ca_file() -> str | None:
    """The CA file to trust, or None for the platform default store."""
    override = (os.environ.get("CODINGSTUDIO_CA_FILE") or "").strip()
    if override and os.path.isfile(override):
        return override
    try:
        import certifi                      # noqa: PLC0415 - optional, bundled

        path = certifi.where()
        if path and os.path.isfile(path):
            return path
    except Exception:                       # noqa: BLE001 - not installed
        pass
    # macOS keeps a copy here even without python.org's installer
    for candidate in ("/etc/ssl/cert.pem", "/usr/local/etc/openssl/cert.pem"):
        if os.path.isfile(candidate) and os.path.getsize(candidate) > 0:
            return candidate
    return None


def context() -> ssl.SSLContext:
    """The shared client context (verification on, host names checked)."""
    global _CONTEXT, SOURCE
    with _LOCK:
        if _CONTEXT is not None:
            return _CONTEXT
        path = ca_file()
        ctx = None
        if path:
            try:
                ctx = ssl.create_default_context(cafile=path)
                SOURCE = path
            except Exception:               # noqa: BLE001 - unreadable bundle
                ctx = None
        if ctx is None:
            ctx = ssl.create_default_context()
            SOURCE = "system"
        try:
            if not ctx.get_ca_certs():      # an empty store verifies nothing
                ctx.load_default_certs()
        except Exception:                   # noqa: BLE001
            pass
        _CONTEXT = ctx
        return ctx


def describe() -> dict:
    """What the diagnostics page shows: the CA file in use and its size."""
    path = ca_file()
    try:
        count = len(context().get_ca_certs())
    except Exception:                       # noqa: BLE001
        count = 0
    size = os.path.getsize(path) if path and os.path.isfile(path) else 0
    return {"ca_file": SOURCE or path or "system", "ca_bytes": size,
            "ca_count": count, "openssl": ssl.OPENSSL_VERSION}
