"""Schema, transactions and the db.py helpers (INTERFACES §3.1, §3.2)."""

from __future__ import annotations

import json

import pytest

from studio_server import ALL_TASKS
from studio_server import db
from studio_server.security import hash_password

EXPECTED_TABLES = {
    "users",
    "tokens",
    "projects",
    "members",
    "files",
    "changes",
    "blobs",
    "locks",
    "presence",
    "done",
    "assignment_runs",
    "assignments",
    "conflicts",
    "audit",
}


def test_init_db_sets_user_version_and_tables(tmp_path):
    data_dir = tmp_path / "data"
    version = db.init_db(data_dir)
    assert version == db.SCHEMA_VERSION == 1
    assert (data_dir / "studio.db").exists()

    conn = db.connect(data_dir)
    try:
        assert db.get_schema_version(conn) == 1
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert EXPECTED_TABLES <= names
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    finally:
        conn.close()


def test_init_db_is_idempotent(tmp_path):
    assert db.init_db(tmp_path) == 1
    db.init_db(tmp_path)
    conn = db.connect(tmp_path)
    try:
        assert db.get_schema_version(conn) == 1
    finally:
        conn.close()


@pytest.fixture
def conn(tmp_path):
    db.init_db(tmp_path)
    connection = db.connect(tmp_path)
    yield connection
    connection.close()


def _change(conn, project_id, seq, path="Codings/X - A.json", kind="push"):
    conn.execute(
        "INSERT INTO changes (project_id, seq, path, base_hash, new_hash, user_id,"
        " client_id, ts, kind) VALUES (?, ?, ?, NULL, 'h', 1, 'c1', ?, ?)",
        (project_id, seq, path, db.utcnow(), kind),
    )


def test_next_seq_counts_per_project(conn):
    assert db.next_seq(conn, "p1") == 1
    with db.transaction(conn, immediate=True):
        seq = db.next_seq(conn, "p1")
        _change(conn, "p1", seq)
    assert db.next_seq(conn, "p1") == 2
    assert db.next_seq(conn, "p2") == 1


def test_transaction_rolls_back(conn):
    with db.transaction(conn, immediate=True):
        _change(conn, "p1", db.next_seq(conn, "p1"))
    assert db.next_seq(conn, "p1") == 2

    with pytest.raises(RuntimeError):
        with db.transaction(conn, immediate=True):
            _change(conn, "p1", db.next_seq(conn, "p1"))
            raise RuntimeError("boom")

    assert db.next_seq(conn, "p1") == 2
    count = conn.execute("SELECT COUNT(*) FROM changes").fetchone()[0]
    assert count == 1


def test_audit_writes_and_serialises_detail(conn):
    db.audit(conn, "p1", 7, "settings.update", {"changed": ["lock_ttl_h"]})
    row = conn.execute("SELECT * FROM audit ORDER BY id DESC").fetchone()
    assert row["project_id"] == "p1"
    assert row["user_id"] == 7
    assert row["action"] == "settings.update"
    assert json.loads(row["detail"]) == {"changed": ["lock_ttl_h"]}
    assert row["ts"].endswith("Z")

    # inside an explicit transaction the caller commits
    with db.transaction(conn, immediate=True):
        db.audit(conn, "p1", 7, "second", None)
    assert conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0] == 2


def test_get_member_is_effective(conn):
    owner = db.insert_user(conn, "owner", hash_password("pw-owner"), is_admin=False)
    coder = db.insert_user(conn, "coder", hash_password("pw-coder"))
    root = db.insert_user(conn, "root", hash_password("pw-root"), is_admin=True)
    project = db.create_project(conn, "pid-1", "P", int(owner["id"]))

    assert db.get_member(conn, project["id"], int(coder["id"])) is None

    db.upsert_member(conn, project["id"], int(coder["id"]), "coder", ["Anna"], ["coding"])
    member = db.get_member(conn, project["id"], int(coder["id"]))
    assert member["role"] == "coder"
    assert member["tags"] == ["Anna"]
    assert member["tasks"] == ["coding"]
    assert db.has_tag(member, "Anna")
    assert not db.has_tag(member, "Bob")
    assert db.has_task(member, "coding")
    assert not db.has_task(member, "settle")

    owner_member = db.get_member(conn, project["id"], int(owner["id"]))
    assert owner_member["role"] == "owner"
    assert owner_member["tasks"] == list(ALL_TASKS)
    assert owner_member["tags"] == ["*"]
    assert db.has_tag(owner_member, "Anyone")

    admin_member = db.get_member(conn, project["id"], int(root["id"]))
    assert admin_member["role"] == "owner"
    assert admin_member["tasks"] == list(ALL_TASKS)


def test_cli_create_admin_and_reset_password(tmp_path):
    from studio_server.__main__ import main
    from studio_server.security import verify_password

    data = str(tmp_path / "cli")
    assert main(["create-admin", "--username", "root", "--password", "pw1", "--data", data]) == 0

    connection = db.connect(data)
    try:
        user = db.get_user_by_username(connection, "root")
        assert user is not None and user["is_admin"] == 1
        assert verify_password("pw1", user["pw_hash"])
    finally:
        connection.close()

    assert main(["reset-password", "--username", "root", "--password", "pw2", "--data", data]) == 0
    connection = db.connect(data)
    try:
        user = db.get_user_by_username(connection, "root")
        assert verify_password("pw2", user["pw_hash"])
    finally:
        connection.close()

    assert main(["reset-password", "--username", "nobody", "--password", "pw2", "--data", data]) == 1


def test_cli_create_admin_reads_installer_env(tmp_path, monkeypatch):
    from studio_server.__main__ import main

    data = str(tmp_path / "cli-env")
    monkeypatch.setenv("ADMIN_USER", "envadmin")
    monkeypatch.setenv("ADMIN_PASSWORD", "env-password")
    assert main(["create-admin", "--data", data]) == 0

    connection = db.connect(data)
    try:
        user = db.get_user_by_username(connection, "envadmin")
        assert user is not None and user["is_admin"] == 1
    finally:
        connection.close()


def test_project_settings_defaults_and_merge(conn):
    project = db.create_project(conn, "pid-2", "P2", 1)
    settings = project["settings"]
    assert settings["blinding"] == {
        "abstract": "blinded",
        "fulltext": "blinded",
        "coding": "blinded",
    }
    assert settings["lock_ttl_h"] == 24
    assert settings["max_offline_lease_h"] == 72

    merged = db.merge_settings(settings, {"blinding": {"coding": "visible"}})
    assert merged["blinding"]["coding"] == "visible"
    assert merged["blinding"]["abstract"] == "blinded"
    assert merged["lock_ttl_h"] == 24
    # merge_settings does not mutate its inputs
    assert settings["blinding"]["coding"] == "blinded"
