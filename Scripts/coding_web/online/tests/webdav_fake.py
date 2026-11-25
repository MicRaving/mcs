"""A tiny in-process WebDAV server (stdlib only) for the Nextcloud-mode tests.

Why not only WsgiDAV: it evaluates ``If-Match``/``If-None-Match`` correctly, but
the check and the write are **not atomic** with each other (``do_PUT`` calls
``_evaluate_if_headers``, then ``create_empty_resource``; two concurrent creates
both see "no resource" and both succeed).  Atomic conditional create is the one
semantic the Nextcloud lock protocol stands on, so the tests need a server that
holds one lock around check-and-write.  :mod:`wsgidav_server` is kept as the
compatibility cross-check: the same scenarios run against it, and the lock-race
assertion is relaxed there (which is itself the finding).

Semantics implemented (all the backend relies on):

=============================  ===========================================
``PROPFIND`` depth 0/1/∞      207 multistatus, ``getetag``/``getcontentlength``/
                              ``getlastmodified``/``resourcetype``
``GET`` with ``Range``        bytes=start-end -> 206 + Content-Range
``PUT``                       201 create / 204 replace; ``If-None-Match: *``
                              and ``If-Match`` are checked + written **atomically**
``DELETE``                    ``If-Match`` honoured
``MKCOL``                     409 when the parent is missing
``MOVE``                      Destination header, Overwrite T/F
=============================  ===========================================

Optional knobs (the spike needs them against a real server; the tests use the
first two): ``refuse_infinity`` -> 403 like a locked-down university share,
``rate_limit_every`` -> every Nth request answers 429 + ``Retry-After``.

The server also records ``stats`` (method and per-path counters), so a test can
assert "only the changed file was downloaded".
"""

from __future__ import annotations

import hashlib
import os
import shutil
import threading
import urllib.parse
import xml.sax.saxutils as saxutils
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DAV_ERROR = (b'<?xml version="1.0" encoding="utf-8"?>'
             b'<D:error xmlns:D="DAV:"><D:propfind-finite-depth/></D:error>')


def _etag_of(path: str) -> str | None:
    try:
        if os.path.isdir(path):
            h = hashlib.md5()
            for name in sorted(os.listdir(path)):
                h.update(name.encode("utf-8", "replace"))
                child = os.path.join(path, name)
                h.update((_etag_of(child) or "").encode())
            return '"' + h.hexdigest() + '"'
        with open(path, "rb") as fh:
            h = hashlib.md5()
            while True:
                chunk = fh.read(1 << 20)
                if not chunk:
                    break
                h.update(chunk)
        return '"' + h.hexdigest() + '"'
    except OSError:
        return None


def _normalise_etag(value: str) -> str:
    v = (value or "").strip()
    if v[:2].upper() == "W/":
        v = v[2:].strip()
    if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        v = v[1:-1]
    return v


class _Precondition(Exception):
    pass


