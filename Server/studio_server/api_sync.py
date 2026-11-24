"""Blobs, manifest, push and conflicts (INTERFACES §3.6).

The sync half of the protocol:

* ``POST /blobs/missing`` — which of these hashes does the server not have?
* ``PUT  /blobs/{hash}``  — streamed, content-verified upload (gzip bodies are
  decoded incrementally by :mod:`studio_server.blobs`).
* ``GET  /blobs/{hash}``  — streamed download with ``Range`` and optional
  gzip; only blobs reachable from a path the caller may read.
* ``GET  /manifest``      — the readable file catalogue since a sequence
  number, plus the caller's ``visibility_epoch``.
* ``POST /push``          — per-entry three-way sync (accept/merge/conflict).
* ``GET  /conflicts`` and ``POST /conflicts/{id}/resolve``.

``push`` processes its entries in order, one ``BEGIN IMMEDIATE`` transaction
per entry exactly as §3.6 spells it out (the entry's ``files`` + ``changes``
write plus its lock renewal are atomic).  WAL with ``synchronous = NORMAL``
makes 2 000 such transactions cost well under a second, so the literal
per-entry transaction is also fast enough for an owner's bulk publish.

A push is idempotent: an entry whose ``new_hash`` is already the head (or a
delete of an already-deleted path) is answered ``accepted`` with the existing
seq and no new ``changes`` row, and a merge whose result equals the head is
answered ``merged`` with the head hash — a retried push never creates a no-op
change.  Every result carries ``hash``: the new head for ``accepted``/
``merged``, the **current** head (or null) for ``conflict``/``rejected`` so the
client can restore the server version without another request.
"""

from __future__ import annotations

import hashlib
import json
import zlib
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from online_shared.merge import merge3
from online_shared.protocol import classify, normalize_path, sha256_bytes

from .api_locks import renew_lock_for_push
from .auth import fail, project_member
from .blobs import (
    BadRange,
    BlobHashMismatch,
    BlobStore,
    BlobTooLarge,
    InvalidHash,
    gunzip_aiter,
    iter_file,
    iter_gzip_file,
    parse_range,
    validate_hash,
)
from .db import audit, get_db, next_seq, transaction, utcnow
from .visibility import can_read, write_denial

router = APIRouter(tags=["sync"])

#: Responses smaller than this are served uncompressed (§3.6: > 4 KB).
GZIP_MIN_BYTES = 4 * 1024


# ── helpers ────────────────────────────────────────────────────────────


def blob_store(request: Request) -> BlobStore:
    return BlobStore(request.app.state.cfg.data_dir)


def head_seq(conn, project_id: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) FROM changes WHERE project_id = ?",
        (project_id,),
    ).fetchone()
    return int(row[0]) if row else 0


def member_done(conn, member: dict[str, Any]) -> set[tuple[str, str]]:
    """The caller's ``(tag, key)`` done marks (coding stage is what matters)."""
    tags = list(member.get("tags") or [])
    if not tags or member.get("role") == "owner":
        return set()
    placeholders = ",".join("?" for _ in tags)
    rows = conn.execute(
        f"SELECT tag, key FROM done WHERE project_id = ? AND tag IN ({placeholders})",
        (member["project_id"], *tags),
    ).fetchall()
    return {(row["tag"], row["key"]) for row in rows}


