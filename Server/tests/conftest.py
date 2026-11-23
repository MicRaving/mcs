"""Shared fixtures: an app on a tmp data dir, accounts and bearer headers."""

from __future__ import annotations

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
def project(client, admin_headers) -> dict:
    response = client.post(
        "/api/projects", json={"name": "Meta-analysis"}, headers=admin_headers
    )
    assert response.status_code == 201, response.text
    return response.json()
