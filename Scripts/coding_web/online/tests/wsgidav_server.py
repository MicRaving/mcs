"""WsgiDAV + Cheroot test server - the *compatibility* cross-check.

The Nextcloud-mode tests run their scenarios against :mod:`webdav_fake` (which
guarantees atomic conditional writes) **and**, when this optional dependency is
installed, against a real WebDAV implementation.  The test suite skips the
second parameter cleanly when it is missing::

    python -m venv temp/nextcloud/venv
    temp/nextcloud/venv/Scripts/python -m pip install wsgidav cheroot

WsgiDAV is imported from that venv's ``site-packages`` (pure Python, same
interpreter version); nothing is installed into the global interpreter.
"""

from __future__ import annotations

import os
import sys
import threading

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", "..", "..", ".."))
_VENV_SITE = os.path.join(_REPO, "temp", "nextcloud", "venv", "Lib", "site-packages")


def site_packages() -> str | None:
    return _VENV_SITE if os.path.isdir(_VENV_SITE) else None


def available() -> bool:
    """True when ``wsgidav`` and ``cheroot`` can be imported."""
    site = site_packages()
    if site and site not in sys.path:
        sys.path.append(site)
    try:
        import cheroot.wsgi  # noqa: F401
        import wsgidav.wsgidav_app  # noqa: F401
    except Exception:                       # noqa: BLE001 - any import failure means "no"
        return False
    return True


class _RefuseInfinity:
    """WSGI middleware: answer ``PROPFIND Depth: infinity`` with 403 (like a
    locked-down share), so the client's Depth-1 walk is exercised."""

    BODY = (b'<?xml version="1.0" encoding="utf-8"?>'
            b'<D:error xmlns:D="DAV:"><D:propfind-finite-depth/></D:error>')

    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        if (environ.get("REQUEST_METHOD") or "").upper() == "PROPFIND" \
                and (environ.get("HTTP_DEPTH") or "").lower() == "infinity":
            start_response("403 Forbidden",
                           [("Content-Type", "application/xml; charset=utf-8"),
                            ("Content-Length", str(len(self.BODY)))])
            return [self.BODY]
        return self.app(environ, start_response)


class WsgiDAVServer:
    def __init__(self, root: str, *, refuse_infinity: bool = False, numthreads: int = 10):
        if not available():
            raise RuntimeError("wsgidav/cheroot are not available")
        import cheroot.wsgi
        from wsgidav.wsgidav_app import WsgiDAVApp

        os.makedirs(root, exist_ok=True)
        config = {
            "provider_mapping": {"/": root},
            "http_authenticator": {"domain_controller": None, "accept_basic": True,
                                   "accept_digest": False, "default_to_digest": False},
            "simple_dc": {"user_mapping": {"*": True}},
            "dir_browser": {"enable": False},
            "logging": {"enable": False},
            "verbose": 1,
            "hotfixes": {"emulate_win32_lastmod": False, "re_encode_path_info": True},
        }
        app = WsgiDAVApp(config)
        if refuse_infinity:
            app = _RefuseInfinity(app)
        self._server = cheroot.wsgi.Server(("127.0.0.1", 0), app, numthreads=int(numthreads))
        self._server.prepare()
        self._thread: threading.Thread | None = None

    def start(self) -> str:
        self._thread = threading.Thread(target=self._server.serve, name="wsgidav", daemon=True)
        self._thread.start()
        return self.url

    def stop(self) -> None:
        try:
            self._server.stop()
        except Exception:                   # noqa: BLE001
            pass
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    @property
    def url(self) -> str:
        host, port = self._server.bind_addr[:2]
        return f"http://{host}:{port}"