def visibility_epoch(conn, member: dict[str, Any]) -> str:
    """A hash of everything that can grow this caller's readable set (§3.6).

    Membership (role/tags/tasks), the blinding settings and the count/latest
    timestamp of the caller's own done rows — a change in any of them means a
    ``manifest?since=0`` is due.  ``settings.blinding`` also governs the
    ``blind_stages`` rule (the master workbook, settlements, the agreed full
    text and the ``Results/`` agreement products), so flipping a stage to
    "visible" or back changes this epoch too.
    """
    tags = list(member.get("tags") or [])
    done_rows = 0
    done_latest = ""
    if tags:
        placeholders = ",".join("?" for _ in tags)
        row = conn.execute(
            f"SELECT COUNT(*), COALESCE(MAX(ts), '') FROM done"
            f" WHERE project_id = ? AND tag IN ({placeholders})",
            (member["project_id"], *tags),
        ).fetchone()
        if row is not None:
            done_rows, done_latest = int(row[0]), row[1] or ""
    settings = member["project"].get("settings") or {}
    payload = {
        "user_id": int(member.get("user_id") or 0),
        "role": member.get("role") or "",
        "tags": sorted(tags),
        "tasks": sorted(member.get("tasks") or []),
        "blinding": {
            stage: (settings.get("blinding") or {}).get(stage, "blinded")
            for stage in ("abstract", "fulltext", "coding")
        },
        "done": [done_rows, done_latest],
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def record_blob(conn, hash: str, size: int) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO blobs (hash, size, created) VALUES (?, ?, ?)",
        (hash, int(size), utcnow()),
    )


def _head_row(conn, project_id: str, path: str):
    return conn.execute(
        "SELECT hash, seq, deleted FROM files WHERE project_id = ? AND path = ?",
        (project_id, path),
    ).fetchone()


def _head_hash(conn, project_id: str, path: str) -> str | None:
    row = _head_row(conn, project_id, path)
    if row is None or row["deleted"]:
        return None
    return row["hash"]


def _lock_row(conn, project_id: str, key: str | None) -> dict[str, Any] | None:
    if not key:
        return None
    row = conn.execute(
        "SELECT * FROM locks WHERE project_id = ? AND key = ?",
        (project_id, key),
    ).fetchone()
    return dict(row) if row is not None else None


def _write_file(
    conn,
    member: dict[str, Any],
    fc,
    path: str,
    store: BlobStore,
    base_hash: str | None,
    new_hash: str | None,
    *,
    client_id: str | None,
    kind: str,
) -> int:
    """Apply one head change: a new ``seq``, the ``files`` row, a ``changes`` row."""
    pid = member["project_id"]
    uid = int(member["user_id"])
    seq = next_seq(conn, pid)
    now = utcnow()
    size = 0 if new_hash is None else store.size(new_hash)
    deleted = 1 if new_hash is None else 0
    conn.execute(
        "INSERT INTO files (project_id, path, kind, key, tag, hash, size, seq,"
        " deleted, updated_by, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(project_id, path) DO UPDATE SET kind = excluded.kind,"
        " key = excluded.key, tag = excluded.tag, hash = excluded.hash,"
        " size = excluded.size, seq = excluded.seq, deleted = excluded.deleted,"
        " updated_by = excluded.updated_by, updated_at = excluded.updated_at",
        (
            pid,
            path,
            fc.kind,
            fc.key,
            fc.tag,
            new_hash,
            size,
            seq,
            deleted,
            uid,
            now,
        ),
    )
    conn.execute(
        "INSERT INTO changes (project_id, seq, path, base_hash, new_hash, user_id,"
        " client_id, ts, kind) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (pid, seq, path, base_hash, new_hash, uid, client_id, now, kind),
    )
    if fc.kind == "shared" and fc.key:
        renew_lock_for_push(
            conn, pid, fc.key, uid, member["project"].get("settings") or {}, now
        )
    return seq


def _rejected(path: str, reason: str, server_hash: str | None = None) -> dict[str, Any]:
    """A refused entry; ``hash`` is the current head so the client can restore it."""
    return {
        "path": path,
        "status": "rejected",
        "reason": reason,
        "hash": server_hash,
    }


