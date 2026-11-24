"""Exceptions of the online-project client (INTERFACES §3.10, §4).

Every network call of :mod:`client` maps its failure to one of these, so the
sync engine only has to tell three situations apart:

* :class:`OfflineError` - the server cannot be reached right now (connection
  refused, DNS, timeout, a 502/503/504 from a proxy).  Retry later; nothing
  the user did is wrong.
* :class:`AuthError` / :class:`ProtocolMismatch` - retrying will not help
  until the user logs in again / updates the app.
* everything else (:class:`ServerError` and its subclasses) - the request
  itself was refused.
"""

from __future__ import annotations


class OnlineError(Exception):
    """Base class of every error raised by the online client."""


class OfflineError(OnlineError):
    """The server is unreachable (connection error, timeout, gateway 5xx)."""


class ServerError(OnlineError):
    """The server answered with an error.

    ``status`` is the HTTP status, ``code`` the contract's ``detail`` code
    (``forbidden``, ``not_found``, ...), ``payload`` the decoded JSON body.
    """

    def __init__(self, message: str = "", status: int = 0, code: str = "", payload=None):
        super().__init__(message or code or f"HTTP {status}")
        self.status = status
        self.code = code
        self.payload = payload if payload is not None else {}


class AuthError(ServerError):
    """401 - missing, invalid or expired token / wrong password."""


class ForbiddenError(ServerError):
    """403 - the member may not do this."""


class NotFoundError(ServerError):
    """404."""


class BadRequestError(ServerError):
    """400."""


class RateLimitedError(ServerError):
    """429 (login rate limit)."""


class ConflictError(ServerError):
    """409 other than a lock (e.g. a stale resolve)."""


class LockedError(ServerError):
    """409 ``locked`` - the study key is locked by someone else.

    ``holder`` is the server's holder object (``user``, ``display_name``,
    ``expires``, ``offline`` ...) or ``{}`` when the server sent none.
    """

    def __init__(self, message: str = "", status: int = 409, code: str = "locked",
                 payload=None, holder: dict | None = None):
        super().__init__(message or "locked", status, code, payload)
        self.holder = holder or {}


class ProtocolMismatch(ServerError):
    """426 - client and server speak different protocol versions."""


class NotConnectedError(OnlineError):
    """A store method that needs an open project was called without one."""
