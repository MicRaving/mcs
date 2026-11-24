"""Talking to the studio server's auth API on the user's behalf.

The gateway has no credential store of its own: ``POST /api/auth/login``
returns the server's opaque token, which becomes the browser's session cookie.
``GET /api/auth/me`` answers are cached for ``auth_ttl`` seconds per token, so
a revoked token or a reset password takes effect within that time.
"""

from __future__ import annotations

import hashlib
import time

import httpx


class AuthFailed(Exception):
    def __init__(self, status: int, code: str = "unauthorized", message: str = ""):
        super().__init__(message or code)
        self.status = status
        self.code = code
        self.message = message or code


class ServerAuth:
    def __init__(self, server: str, ttl: float = 60.0, http: httpx.AsyncClient | None = None):
        self.server = server.rstrip("/")
        self.ttl = ttl
        self.http = http or httpx.AsyncClient(timeout=httpx.Timeout(30.0))
        self._cache: dict[str, tuple[float, dict]] = {}

    @staticmethod
    def _key(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def _fail(resp: httpx.Response) -> AuthFailed:
        try:
            body = resp.json()
        except ValueError:
            body = {}
        return AuthFailed(resp.status_code, str(body.get("detail") or "error"),
                          str(body.get("message") or ""))

    async def login(self, username: str, password: str, forwarded_for: str | None) -> dict:
        headers = {"X-Forwarded-For": forwarded_for} if forwarded_for else {}
        r = await self.http.post(self.server + "/api/auth/login",
                                 json={"username": username, "password": password}, headers=headers)
        if r.status_code != 200:
            raise self._fail(r)
        data = r.json()
        if not data.get("token"):
            raise AuthFailed(502, "error", "the server returned no token")
        return data

    async def me(self, token: str) -> dict:
        key = self._key(token)
        hit = self._cache.get(key)
        now = time.monotonic()
        if hit and now - hit[0] < self.ttl:
            return hit[1]
        r = await self.http.get(self.server + "/api/auth/me",
                                headers={"Authorization": f"Bearer {token}"})
        if r.status_code != 200:
            self._cache.pop(key, None)
            raise self._fail(r)
        data = r.json()
        self._cache[key] = (now, data)
        if len(self._cache) > 2000:
            for k in sorted(self._cache, key=lambda k: self._cache[k][0])[:1000]:
                self._cache.pop(k, None)
        return data

    async def logout(self, token: str) -> None:
        self._cache.pop(self._key(token), None)
        try:
            await self.http.post(self.server + "/api/auth/logout",
                                 headers={"Authorization": f"Bearer {token}"})
        except httpx.HTTPError:
            pass

    def forget(self, token: str) -> None:
        self._cache.pop(self._key(token), None)

    async def close(self) -> None:
        await self.http.aclose()