def _apply_entry(
    conn,
    store: BlobStore,
    member: dict[str, Any],
    done: set[tuple[str, str]],
    client_id: str | None,
    entry: "PushEntry",
) -> dict[str, Any]:
    pid = member["project_id"]
    path = normalize_path(entry.path or "")
    if not path or path.startswith("/") or any(part in ("", "..") for part in path.split("/")):
        return _rejected(entry.path or "", "bad_path")

    head = _head_row(conn, pid, path)
    server_hash = None if head is None or head["deleted"] else head["hash"]
    server_seq = int(head["seq"] or 0) if head is not None else 0

    new_hash = (entry.new_hash or "").strip() or None
    base_hash = (entry.base_hash or "").strip() or None

    # 0 — every referenced blob must exist (§3.6).
    if new_hash is not None:
        try:
            new_hash = validate_hash(new_hash)
        except InvalidHash:
            return _rejected(path, "missing_blob", server_hash)
        if not store.exists(new_hash):
            return _rejected(path, "missing_blob", server_hash)

    # 1 — classify and authorize.
    fc = classify(path)
    if fc.kind == "excluded":
        return _rejected(path, "excluded", server_hash)
    lock = _lock_row(conn, pid, fc.key)
    denial = write_denial(member, fc, done, lock, utcnow())
    if denial is not None:
        return _rejected(path, denial, server_hash)

    # 2 — already at the requested version (a retried push): no no-op change.
    # An accepted push by the lock holder still renews the lock (§3.7).
    if new_hash == server_hash:
        if fc.kind == "shared" and fc.key:
            renew_lock_for_push(
                conn,
                pid,
                fc.key,
                int(member["user_id"]),
                member["project"].get("settings") or {},
            )
        return {
            "path": path,
            "status": "accepted",
            "seq": server_seq,
            "hash": server_hash,
        }

    # 3 — fast-forward.
    if (base_hash or None) == (server_hash or None):
        seq = _write_file(
            conn,
            member,
            fc,
            path,
            store,
            server_hash,
            new_hash,
            client_id=client_id,
            kind="push",
        )
        return {"path": path, "status": "accepted", "seq": seq, "hash": new_hash}

    # 4 — three-way merge when all three versions are available.
    if base_hash and server_hash and new_hash:
        base_bytes = _read_blob(store, base_hash)
        server_bytes = _read_blob(store, server_hash)
        client_bytes = _read_blob(store, new_hash)
        if base_bytes is not None and server_bytes is not None and client_bytes is not None:
            result = merge3(path, base_bytes, server_bytes, client_bytes)
            if result.ok and result.merged is not None:
                merged_hash = sha256_bytes(result.merged)
                if merged_hash == server_hash:
                    # the merge landed on the version the server already has
                    if fc.kind == "shared" and fc.key:
                        renew_lock_for_push(
                            conn,
                            pid,
                            fc.key,
                            int(member["user_id"]),
                            member["project"].get("settings") or {},
                        )
                    return {
                        "path": path,
                        "status": "merged",
                        "seq": server_seq,
                        "hash": server_hash,
                    }
                store.write_bytes(merged_hash, result.merged)
                record_blob(conn, merged_hash, len(result.merged))
                seq = _write_file(
                    conn,
                    member,
                    fc,
                    path,
                    store,
                    server_hash,
                    merged_hash,
                    client_id=client_id,
                    kind="merge",
                )
                return {
                    "path": path,
                    "status": "merged",
                    "seq": seq,
                    "hash": merged_hash,
                }

    # 5 — conflict: record it, leave the server head untouched.
    cursor = conn.execute(
        "INSERT INTO conflicts (project_id, path, base_hash, server_hash,"
        " client_hash, user_id, client_id, state, resolution, ts)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, 'open', NULL, ?)",
        (
            pid,
            path,
            base_hash,
            server_hash,
            new_hash,
            int(member["user_id"]),
            client_id,
            utcnow(),
        ),
    )
    return {
        "path": path,
        "status": "conflict",
        "conflict_id": int(cursor.lastrowid),
        "hash": server_hash,
    }


def _read_blob(store: BlobStore, hash: str) -> bytes | None:
    try:
        if not store.exists(hash):
            return None
        return store.read_bytes(hash)
    except OSError:
        return None


# ── blobs ──────────────────────────────────────────────────────────────


class MissingBody(BaseModel):
    hashes: list[str] = Field(default_factory=list)


