"""Assignments: preview, commit, manual, list, runs and undo (INTERFACES §3.9).

The allocation algorithm itself is the pure :func:`studio_server.assign.plan_assignment`
(unchanged here); this module only wraps it:

* ``preview`` / ``commit`` recompute the plan from the request (a commit with
  the same seed is always the same plan) — the owner does both;
* ``manual`` writes an explicit ``keys × tags`` cross product as a run;
* a *run* is one committed set of rows; ``DELETE /runs/{id}`` marks it undone
  instead of deleting history, and every list query reads only live runs;
* any member may list **its own** assignments, the owner lists everything.
"""

from __future__ import annotations

import json
import secrets
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from . import STAGES
from .assign import plan_assignment
from .auth import fail, project_member, require_owner
from .db import audit, get_db, transaction, utcnow

router = APIRouter(tags=["assignments"])


class CoderShare(BaseModel):
    tag: str
    share: float


class PlanBody(BaseModel):
    stage: str
    keys: list[str] = Field(default_factory=list)
    coders: list[CoderShare] = Field(default_factory=list)
    overlap_pct: float = 0
    overlap_mode: str = "pairs"
    seed: int | None = None


class ManualBody(BaseModel):
    stage: str
    keys: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


def _stage(value: str) -> str:
    stage = (value or "").strip()
    if stage not in STAGES:
        raise fail(400, "bad_request", f"stage must be one of {', '.join(STAGES)}.")
    return stage


def _plan(body: PlanBody) -> dict[str, Any]:
    _stage(body.stage)
    seed = body.seed if body.seed is not None else secrets.randbits(31)
    coders = [(coder.tag, coder.share) for coder in body.coders]
    try:
        return plan_assignment(
            list(body.keys), coders, body.overlap_pct, body.overlap_mode, seed
        )
    except (TypeError, ValueError) as exc:
        raise fail(400, "bad_request", str(exc))


def _new_run(conn, pid: str, stage: str, params: dict[str, Any], user_id: int) -> str:
    run_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO assignment_runs (run_id, project_id, stage, params, created_by,"
        " ts, undone) VALUES (?, ?, ?, ?, ?, ?, 0)",
        (run_id, pid, stage, json.dumps(params, ensure_ascii=False), user_id, utcnow()),
    )
    return run_id


def _insert_rows(conn, pid: str, run_id: str, stage: str, pairs) -> int:
    rows = [
        (pid, run_id, stage, str(key), str(tag)) for key, tag in pairs
    ]
    if rows:
        conn.executemany(
            "INSERT INTO assignments (project_id, run_id, stage, key, tag)"
            " VALUES (?, ?, ?, ?, ?)",
            rows,
        )
    return len(rows)


@router.post("/api/p/{pid}/assignments/preview")
def preview(
    body: PlanBody,
    member: dict[str, Any] = Depends(require_owner),
) -> dict[str, Any]:
    return _plan(body)


@router.post("/api/p/{pid}/assignments/commit")
def commit(
    body: PlanBody,
    member: dict[str, Any] = Depends(require_owner),
    conn=Depends(get_db),
) -> dict[str, Any]:
    plan = _plan(body)
    stage = _stage(body.stage)
    pid = member["project_id"]
    pairs = [
        (key, tag) for key, tags in plan["assignments"].items() for tag in tags
    ]
    with transaction(conn, immediate=True):
        run_id = _new_run(
            conn, pid, stage, dict(plan["params"], mode="planned"), int(member["user_id"])
        )
        _insert_rows(conn, pid, run_id, stage, pairs)
        audit(
            conn,
            pid,
            int(member["user_id"]),
            "assignments.commit",
            {"run_id": run_id, "stage": stage, "rows": len(pairs)},
        )
    return {"run_id": run_id}


