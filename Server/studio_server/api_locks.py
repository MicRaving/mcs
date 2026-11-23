"""Study-key locks (INTERFACES §3.7).

A lock is advisory: it does not stop a client from editing a file, it tells the
other clients that one user is working on a study key (and makes the server
reject a *shared* write by anybody else while it is live, §3.5/§3.6).

* Acquire and renew are the same call (``POST``), done inside one
  ``BEGIN IMMEDIATE`` transaction so two racing clients cannot both win; the
  loser gets 409 with the current ``holder``.
* A lock expires after ``settings.lock_ttl_h`` hours, or after
  ``min(offline_hours, settings.max_offline_lease_h)`` when the client announces
  it is going offline.
* A successful push of a shared file of key K by the holder renews K's lock
  (:func:`renew_lock_for_push`, called from ``api_sync``).
* ``DELETE`` releases the holder's own lock; the owner may force-release any
  lock with ``?force=1``.  Both are audited.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from online_shared.protocol import classify

from .auth import fail, project_member
from .db import audit, get_db, transaction, utcnow
from .visibility import is_owner, lock_expired

router = APIRouter(tags=["locks"])

#: The tasks that write a shared file (§3.5: settle, harmonize, reproduce).
SHARED_TASKS = ("settle", "harmonize", "reproduce")
_TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


# ── time helpers ───────────────────────────────────────────────────────


def _parse_ts(value: str) -> datetime:
    return datetime.strptime(str(value), _TS_FORMAT).replace(tzinfo=timezone.utc)


def iso_plus(now: str, seconds: float) -> str:
    """``now + seconds`` in the fixed-width UTC format the server stores."""
    return (_parse_ts(now) + timedelta(seconds=float(seconds))).strftime(_TS_FORMAT)


def _seconds_between(start: str, end: str) -> float:
    try:
        return (_parse_ts(end) - _parse_ts(start)).total_seconds()
    except (TypeError, ValueError):
        return 0.0


def _setting_hours(settings: dict[str, Any], name: str, default: float) -> float:
    value = (settings or {}).get(name, default)
    try:
        hours = float(value)
    except (TypeError, ValueError):
        return float(default)
    return hours if hours > 0 else float(default)


# ── rows ───────────────────────────────────────────────────────────────


def get_lock(conn, project_id: str, key: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM locks WHERE project_id = ? AND key = ?",
        (project_id, key),
    ).fetchone()
    return dict(row) if row is not None else None


def _public_lock(lock: dict[str, Any], user: dict[str, Any] | None, now: str) -> dict[str, Any]:
    return {
        "key": lock["key"],
        "user": (user or {}).get("username") or "",
        "user_id": int(lock["user_id"]),
        "display_name": (user or {}).get("display_name") or "",
        "client_id": lock.get("client_id"),
        "acquired": lock["acquired"],
        "renewed": lock["renewed"],
        "expires": lock["expires"],
        "offline": bool(lock.get("offline")),
        "expired": lock_expired(lock, now),
    }


def _user_row(conn, user_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT id, username, display_name FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    return dict(row) if row is not None else None


# ── permission: may this member lock this key? ─────────────────────────


def key_shared_tasks(conn, project_id: str, key: str) -> set[str]:
    """The write tasks of the *shared* files that already exist for *key*."""
    tasks: set[str] = set()
    rows = conn.execute(
        "SELECT path FROM files WHERE project_id = ? AND key = ? AND kind = 'shared'",
        (project_id, key),
    ).fetchall()
    for row in rows:
        task = classify(row["path"]).task
        if task:
            tasks.add(str(task))
    return tasks


def may_lock_key(conn, member: dict[str, Any], key: str) -> bool:
    """Owner always; otherwise the member needs a task that writes a shared file.

    When the key has no shared file yet (a coder starting fresh work), any of
    the three shared-write tasks qualifies — the lock has to be obtainable
    *before* the first push.
    """
    if is_owner(member):
        return True
    tasks = set(member.get("tasks") or [])
    if not tasks:
        return False
    existing = key_shared_tasks(conn, member["project_id"], key)
    needed = existing or set(SHARED_TASKS)
    return bool(tasks & needed)


# ── push hook ──────────────────────────────────────────────────────────


def renew_lock_for_push(
    conn,
    project_id: str,
    key: str,
    user_id: int,
    settings: dict[str, Any],
    now: str | None = None,
) -> bool:
    """Extend the holder's lock for *key* after an accepted shared write (§3.7).

    The lease length of the existing lock is preserved (an offline lease stays
    an offline lease), bounded by the configured maxima.  Returns True when a
    lock was renewed.
    """
    lock = get_lock(conn, project_id, key)
    if lock is None or int(lock["user_id"]) != int(user_id):
        return False
    moment = now or utcnow()
    ttl_hours = _setting_hours(settings, "lock_ttl_h", 24)
    offline_hours = _setting_hours(settings, "max_offline_lease_h", 72)
    lease = _seconds_between(lock["renewed"], lock["expires"])
    if lease <= 0:
        lease = ttl_hours * 3600
    cap = (offline_hours if lock.get("offline") else ttl_hours) * 3600
    lease = max(1.0, min(lease, float(cap)))
    conn.execute(
        "UPDATE locks SET renewed = ?, expires = ? WHERE project_id = ? AND key = ?",
        (moment, iso_plus(moment, lease), project_id, key),
    )
    return True


# ── endpoints ──────────────────────────────────────────────────────────


class LockBody(BaseModel):
    client_id: str | None = None
    offline_hours: float | None = None


@router.get("/api/p/{pid}/locks")
def list_locks(
    member: dict[str, Any] = Depends(project_member),
    conn=Depends(get_db),
) -> list[dict[str, Any]]:
    now = utcnow()
    rows = conn.execute(
        "SELECT * FROM locks WHERE project_id = ? ORDER BY key",
        (member["project_id"],),
    ).fetchall()
    locks = []
    for row in rows:
        lock = dict(row)
        locks.append(_public_lock(lock, _user_row(conn, int(lock["user_id"])), now))
    return locks


@router.post("/api/p/{pid}/locks/{key}")
def acquire_lock(
    key: str,
    body: LockBody,
    member: dict[str, Any] = Depends(project_member),
    conn=Depends(get_db),
):
    project_key = (key or "").strip()
    if not project_key or "/" in project_key:
        raise fail(400, "bad_request", "A lock key must be a study key.")

    settings = member["project"].get("settings") or {}
    offline_hours = body.offline_hours
    if offline_hours is not None:
        try:
            offline_hours = float(offline_hours)
        except (TypeError, ValueError):
            raise fail(400, "bad_request", "offline_hours must be a number.")
        if offline_hours <= 0:
            raise fail(400, "bad_request", "offline_hours must be positive.")

    if not may_lock_key(conn, member, project_key):
        raise fail(
            403,
            "forbidden",
            "Locking a study key needs a task that writes its shared files.",
        )

    ttl_hours = _setting_hours(settings, "lock_ttl_h", 24)
    max_offline = _setting_hours(settings, "max_offline_lease_h", 72)
    now = utcnow()

    with transaction(conn, immediate=True):
        existing = get_lock(conn, member["project_id"], project_key)
        if (
            existing is not None
            and int(existing["user_id"]) != int(member["user_id"])
            and not lock_expired(existing, now)
        ):
            holder = _public_lock(existing, _user_row(conn, int(existing["user_id"])), now)
            return JSONResponse(
                status_code=409,
                content={
                    "detail": "locked",
                    "message": "Another user holds this lock.",
                    "holder": holder,
                },
            )

        if offline_hours is not None:
            lease_secs = min(offline_hours, max_offline) * 3600
            offline = 1
        else:
            lease_secs = ttl_hours * 3600
            offline = 0
        expires = iso_plus(now, lease_secs)

        if existing is None:
            conn.execute(
                "INSERT INTO locks (project_id, key, user_id, client_id, acquired,"
                " renewed, expires, offline) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    member["project_id"],
                    project_key,
                    int(member["user_id"]),
                    (body.client_id or "").strip() or None,
                    now,
                    now,
                    expires,
                    offline,
                ),
            )
            action = "lock.acquire"
        else:
            acquired = existing["acquired"] if int(existing["user_id"]) == int(
                member["user_id"]
            ) else now
            conn.execute(
                "UPDATE locks SET user_id = ?, client_id = ?, acquired = ?,"
                " renewed = ?, expires = ?, offline = ?"
                " WHERE project_id = ? AND key = ?",
                (
                    int(member["user_id"]),
                    (body.client_id or "").strip() or None,
                    acquired,
                    now,
                    expires,
                    offline,
                    member["project_id"],
                    project_key,
                ),
            )
            action = "lock.renew"
        audit(
            conn,
            member["project_id"],
            int(member["user_id"]),
            action,
            {"key": project_key, "expires": expires, "offline": bool(offline)},
        )
        fresh = get_lock(conn, member["project_id"], project_key)
        assert fresh is not None
    return _public_lock(fresh, _user_row(conn, int(fresh["user_id"])), utcnow())


@router.delete("/api/p/{pid}/locks/{key}")
def release_lock(
    key: str,
    force: int = Query(default=0),
    member: dict[str, Any] = Depends(project_member),
    conn=Depends(get_db),
) -> dict[str, Any]:
    project_key = (key or "").strip()
    lock = get_lock(conn, member["project_id"], project_key)
    if lock is None:
        raise fail(404, "not_found", "No lock on that study key.")
    holder = int(lock["user_id"]) == int(member["user_id"])
    owner = is_owner(member)
    if not holder and not (owner and force):
        raise fail(
            403,
            "forbidden",
            "Only the holder may release a lock; the owner needs ?force=1.",
        )
    with transaction(conn, immediate=True):
        conn.execute(
            "DELETE FROM locks WHERE project_id = ? AND key = ?",
            (member["project_id"], project_key),
        )
        audit(
            conn,
            member["project_id"],
            int(member["user_id"]),
            "lock.release" if holder else "lock.force_release",
            {"key": project_key, "holder": int(lock["user_id"])},
        )
    return {"ok": True, "key": project_key}


__all__ = [
    "SHARED_TASKS",
    "acquire_lock",
    "get_lock",
    "iso_plus",
    "key_shared_tasks",
    "list_locks",
    "may_lock_key",
    "release_lock",
    "renew_lock_for_push",
    "router",
]