class FakeState:
    def __init__(self, root: str, *, refuse_infinity: bool = False,
                 rate_limit_every: int = 0, username_required: bool = False,
                 latency: tuple | None = None, fast_etags: bool = False,
                 bandwidth: float | None = None):
        self.root = os.path.abspath(root)
        #: bytes/s per GET response (None: unlimited)
        self.bandwidth = float(bandwidth) if bandwidth else None
        #: ``(lo, hi)`` seconds slept per request (a realistic remote share)
        self.latency = tuple(latency) if latency else None
        #: Apache/Nextcloud-like ETags from (inode, mtime, size) instead of the
        #: content, and folder ETags cached per write generation: a 25k-file
        #: share answers a PROPFIND without reading 25k files (benchmark)
        self.fast_etags = bool(fast_etags)
        self.gen = 0
        self._etag_cache: dict = {}
        self.refuse_infinity = bool(refuse_infinity)
        self.rate_limit_every = int(rate_limit_every or 0)
        self.username_required = bool(username_required)
        self.lock = threading.RLock()
        self.count = 0
        self.methods: dict[str, int] = {}
        self.gets: dict[str, int] = {}
        self.requests: list[tuple[str, str, str]] = []      # (user, method, path)
        self.throttled = 0
        os.makedirs(self.root, exist_ok=True)

    # -- helpers ----------------------------------------------------------
    def full(self, rel: str) -> str:
        rel = (rel or "").strip("/")
        path = os.path.abspath(os.path.join(self.root, *[p for p in rel.split("/") if p]))
        if not (path == self.root or path.startswith(self.root + os.sep)):
            raise PermissionError("outside the share")
        return path

    def rel(self, path: str) -> str:
        r = os.path.relpath(path, self.root).replace("\\", "/")
        return "" if r == "." else r

    def bump(self) -> None:
        """A write happened (the fake's own, or a test writing to the disk)."""
        with self.lock:
            self.gen += 1

    def etag(self, path: str) -> str | None:
        if not self.fast_etags:
            return _etag_of(path)
        try:
            st = os.stat(path)
        except OSError:
            return None
        import stat as _stat
        if _stat.S_ISDIR(st.st_mode):
            return self._dir_etag(path)
        return self._file_etag(self.rel(path), st)

    @staticmethod
    def _file_etag(rel: str, st) -> str:
        return '"' + hashlib.md5(f"{rel}-{st.st_mtime_ns}-{st.st_size}".encode()).hexdigest() + '"'

    def _dir_etag(self, path: str) -> str:
        key = ("d", path, self.gen)
        hit = self._etag_cache.get(key)
        if hit is not None:
            return hit
        h = hashlib.md5()
        try:
            with os.scandir(path) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError:
            entries = []
        for e in entries:
            h.update(e.name.encode("utf-8", "replace"))
            try:
                if e.is_dir():
                    h.update(self._dir_etag(e.path).encode())
                else:
                    h.update(self._file_etag(self.rel(e.path), e.stat()).encode())
            except OSError:
                continue
        val = '"' + h.hexdigest() + '"'
        self._etag_cache[key] = val
        return val


