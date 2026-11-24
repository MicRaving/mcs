"""Done marks (INTERFACES §3.8).

``done(key, stage, tag)`` is how a coder says "I have finished this study (at
this stage)".  It is the key that lifts coding blinding for that study (§3.5)
and it is the precondition for settling (``Extraction/``, §3.5); the caller's
``visibility_epoch`` (``api_sync.visibility_epoch``) changes with it, so every
client re-pulls the manifest.

A member may mark done only for one of its own tags (the owner for any tag);
only the owner may reopen (delete) a mark.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from . import STAGES
from .auth import fail, project_member, require_owner
from .db import audit, get_db, utcnow
from .visibility import has_tag

router = APIRouter(tags=["done"])


class DoneBody(BaseModel):
    key: str
    stage: str
    tag: str


class DoneDelete(DoneBody):
    pass


def _validate(body: DoneBody) -> tuple[str, str, str]:
    key = (body.key or "").strip()
    stage = (body.stage or "").strip()
    tag = (body.tag or "").strip()
    if not key:
        raise fail(400, "bad_request", "key is required.")
    if stage not in STAGES:
        raise fail(400, "bad_request", f"stage must be one of {', '.join(STAGES)}.")
    if not tag:
        raise fail(400, "bad_request", "tag is required.")
    return key, stage, tag


def _public(row: Any) -> dict[str, Any]:
    return {
        "key": row["key"],
        "stage": row["stage"],
        "tag": row["tag"],
        "user_id": row["user_id"],
        "ts": row["ts"],
    }


@router.post("/api/p/{pid}/done")
def mark_done(
    body: DoneBody,
    member: dict[str, Any] = Depends(project_member),
    conn=Depends(get_db),
) -> dict[str, Any]:
    key, stage, tag = _validate(body)
    if not has_tag(member, tag):
        raise fail(403, "forbidden", "You may only mark done for your own tags.")
    pid = member["project_id"]
    now = utcnow()
    conn.execute(
        "INSERT INTO done (project_id, tag, key, stage, user_id, ts)"
        " VALUES (?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(project_id, tag, key, stage) DO UPDATE SET"
        " user_id = excluded.user_id, ts = excluded.ts",
        (pid, tag, key, stage, int(member["user_id"]), now),
    )
    audit(
        conn,
        pid,
        int(member["user_id"]),
        "done.mark",
        {"key": key, "stage": stage, "tag": tag},
    )
    row = conn.execute(
        "SELECT * FROM done WHERE project_id = ? AND tag = ? AND key = ? AND stage = ?",
        (pid, tag, key, stage),
    ).fetchone()
    return {"done": _public(row) if row is not None else None}


@router.get("/api/p/{pid}/done")
def list_done(
    tag: str | None = Query(default=None),
    stage: str | None = Query(default=None),
    member: dict[str, Any] = Depends(project_member),
    conn=Depends(get_db),
) -> dict[str, Any]:
    sql = "SELECT * FROM done WHERE project_id = ?"
    params: list[Any] = [member["project_id"]]
    if tag:
        sql += " AND tag = ?"
        params.append(tag)
    if stage:
        sql += " AND stage = ?"
        params.append(stage)
    sql += " ORDER BY key, stage, tag"
    rows = conn.execute(sql, params).fetchall()
    return {"done": [_public(row) for row in rows]}


@router.delete("/api/p/{pid}/done")
def reopen(
    body: DoneDelete,
    member: dict[str, Any] = Depends(require_owner),
    conn=Depends(get_db),
) -> dict[str, Any]:
    key, stage, tag = _validate(body)
    pid = member["project_id"]
    cursor = conn.execute(
        "DELETE FROM done WHERE project_id = ? AND tag = ? AND key = ? AND stage = ?",
        (pid, tag, key, stage),
    )
    audit(
        conn,
        pid,
        int(member["user_id"]),
        "done.reopen",
        {"key": key, "stage": stage, "tag": tag},
    )
    return {"ok": True, "deleted": cursor.rowcount > 0}


__all__ = ["list_done", "mark_done", "reopen", "router"]
