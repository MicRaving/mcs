"""Thin HTTP client for the studio server (INTERFACES §3.3-§3.9).

Stdlib only (``urllib.request``): ``requests`` is not in the app's
``requirements.txt`` and the packaged app should not grow a dependency for a
dozen JSON calls.

The network itself sits behind a *transport* seam::

    transport.request(method, path, *, query=None, headers=None, body=None,
                      stream_to=None, timeout=None) -> Response

The default :class:`UrllibTransport` talks HTTP; the tests inject an in-memory
fake server with the same signature.  A transport raises ``OSError`` (or
:class:`~.errors.OfflineError`) when the server cannot be reached and returns
a :class:`Response` for every HTTP status, errors included; the mapping of a
status to an exception lives here, in :meth:`ServerClient._check`.

Tokens are never logged: ``repr(ServerClient)`` hides it and no method prints.
"""

from __future__ import annotations

import gzip
import io
import json
import os
import shutil
import socket
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, BinaryIO

from online_shared import protocol

from .errors import (
    AuthError,
    BadRequestError,
    ConflictError,
    ForbiddenError,
    LockedError,
    NotFoundError,
    OfflineError,
    ProtocolMismatch,
    RateLimitedError,
    ServerError,
)

DEFAULT_TIMEOUT = 30.0
GZIP_MIN_BYTES = 4096


@dataclass
class Response:
    status: int
    headers: dict = field(default_factory=dict)   # lower-case header names
    body: bytes = b""                              # empty when streamed to a file

    def json(self) -> Any:
        if not self.body:
            return None
        return json.loads(self.body.decode("utf-8"))