@router.post("/api/p/{pid}/blobs/missing")
def blobs_missing(
    body: MissingBody,
    request: Request,
    member: dict[str, Any] = Depends(project_member),
) -> dict[str, Any]:
    store = blob_store(request)
    missing = [hash for hash in body.hashes if not store.exists(hash)]
    return {"missing": missing}


@router.put("/api/p/{pid}/blobs/{hash}")
async def put_blob(
    hash: str,
    request: Request,
    member: dict[str, Any] = Depends(project_member),
    conn=Depends(get_db),
) -> dict[str, Any]:
    try:
        digest = validate_hash(hash)
    except InvalidHash:
        raise fail(400, "bad_request", "The blob name must be a sha256 hex digest.")

    store = blob_store(request)
    encoding = (request.headers.get("content-encoding") or "").strip().lower()
    if encoding not in ("", "identity", "gzip"):
        raise fail(400, "bad_request", f"Unsupported Content-Encoding {encoding!r}.")

    declared = (request.headers.get("content-length") or "").strip()
    if declared.isdigit() and int(declared) > store.max_bytes:
        raise fail(
            413,
            "bad_request",
            f"The upload exceeds the {store.max_bytes}-byte limit.",
        )

    chunks = request.stream()
    if encoding == "gzip":
        chunks = gunzip_aiter(chunks)

    try:
        size = await store.write_stream(digest, chunks)
    except BlobHashMismatch:
        raise fail(400, "bad_request", "The blob's content does not match its name.")
    except BlobTooLarge:
        raise fail(413, "bad_request", "The upload exceeds the blob size limit.")
    except zlib.error:
        raise fail(400, "bad_request", "The gzip body could not be decoded.")

    with transaction(conn):
        record_blob(conn, digest, size)
    return {"hash": digest, "size": size}


def may_read_blob(conn, member: dict[str, Any], hash: str) -> bool:
    """True when some readable path (now or in the past) carries *hash* (§3.6)."""
    pid = member["project_id"]
    uid = int(member["user_id"])
    owner = member.get("role") == "owner"

    candidates: set[str] = set()
    for row in conn.execute(
        "SELECT path FROM files WHERE project_id = ? AND hash = ?", (pid, hash)
    ):
        candidates.add(row["path"])
    for row in conn.execute(
        "SELECT DISTINCT path FROM changes WHERE project_id = ?"
        " AND (new_hash = ? OR base_hash = ?)",
        (pid, hash, hash),
    ):
        candidates.add(row["path"])

    if candidates:
        done = member_done(conn, member)
        settings = member["project"].get("settings") or {}
        for path in candidates:
            if can_read(member, classify(path), done, settings):
                return True

    for row in conn.execute(
        "SELECT user_id FROM conflicts WHERE project_id = ?"
        " AND (server_hash = ? OR client_hash = ?)",
        (pid, hash, hash),
    ):
        if owner or row["user_id"] == uid:
            return True
    return False


@router.get("/api/p/{pid}/blobs/{hash}")
def get_blob(
    hash: str,
    request: Request,
    member: dict[str, Any] = Depends(project_member),
    conn=Depends(get_db),
):
    try:
        digest = validate_hash(hash)
    except InvalidHash:
        raise fail(404, "not_found", "Blob not found.")

    store = blob_store(request)
    if not store.exists(digest):
        raise fail(404, "not_found", "Blob not found.")
    if not may_read_blob(conn, member, digest):
        raise fail(403, "forbidden", "You may not read this blob.")

    size = store.size(digest)
    path = store.path(digest)
    range_header = request.headers.get("range")

    try:
        asked = parse_range(range_header, size)
    except BadRange:
        return Response(
            status_code=416,
            headers={"Content-Range": f"bytes */{size}"},
        )

    if asked is not None:
        start, end = asked
        return StreamingResponse(
            iter_file(path, start, end - start + 1),
            status_code=206,
            media_type="application/octet-stream",
            headers={
                "Content-Range": f"bytes {start}-{end}/{size}",
                "Content-Length": str(end - start + 1),
                "Accept-Ranges": "bytes",
            },
        )

    accepts_gzip = "gzip" in (request.headers.get("accept-encoding") or "").lower()
    if accepts_gzip and size > GZIP_MIN_BYTES:
        return StreamingResponse(
            iter_gzip_file(path),
            media_type="application/octet-stream",
            headers={
                "Content-Encoding": "gzip",
                "Vary": "Accept-Encoding",
                "Accept-Ranges": "bytes",
            },
        )

    return StreamingResponse(
        iter_file(path),
        media_type="application/octet-stream",
        headers={"Content-Length": str(size), "Accept-Ranges": "bytes"},
    )


