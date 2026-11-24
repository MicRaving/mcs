"""Server-admin account management (INTERFACES §3.4).

Adapted from QCnext's metadata-DB user helpers and ``api/v1/auth.py``
(https://github.com/MicRaving/qcnext): accounts are created and edited here,
never by self-registration.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel

from .auth import fail, require_admin
from .db import (
    audit,
    get_db,
    get_user_by_id,
    get_user_by_username,
    insert_user,
    list_users,
    revoke_user_tokens,
    update_user,
)
from .security import hash_password

router = APIRouter(prefix="/api/admin", tags=["admin"])

MAX_USERNAME = 64


def _admin_user(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "username": row["username"],
        "display_name": row["display_name"] or "",
        "email": row["email"] or "",
        "is_admin": bool(row["is_admin"]),
        "disabled": bool(row["disabled"]),
        "created": row["created"],
    }


def _clean_username(username: str) -> str:
    value = (username or "").strip()
    if not value or len(value) > MAX_USERNAME or any(ch.isspace() for ch in value):
        raise fail(
            400,
            "bad_request",
            f"A username must be 1-{MAX_USERNAME} characters without whitespace.",
        )
    return value


class UserCreate(BaseModel):
    username: str
    display_name: str = ""
    email: str = ""
    password: str
    is_admin: bool = False


class UserUpdate(BaseModel):
    display_name: str | None = None
    email: str | None = None
    is_admin: bool | None = None
    disabled: bool | None = None
    password: str | None = None


@router.get("/users")
def users(
    admin: dict[str, Any] = Depends(require_admin),
    conn=Depends(get_db),
) -> dict[str, Any]:
    return {"users": [_admin_user(row) for row in list_users(conn)]}


@router.post("/users", status_code=201)
def create_user(
    body: UserCreate,
    response: Response,
    admin: dict[str, Any] = Depends(require_admin),
    conn=Depends(get_db),
) -> dict[str, Any]:
    username = _clean_username(body.username)
    if not body.password:
        raise fail(400, "bad_request", "A password is required.")
    if get_user_by_username(conn, username) is not None:
        raise fail(409, "conflict", "That username is already taken.")
    row = insert_user(
        conn,
        username,
        hash_password(body.password),
        display_name=body.display_name or "",
        email=body.email or "",
        is_admin=bool(body.is_admin),
    )
    audit(
        conn,
        None,
        int(admin["id"]),
        "user.create",
        {"user_id": int(row["id"]), "username": row["username"],
         "is_admin": bool(row["is_admin"])},
    )
    response.headers["Location"] = f"/api/admin/users/{int(row['id'])}"
    return {"user": _admin_user(row)}


@router.patch("/users/{user_id}")
def edit_user(
    user_id: int,
    body: UserUpdate,
    admin: dict[str, Any] = Depends(require_admin),
    conn=Depends(get_db),
) -> dict[str, Any]:
    target = get_user_by_id(conn, user_id)
    if target is None:
        raise fail(404, "not_found", "User not found.")

    changes = body.model_dump(exclude_unset=True)
    if not changes:
        raise fail(400, "bad_request", "No fields to update.")

    if user_id == int(admin["id"]):
        if changes.get("is_admin") is False:
            raise fail(400, "bad_request", "You cannot remove your own admin role.")
        if changes.get("disabled") is True:
            raise fail(400, "bad_request", "You cannot disable your own account.")

    fields: dict[str, Any] = {}
    for name in ("display_name", "email"):
        if changes.get(name) is not None:
            fields[name] = changes[name]
    for name in ("is_admin", "disabled"):
        if changes.get(name) is not None:
            fields[name] = bool(changes[name])
    if changes.get("password") is not None:
        if not changes["password"]:
            raise fail(400, "bad_request", "A password must not be empty.")
        fields["pw_hash"] = hash_password(changes["password"])

    row = update_user(conn, user_id, **fields)
    assert row is not None

    if fields.get("disabled") or "pw_hash" in fields:
        revoke_user_tokens(conn, user_id)

    audit(
        conn,
        None,
        int(admin["id"]),
        "user.update",
        {"user_id": user_id, "fields": sorted(fields)},
    )
    return {"user": _admin_user(row)}


__all__ = ["router"]
