"""Low-memory fixes (Results/LOWMEM_server_assessment_measurements.md):
OWASP argon2 parameters with rehash on login, and indexed blob permission lookups."""

from __future__ import annotations

from argon2 import PasswordHasher

from studio_server import db
from studio_server.security import _hasher, needs_rehash

from conftest import login


def test_new_hashes_use_the_owasp_parameters():
    h = _hasher.hash("pw")
    assert "m=19456,t=2,p=1" in h


def test_an_old_default_hash_is_upgraded_on_login(cfg, client):
    old = PasswordHasher().hash("legacy-password")          # library defaults: 64 MiB, p=4
    assert needs_rehash(old)
    conn = db.connect(cfg.data_dir)
    try:
        user = db.insert_user(conn, "legacy", old)
    finally:
        conn.close()
    login(client, "legacy", "legacy-password")
    conn = db.connect(cfg.data_dir)
    try:
        stored = db.get_user_by_id(conn, int(user["id"]))["pw_hash"]
    finally:
        conn.close()
    assert stored != old and "m=19456,t=2,p=1" in stored
    login(client, "legacy", "legacy-password")               # the new hash verifies


def test_blob_permission_lookups_use_indexes(cfg, client):
    conn = db.connect(cfg.data_dir)
    try:
        for sql, args in (
            ("SELECT path FROM files WHERE project_id = ? AND hash = ?", ("p", "h")),
            ("SELECT path FROM changes WHERE project_id = ? AND new_hash = ?"
             " UNION SELECT path FROM changes WHERE project_id = ? AND base_hash = ?",
             ("p", "h", "p", "h")),
        ):
            plan = " ".join(str(tuple(r)) for r in conn.execute("EXPLAIN QUERY PLAN " + sql, args))
            assert "SCAN" not in plan.replace("SCAN SUBQUERY", ""), plan
    finally:
        conn.close()