# ── manifest ───────────────────────────────────────────────────────────


@router.get("/api/p/{pid}/manifest")
def manifest(
    since: int = Query(default=0, ge=0),
    member: dict[str, Any] = Depends(project_member),
    conn=Depends(get_db),
) -> dict[str, Any]:
    pid = member["project_id"]
    done = member_done(conn, member)
    settings = member["project"].get("settings") or {}

    if since > 0:
        rows = conn.execute(
            "SELECT path, hash, size, kind, key, tag, seq, deleted FROM files"
            " WHERE project_id = ? AND seq > ? ORDER BY path",
            (pid, since),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT path, hash, size, kind, key, tag, seq, deleted FROM files"
            " WHERE project_id = ? ORDER BY path",
            (pid,),
        ).fetchall()

    files = []
    for row in rows:
        path = row["path"]
        fc = classify(path)
        if not can_read(member, fc, done, settings):
            continue
        files.append(
            {
                "path": path,
                "hash": row["hash"],
                "size": int(row["size"] or 0),
                "kind": row["kind"] or fc.kind,
                "key": row["key"] if row["key"] is not None else fc.key,
                "tag": row["tag"] if row["tag"] is not None else fc.tag,
                "seq": int(row["seq"] or 0),
                "deleted": bool(row["deleted"]),
            }
        )
    return {
        "seq": head_seq(conn, pid),
        "visibility_epoch": visibility_epoch(conn, member),
        "files": files,
    }


# ── push ───────────────────────────────────────────────────────────────


class PushEntry(BaseModel):
    path: str
    base_hash: str | None = None
    new_hash: str | None = None
    size: int | None = None


class PushBody(BaseModel):
    client_id: str | None = None
    entries: list[PushEntry] = Field(default_factory=list)


@router.post("/api/p/{pid}/push")
def push(
    body: PushBody,
    request: Request,
    member: dict[str, Any] = Depends(project_member),
    conn=Depends(get_db),
) -> dict[str, Any]:
    store = blob_store(request)
    done = member_done(conn, member)
    client_id = (body.client_id or "").strip() or None
    results: list[dict[str, Any]] = []
    for entry in body.entries:
        # §3.6: one transaction per entry.
        with transaction(conn, immediate=True):
            results.append(_apply_entry(conn, store, member, done, client_id, entry))
    return {"seq": head_seq(conn, member["project_id"]), "results": results}


# ── conflicts ──────────────────────────────────────────────────────────


def _conflict_public(row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "path": row["path"],
        "base_hash": row["base_hash"],
        "server_hash": row["server_hash"],
        "client_hash": row["client_hash"],
        "user_id": row["user_id"],
        "client_id": row["client_id"],
        "state": row["state"],
        "resolution": row["resolution"],
        "ts": row["ts"],
    }


@router.get("/api/p/{pid}/conflicts")
def list_conflicts(
    state: str = Query(default="open"),
    member: dict[str, Any] = Depends(project_member),
    conn=Depends(get_db),
) -> dict[str, Any]:
    state = (state or "open").strip() or "open"
    if state not in ("open", "resolved", "all"):
        raise fail(400, "bad_request", "state must be open, resolved or all.")
    pid = member["project_id"]
    sql = "SELECT * FROM conflicts WHERE project_id = ?"
    params: list[Any] = [pid]
    if state != "all":
        sql += " AND state = ?"
        params.append(state)
    if member.get("role") != "owner":
        sql += " AND user_id = ?"
        params.append(int(member["user_id"]))
    sql += " ORDER BY id"
    rows = conn.execute(sql, params).fetchall()
    return {"conflicts": [_conflict_public(row) for row in rows]}


