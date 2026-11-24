"""Presence heartbeats (INTERFACES §3.8).

Each client announces itself every ``HEARTBEAT_SECS`` with
``POST /presence {client_id, key?, tab?, app_version}`` and receives the other
live clients of the project (rows younger than ``PRESENCE_TTL_SECS``).
Rows are keyed by ``(project_id, client_id)``, so a client that crashes simply
ages out; every call prunes the expired rows of the project.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

try:  # §0.1: the shim in studio_server/__init__ has already run.
    from online_shared.protocol import PRESENCE_TTL_SECS
except ImportError:  # pragma: no cover - shared module absent (contract §1)
    PRESENCE_TTL_SECS = 300

from .auth import fail, project_member
from .db import get_db, iso_after, utcnow
from .visibility import is_owner

router = APIRouter(tags=["presence"])


class PresenceBody(BaseModel):
    client_id: str
    key: str | None = None
    tab: str | None = None
    app_version: str | None = None


def _cutoff(now: str | None = None) -> str:
    """The oldest timestamp that still counts as live (``now - TTL``)."""
    if now is None:
        return iso_after(-PRESENCE_TTL_SECS)
    # `iso_after` reads the real clock; when a caller supplies `now` we keep
    # the arithmetic on that value instead.
    moment = datetime.strptime(now, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return (moment - timedelta(seconds=PRESENCE_TTL_SECS)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _prune(conn, project_id: str, cutoff: str) -> None:
    conn.execute(
        "DELETE FROM presence WHERE project_id = ? AND ts <= ?",
        (project_id, cutoff),
    )


@router.post("/api/p/{pid}/presence")
def heartbeat(
    body: PresenceBody,
    member: dict[str, Any] = Depends(project_member),
    conn=Depends(get_db),
) -> dict[str, Any]:
    client_id = (body.client_id or "").strip()
    if not client_id:
        raise fail(400, "bad_request", "client_id is required.")
    pid = member["project_id"]
    now = utcnow()
    cutoff = _cutoff(now)

    _prune(conn, pid, cutoff)
    conn.execute(
        "INSERT INTO presence (project_id, user_id, client_id, key, tab,"
        " app_version, ts) VALUES (?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(project_id, client_id) DO UPDATE SET"
        " user_id = excluded.user_id, key = excluded.key, tab = excluded.tab,"
        " app_version = excluded.app_version, ts = excluded.ts",
        (
            pid,
            int(member["user_id"]),
            client_id,
            body.key,
            body.tab,
            body.app_version,
            now,
        ),
    )
    conn.commit()

    rows = conn.execute(
        "SELECT p.user_id, p.key, p.tab, p.app_version, p.ts,"
        " u.username, u.display_name"
        " FROM presence p JOIN users u ON u.id = p.user_id"
        " WHERE p.project_id = ? AND p.ts > ? AND p.client_id != ?"
        " ORDER BY p.ts DESC",
        (pid, cutoff, client_id),
    ).fetchall()
    others = [
        {
            "user": row["username"],
            "user_id": int(row["user_id"]),
            "display_name": row["display_name"] or "",
            "key": row["key"],
            "tab": row["tab"],
            "app_version": row["app_version"],
            "ts": row["ts"],
        }
        for row in rows
    ]
    return {"others": others}


@router.delete("/api/p/{pid}/presence/{client_id}")
def leave(
    client_id: str,
    member: dict[str, Any] = Depends(project_member),
    conn=Depends(get_db),
) -> dict[str, Any]:
    pid = member["project_id"]
    if is_owner(member):
        conn.execute(
            "DELETE FROM presence WHERE project_id = ? AND client_id = ?",
            (pid, client_id),
        )
    else:
        conn.execute(
            "DELETE FROM presence WHERE project_id = ? AND client_id = ? AND user_id = ?",
            (pid, client_id, int(member["user_id"])),
        )
    conn.commit()
    return {"ok": True}


__all__ = ["PRESENCE_TTL_SECS", "heartbeat", "leave", "router"]
