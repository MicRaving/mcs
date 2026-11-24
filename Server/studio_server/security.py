"""Password hashing, opaque tokens and the login rate limiter.

Adapted from QCnext ``services/password.py`` and ``services/token_service.py``
(https://github.com/MicRaving/qcnext) — same hashing scheme (argon2id via
``argon2-cffi``) and the same "opaque token, sha256 at rest" rule.  Passkeys are
not part of v1.
"""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
from collections import deque

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_hasher = PasswordHasher()  # argon2id, library defaults

TOKEN_BYTES = 32


def hash_password(password: str) -> str:
    """argon2id hash of ``password``; empty passwords are refused."""
    if not password:
        raise ValueError("empty password")
    return _hasher.hash(password)


def verify_password(password: str, stored: str) -> bool:
    """Constant-time argon2 verify; an empty/unknown hash is a mismatch."""
    if not stored:
        return False
    try:
        return _hasher.verify(stored, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(stored: str) -> bool:
    try:
        return _hasher.check_needs_rehash(stored)
    except (InvalidHashError, ValueError):
        return False


def new_token() -> str:
    """A fresh opaque token (32 random bytes, urlsafe)."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def token_hash(raw: str) -> str:
    """The sha256 hex digest that is stored in ``tokens.token_hash``."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def constant_time_equals(a: str, b: str) -> bool:
    return secrets.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


class LoginRateLimiter:
    """Failed logins per ``(ip, username)`` within a sliding window (§3.3).

    In-memory and per process (one uvicorn worker, §3.1).  A key is blocked
    once it has accumulated ``limit`` failures inside the window; a successful
    login clears it.  Blocked attempts are not recorded again (so the window
    slides instead of growing).
    """

    def __init__(
        self,
        limit: int = 10,
        window_secs: float = 600.0,
        clock=time.monotonic,
    ) -> None:
        self.limit = int(limit)
        self.window_secs = float(window_secs)
        self._clock = clock
        self._failures: dict[tuple[str, str], deque[float]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def key(ip: str, username: str) -> tuple[str, str]:
        return (ip or "", (username or "").strip().lower())

    def _fresh(self, key: tuple[str, str]) -> deque[float]:
        hits = self._failures.get(key)
        if hits is None:
            hits = deque()
            self._failures[key] = hits
        cutoff = self._clock() - self.window_secs
        while hits and hits[0] <= cutoff:
            hits.popleft()
        return hits

    def _prune_empty(self) -> None:
        if len(self._failures) <= 512:
            return
        for key in [k for k, hits in self._failures.items() if not hits]:
            del self._failures[key]

    def is_blocked(self, key: tuple[str, str]) -> bool:
        with self._lock:
            return len(self._fresh(key)) >= self.limit

    def retry_after(self, key: tuple[str, str]) -> int:
        """Seconds until the oldest failure leaves the window (≥ 1)."""
        with self._lock:
            hits = self._fresh(key)
            if not hits:
                return 1
            remaining = self.window_secs - (self._clock() - hits[0])
            return max(1, int(remaining + 0.999))

    def record_failure(self, key: tuple[str, str]) -> None:
        with self._lock:
            self._prune_empty()
            hits = self._fresh(key)
            hits.append(self._clock())

    def reset(self, key: tuple[str, str]) -> None:
        with self._lock:
            self._failures.pop(key, None)