class ResolveBody(BaseModel):
    resolution: str
    new_hash: str | None = None


@router.post("/api/p/{pid}/conflicts/{conflict_id}/resolve")
def resolve_conflict(
    conflict_id: int,
    body: ResolveBody,
    request: Request,
    member: dict[str, Any] = Depends(project_member),
    conn=Depends(get_db),
) -> dict[str, Any]:
    pid = member["project_id"]
    row = conn.execute(
        "SELECT * FROM conflicts WHERE id = ? AND project_id = ?",
        (conflict_id, pid),
    ).fetchone()
    if row is None:
        raise fail(404, "not_found", "Conflict not found.")
    if row["state"] != "open":
        raise fail(400, "bad_request", "This conflict is already resolved.")
    if member.get("role") != "owner" and row["user_id"] != int(member["user_id"]):
        raise fail(403, "forbidden", "This conflict belongs to another user.")
    if body.resolution not in ("mine", "theirs", "merged"):
        raise fail(400, "bad_request", "resolution must be mine, theirs or merged.")

    seq: int | None = None
    if body.resolution != "theirs":
        if body.resolution == "mine":
            new_hash = (body.new_hash or "").strip() or row["client_hash"]
        else:  # merged
            new_hash = (body.new_hash or "").strip() or None
            if not new_hash:
                raise fail(400, "bad_request", "A merged resolution needs new_hash.")

        if new_hash is not None:
            try:
                new_hash = validate_hash(new_hash)
            except InvalidHash:
                raise fail(400, "bad_request", "new_hash must be a sha256 hex digest.")
            store = blob_store(request)
            if not store.exists(new_hash):
                raise fail(400, "bad_request", "The resolution blob does not exist.")

        path = row["path"]
        fc = classify(path)
        if fc.kind == "excluded":
            raise fail(400, "bad_request", "An excluded path cannot be written.")
        lock = _lock_row(conn, pid, fc.key)
        denial = write_denial(member, fc, member_done(conn, member), lock, utcnow())
        if denial == "locked":
            raise fail(409, "locked", "The file is locked by another user.")
        if denial is not None:
            raise fail(403, "forbidden", "You may not write this file.")

        with transaction(conn, immediate=True):
            seq = _write_file(
                conn,
                member,
                fc,
                path,
                blob_store(request),
                _head_hash(conn, pid, path) or row["server_hash"],
                new_hash,
                client_id=row["client_id"],
                kind="resolve",
            )
            conn.execute(
                "UPDATE conflicts SET state = 'resolved', resolution = ? WHERE id = ?",
                (body.resolution, conflict_id),
            )
            audit(
                conn,
                pid,
                int(member["user_id"]),
                "conflict.resolve",
                {"id": conflict_id, "resolution": body.resolution, "path": path},
            )
    else:
        with transaction(conn, immediate=True):
            conn.execute(
                "UPDATE conflicts SET state = 'resolved', resolution = 'theirs' WHERE id = ?",
                (conflict_id,),
            )
            audit(
                conn,
                pid,
                int(member["user_id"]),
                "conflict.resolve",
                {"id": conflict_id, "resolution": "theirs", "path": row["path"]},
            )

    fresh = conn.execute(
        "SELECT * FROM conflicts WHERE id = ?", (conflict_id,)
    ).fetchone()
    return {
        "ok": True,
        "seq": head_seq(conn, pid),
        "conflict": _conflict_public(fresh) if fresh is not None else None,
    }


__all__ = [
    "GZIP_MIN_BYTES",
    "blob_store",
    "head_seq",
    "manifest",
    "may_read_blob",
    "member_done",
    "record_blob",
    "router",
    "visibility_epoch",
]
