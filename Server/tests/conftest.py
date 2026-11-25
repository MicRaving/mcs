"""Shared fixtures: an app on a tmp data dir, accounts and bearer headers."""

from __future__ import annotations

# --- test isolation (owner report 2026-11-25): never the real app data -------
import os as _os  # noqa: E402
import sys as _sys  # noqa: E402

_GUARD_DIR = _os.path.abspath(_os.path.join(_os.path.dirname(__file__), '../../Scripts'))
if _GUARD_DIR not in _sys.path:
    _sys.path.insert(0, _GUARD_DIR)
import testing_guard as _testing_guard  # noqa: E402

_testing_guard.isolate()
_testing_guard.pytest_hooks(globals())
# ------------------------------------------------------------------------------

import hashlib
import sys
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parents[1]
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from studio_server import db  # noqa: E402
from studio_server.app import create_app  # noqa: E402
from studio_server.config import Config  # noqa: E402
from studio_server.security import hash_password  # noqa: E402

PROTOCOL_HEADERS = {"X-Studio-Protocol": "1"}

ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "admin-password"
CODER_USERNAME = "coder"
CODER_PASSWORD = "coder-password"
CODER2_USERNAME = "coder2"
CODER2_PASSWORD = "coder2-password"


@pytest.fixture
def cfg(tmp_path) -> Config:
    return Config(data_dir=tmp_path / "data")


@pytest.fixture
def app(cfg):
    return create_app(cfg)


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


def create_user(
    cfg: Config,
    username: str,
    password: str,
    *,
    is_admin: bool = False,
    display_name: str = "",
    email: str = "",
) -> dict:
    conn = db.connect(cfg.data_dir)
    try:
        return db.insert_user(
            conn,
            username,
            hash_password(password),
            display_name=display_name,
            email=email,
            is_admin=is_admin,
        )
    finally:
        conn.close()


def login(client: TestClient, username: str, password: str) -> str:
    response = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200, response.text
    return response.json()["token"]


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", **PROTOCOL_HEADERS}


@pytest.fixture
def admin(cfg) -> dict:
    return create_user(cfg, ADMIN_USERNAME, ADMIN_PASSWORD, is_admin=True)


@pytest.fixture
def admin_token(client, admin) -> str:
    return login(client, ADMIN_USERNAME, ADMIN_PASSWORD)


@pytest.fixture
def admin_headers(admin_token) -> dict[str, str]:
    return auth(admin_token)


@pytest.fixture
def coder(cfg) -> dict:
    return create_user(cfg, CODER_USERNAME, CODER_PASSWORD)


@pytest.fixture
def coder2(cfg) -> dict:
    return create_user(cfg, CODER2_USERNAME, CODER2_PASSWORD)


# ── sync-test helpers (additive; used by test_sync/test_visibility/…) ──


def put_member(
    client: TestClient,
    headers: dict[str, str],
    pid: str,
    user_id: int,
    *,
    role: str = "coder",
    tags: list[str] | None = None,
    tasks: list[str] | None = None,
) -> dict:
    response = client.put(
        f"/api/p/{pid}/members/{user_id}",
        json={
            "role": role,
            "tags": list(tags or []),
            "tasks": list(tasks or []),
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()["member"]


def join_project(
    client: TestClient,
    owner_headers: dict[str, str],
    pid: str,
    user: dict,
    username: str,
    password: str,
    *,
    role: str = "coder",
    tags: list[str] | None = None,
    tasks: list[str] | None = None,
) -> dict[str, str]:
    """Add *user* as a project member and return its bearer headers."""
    put_member(
        client, owner_headers, pid, user["id"], role=role, tags=tags, tasks=tasks
    )
    return auth(login(client, username, password))


def put_blob(
    client: TestClient, headers: dict[str, str], pid: str, data: bytes
) -> str:
    """Upload *data* as a blob; returns its sha256 (the blob name)."""
    digest = hashlib.sha256(data).hexdigest()
    response = client.put(
        f"/api/p/{pid}/blobs/{digest}", content=data, headers=headers
    )
    assert response.status_code == 200, response.text
    return digest


def push_entries(
    client: TestClient,
    headers: dict[str, str],
    pid: str,
    entries: list[dict],
    client_id: str = "test-client",
) -> dict:
    response = client.post(
        f"/api/p/{pid}/push",
        json={"client_id": client_id, "entries": entries},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


def push_file(
    client: TestClient,
    headers: dict[str, str],
    pid: str,
    path: str,
    data: bytes,
    base_hash: str | None = None,
    client_id: str = "test-client",
) -> dict:
    """Upload *data* and push it at *path*; returns the single entry result."""
    digest = put_blob(client, headers, pid, data)
    body = push_entries(
        client,
        headers,
        pid,
        [
            {
                "path": path,
                "base_hash": base_hash,
                "new_hash": digest,
                "size": len(data),
            }
        ],
        client_id=client_id,
    )
    return body["results"][0]


@pytest.fixture
def project(client, admin_headers) -> dict:
    response = client.post(
        "/api/projects", json={"name": "Meta-analysis"}, headers=admin_headers
    )
    assert response.status_code == 201, response.text
    return response.json()