class UrllibTransport:
    """HTTP transport on ``urllib.request``; decodes gzip responses."""

    def __init__(self, base_url: str, timeout: float = DEFAULT_TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def request(self, method: str, path: str, *, query: dict | None = None,
                headers: dict | None = None, body: bytes | None = None,
                stream_to: BinaryIO | None = None, timeout: float | None = None) -> Response:
        url = self.base_url + path
        if query:
            clean = {k: v for k, v in query.items() if v is not None}
            if clean:
                url += "?" + urllib.parse.urlencode(clean)
        req = urllib.request.Request(url, data=body, method=method, headers=dict(headers or {}))
        try:
            resp = urllib.request.urlopen(req, timeout=timeout or self.timeout)
        except urllib.error.HTTPError as exc:
            try:
                data = exc.read()
            except OSError:
                data = b""
            return Response(exc.code, {k.lower(): v for k, v in exc.headers.items()}, data)
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as exc:
            raise OfflineError(f"server unreachable: {exc}") from exc
        try:
            hdrs = {k.lower(): v for k, v in resp.headers.items()}
            stream: Any = resp
            if hdrs.get("content-encoding", "").lower() == "gzip":
                stream = gzip.GzipFile(fileobj=resp)
            if stream_to is not None:
                shutil.copyfileobj(stream, stream_to, 1024 * 1024)
                return Response(resp.status, hdrs, b"")
            return Response(resp.status, hdrs, stream.read())
        except (socket.timeout, TimeoutError, ConnectionError, OSError) as exc:
            raise OfflineError(f"connection lost: {exc}") from exc
        finally:
            resp.close()


class ServerClient:
    """One authenticated connection to a studio server.

    ``transport`` defaults to :class:`UrllibTransport`; pass any object with
    the same ``request`` method (tests: the fake server).
    """

    def __init__(self, base_url: str, token: str | None = None,
                 timeout: float = DEFAULT_TIMEOUT, transport=None):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.transport = transport if transport is not None else UrllibTransport(self.base_url, timeout)

    def __repr__(self) -> str:   # never expose the token
        return f"ServerClient({self.base_url!r}, token={'set' if self.token else 'none'})"

    # -- plumbing ---------------------------------------------------------

    def _headers(self, extra: dict | None = None) -> dict:
        h = {"X-Studio-Protocol": str(protocol.PROTOCOL_VERSION), "Accept": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        if extra:
            h.update(extra)
        return h

    def _call(self, method: str, path: str, *, query: dict | None = None, payload: Any = None,
              body: bytes | None = None, headers: dict | None = None,
              stream_to: BinaryIO | None = None) -> Response:
        hdrs = self._headers(headers)
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        try:
            resp = self.transport.request(method, path, query=query, headers=hdrs, body=body,
                                          stream_to=stream_to, timeout=self.timeout)
        except OfflineError:
            raise
        except (OSError, TimeoutError, ConnectionError) as exc:
            raise OfflineError(f"server unreachable: {exc}") from exc
        self._check(resp)
        return resp

    def _json(self, method: str, path: str, **kw) -> Any:
        return self._call(method, path, **kw).json()

    @staticmethod
    def _check(resp: Response) -> None:
        status = resp.status
        if 200 <= status < 300:
            return
        payload: Any
        try:
            payload = resp.json()
        except (ValueError, UnicodeDecodeError):
            payload = None
        if not isinstance(payload, dict):
            payload = {"raw": payload}
        detail = payload.get("detail")
        code = detail if isinstance(detail, str) else ""
        message = payload.get("message") or code or f"HTTP {status}"
        if status in (502, 503, 504):
            raise OfflineError(f"server unavailable (HTTP {status})")
        if status == 401:
            raise AuthError(message, status, code or "unauthorized", payload)
        if status == 403:
            raise ForbiddenError(message, status, code or "forbidden", payload)
        if status == 404:
            raise NotFoundError(message, status, code or "not_found", payload)
        if status == 426 or code == "protocol_mismatch":
            raise ProtocolMismatch(message, status, "protocol_mismatch", payload)
        if status == 429:
            raise RateLimitedError(message, status, code or "rate_limited", payload)
        if status == 409:
            holder = payload.get("holder")
            if holder is None and isinstance(detail, dict):     # FastAPI-style nesting
                holder = detail.get("holder")
                code = detail.get("detail", code) if isinstance(detail.get("detail"), str) else code
            if holder is not None or code == "locked":
                raise LockedError(message, status, "locked", payload, holder or {})
            raise ConflictError(message, status, code or "conflict", payload)
        if status == 400:
            raise BadRequestError(message, status, code or "bad_request", payload)
        raise ServerError(message, status, code, payload)

    @staticmethod
    def _q(value: str) -> str:
        return urllib.parse.quote(value, safe="")

    def _p(self, pid: str, rest: str = "") -> str:
        return f"/api/p/{self._q(pid)}{rest}"

    # -- auth / projects (§3.3, §3.4) ---------------------------------------

    def health(self) -> dict:
        return self._json("GET", "/api/health")

    def login(self, username: str, password: str) -> dict:
        data = self._json("POST", "/api/auth/login", payload={"username": username, "password": password})
        self.token = data.get("token")
        return data

    def logout(self) -> None:
        try:
            self._call("POST", "/api/auth/logout", payload={})
        finally:
            self.token = None

    def me(self) -> dict:
        return self._json("GET", "/api/auth/me")

    def projects(self) -> list[dict]:
        data = self._json("GET", "/api/projects")
        return data.get("projects", []) if isinstance(data, dict) else list(data or [])

    def project(self, pid: str) -> dict:
        return self._json("GET", self._p(pid))

    # -- sync (§3.6) --------------------------------------------------------

    def manifest(self, pid: str, since: int = 0) -> dict:
        return self._json("GET", self._p(pid, "/manifest"), query={"since": int(since)})

    def blobs_missing(self, pid: str, hashes: list[str]) -> list[str]:
        if not hashes:
            return []
        data = self._json("POST", self._p(pid, "/blobs/missing"), payload={"hashes": list(hashes)})
        return list(data.get("missing", []))

    def put_blob(self, pid: str, digest: str, data: bytes, compress: bool = True) -> None:
        headers = {"Content-Type": "application/octet-stream"}
        body = data
        if compress and len(data) > 256:
            body = gzip.compress(data, compresslevel=6)
            headers["Content-Encoding"] = "gzip"
        self._call("PUT", self._p(pid, f"/blobs/{digest}"), body=body, headers=headers)

    def put_blob_file(self, pid: str, digest: str, path: str, compress: bool = True,
                      small_bytes: int = 8 * 1024 * 1024) -> None:
        """Upload the file at *path* as blob *digest* without loading it whole.

        Small files take the ordinary :meth:`put_blob` path (gzip).  A large
        file is streamed straight from disk (no compression: CPU and disk beat
        a second read) so a 1 GB source never lands in RAM.
        """
        size = os.path.getsize(path)
        if size <= small_bytes or not isinstance(self.transport, UrllibTransport):
            with open(path, "rb") as fh:
                data = fh.read()
            self.put_blob(pid, digest, data, compress=compress and size <= small_bytes)
            return
        headers = {"Content-Type": "application/octet-stream"}
        with open(path, "rb") as fh:
            self._call("PUT", self._p(pid, f"/blobs/{digest}"), body=fh, headers=headers)

    def get_blob(self, pid: str, digest: str, dest_path: str) -> str:
        """Download blob *digest* to *dest_path* (via a temp file next to it).

        The bytes are verified against the hash before the temp file is moved
        into place, so *dest_path* never holds a partial or wrong blob.
        """
        d = os.path.dirname(dest_path) or "."
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".dl-", suffix=".part", dir=d)
        try:
            with os.fdopen(fd, "wb") as fh:
                self._call("GET", self._p(pid, f"/blobs/{digest}"), stream_to=fh,
                           headers={"Accept-Encoding": "gzip", "Accept": "application/octet-stream"})
                fh.flush()
                os.fsync(fh.fileno())
            got = protocol.sha256_file(tmp)
            if got != digest:
                raise ServerError(f"blob {digest[:12]} arrived corrupted", 200, "bad_blob")
            os.replace(tmp, dest_path)
            tmp = ""
            return dest_path
        finally:
            if tmp and os.path.exists(tmp):
                os.unlink(tmp)

    def get_blob_bytes(self, pid: str, digest: str) -> bytes:
        buf = io.BytesIO()
        self._call("GET", self._p(pid, f"/blobs/{digest}"), stream_to=buf,
                   headers={"Accept-Encoding": "gzip", "Accept": "application/octet-stream"})
        data = buf.getvalue()
        if protocol.sha256_bytes(data) != digest:
            raise ServerError(f"blob {digest[:12]} arrived corrupted", 200, "bad_blob")
        return data

    def push(self, pid: str, client_id: str, entries: list[dict]) -> dict:
        return self._json("POST", self._p(pid, "/push"), payload={"client_id": client_id, "entries": entries})

    def conflicts(self, pid: str, state: str | None = "open") -> list[dict]:
        data = self._json("GET", self._p(pid, "/conflicts"), query={"state": state})
        return data.get("conflicts", []) if isinstance(data, dict) else list(data or [])

    def resolve_conflict(self, pid: str, conflict_id, resolution: str, new_hash: str | None = None) -> dict:
        body: dict = {"resolution": resolution}
        if resolution != "theirs":
            body["new_hash"] = new_hash          # null = "mine was a delete"
        return self._json("POST", self._p(pid, f"/conflicts/{self._q(str(conflict_id))}/resolve"),
                          payload=body) or {}

    # -- locks (§3.7) -------------------------------------------------------

    def locks(self, pid: str) -> list[dict]:
        data = self._json("GET", self._p(pid, "/locks"))
        return data.get("locks", []) if isinstance(data, dict) else list(data or [])

    def acquire_lock(self, pid: str, key: str, client_id: str, offline_hours: float | None = None) -> dict:
        body: dict = {"client_id": client_id}
        if offline_hours is not None:
            body["offline_hours"] = offline_hours
        return self._json("POST", self._p(pid, f"/locks/{self._q(key)}"), payload=body) or {}

    def release_lock(self, pid: str, key: str, force: bool = False) -> None:
        self._call("DELETE", self._p(pid, f"/locks/{self._q(key)}"),
                   query={"force": 1} if force else None)

    # -- presence / done / assignments (§3.8, §3.9) -------------------------

    def presence(self, pid: str, client_id: str, key: str | None = None, tab: str | None = None,
                 app_version: str = "") -> list[dict]:
        data = self._json("POST", self._p(pid, "/presence"),
                          payload={"client_id": client_id, "key": key, "tab": tab, "app_version": app_version})
        return (data or {}).get("others", [])

    def presence_close(self, pid: str, client_id: str) -> None:
        self._call("DELETE", self._p(pid, f"/presence/{self._q(client_id)}"))

    def mark_done(self, pid: str, key: str, stage: str, tag: str) -> dict:
        return self._json("POST", self._p(pid, "/done"), payload={"key": key, "stage": stage, "tag": tag}) or {}

    def done_list(self, pid: str, tag: str | None = None, stage: str | None = None) -> list[dict]:
        data = self._json("GET", self._p(pid, "/done"), query={"tag": tag, "stage": stage})
        return data.get("done", []) if isinstance(data, dict) else list(data or [])

    def reopen_done(self, pid: str, key: str, stage: str, tag: str) -> None:
        self._call("DELETE", self._p(pid, "/done"), payload={"key": key, "stage": stage, "tag": tag})

    def assignments(self, pid: str, stage: str | None = None, tag: str | None = None) -> list[dict]:
        data = self._json("GET", self._p(pid, "/assignments"), query={"stage": stage, "tag": tag})
        return data.get("assignments", []) if isinstance(data, dict) else list(data or [])

    # -- admin passthrough (§3.4, §3.9) -------------------------------------
    # These only exist so ``routes_online`` can offer the owner the server's
    # own endpoints; nothing in the sync engine depends on them.

    def members(self, pid: str) -> list[dict]:
        data = self._json("GET", self._p(pid, "/members"))
        return data.get("members", []) if isinstance(data, dict) else list(data or [])

    def put_member(self, pid: str, user_id, payload: dict) -> dict:
        return self._json("PUT", self._p(pid, f"/members/{int(user_id)}"), payload=payload) or {}

    def delete_member(self, pid: str, user_id) -> None:
        self._call("DELETE", self._p(pid, f"/members/{int(user_id)}"))

    def update_settings(self, pid: str, patch: dict) -> dict:
        data = self._json("PATCH", self._p(pid, "/settings"), payload=patch) or {}
        return data.get("settings", data) if isinstance(data, dict) else data

    def audit(self, pid: str, limit: int = 100) -> list[dict]:
        data = self._json("GET", self._p(pid, "/audit"), query={"limit": int(limit)})
        return data.get("audit", []) if isinstance(data, dict) else list(data or [])

    def assignment_preview(self, pid: str, payload: dict) -> dict:
        return self._json("POST", self._p(pid, "/assignments/preview"), payload=payload) or {}

    def assignment_commit(self, pid: str, payload: dict) -> dict:
        return self._json("POST", self._p(pid, "/assignments/commit"), payload=payload) or {}

    def assignment_manual(self, pid: str, payload: dict) -> dict:
        return self._json("POST", self._p(pid, "/assignments/manual"), payload=payload) or {}

    def assignment_runs(self, pid: str) -> list[dict]:
        data = self._json("GET", self._p(pid, "/assignments/runs"))
        return data.get("runs", []) if isinstance(data, dict) else list(data or [])

    def assignment_run_delete(self, pid: str, run_id: str) -> None:
        self._call("DELETE", self._p(pid, f"/assignments/runs/{self._q(str(run_id))}"))

    def admin_users(self) -> list[dict]:
        data = self._json("GET", "/api/admin/users")
        return data.get("users", []) if isinstance(data, dict) else list(data or [])

    def admin_create_user(self, payload: dict) -> dict:
        return self._json("POST", "/api/admin/users", payload=payload) or {}

    def admin_patch_user(self, user_id, payload: dict) -> dict:
        return self._json("PATCH", f"/api/admin/users/{int(user_id)}", payload=payload) or {}
