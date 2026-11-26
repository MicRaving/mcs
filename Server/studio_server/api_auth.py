"""Auth endpoints (INTERFACES §3.3).

Adapted from QCnext ``api/v1/auth.py`` (https://github.com/MicRaving/qcnext),
minus passkeys and registration: accounts are created by a server admin
(``/api/admin/users`` or the ``create-admin`` CLI).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel

from .auth import (
    bearer_token,
    client_ip,
    current_user,
    fail,
    issue_token,
    revoke_current,
)
from .db import (
    audit,
    get_db,
    get_user_by_username,
    list_projects_for_user,
    prune_expired_tokens,
    public_user,
    revoke_user_tokens,
    update_user,
)
from .security import hash_password, needs_rehash, token_hash, verify_password

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginBody(BaseModel):
    username: str
    password: str


class PasswordBody(BaseModel):
    old: str
    new: str


def _projects_for(conn, user: dict[str, Any]) -> list[dict[str, Any]]:
    rows = list_projects_for_user(conn, int(user["id"]), is_admin=bool(user["is_admin"]))
    return [
        {"id": row["id"], "name": row["name"], "role": row["role"]} for row in rows
    ]


@router.post("/login")
def login(body: LoginBody, request: Request, conn=Depends(get_db)) -> dict[str, Any]:
    cfg = request.app.state.cfg
    limiter = request.app.state.rate_limiter
    key = limiter.key(client_ip(request), body.username)

    if limiter.is_blocked(key):
        raise fail(
            429,
            "rate_limited",
            headers={"Retry-After": str(limiter.retry_after(key))},
        )

    user = get_user_by_username(conn, body.username)
    stored = user["pw_hash"] if user else ""
    ok = verify_password(body.password, stored)
    if user is None or not ok:
        limiter.record_failure(key)
        raise fail(401, "unauthorized", "Invalid username or password.")
    if user["disabled"]:
        limiter.record_failure(key)
        raise fail(401, "unauthorized", "This account is disabled.")

    limiter.reset(key)
    if needs_rehash(stored):
        # a hash from before the OWASP parameters: upgrade it now that we
        # know the password (keeps later logins at 19 MiB instead of 64)
        update_user(conn, int(user["id"]), pw_hash=hash_password(body.password))
    prune_expired_tokens(conn)
    raw, expires = issue_token(conn, int(user["id"]), cfg.token_ttl_secs)
    return {"token": raw, "expires_at": expires, "user": public_user(user)}


@router.post("/logout")
def logout(
    authorization: str = Header(default=""),
    user: dict[str, Any] = Depends(current_user),
    conn=Depends(get_db),
) -> dict[str, Any]:
    revoke_current(conn, authorization)
    return {"ok": True}


@router.get("/me")
def me(
    user: dict[str, Any] = Depends(current_user),
    conn=Depends(get_db),
) -> dict[str, Any]:
    return {"user": public_user(user), "projects": _projects_for(conn, user)}


@router.post("/password")
def change_password(
    body: PasswordBody,
    authorization: str = Header(default=""),
    user: dict[str, Any] = Depends(current_user),
    conn=Depends(get_db),
) -> dict[str, Any]:
    if not body.new:
        raise fail(400, "bad_request", "The new password must not be empty.")
    if not verify_password(body.old, user["pw_hash"]):
        raise fail(400, "bad_request", "The current password is wrong.")

    new_hash = hash_password(body.new)
    update_user(conn, int(user["id"]), pw_hash=new_hash)

    # Keep this session alive, drop every other token of the account.
    keep = token_hash(bearer_token(authorization))
    revoke_user_tokens(conn, int(user["id"]), except_hash=keep)
    audit(conn, None, int(user["id"]), "user.password", {"username": user["username"]})
    return {"ok": True}


__all__ = ["router"]
