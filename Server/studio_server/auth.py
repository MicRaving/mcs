"""Authentication dependencies and error vocabulary (INTERFACES §3.3, §3.10).

Adapted from QCnext ``api/v1/auth_deps.py`` and ``services/password.py`` /
``services/token_service.py`` (https://github.com/MicRaving/qcnext): opaque
bearer tokens, sha256 at rest, argon2id passwords, 30-day sliding expiry.
FastAPI dependencies here are plain ``def`` functions, so they run in the
threadpool together with the ``sqlite3`` connection they use.

Errors always serialise as ``{"detail": "<code>", "message": "<human>"}``;
``app.py`` installs the handler that turns ``APIError`` (and any other
``HTTPException``) into that shape.
"""

from __future__ import annotations

from typing import Any

from fastapi import Depends, Header, HTTPException, Request

from . import PROTOCOL_VERSION
from .db import (
    get_db,
    get_member,
    get_project,
    iso_after,
    lookup_token,
    record_token,
    revoke_token,
    touch_token,
)
from .security import new_token, token_hash

CODE_MESSAGES: dict[str, str] = {
    "unauthorized": "Authentication required.",
    "forbidden": "You do not have permission to do that.",
    "not_found": "Not found.",
    "locked": "The resource is locked.",
    "conflict": "The request conflicts with the current state.",
    "bad_request": "The request is invalid.",
    "rate_limited": "Too many failed attempts; try again later.",
    "protocol_mismatch": (
        f"This server speaks protocol {PROTOCOL_VERSION}; update the client."
    ),
}

_STATUS_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    409: "conflict",
    423: "locked",
    426: "protocol_mismatch",
    429: "rate_limited",
}


class APIError(HTTPException):
    """An ``HTTPException`` that carries a contract error code and a message."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(status_code=status_code, detail=code, headers=headers)
        self.code = code
        self.message = message or CODE_MESSAGES.get(code, code)


def fail(
    status_code: int,
    code: str,
    message: str | None = None,
    headers: dict[str, str] | None = None,
) -> APIError:
    """Build the exception for an endpoint: ``raise fail(403, "forbidden")``."""
    return APIError(status_code, code, message, headers)


# ── bearer tokens ──────────────────────────────────────────────────────


def bearer_token(authorization: str) -> str:
    """The raw token from an ``Authorization: Bearer …`` header."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise fail(401, "unauthorized", "Missing bearer token.")
    raw = authorization[len("Bearer ") :].strip()
    if not raw:
        raise fail(401, "unauthorized", "Missing bearer token.")
    return raw


def issue_token(
    conn, user_id: int, ttl_secs: float, label: str = "login"
) -> tuple[str, str]:
    """Create a token; returns ``(raw_token, expires_at)`` (raw shown once)."""
    raw = new_token()
    expires = iso_after(ttl_secs)
    record_token(conn, user_id, token_hash(raw), expires, label)
    return raw, expires


def resolve_user(conn, authorization: str, ttl_secs: float) -> dict[str, Any]:
    """The live user for a bearer header; slides the token expiry on use."""
    raw = bearer_token(authorization)
    hashed = token_hash(raw)
    user = lookup_token(conn, hashed)
    if user is None or user.get("disabled"):
        raise fail(401, "unauthorized", "Invalid or expired token.")
    touch_token(conn, hashed, iso_after(ttl_secs))
    return user


def revoke_current(conn, authorization: str) -> None:
    try:
        raw = bearer_token(authorization)
    except HTTPException:
        return
    revoke_token(conn, token_hash(raw))


# ── dependencies ───────────────────────────────────────────────────────


def current_user(
    request: Request,
    conn=Depends(get_db),
    authorization: str = Header(default=""),
) -> dict[str, Any]:
    """Resolve ``Authorization: Bearer <token>`` to the user row (401 if not)."""
    return resolve_user(conn, authorization, request.app.state.cfg.token_ttl_secs)


def project_member(
    pid: str,
    request: Request,
    user: dict[str, Any] = Depends(current_user),
    conn=Depends(get_db),
) -> dict[str, Any]:
    """404 when the project is unknown, 403 when the caller is not a member.

    Returns the **effective** member ``{role, tags, tasks, ...}`` (a server admin
    is an owner of every project) with the project row attached as
    ``member["project"]``.
    """
    project = get_project(conn, pid)
    if project is None:
        raise fail(404, "not_found", "Project not found.")
    member = get_member(conn, pid, int(user["id"]))
    if member is None:
        raise fail(403, "forbidden", "You are not a member of this project.")
    member["project_id"] = pid
    member["project"] = project
    member["is_admin"] = bool(user["is_admin"])
    return member


def require_owner(
    pid: str,
    member: dict[str, Any] = Depends(project_member),
) -> dict[str, Any]:
    """Owner-only guard: ``member = Depends(require_owner)``."""
    if member.get("role") != "owner":
        raise fail(403, "forbidden", "Owner role required.")
    return member


def require_admin(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    """Server-admin guard: ``user = Depends(require_admin)``."""
    if not user.get("is_admin"):
        raise fail(403, "forbidden", "Server admin role required.")
    return user


def client_ip(request: Request) -> str:
    """Rate-limit key source; honours ``X-Forwarded-For`` only when proxied."""
    if getattr(request.app.state.cfg, "trust_proxy", False):
        forwarded = request.headers.get("x-forwarded-for", "")
        first = forwarded.split(",", 1)[0].strip()
        if first:
            return first
    client = request.client
    return getattr(client, "host", None) or "unknown"
