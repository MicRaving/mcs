"""SQLite persistence for the studio server (INTERFACES §3.2).

One stdlib ``sqlite3`` connection per request (opened by ``get_db``), WAL and a
busy timeout so reads never block on a writer for long.  ``PRAGMA user_version``
is the schema version.  The schema is created (``CREATE TABLE IF NOT EXISTS``)
on every start, so the database is self-healing after an upgrade.

Adapted from QCnext ``persistence/metadata_db.py`` and ``metadata_schema.py``
(https://github.com/MicRaving/qcnext), with SQLAlchemy/aiosqlite replaced by
plain ``sqlite3`` and the table layout replaced by the online-projects contract.
All tables the contract lists exist here, including the ones other modules
(sync, locks, presence, assignments) will use.
"""

from __future__ import annotations

import copy
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

from fastapi import Request

from . import ALL_TAGS, ALL_TASKS, TASKS

SCHEMA_VERSION = 1
DB_NAME = "studio.db"

#: Project settings defaults (§3.2).
DEFAULT_SETTINGS: dict[str, Any] = {
    "blinding": {"abstract": "blinded", "fulltext": "blinded", "coding": "blinded"},
    "lock_ttl_h": 24,
    "max_offline_lease_h": 72,
}

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id           INTEGER PRIMARY KEY,
    username     TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display_name TEXT NOT NULL DEFAULT '',
    email        TEXT NOT NULL DEFAULT '',
    pw_hash      TEXT NOT NULL DEFAULT '',
    is_admin     INTEGER NOT NULL DEFAULT 0,
    disabled     INTEGER NOT NULL DEFAULT 0,
    created      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tokens (
    token_hash TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    created    TEXT NOT NULL,
    expires    TEXT NOT NULL,
    label      TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS projects (
    id       TEXT PRIMARY KEY,
    name     TEXT NOT NULL,
    created  TEXT NOT NULL,
    settings TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS members (
    project_id TEXT NOT NULL,
    user_id    INTEGER NOT NULL,
    role       TEXT NOT NULL,
    tags       TEXT NOT NULL DEFAULT '[]',
    tasks      TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (project_id, user_id)
);

CREATE TABLE IF NOT EXISTS files (
    project_id TEXT NOT NULL,
    path       TEXT NOT NULL,
    kind       TEXT,
    key        TEXT,
    tag        TEXT,
    hash       TEXT,
    size       INTEGER NOT NULL DEFAULT 0,
    seq        INTEGER NOT NULL DEFAULT 0,
    deleted    INTEGER NOT NULL DEFAULT 0,
    updated_by INTEGER,
    updated_at TEXT,
    PRIMARY KEY (project_id, path)
);

CREATE TABLE IF NOT EXISTS changes (
    project_id TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    path       TEXT NOT NULL,
    base_hash  TEXT,
    new_hash   TEXT,
    user_id    INTEGER,
    client_id  TEXT,
    ts         TEXT NOT NULL,
    kind       TEXT NOT NULL DEFAULT 'push',
    PRIMARY KEY (project_id, seq)
);

CREATE TABLE IF NOT EXISTS blobs (
    hash    TEXT PRIMARY KEY,
    size    INTEGER NOT NULL DEFAULT 0,
    created TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS locks (
    project_id TEXT NOT NULL,
    key        TEXT NOT NULL,
    user_id    INTEGER NOT NULL,
    client_id  TEXT,
    acquired   TEXT NOT NULL,
    renewed    TEXT NOT NULL,
    expires    TEXT NOT NULL,
    offline    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (project_id, key)
);

CREATE TABLE IF NOT EXISTS presence (
    project_id  TEXT NOT NULL,
    user_id     INTEGER NOT NULL,
    client_id   TEXT NOT NULL,
    key         TEXT,
    tab         TEXT,
    app_version TEXT,
    ts          TEXT NOT NULL,
    PRIMARY KEY (project_id, client_id)
);

CREATE TABLE IF NOT EXISTS done (
    project_id TEXT NOT NULL,
    tag        TEXT NOT NULL,
    key        TEXT NOT NULL,
    stage      TEXT NOT NULL,
    user_id    INTEGER,
    ts         TEXT NOT NULL,
    PRIMARY KEY (project_id, tag, key, stage)
);

CREATE TABLE IF NOT EXISTS assignment_runs (
    run_id     TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    stage      TEXT NOT NULL,
    params     TEXT NOT NULL DEFAULT '{}',
    created_by INTEGER,
    ts         TEXT NOT NULL,
    undone     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS assignments (
    project_id TEXT NOT NULL,
    run_id     TEXT NOT NULL,
    stage      TEXT NOT NULL,
    key        TEXT NOT NULL,
    tag        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conflicts (
    id          INTEGER PRIMARY KEY,
    project_id  TEXT NOT NULL,
    path        TEXT NOT NULL,
    base_hash   TEXT,
    server_hash TEXT,
    client_hash TEXT,
    user_id     INTEGER,
    client_id   TEXT,
    state       TEXT NOT NULL DEFAULT 'open',
    resolution  TEXT,
    ts          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit (
    id         INTEGER PRIMARY KEY,
    ts         TEXT NOT NULL,
    project_id TEXT,
    user_id    INTEGER,
    action     TEXT NOT NULL,
    detail     TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_members_user      ON members(user_id);
CREATE INDEX IF NOT EXISTS idx_tokens_user       ON tokens(user_id);
CREATE INDEX IF NOT EXISTS idx_changes_path      ON changes(project_id, path);
CREATE INDEX IF NOT EXISTS idx_files_seq         ON files(project_id, seq);
CREATE INDEX IF NOT EXISTS idx_files_hash        ON files(project_id, hash);
CREATE INDEX IF NOT EXISTS idx_changes_new_hash  ON changes(project_id, new_hash);
CREATE INDEX IF NOT EXISTS idx_changes_base_hash ON changes(project_id, base_hash);
CREATE INDEX IF NOT EXISTS idx_audit_project     ON audit(project_id, id);
CREATE INDEX IF NOT EXISTS idx_conflicts_project ON conflicts(project_id, state);
CREATE INDEX IF NOT EXISTS idx_assign_project    ON assignments(project_id, stage, key);
CREATE INDEX IF NOT EXISTS idx_assignments_run   ON assignments(run_id);
CREATE INDEX IF NOT EXISTS idx_done_project      ON done(project_id, stage, key);
CREATE INDEX IF NOT EXISTS idx_presence_ts       ON presence(project_id, ts);
"""


# ── time / json helpers ────────────────────────────────────────────────


def utcnow() -> str:
    """UTC ISO-8601 with ``Z`` (fixed width, so string compare = time compare)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_after(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _json_load(value: Any, default: Any) -> Any:
    if value is None or value == "":
        return copy.deepcopy(default)
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return copy.deepcopy(default)


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


# ── connection / schema ────────────────────────────────────────────────


def connect(data_dir: str | Path) -> sqlite3.Connection:
    """Open ``<data_dir>/studio.db`` for one request (thread-pool safe)."""
    root = Path(data_dir)
    root.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(root / DB_NAME),
        timeout=5.0,
        check_same_thread=False,
        isolation_level=None,  # autocommit; `transaction()` owns BEGIN/COMMIT
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    except sqlite3.DatabaseError:  # exotic filesystem: keep the default mode
        pass
    return conn


def get_schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0]) if row else 0


def init_db(data_dir: str | Path) -> int:
    """Create/migrate the schema; returns the applied ``user_version``."""
    conn = connect(data_dir)
    try:
        conn.executescript(SCHEMA_SQL)
        if get_schema_version(conn) < SCHEMA_VERSION:
            conn.execute(f"PRAGMA user_version = {int(SCHEMA_VERSION)}")
        return get_schema_version(conn)
    finally:
        conn.close()


def get_db(request: Request) -> Iterator[sqlite3.Connection]:
    """FastAPI dependency: one connection per request, closed afterwards."""
    conn = connect(request.app.state.cfg.data_dir)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection, immediate: bool = False) -> Iterator[sqlite3.Connection]:
    """``BEGIN [IMMEDIATE]`` … ``COMMIT``/``ROLLBACK``; joins an open transaction."""
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield conn
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()


def next_seq(conn: sqlite3.Connection, project_id: str) -> int:
    """The next per-project change sequence number (call inside the write txn)."""
    row = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 FROM changes WHERE project_id = ?",
        (project_id,),
    ).fetchone()
    return int(row[0]) if row else 1


def audit(
    conn: sqlite3.Connection,
    project_id: str | None,
    user_id: int | None,
    action: str,
    detail: Mapping[str, Any] | list[Any] | str | None = None,
) -> None:
    """Append an audit row; commits when not already inside a transaction."""
    if detail is None:
        text = ""
    elif isinstance(detail, str):
        text = detail
    else:
        text = _json_dump(detail)
    conn.execute(
        "INSERT INTO audit (ts, project_id, user_id, action, detail)"
        " VALUES (?, ?, ?, ?, ?)",
        (utcnow(), project_id, user_id, action, text),
    )
    if not conn.in_transaction:
        conn.commit()


# ── users ──────────────────────────────────────────────────────────────


def public_user(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "username": row["username"],
        "display_name": row["display_name"] or "",
        "email": row["email"] or "",
        "is_admin": bool(row["is_admin"]),
    }


def get_user_by_id(conn: sqlite3.Connection, user_id: int) -> dict[str, Any] | None:
    return _row_dict(
        conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    )


def get_user_by_username(conn: sqlite3.Connection, username: str) -> dict[str, Any] | None:
    return _row_dict(
        conn.execute(
            "SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username,)
        ).fetchone()
    )


def list_users(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM users ORDER BY username COLLATE NOCASE").fetchall()
    return [dict(r) for r in rows]


def count_users(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) FROM users").fetchone()
    return int(row[0]) if row else 0


def insert_user(
    conn: sqlite3.Connection,
    username: str,
    pw_hash: str,
    *,
    display_name: str = "",
    email: str = "",
    is_admin: bool = False,
) -> dict[str, Any]:
    created = utcnow()
    cur = conn.execute(
        "INSERT INTO users (username, display_name, email, pw_hash, is_admin,"
        " disabled, created) VALUES (?, ?, ?, ?, ?, 0, ?)",
        (username, display_name, email, pw_hash, 1 if is_admin else 0, created),
    )
    row = get_user_by_id(conn, int(cur.lastrowid))
    assert row is not None  # just inserted
    return row


def update_user(conn: sqlite3.Connection, user_id: int, **fields: Any) -> dict[str, Any] | None:
    """Set a whitelist of columns; returns the fresh row (None when absent)."""
    allowed = {"display_name", "email", "pw_hash", "is_admin", "disabled"}
    sets, values = [], []
    for name, value in fields.items():
        if name not in allowed:
            continue
        sets.append(f"{name} = ?")
        values.append(int(value) if name in ("is_admin", "disabled") else value)
    if sets:
        values.append(user_id)
        conn.execute(f"UPDATE users SET {', '.join(sets)} WHERE id = ?", values)
    return get_user_by_id(conn, user_id)


# ── tokens ─────────────────────────────────────────────────────────────


def record_token(
    conn: sqlite3.Connection,
    user_id: int,
    hashed: str,
    expires: str,
    label: str = "login",
) -> None:
    conn.execute(
        "INSERT INTO tokens (token_hash, user_id, created, expires, label)"
        " VALUES (?, ?, ?, ?, ?)",
        (hashed, user_id, utcnow(), expires, label),
    )


def lookup_token(conn: sqlite3.Connection, hashed: str) -> dict[str, Any] | None:
    """The user row for a live token (expired tokens and disabled users fail)."""
    row = conn.execute(
        "SELECT u.* FROM tokens t JOIN users u ON u.id = t.user_id"
        " WHERE t.token_hash = ? AND t.expires > ? AND u.disabled = 0",
        (hashed, utcnow()),
    ).fetchone()
    return _row_dict(row)


def touch_token(conn: sqlite3.Connection, hashed: str, expires: str) -> None:
    conn.execute("UPDATE tokens SET expires = ? WHERE token_hash = ?", (expires, hashed))


def revoke_token(conn: sqlite3.Connection, hashed: str) -> None:
    conn.execute("DELETE FROM tokens WHERE token_hash = ?", (hashed,))


def revoke_user_tokens(
    conn: sqlite3.Connection, user_id: int, except_hash: str | None = None
) -> None:
    if except_hash:
        conn.execute(
            "DELETE FROM tokens WHERE user_id = ? AND token_hash != ?",
            (user_id, except_hash),
        )
    else:
        conn.execute("DELETE FROM tokens WHERE user_id = ?", (user_id,))


def prune_expired_tokens(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM tokens WHERE expires <= ?", (utcnow(),))


# ── settings ───────────────────────────────────────────────────────────


def default_settings() -> dict[str, Any]:
    return copy.deepcopy(DEFAULT_SETTINGS)


def merge_settings(base: Mapping[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]:
    """Deep-merge ``patch`` into ``base`` (nested dicts merge, scalars replace)."""
    out: dict[str, Any] = copy.deepcopy(dict(base))
    for key, value in patch.items():
        if isinstance(value, Mapping) and isinstance(out.get(key), Mapping):
            out[key] = merge_settings(out[key], value)  # type: ignore[arg-type]
        else:
            out[key] = copy.deepcopy(value)
    return out


def project_settings(row: Mapping[str, Any] | None) -> dict[str, Any]:
    """Stored settings merged over the defaults (so keys are always present)."""
    stored = _json_load(row.get("settings") if row else None, {})
    return merge_settings(DEFAULT_SETTINGS, stored if isinstance(stored, dict) else {})


# ── projects ───────────────────────────────────────────────────────────


def new_project_id() -> str:
    return str(uuid.uuid4())


def get_project(conn: sqlite3.Connection, project_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM projects WHERE id = ?", (project_id,)
    ).fetchone()
    if row is None:
        return None
    project = dict(row)
    project["settings"] = project_settings(project)
    return project


def create_project(
    conn: sqlite3.Connection,
    project_id: str,
    name: str,
    owner_id: int,
    settings: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    with transaction(conn, immediate=True):
        conn.execute(
            "INSERT INTO projects (id, name, created, settings) VALUES (?, ?, ?, ?)",
            (
                project_id,
                name,
                utcnow(),
                _json_dump(merge_settings(DEFAULT_SETTINGS, settings or {})),
            ),
        )
        conn.execute(
            "INSERT INTO members (project_id, user_id, role, tags, tasks)"
            " VALUES (?, ?, 'owner', '[]', ?)",
            (project_id, owner_id, _json_dump(list(ALL_TASKS))),
        )
    project = get_project(conn, project_id)
    assert project is not None
    return project


def list_projects_for_user(
    conn: sqlite3.Connection, user_id: int, is_admin: bool = False
) -> list[dict[str, Any]]:
    """Projects the user belongs to (server admins: every project, as owner)."""
    if is_admin:
        rows = conn.execute(
            "SELECT p.id, p.name, p.created, p.settings, m.role AS member_role"
            " FROM projects p LEFT JOIN members m"
            " ON m.project_id = p.id AND m.user_id = ?"
            " ORDER BY p.name COLLATE NOCASE",
            (user_id,),
        ).fetchall()
        return [
            {**dict(r), "role": r["member_role"] or "owner"} for r in rows
        ]
    rows = conn.execute(
        "SELECT p.id, p.name, p.created, p.settings, m.role AS member_role"
        " FROM projects p JOIN members m ON m.project_id = p.id"
        " WHERE m.user_id = ? ORDER BY p.name COLLATE NOCASE",
        (user_id,),
    ).fetchall()
    return [{**dict(r), "role": r["member_role"]} for r in rows]


def set_project_settings(
    conn: sqlite3.Connection, project_id: str, settings: Mapping[str, Any]
) -> None:
    conn.execute(
        "UPDATE projects SET settings = ? WHERE id = ?",
        (_json_dump(settings), project_id),
    )


# ── members ────────────────────────────────────────────────────────────


def member_public(row: Mapping[str, Any]) -> dict[str, Any]:
    row = dict(row)
    return {
        "user_id": int(row["user_id"]),
        "username": row.get("username", ""),
        "display_name": row.get("display_name", "") or "",
        "role": row["role"],
        "tags": _json_load(row.get("tags"), []),
        "tasks": _json_load(row.get("tasks"), []),
    }


def get_member(
    conn: sqlite3.Connection, project_id: str, user_id: int
) -> dict[str, Any] | None:
    """The *effective* membership: ``{role, tags, tasks}`` (+ identity).

    Owner ⇒ all tasks and the wildcard tag ``"*"``.  A server admin is an owner
    of every project (§3.4).  ``None`` when the user is not a member (or is
    disabled).
    """
    user = conn.execute(
        "SELECT id, username, display_name, is_admin, disabled FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    if user is None or user["disabled"]:
        return None
    if user["is_admin"]:
        return {
            "user_id": int(user["id"]),
            "username": user["username"],
            "display_name": user["display_name"] or "",
            "role": "owner",
            "tags": list(ALL_TAGS),
            "tasks": list(ALL_TASKS),
        }
    row = conn.execute(
        "SELECT * FROM members WHERE project_id = ? AND user_id = ?",
        (project_id, user_id),
    ).fetchone()
    if row is None:
        return None
    member = {
        "user_id": int(row["user_id"]),
        "username": user["username"],
        "display_name": user["display_name"] or "",
        "role": row["role"],
        "tags": _json_load(row["tags"], []),
        "tasks": _json_load(row["tasks"], []),
    }
    if member["role"] == "owner":
        member["tags"] = list(ALL_TAGS)
        member["tasks"] = list(ALL_TASKS)
    return member


def has_task(member: Mapping[str, Any], task: str) -> bool:
    """Owner passes every task check (§3.4)."""
    return member.get("role") == "owner" or task in (member.get("tasks") or [])


def has_tag(member: Mapping[str, Any], tag: str) -> bool:
    """Owner passes every tag check; ``"*"`` matches anything."""
    if member.get("role") == "owner":
        return True
    tags = member.get("tags") or []
    return tag in tags or "*" in tags


def get_member_row(
    conn: sqlite3.Connection, project_id: str, user_id: int
) -> dict[str, Any] | None:
    return _row_dict(
        conn.execute(
            "SELECT * FROM members WHERE project_id = ? AND user_id = ?",
            (project_id, user_id),
        ).fetchone()
    )


def list_members(conn: sqlite3.Connection, project_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT m.user_id, m.role, m.tags, m.tasks, u.username, u.display_name"
        " FROM members m JOIN users u ON u.id = m.user_id"
        " WHERE m.project_id = ? ORDER BY u.username COLLATE NOCASE",
        (project_id,),
    ).fetchall()
    return [member_public(r) for r in rows]


def upsert_member(
    conn: sqlite3.Connection,
    project_id: str,
    user_id: int,
    role: str,
    tags: list[str],
    tasks: list[str],
) -> None:
    conn.execute(
        "INSERT INTO members (project_id, user_id, role, tags, tasks)"
        " VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT(project_id, user_id) DO UPDATE SET"
        " role = excluded.role, tags = excluded.tags, tasks = excluded.tasks",
        (project_id, user_id, role, _json_dump(tags), _json_dump(tasks)),
    )


def remove_member(conn: sqlite3.Connection, project_id: str, user_id: int) -> bool:
    cur = conn.execute(
        "DELETE FROM members WHERE project_id = ? AND user_id = ?",
        (project_id, user_id),
    )
    return cur.rowcount > 0


def count_owners(conn: sqlite3.Connection, project_id: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM members WHERE project_id = ? AND role = 'owner'",
        (project_id,),
    ).fetchone()
    return int(row[0]) if row else 0


__all__ = [
    "ALL_TAGS",
    "ALL_TASKS",
    "DEFAULT_SETTINGS",
    "SCHEMA_VERSION",
    "TASKS",
    "audit",
    "connect",
    "count_owners",
    "count_users",
    "create_project",
    "default_settings",
    "get_db",
    "get_member",
    "get_member_row",
    "get_project",
    "get_schema_version",
    "get_user_by_id",
    "get_user_by_username",
    "has_tag",
    "has_task",
    "init_db",
    "insert_user",
    "iso_after",
    "list_members",
    "list_projects_for_user",
    "list_users",
    "lookup_token",
    "member_public",
    "merge_settings",
    "new_project_id",
    "next_seq",
    "project_settings",
    "prune_expired_tokens",
    "public_user",
    "record_token",
    "remove_member",
    "revoke_token",
    "revoke_user_tokens",
    "set_project_settings",
    "touch_token",
    "transaction",
    "update_user",
    "upsert_member",
    "utcnow",
]
