"""Minimal WebDAV client (stdlib only) for the "Nextcloud without server" mode.

Why not ``requests``: the packaged app does not ship it (see ``client.py``),
and the six verbs this mode needs are plain HTTP with an XML response body.

The surface is deliberately small - everything :mod:`~.nextcloud` needs and
nothing else:

    propfind(path="", depth="0"|"1"|"infinity") -> [Prop]
    stat(path) -> Prop                 (raises NotFoundError)
    exists(path) -> bool
    get(path, dest=None, byte_range=None, headers=..., query=..., stream_to=...)
    put(path, data|file, if_match=..., if_none_match=True) -> WebDAVResponse
    delete(path, if_match=...)
    mkcol(path) / makedirs(path)
    move(src, dst, overwrite=True)

``GET`` on a *collection* is Nextcloud's folder-archive extension (``Accept:
application/zip`` / ``application/x-tar``, optional ``files`` filter) - see
:meth:`get`; :mod:`~.nextcloud` turns that into one request per batch of files
instead of one request per file.

Behaviour that matters for the backend:

* **Conditional requests.**  ``If-Match`` (a bare ETag is quoted for you) and
  ``If-None-Match: *`` are sent verbatim; a failing precondition surfaces as
  :class:`PreconditionFailed` so the caller can merge/retry.  Whether the
  server evaluates them atomically is exactly what ``nextcloud_spike.py``
  measures - this client never assumes it.
* **Errors** are the app's own exceptions (:mod:`.errors`), so
  ``routes_online`` maps them to HTTP without a second translation table:
  401/403/404/400/429 are themselves, 502/503/504 and connection trouble are
  :class:`OfflineError`, 412 is :class:`PreconditionFailed`, the rest is
  :class:`ServerError`.
* **Throttling.**  Requests in flight are gated (a condition variable) at
  :attr:`WebDAV.parallel_limit`: the client starts at
  :data:`DEFAULT_PARALLEL` and widens to :attr:`max_parallel` after
  :data:`PARALLEL_ADAPT_AFTER` unthrottled responses, and drops back to the
  start value on any 429/503.  429/503 are retried honouring ``Retry-After``
  with exponential backoff - a university Nextcloud limits requests and a sync
  loop must not turn that into an outage.
* **No plaintext credentials.**  ``http://`` is refused unless the host is a
  loopback address (the local test server) or the caller explicitly allows it.
* **Test seam.**  ``offline`` and ``hook(method, path, phase[, response])``
  play the role ``FakeTransport`` plays for the studio server: switch the
  network off, or die in the middle of a request.

Paths handed to this class are POSIX paths **relative to the WebDAV folder**
(``Codings/Smith (2020) - A.json``), never absolute URLs; the client owns the
base URL and the URL quoting.
"""

from __future__ import annotations

import base64
import http.client
import os
import shutil
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from .errors import (
    AuthError,
    BadRequestError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    OfflineError,
    RateLimitedError,
    ServerError,
)

DAV_NS = "{DAV:}"
DEFAULT_TIMEOUT = 30.0
#: Hard cap on requests in flight.  The client *starts* at
#: :data:`DEFAULT_PARALLEL` and widens to the cap while no 429/503 is seen
#: (:meth:`WebDAV._note_result`), so a throttling share never sees the burst it
#: did not ask for.
DEFAULT_PARALLEL = 4
MAX_PARALLEL_REQUESTS = 8
#: consecutive unthrottled requests before one more slot opens
PARALLEL_ADAPT_AFTER = 16
MAX_REDIRECTS = 3
_RETRY_STATUS = (429, 503)
_LOOPBACK = ("127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1")
_CHUNK = 1024 * 1024


def is_loopback(url_or_host: str) -> bool:
    """True for a local address (the only case where plain http is allowed)."""
    raw = str(url_or_host or "").strip()
    host = urllib.parse.urlsplit(raw if "://" in raw else "//" + raw).hostname or raw
    return host.lower().strip("[]") in _LOOPBACK


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------