class _Handler(BaseHTTPRequestHandler):
    server_version = "WebDAVFake/1.0"
    protocol_version = "HTTP/1.1"

    # -- plumbing ---------------------------------------------------------
    def log_message(self, fmt, *args):                     # keep pytest quiet
        pass

    @property
    def state(self) -> FakeState:
        return self.server.state                        # type: ignore[attr-defined]

    def _user(self) -> str:
        raw = self.headers.get("Authorization") or ""
        if raw.lower().startswith("basic "):
            import base64
            try:
                return base64.b64decode(raw[6:]).decode("utf-8").split(":", 1)[0]
            except Exception:                            # noqa: BLE001
                return ""
        return ""

    def _begin(self) -> str | None:
        """Record the request; returns an early response code when throttling."""
        # read the body first, always: on a kept-alive connection an unread
        # PROPFIND body would be parsed as the next request line
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        self._req_body = self.rfile.read(length) if length else b""
        latency = self.state.latency
        if latency:
            import random
            import time as _time
            _time.sleep(random.uniform(*latency))
        user = self._user()
        path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
        target = path.strip("/")
        with self.state.lock:
            self.state.count += 1
            self.state.methods[self.command] = self.state.methods.get(self.command, 0) + 1
            self.state.requests.append((user, self.command, path))
            if self.command == "GET":
                self.state.gets[target] = self.state.gets.get(target, 0) + 1
            throttle = (self.state.rate_limit_every
                        and self.state.count % self.state.rate_limit_every == 0)
            if throttle:
                self.state.throttled += 1
        if self.state.username_required and not user:
            self._send(401, b"", {"WWW-Authenticate": 'Basic realm="fake"'})
            return None
        if throttle:
            self._send(429, b"slow down", {"Retry-After": "0"})
            return None
        return user

    def _send(self, status: int, body: bytes = b"", headers: dict | None = None) -> None:
        self.send_response(status)
        hdrs = {"Content-Length": str(len(body))}
        hdrs.update(headers or {})
        for key, value in hdrs.items():
            self.send_header(key, value)
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def _body(self) -> bytes:
        return getattr(self, "_req_body", b"")

    # -- conditionals -------------------------------------------------------
    def _check(self, full: str) -> None:
        """Evaluate If-Match / If-None-Match; call while holding state.lock."""
        inm = self.headers.get("If-None-Match")
        im = self.headers.get("If-Match")
        exists = os.path.exists(full)
        current = _normalise_etag(self.state.etag(full) or "")
        if inm is not None:
            if inm.strip() == "*":
                if exists:
                    raise _Precondition()
            elif exists:
                tags = {_normalise_etag(t) for t in inm.split(",")}
                if current in tags:
                    raise _Precondition()
        if im is not None:
            if not exists:
                raise _Precondition()
            if im.strip() != "*":
                tags = {_normalise_etag(t) for t in im.split(",")}
                if current not in tags:
                    raise _Precondition()

    # -- methods -----------------------------------------------------------
    def do_PROPFIND(self):                                  # noqa: N802
        if self._begin() is None:
            return
        depth = (self.headers.get("Depth") or "infinity").lower()
        full = None
        try:
            full = self.state.full(urllib.parse.unquote(urllib.parse.urlsplit(self.path).path))
        except PermissionError:
            return self._send(403, b"outside the share")
        if not os.path.exists(full):
            return self._send(404, b"not found")
        if depth == "infinity" and self.state.refuse_infinity:
            return self._send(403, DAV_ERROR, {"Content-Type": "application/xml; charset=utf-8"})
        import email.utils
        # (path, is_dir, stat) - scandir's cached stat keeps a 25k-entry
        # listing fast on Windows (a stat per entry cost ~1 ms there)
        rows = [(full, os.path.isdir(full), os.stat(full))]
        if depth in ("1", "infinity"):
            queue = [full]
            while queue:
                folder = queue.pop(0)
                try:
                    with os.scandir(folder) as it:
                        entries = sorted(it, key=lambda e: e.name)
                except OSError:
                    continue
                for e in entries:
                    try:
                        is_dir = e.is_dir()
                        st = e.stat()
                    except OSError:
                        continue
                    rows.append((e.path, is_dir, st))
                    if is_dir and depth == "infinity":
                        queue.append(e.path)
        parts = []
        for path, is_dir, st in rows:
            rel = self.state.rel(path)
            href = "/" + urllib.parse.quote(rel) if rel else "/"
            if self.state.fast_etags and not is_dir:
                etag = self.state._file_etag(rel, st)
            else:
                etag = self.state.etag(path) or ""
            size = 0 if is_dir else st.st_size
            mtime = st.st_mtime
            parts.append(
                "<D:response><D:href>{href}</D:href><D:propstat><D:prop>"
                "<D:resourcetype>{rt}</D:resourcetype>"
                "<D:getetag>{etag}</D:getetag>"
                "<D:getcontentlength>{size}</D:getcontentlength>"
                "<D:getlastmodified>{lm}</D:getlastmodified>"
                "</D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>".format(
                    href=saxutils.escape(href),
                    rt="<D:collection/>" if is_dir else "",
                    etag=saxutils.escape(etag),
                    size=size,
                    lm=email.utils.formatdate(mtime, usegmt=True)))
        body = ('<?xml version="1.0" encoding="utf-8"?>'
                '<D:multistatus xmlns:D="DAV:">' + "".join(parts) + "</D:multistatus>").encode()
        self._send(207, body, {"Content-Type": "application/xml; charset=utf-8"})

    def do_GET(self):                                       # noqa: N802
        if self._begin() is None:
            return
        try:
            full = self.state.full(urllib.parse.unquote(urllib.parse.urlsplit(self.path).path))
        except PermissionError:
            return self._send(403, b"outside the share")
        if not os.path.isfile(full):
            return self._send(404, b"not found")
        with open(full, "rb") as fh:
            data = fh.read()
        headers = {"Content-Type": "application/octet-stream",
                   "ETag": self.state.etag(full) or '""',
                   "Accept-Ranges": "bytes"}
        rng = (self.headers.get("Range") or "").strip()
        if rng.startswith("bytes=") and "-" in rng:
            try:
                start_s, end_s = rng[6:].split("-", 1)
                start = int(start_s or 0)
                end = int(end_s) if end_s else len(data) - 1
                end = min(end, len(data) - 1)
                chunk = data[start:end + 1]
                headers["Content-Range"] = f"bytes {start}-{end}/{len(data)}"
                return self._send(206, chunk, headers)
            except (TypeError, ValueError):
                pass
        if self.state.bandwidth:
            import time as _time
            _time.sleep(len(data) / self.state.bandwidth)
        self._send(200, data, headers)

    def do_HEAD(self):                                      # noqa: N802
        if self._begin() is None:
            return
        try:
            full = self.state.full(urllib.parse.unquote(urllib.parse.urlsplit(self.path).path))
        except PermissionError:
            return self._send(403, b"outside the share")
        if not os.path.exists(full):
            return self._send(404, b"not found")
        self._send(200, b"", {"ETag": self.state.etag(full) or '""'})

    def do_PUT(self):                                       # noqa: N802
        if self._begin() is None:
            return
        data = self._body()
        try:
            full = self.state.full(urllib.parse.unquote(urllib.parse.urlsplit(self.path).path))
        except PermissionError:
            return self._send(403, b"outside the share")
        parent = os.path.dirname(full)
        if not os.path.isdir(parent):
            return self._send(409, b"parent must be a collection")
        with self.state.lock:                               # check + write, atomically
            try:
                self._check(full)
            except _Precondition:
                return self._send(412, b"precondition failed")
            created = not os.path.exists(full)
            tmp = full + ".put-tmp"
            with open(tmp, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, full)
            self.state.gen += 1
        self._send(201 if created else 204, b"", {"ETag": self.state.etag(full) or '""'})

    def do_DELETE(self):                                    # noqa: N802
        if self._begin() is None:
            return
        try:
            full = self.state.full(urllib.parse.unquote(urllib.parse.urlsplit(self.path).path))
        except PermissionError:
            return self._send(403, b"outside the share")
        with self.state.lock:
            if not os.path.exists(full):
                return self._send(404, b"not found")
            try:
                self._check(full)
            except _Precondition:
                return self._send(412, b"precondition failed")
            self.state.gen += 1
            if os.path.isdir(full):
                shutil.rmtree(full, ignore_errors=True)
            else:
                try:
                    os.unlink(full)
                except OSError:
                    pass
        self._send(204, b"")

    def do_MKCOL(self):                                     # noqa: N802
        if self._begin() is None:
            return
        try:
            full = self.state.full(urllib.parse.unquote(urllib.parse.urlsplit(self.path).path))
        except PermissionError:
            return self._send(403, b"outside the share")
        if os.path.exists(full):
            return self._send(405, b"already exists")
        if not os.path.isdir(os.path.dirname(full)):
            return self._send(409, b"parent must be a collection")
        try:
            os.mkdir(full)
        except OSError:
            return self._send(409, b"cannot create")
        self.state.bump()
        self._send(201, b"")

    def do_MOVE(self):                                      # noqa: N802
        if self._begin() is None:
            return
        src = self.state.full(urllib.parse.unquote(urllib.parse.urlsplit(self.path).path))
        dest_url = urllib.parse.urlsplit(self.headers.get("Destination") or "")
        if not dest_url.path:
            return self._send(400, b"Destination required")
        dest = self.state.full(urllib.parse.unquote(dest_url.path))
        if not os.path.exists(src):
            return self._send(404, b"not found")
        if os.path.exists(dest) and (self.headers.get("Overwrite") or "T").upper() == "F":
            return self._send(412, b"destination exists")
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        os.replace(src, dest)
        self.state.bump()
        self._send(201, b"")