@router.post("/api/p/{pid}/assignments/manual")
def manual(
    body: ManualBody,
    member: dict[str, Any] = Depends(require_owner),
    conn=Depends(get_db),
) -> dict[str, Any]:
    stage = _stage(body.stage)
    keys = list(dict.fromkeys(str(key) for key in body.keys if str(key).strip()))
    tags = list(dict.fromkeys(str(tag) for tag in body.tags if str(tag).strip()))
    if not keys:
        raise fail(400, "bad_request", "manual assignment needs at least one key.")
    if not tags:
        raise fail(400, "bad_request", "manual assignment needs at least one tag.")
    pid = member["project_id"]
    params = {"mode": "manual", "stage": stage, "keys": len(keys), "tags": tags}
    pairs = [(key, tag) for key in keys for tag in tags]
    with transaction(conn, immediate=True):
        run_id = _new_run(conn, pid, stage, params, int(member["user_id"]))
        _insert_rows(conn, pid, run_id, stage, pairs)
        audit(
            conn,
            pid,
            int(member["user_id"]),
            "assignments.manual",
            {"run_id": run_id, "stage": stage, "rows": len(pairs)},
        )
    return {"run_id": run_id}


@router.get("/api/p/{pid}/assignments")
def list_assignments(
    stage: str | None = Query(default=None),
    tag: str | None = Query(default=None),
    member: dict[str, Any] = Depends(project_member),
    conn=Depends(get_db),
) -> dict[str, Any]:
    if stage is not None and stage.strip() and stage.strip() not in STAGES:
        raise fail(400, "bad_request", f"stage must be one of {', '.join(STAGES)}.")
    pid = member["project_id"]
    sql = (
        "SELECT a.key, a.tag, a.stage, a.run_id FROM assignments a"
        " JOIN assignment_runs r ON r.run_id = a.run_id"
        " WHERE a.project_id = ? AND r.undone = 0"
    )
    params: list[Any] = [pid]
    if stage and stage.strip():
        sql += " AND a.stage = ?"
        params.append(stage.strip())
    if member.get("role") != "owner":
        own = [str(t) for t in (member.get("tags") or [])]
        if not own:
            return {"assignments": []}
        if "*" not in own:
            placeholders = ",".join("?" for _ in own)
            sql += f" AND a.tag IN ({placeholders})"
            params.extend(own)
    if tag and tag.strip():
        sql += " AND a.tag = ?"
        params.append(tag.strip())
    sql += " ORDER BY a.key, a.tag"
    rows = conn.execute(sql, params).fetchall()
    return {
        "assignments": [
            {
                "key": row["key"],
                "tag": row["tag"],
                "stage": row["stage"],
                "run_id": row["run_id"],
            }
            for row in rows
        ]
    }


def _public_run(row) -> dict[str, Any]:
    try:
        params = json.loads(row["params"] or "{}")
    except (TypeError, ValueError):
        params = {}
    return {
        "run_id": row["run_id"],
        "stage": row["stage"],
        "params": params,
        "created_by": row["created_by"],
        "ts": row["ts"],
        "undone": bool(row["undone"]),
    }


@router.get("/api/p/{pid}/assignments/runs")
def list_runs(
    member: dict[str, Any] = Depends(require_owner),
    conn=Depends(get_db),
) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT * FROM assignment_runs WHERE project_id = ? ORDER BY ts DESC, run_id",
        (member["project_id"],),
    ).fetchall()
    return {"runs": [_public_run(row) for row in rows]}


@router.delete("/api/p/{pid}/assignments/runs/{run_id}")
def undo_run(
    run_id: str,
    member: dict[str, Any] = Depends(require_owner),
    conn=Depends(get_db),
) -> dict[str, Any]:
    pid = member["project_id"]
    with transaction(conn, immediate=True):
        cursor = conn.execute(
            "UPDATE assignment_runs SET undone = 1"
            " WHERE run_id = ? AND project_id = ? AND undone = 0",
            (run_id, pid),
        )
        if cursor.rowcount == 0:
            exists = conn.execute(
                "SELECT 1 FROM assignment_runs WHERE run_id = ? AND project_id = ?",
                (run_id, pid),
            ).fetchone()
            raise fail(
                404 if exists is None else 400,
                "not_found" if exists is None else "bad_request",
                "Run not found." if exists is None else "Run is already undone.",
            )
        audit(
            conn,
            pid,
            int(member["user_id"]),
            "assignments.undo",
            {"run_id": run_id},
        )
    return {"ok": True, "run_id": run_id}


__all__ = ["commit", "list_assignments", "list_runs", "manual", "preview", "router", "undo_run"]