class PreconditionFailed(ServerError):
    """412 - ``If-Match``/``If-None-Match`` did not hold (someone else wrote)."""

    def __init__(self, message: str = "precondition failed", payload=None):
        super().__init__(message, 412, "precondition_failed", payload)


class DepthInfinityRefused(ForbiddenError):
    """``PROPFIND Depth: infinity`` is disabled on this server (403/409/501)."""


# ---------------------------------------------------------------------------
# values
# ---------------------------------------------------------------------------

@dataclass
class Prop:
    """One WebDAV resource as reported by PROPFIND."""

    path: str                       # relative to the WebDAV folder, POSIX
    is_dir: bool = False
    etag: str | None = None         # bare (unquoted) entity tag
    size: int = 0
    last_modified: str | None = None
    content_type: str | None = None


@dataclass
class WebDAVResponse:
    status: int
    headers: dict = field(default_factory=dict)     # lower-case names
    body: bytes = b""

    @property
    def etag(self) -> str | None:
        return normalize_etag(self.headers.get("etag"))

    def json(self):
        import json
        return json.loads(self.body.decode("utf-8")) if self.body else None


def normalize_etag(value) -> str | None:
    """``W/"abc"`` / ``"abc"`` / ``abc`` -> ``abc`` (empty -> None)."""
    if value is None:
        return None
    v = str(value).strip()
    if v[:2].upper() == "W/":
        v = v[2:].strip()
    if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        v = v[1:-1]
    return v or None


def quote_etag(value: str) -> str:
    """Turn a bare or quoted ETag into an ``If-Match`` value."""
    v = str(value).strip()
    if v.startswith('"') and v.endswith('"'):
        return v
    return f'"{v}"'


def _quote_rel(rel: str) -> str:
    """URL-quote a POSIX relative path, segment by segment."""
    parts = [p for p in str(rel).replace("\\", "/").split("/") if p not in ("", ".")]
    if ".." in parts:
        raise ValueError(f"path escapes the WebDAV folder: {rel!r}")
    return "/".join(urllib.parse.quote(p, safe="") for p in parts)


# ---------------------------------------------------------------------------
# the client
# ---------------------------------------------------------------------------