class WebDAVFake:
    """A WebDAV share served from *root* on 127.0.0.1 (random port)."""

    def __init__(self, root: str, *, refuse_infinity: bool = False,
                 rate_limit_every: int = 0, username_required: bool = False,
                 latency: tuple | None = None, fast_etags: bool = False,
                 bandwidth: float | None = None):
        self.state = FakeState(root, refuse_infinity=refuse_infinity,
                               rate_limit_every=rate_limit_every,
                               username_required=username_required,
                               latency=latency, fast_etags=fast_etags, bandwidth=bandwidth)
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._httpd.daemon_threads = True
        self._httpd.state = self.state                     # type: ignore[attr-defined]
        self._thread: threading.Thread | None = None

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> str:
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        name="webdav-fake", daemon=True)
        self._thread.start()
        return self.url

    def stop(self) -> None:
        try:
            self._httpd.shutdown()
        except Exception:                                  # noqa: BLE001
            pass
        try:
            self._httpd.server_close()
        except Exception:                                  # noqa: BLE001
            pass
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    @property
    def url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def root(self) -> str:
        return self.state.root

    # -- assertions used by the tests/spike --------------------------------
    def resets(self) -> None:
        with self.state.lock:
            self.state.count = 0
            self.state.methods.clear()
            self.state.gets.clear()
            self.state.requests.clear()
            self.state.throttled = 0
