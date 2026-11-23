"""Projects, project detail, settings, members and the audit trail (§3.4).

The registry/ACL pattern is adapted from QCnext ``api/v1/server_projects.py``
(https://github.com/MicRaving/qcnext): a member row carries the project role,
and a server admin is an effective owner of every project.  Roles are
``owner|coder|viewer`` and each member carries coder ``tags`` plus ``tasks``.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Body, Depends, Query
from pydantic import BaseModel

from . import PROTOCOL_VERSION, ROLES, STAGES, TASKS
from .auth import current_user, fail, project_member, require_admin, require_owner
from .db import (
    audit,
    count_owners,
    create_project,
    get_db,
    get_member_row,
    get_user_by_id,
    list_members,
    list_projects_for_user,
    member_public,
    merge_settings,
    new_project_id,
    project_settings,
    remove_member,
    set_project_settings,
    upsert_member,
)

router = APIRouter(tags=["projects"])

MAX_PROJECT_NAME = 120
BLINDING_VALUES = ("blinded", "visible")


class ProjectCreate(BaseModel):
    name: str


class MemberBody(BaseModel):
    role: str
    tags: list[str] = []
    tasks: list[str] = []


def _validate_settings(settings: dict[str, Any]) -> None:
    blinding = settings.get("blinding")
    if not isinstance(blinding, dict):
        raise fail(400, "bad_request", "settings.blinding must be an object.")
    for stage in STAGES:
        value = blinding.get(stage)
        if value not in BLINDING_VALUES:
            raise fail(
                400,
                "bad_request",
                f"settings.blinding.{stage} must be 'blinded' or 'visible'.",
            )
    for name in ("lock_ttl_h", "max_offline_lease_h"):
        value = settings.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise fail(400, "bad_request", f"settings.{name} must be a positive integer.")


def _validate_member(body: MemberBody) -> tuple[list[str], list[str]]:
    if body.role not in ROLES:
        raise fail(400, "bad_request", f"role must be one of {', '.join(ROLES)}.")
    tags = [str(tag) for tag in body.tags if str(tag).strip()]
    unknown = [task for task in body.tasks if task not in TASKS]
    if unknown:
        raise fail(400, "bad_request", f"unknown task(s): {', '.join(unknown)}.")
    tasks = [task for task in TASKS if task in body.tasks]
    return tags, tasks


def _me(member: dict[str, Any]) -> dict[str, Any]:
    return {
        "role": member["role"],
        "tags": list(member.get("tags") or []),
        "tasks": list(member.get("tasks") or []),
    }


@router.get("/api/projects")
def my_projects(
    user: dict[str, Any] = Depends(current_user),
    conn=Depends(get_db),
) -> dict[str, Any]:
    rows = list_projects_for_user(conn, int(user["id"]), is_admin=bool(user["is_admin"]))
    return {
        "projects": [
            {"id": row["id"], "name": row["name"], "role": row["role"]} for row in rows
        ]
    }


@router.post("/api/projects", status_code=201)
def create(
    body: ProjectCreate,
    user: dict[str, Any] = Depends(require_admin),
    conn=Depends(get_db),
) -> dict[str, Any]:
    name = (body.name or "").strip()
    if not name or len(name) > MAX_PROJECT_NAME:
        raise fail(
            400, "bad_request", f"A project name must be 1-{MAX_PROJECT_NAME} characters."
        )
    project_id = new_project_id()
    project = create_project(conn, project_id, name, int(user["id"]))
    audit(conn, project_id, int(user["id"]), "project.create", {"name": name})
    return {
        "id": project["id"],
        "name": project["name"],
        "settings": project["settings"],
    }


@router.get("/api/p/{pid}")
def project_detail(member: dict[str, Any] = Depends(project_member)) -> dict[str, Any]:
    project = member["project"]
    return {
        "id": project["id"],
        "name": project["name"],
        "created": project["created"],
        "settings": project_settings(project),
        "me": _me(member),
        "protocol": PROTOCOL_VERSION,
    }


@router.patch("/api/p/{pid}/settings")
def patch_settings(
    payload: dict[str, Any] = Body(...),
    member: dict[str, Any] = Depends(require_owner),
    conn=Depends(get_db),
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise fail(400, "bad_request", "The settings patch must be a JSON object.")
    current = project_settings(member["project"])
    updated = merge_settings(current, payload)
    _validate_settings(updated)
    set_project_settings(conn, member["project_id"], updated)
    audit(
        conn,
        member["project_id"],
        int(member["user_id"]),
        "settings.update",
        {"changed": sorted(payload)},
    )
    return {"settings": updated}


@router.get("/api/p/{pid}/members")
def members(
    member: dict[str, Any] = Depends(project_member),
    conn=Depends(get_db),
) -> dict[str, Any]:
    return {"members": list_members(conn, member["project_id"])}


@router.put("/api/p/{pid}/members/{user_id}")
def put_member(
    user_id: int,
    body: MemberBody,
    member: dict[str, Any] = Depends(require_owner),
    conn=Depends(get_db),
) -> dict[str, Any]:
    pid = member["project_id"]
    if get_user_by_id(conn, user_id) is None:
        raise fail(404, "not_found", "User not found.")
    tags, tasks = _validate_member(body)

    existing = get_member_row(conn, pid, user_id)
    if (
        existing is not None
        and existing["role"] == "owner"
        and body.role != "owner"
        and count_owners(conn, pid) <= 1
    ):
        raise fail(400, "bad_request", "A project needs at least one owner.")

    upsert_member(conn, pid, user_id, body.role, tags, tasks)
    audit(
        conn,
        pid,
        int(member["user_id"]),
        "member.set",
        {"user_id": user_id, "role": body.role, "tags": tags, "tasks": tasks},
    )
    row = get_member_row(conn, pid, user_id)
    assert row is not None
    return {"member": member_public(row)}


@router.delete("/api/p/{pid}/members/{user_id}")
def delete_member(
    user_id: int,
    member: dict[str, Any] = Depends(require_owner),
    conn=Depends(get_db),
) -> dict[str, Any]:
    pid = member["project_id"]
    existing = get_member_row(conn, pid, user_id)
    if existing is None:
        raise fail(404, "not_found", "Membership not found.")
    if existing["role"] == "owner" and count_owners(conn, pid) <= 1:
        raise fail(400, "bad_request", "A project needs at least one owner.")
    remove_member(conn, pid, user_id)
    audit(conn, pid, int(member["user_id"]), "member.remove", {"user_id": user_id})
    return {"ok": True}


@router.get("/api/p/{pid}/audit")
def audit_trail(
    limit: int = Query(default=100, ge=1, le=1000),
    member: dict[str, Any] = Depends(require_owner),
    conn=Depends(get_db),
) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT a.id, a.ts, a.user_id, u.username, a.action, a.detail"
        " FROM audit a LEFT JOIN users u ON u.id = a.user_id"
        " WHERE a.project_id = ? ORDER BY a.id DESC LIMIT ?",
        (member["project_id"], limit),
    ).fetchall()
    entries = []
    for row in rows:
        detail: Any = row["detail"]
        if detail:
            try:
                detail = json.loads(detail)
            except (TypeError, ValueError):
                pass
        entries.append(
            {
                "id": int(row["id"]),
                "ts": row["ts"],
                "user_id": row["user_id"],
                "username": row["username"],
                "action": row["action"],
                "detail": detail,
            }
        )
    return {"audit": entries}


__all__ = ["router"]