class WebDAV:
    """One folder on a WebDAV server, addressed by *base_url*."""

    def __init__(self, base_url: str, username: str | None = None, password: str | None = None, *,
                 timeout: float = DEFAULT_TIMEOUT, allow_insecure_http: bool = False,
                 max_parallel: int = MAX_PARALLEL_REQUESTS, retries: int = 3,
                 max_retry_wait: float = 60.0, clock=time.time, sleep=time.sleep,
                 ssl_context=None):
        u = urllib.parse.urlsplit(base_url if "://" in base_url else "https://" + base_url)
        scheme = (u.scheme or "https").lower()
        host = (u.hostname or "").lower()
        if not host:
            raise ValueError(f"not a WebDAV URL: {base_url!r}")
        if scheme not in ("https", "http"):
            raise ValueError(f"unsupported scheme {scheme!r} (https only)")
        if scheme == "http" and host not in _LOOPBACK and not allow_insecure_http:
            raise ValueError("refusing plain http to a non-local host: Nextcloud mode requires https "
                             "(pass allow_insecure_http=True only for a local test server)")
        self.base_url = base_url.rstrip("/")
        self.scheme, self.host = scheme, host
        self.port = u.port or (443 if scheme == "https" else 80)
        self.base_path = u.path.rstrip("/")
        self.username = username
        self.timeout = timeout
        self.retries = max(0, int(retries))
        self.max_retry_wait = float(max_retry_wait)
        self.clock = clock
        self.sleep = sleep
        self.ssl_context = ssl_context
        self._auth = None
        if username is not None and password is not None:
            raw = f"{username}:{password}".encode("utf-8")
            self._auth = "Basic " + base64.b64encode(raw).decode("ascii")
        self.max_parallel = max(1, int(max_parallel))
        #: requests in flight right now / the gate that bounds them
        self._inflight = 0
        self._gate = threading.Condition()
        self._limit = max(1, min(DEFAULT_PARALLEL, self.max_parallel))
        self._ok_streak = 0
        self._lock = threading.Lock()
        self._pool: dict[tuple, list] = {}
        self._pool_max = self.max_parallel + 2
        self._closed = False
        #: the server ignores If-Match / If-None-Match (PowerFolder): check them
        #: client-side right before the write (see :meth:`_check_precondition`)
        self.emulate_conditionals = False
        self._counts: dict[str, int] = {}
        self.throttled: list[dict] = []          # [{status, wait, method, path}]
        # test seams (the role FakeTransport plays for the studio server)
        self.offline = False
        self.hook = None                          # hook(method, path, phase, response=None)

    def __repr__(self) -> str:                    # never leak the password
        return f"WebDAV({self.base_url!r}, user={self.username!r})"

    # -- URLs -----------------------------------------------------------------

    def url_for(self, path: str = "") -> str:
        rel = _quote_rel(path)
        base = self.base_path or ""
        tail = f"{base}/{rel}" if rel else base
        return f"{self.scheme}://{self.host}:{self.port}{tail or '/'}"

    def _request_target(self, path: str, query: dict | None = None) -> str:
        rel = _quote_rel(path)
        base = self.base_path or ""
        target = (f"{base}/{rel}" if rel else base) or "/"
        if query:
            pairs = {k: v for k, v in query.items() if v is not None}
            if pairs:
                # %20 for spaces (``_quote_rel`` quotes the path the same way);
                # ``files`` carries a JSON array, so no "+" anywhere.
                target = target + "?" + urllib.parse.urlencode(pairs, quote_via=urllib.parse.quote)
        return target

    # -- one request ------------------------------------------------------------

    def request(self, method: str, path: str = "", *, headers: dict | None = None,
                body=None, stream_to=None, timeout: float | None = None,
                query: dict | None = None) -> WebDAVResponse:
        """Send one request; returns every answer except a failed precondition.

        *body* may be ``bytes`` or a binary file object (streamed).  *stream_to*
        receives the response body instead of buffering it; when it exposes a
        ``start(status, headers)`` method that is called **before** the body is
        read, so a caller can refuse a response it did not ask for (the folder
        archive check does exactly that).  *query* is URL-encoded into the
        request target (Nextcloud's ``?accept=tar&files=[...]`` form).
        """
        if self.offline:
            raise OfflineError("nextcloud: the network is switched off")
        hdrs = {"Accept": "*/*", "User-Agent": "CodingStudio-nextcloud"}
        if self._auth:
            hdrs["Authorization"] = self._auth
        if headers:
            hdrs.update({k: v for k, v in headers.items() if v is not None})
        attempt = 0
        while True:
            if self.hook:
                self.hook(method, path, "before")
            resp = self._exchange(method, path, hdrs, body, stream_to, timeout, query)
            with self._lock:
                self._counts[method] = self._counts.get(method, 0) + 1
            self._note_result(resp)
            if resp.status in _RETRY_STATUS and attempt < self.retries:
                wait = self._retry_after(resp, attempt)
                with self._lock:
                    self.throttled.append({"status": resp.status, "wait": wait,
                                           "method": method, "path": path})
                if hasattr(body, "seek"):
                    try:
                        body.seek(0)
                    except (OSError, ValueError):
                        pass
                attempt += 1
                self.sleep(wait)
                continue
            if self.hook:
                self.hook(method, path, "after", resp)
            self._raise_for(resp, method, path)
            return resp

    def _exchange(self, method: str, path: str, headers: dict, body, stream_to,
                  timeout: float | None, query: dict | None = None):
        """One HTTP round, following redirects (Nextcloud bounces stale URLs)."""
        target = self._request_target(path, query)
        host, port, scheme = self.host, self.port, self.scheme
        for _hop in range(MAX_REDIRECTS + 1):
            self._enter()
            try:
                resp = self._send_once(scheme, host, port, method, target, headers, body,
                                       stream_to, timeout)
            finally:
                self._leave()
            if resp.status in (301, 302, 303, 307, 308):
                loc = resp.headers.get("location")
                if not loc:
                    return resp
                url = urllib.parse.urlsplit(urllib.parse.urljoin(
                    f"{scheme}://{host}:{port}{target}", loc))
                host, port, scheme = url.hostname or host, url.port or port, url.scheme or scheme
                target = url.path + (f"?{url.query}" if url.query else "")
                continue
            return resp
        raise ServerError(f"too many redirects for {path!r}", 310, "too_many_redirects")

    # -- the parallelism gate ------------------------------------------------------
    #
    # A semaphore cannot widen, and the owner's share throttled the 4-at-a-time
    # client rarely enough that 4 was never the ceiling: while no 429/503 is
    # seen the gate opens one slot per ``PARALLEL_ADAPT_AFTER`` answers (to
    # ``max_parallel``); one throttle closes it again immediately.

    @property
    def parallel_limit(self) -> int:
        """The number of requests currently allowed in flight."""
        with self._gate:
            return self._limit

    def _enter(self) -> None:
        with self._gate:
            while self._inflight >= self._limit:
                self._gate.wait(0.5)
            self._inflight += 1

    def _leave(self) -> None:
        with self._gate:
            self._inflight = max(0, self._inflight - 1)
            self._gate.notify()

    def _note_result(self, resp: WebDAVResponse) -> None:
        with self._gate:
            if resp.status in _RETRY_STATUS:
                self._limit = max(1, min(self._limit, DEFAULT_PARALLEL))
                self._ok_streak = 0
                self._gate.notify_all()
                return
            if not (200 <= resp.status < 400):
                return
            self._ok_streak += 1
            if self._ok_streak >= PARALLEL_ADAPT_AFTER and self._limit < self.max_parallel:
                self._limit += 1
                self._ok_streak = 0
                self._gate.notify_all()

    # -- keep-alive connections --------------------------------------------------
    #
    # A fresh TCP (+ TLS) connection per request made every request on a real
    # share cost a handshake: the owner's first sync spent ~0.4 s per file
    # (46 min for 6.9k core files, 2026-11-25).  Connections are now pooled per
    # (scheme, host, port) and reused; a reused connection that the server
    # closed while idle is retried once on a fresh one.

    def _take_conn(self, scheme, host, port, timeout):
        key = (scheme, host, port)
        with self._lock:
            pool = self._pool.get(key)
            if pool:
                return pool.pop(), True
        cls = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
        kwargs = {"timeout": timeout or self.timeout}
        if scheme == "https" and self.ssl_context is not None:
            kwargs["context"] = self.ssl_context
        return cls(host, port, **kwargs), False

    def _give_conn(self, key, conn) -> None:
        with self._lock:
            pool = self._pool.setdefault(key, [])
            if len(pool) < self._pool_max and not self._closed:
                pool.append(conn)
                return
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass

    def close(self) -> None:
        """Close every pooled connection (the client stays usable)."""
        with self._lock:
            pools, self._pool = self._pool, {}
        for conns in pools.values():
            for conn in conns:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass

    _STALE = (http.client.RemoteDisconnected, http.client.BadStatusLine,
              ConnectionResetError, ConnectionAbortedError, BrokenPipeError)

    def _send_once(self, scheme, host, port, method, target, headers, body, stream_to, timeout):
        key = (scheme, host, port)
        for attempt in (0, 1):
            conn, reused = self._take_conn(scheme, host, port, timeout)
            if timeout and conn.timeout != timeout:
                conn.timeout = timeout
                if conn.sock is not None:
                    try:
                        conn.sock.settimeout(timeout)
                    except OSError:
                        pass
            keep = False
            started = False
            try:
                conn.request(method, target, body=body, headers=dict(headers))
                raw = conn.getresponse()
                hdrs = {k.lower(): v for k, v in raw.getheaders()}
                if stream_to is not None and 200 <= raw.status < 300:
                    started = True
                    start = getattr(stream_to, "start", None)
                    if start is not None:
                        # refuse the body before a byte of it is copied (an old
                        # Nextcloud answers a collection GET with an HTML page)
                        start(raw.status, hdrs)
                    shutil.copyfileobj(raw, stream_to, _CHUNK)
                    data = b""
                else:
                    data = raw.read()
                keep = not raw.will_close
                return WebDAVResponse(raw.status, hdrs, data)
            except self._STALE as exc:
                if reused and attempt == 0 and not started:
                    # the server closed the idle connection: one fresh retry
                    if hasattr(body, "seek"):
                        try:
                            body.seek(0)
                        except (OSError, ValueError):
                            pass
                    try:
                        conn.close()
                    except Exception:  # noqa: BLE001
                        pass
                    continue
                raise OfflineError(f"nextcloud unreachable: {type(exc).__name__}: {exc}") from exc
            except (http.client.HTTPException, OSError, TimeoutError, ConnectionError) as exc:
                raise OfflineError(f"nextcloud unreachable: {type(exc).__name__}: {exc}") from exc
            finally:
                if keep:
                    self._give_conn(key, conn)
                else:
                    try:
                        conn.close()
                    except Exception:  # noqa: BLE001
                        pass
        raise OfflineError("nextcloud unreachable: the connection was closed twice")

    def _retry_after(self, resp: WebDAVResponse, attempt: int) -> float:
        raw = (resp.headers.get("retry-after") or "").strip()
        wait: float | None = None
        if raw:
            try:
                wait = float(raw)
            except ValueError:
                try:
                    from email.utils import parsedate_to_datetime
                    when = parsedate_to_datetime(raw)
                    wait = max(0.0, when.timestamp() - self.clock())
                except (TypeError, ValueError, OverflowError):
                    wait = None
        if wait is None:
            wait = min(2.0 ** attempt, 30.0)
        return max(0.0, min(wait, self.max_retry_wait))

    @staticmethod
    def _raise_for(resp: WebDAVResponse, method: str, path: str) -> None:
        status = resp.status
        if 200 <= status < 300:
            return
        detail = ""
        if resp.body:
            try:
                detail = resp.body.decode("utf-8", "replace")[:400]
            except Exception:  # noqa: BLE001
                detail = ""
        msg = f"{method} {path or '/'}: HTTP {status}" + (f" ({detail})" if detail else "")
        if status == 412:
            raise PreconditionFailed(msg)
        if status == 401:
            raise AuthError(msg, status, "unauthorized")
        if status == 403:
            raise ForbiddenError(msg, status, "forbidden")
        if status == 404:
            raise NotFoundError(msg, status, "not_found")
        if status == 400:
            raise BadRequestError(msg, status, "bad_request")
        if status == 409:
            raise ConflictError(msg, status, "conflict")
        if status == 429:
            raise RateLimitedError(msg, status, "rate_limited")
        if status in (502, 503, 504):
            raise OfflineError(msg)
        raise ServerError(msg, status, f"http_{status}")

    # -- PROPFIND ---------------------------------------------------------------

    _PROPFIND_BODY = (b'<?xml version="1.0" encoding="utf-8"?>'
                      b'<D:propfind xmlns:D="DAV:"><D:prop>'
                      b'<D:resourcetype/><D:getetag/><D:getcontentlength/>'
                      b'<D:getlastmodified/><D:getcontenttype/>'
                      b'</D:prop></D:propfind>')

    def propfind(self, path: str = "", depth: str = "0") -> list[Prop]:
        """List *path* (and, with depth 1/infinity, its descendants)."""
        depth = str(depth)
        if depth not in ("0", "1", "infinity"):
            raise ValueError(f"bad depth {depth!r}")
        headers = {"Depth": depth, "Content-Type": "application/xml; charset=utf-8"}
        try:
            resp = self.request("PROPFIND", path, headers=headers, body=self._PROPFIND_BODY)
        except (ForbiddenError, BadRequestError, ConflictError, ServerError) as exc:
            if depth == "infinity" and getattr(exc, "status", 0) in (400, 403, 409, 501):
                raise DepthInfinityRefused(str(exc), getattr(exc, "status", 403),
                                           "depth_infinity_refused") from exc
            raise
        return self.parse_multistatus(resp.body)

    def parse_multistatus(self, data: bytes) -> list[Prop]:
        """Decode a 207 body into :class:`Prop` rows (paths relative to the folder)."""
        if not data:
            return []
        try:
            root = ET.fromstring(data)
        except ET.ParseError:
            return []
        out: list[Prop] = []
        base = self.base_path or ""
        for node in root.findall(f"{DAV_NS}response"):
            href = (node.findtext(f"{DAV_NS}href") or "").strip()
            if not href:
                continue
            path = urllib.parse.urlsplit(href).path if "://" in href else href
            path = urllib.parse.unquote(path)
            if base and path == base:
                path = ""
            elif base and path.startswith(base + "/"):
                path = path[len(base) + 1:]
            path = path.strip("/")
            props: dict = {}
            for ps in node.findall(f"{DAV_NS}propstat"):
                status = (ps.findtext(f"{DAV_NS}status") or "")
                if " 200 " not in status and not status.endswith(" 200"):
                    continue
                prop = ps.find(f"{DAV_NS}prop")
                if prop is None:
                    continue
                for child in prop:
                    props[child.tag] = child
            rt = props.get(f"{DAV_NS}resourcetype")
            is_dir = rt is not None and rt.find(f"{DAV_NS}collection") is not None
            etag = normalize_etag(props[f"{DAV_NS}getetag"].text
                                  if f"{DAV_NS}getetag" in props else None)
            size_raw = props[f"{DAV_NS}getcontentlength"].text \
                if f"{DAV_NS}getcontentlength" in props else None
            try:
                size = int(size_raw) if size_raw not in (None, "") else 0
            except (TypeError, ValueError):
                size = 0
            out.append(Prop(path=path, is_dir=is_dir, etag=etag, size=size,
                            last_modified=props[f"{DAV_NS}getlastmodified"].text
                            if f"{DAV_NS}getlastmodified" in props else None,
                            content_type=props[f"{DAV_NS}getcontenttype"].text
                            if f"{DAV_NS}getcontenttype" in props else None))
        return out

    def stat(self, path: str) -> Prop:
        rows = self.propfind(path, depth="0")
        for row in rows:
            if row.path == path.strip("/"):
                return row
        if rows:
            return rows[0]
        raise NotFoundError(f"not found: {path!r}", 404, "not_found")

    def exists(self, path: str) -> bool:
        try:
            self.stat(path)
            return True
        except NotFoundError:
            return False

    # -- GET / PUT / DELETE / MKCOL / MOVE --------------------------------------

    def get(self, path: str, dest: str | None = None, byte_range: tuple[int, int] | None = None,
            *, headers: dict | None = None, query: dict | None = None,
            stream_to=None, timeout: float | None = None):
        """Download *path*; to *dest* when given (returns the response), to
        *stream_to* when given (the caller's sink), else to memory.

        ``GET`` on a collection is Nextcloud's folder-archive extension::

            dav.get("", headers={"Accept": "application/x-tar"},
                    query={"files": '["Text/A.txt", "Extraction/B.json"]'})

        Backends that do not implement it answer with the WebDAV HTML page,
        so the caller must look at the response content type (that is why the
        ``stream_to`` sink may refuse the body in ``start()``).
        """
        hdrs = dict(headers or {})
        if byte_range is not None:
            hdrs["Range"] = f"bytes={int(byte_range[0])}-{int(byte_range[1])}"
        if stream_to is not None:
            return self.request("GET", path, headers=hdrs, query=query,
                                stream_to=stream_to, timeout=timeout)
        if dest is None:
            resp = self.request("GET", path, headers=hdrs, query=query, timeout=timeout)
            return resp.body
        d = os.path.dirname(dest)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(dest, "wb") as fh:
            resp = self.request("GET", path, headers=hdrs, query=query,
                                stream_to=fh, timeout=timeout)
            fh.flush()
            os.fsync(fh.fileno())
        return resp

    def put(self, path: str, data, *, if_match: str | None = None,
            if_none_match: bool = False, content_type: str = "application/octet-stream",
            headers: dict | None = None) -> WebDAVResponse:
        """PUT *data* (bytes or a binary file object); returns 201/204."""
        if self.emulate_conditionals and (if_match or if_none_match):
            self._check_precondition(path, if_match, if_none_match)
        hdrs = {"Content-Type": content_type}
        if if_match:
            hdrs["If-Match"] = quote_etag(if_match)
        if if_none_match:
            hdrs["If-None-Match"] = "*"
        if isinstance(data, (bytes, bytearray, memoryview)):
            hdrs["Content-Length"] = str(len(data))
        elif hasattr(data, "fileno"):
            try:
                hdrs["Content-Length"] = str(os.fstat(data.fileno()).st_size)
            except OSError:
                pass
        if headers:
            hdrs.update(headers)
        return self.request("PUT", path, headers=hdrs, body=data)

    def delete(self, path: str, if_match: str | None = None) -> WebDAVResponse:
        if self.emulate_conditionals and if_match:
            self._check_precondition(path, if_match, False)
        headers = {"If-Match": quote_etag(if_match)} if if_match else None
        return self.request("DELETE", path, headers=headers)

    def _check_precondition(self, path: str, if_match: str | None, if_none_match: bool) -> None:
        """Client-side ``If-Match`` / ``If-None-Match: *`` for a server that ignores them.

        PowerFolder (LRZ Sync+Share) answers 201 to a conditional PUT whatever
        the condition says.  A ``PROPFIND Depth: 0`` right before the write
        turns a stale write into the same :class:`PreconditionFailed` a
        conforming server would raise.  It is a check-then-act, not atomic: two
        writers inside the same few milliseconds can both pass (the lock code
        re-reads its lock file after a settle delay to catch exactly that).
        The headers are still sent, so a server that does honour them wins.
        """
        try:
            prop = self.stat(path)
        except NotFoundError:
            prop = None
        if if_none_match and prop is not None:
            raise PreconditionFailed("the file already exists (checked by the client)")
        if if_match:
            if prop is None:
                raise PreconditionFailed("the file is gone (checked by the client)")
            want = str(if_match).strip().removeprefix("W/").strip('"')
            have = str(prop.etag or "").strip().removeprefix("W/").strip('"')
            if have and want != have:          # no ETag listed: nothing to compare
                raise PreconditionFailed("the file changed (checked by the client)")

    def mkcol(self, path: str) -> WebDAVResponse:
        return self.request("MKCOL", path)

    def makedirs(self, path: str) -> None:
        """MKCOL every missing level of *path* (409 ``parent missing`` is expected)."""
        rel = str(path).replace("\\", "/").strip("/")
        if not rel:
            return
        parts = rel.split("/")
        for i in range(1, len(parts) + 1):
            sub = "/".join(parts[:i])
            try:
                self.mkcol(sub)
            except ConflictError:
                pass                     # already there (or a lost race)
            except ServerError as exc:
                status = getattr(exc, "status", 0)
                if status in (405, 409):
                    # RFC 4918: MKCOL on an existing collection; WsgiDAV says 405
                    if not self.exists(sub):
                        raise
                else:
                    raise
            except ForbiddenError:
                if not self.exists(sub):
                    raise

    def move(self, src: str, dst: str, overwrite: bool = True) -> WebDAVResponse:
        target = self.url_for(dst)
        return self.request("MOVE", src, headers={"Destination": target,
                                                 "Overwrite": "T" if overwrite else "F"})
