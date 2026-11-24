"""Login, tokens, sliding expiry, rate limiting, me, logout, password (§3.3)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from conftest import ADMIN_PASSWORD, ADMIN_USERNAME, CODER_PASSWORD, CODER_USERNAME
from studio_server import db
from studio_server.security import token_hash


def _iso(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) + delta).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_login_ok_returns_token_and_user(client, admin):
    response = client.post(
        "/api/auth/login",
        json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["token"]
    assert body["user"]["username"] == ADMIN_USERNAME
    assert body["user"]["is_admin"] is True
    assert "pw_hash" not in body["user"]
    assert body["expires_at"].endswith("Z")


def test_login_bad_password_and_unknown_user(client, admin):
    bad = client.post(
        "/api/auth/login",
        json={"username": ADMIN_USERNAME, "password": "wrong"},
    )
    assert bad.status_code == 401
    assert bad.json()["detail"] == "unauthorized"

    unknown = client.post(
        "/api/auth/login", json={"username": "nobody", "password": "whatever"}
    )
    assert unknown.status_code == 401
    assert unknown.json()["detail"] == "unauthorized"


def test_login_is_case_insensitive_for_username(client, admin):
    response = client.post(
        "/api/auth/login",
        json={"username": ADMIN_USERNAME.upper(), "password": ADMIN_PASSWORD},
    )
    assert response.status_code == 200


def test_disabled_user_cannot_login_or_use_token(client, cfg, admin, admin_headers):
    response = client.post(
        "/api/admin/users",
        json={"username": "temp", "password": "temp-password"},
        headers=admin_headers,
    )
    assert response.status_code == 201
    user_id = response.json()["user"]["id"]

    token = client.post(
        "/api/auth/login", json={"username": "temp", "password": "temp-password"}
    ).json()["token"]

    disabled = client.patch(
        f"/api/admin/users/{user_id}", json={"disabled": True}, headers=admin_headers
    )
    assert disabled.status_code == 200
    assert disabled.json()["user"]["disabled"] is True

    assert (
        client.post(
            "/api/auth/login", json={"username": "temp", "password": "temp-password"}
        ).status_code
        == 401
    )
    # the already-issued token died with the disable
    assert (
        client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code
        == 401
    )


def test_me_requires_token(client):
    response = client.get("/api/auth/me")
    assert response.status_code == 401
    response = client.get(
        "/api/auth/me", headers={"Authorization": "Bearer not-a-real-token"}
    )
    assert response.status_code == 401


def test_me_lists_my_projects(client, project, coder, admin_headers):
    added = client.put(
        f"/api/p/{project['id']}/members/{coder['id']}",
        json={"role": "coder", "tags": ["Anna"], "tasks": ["coding"]},
        headers=admin_headers,
    )
    assert added.status_code == 200

    token = client.post(
        "/api/auth/login", json={"username": CODER_USERNAME, "password": CODER_PASSWORD}
    ).json()["token"]
    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    body = me.json()
    assert body["user"]["username"] == CODER_USERNAME
    assert body["projects"] == [
        {"id": project["id"], "name": project["name"], "role": "coder"}
    ]


def test_token_expiry_and_sliding(client, cfg, admin_token):
    headers = {"Authorization": f"Bearer {admin_token}"}
    hashed = token_hash(admin_token)

    conn = db.connect(cfg.data_dir)
    try:
        # force the token close to expiry, then use it
        soon = _iso(timedelta(seconds=60))
        conn.execute("UPDATE tokens SET expires = ? WHERE token_hash = ?", (soon, hashed))
        conn.commit()
    finally:
        conn.close()

    assert client.get("/api/auth/me", headers=headers).status_code == 200

    conn = db.connect(cfg.data_dir)
    try:
        expires = conn.execute(
            "SELECT expires FROM tokens WHERE token_hash = ?", (hashed,)
        ).fetchone()[0]
    finally:
        conn.close()

    # slid back out to the full 30-day TTL
    moment = datetime.strptime(expires, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    remaining = moment - datetime.now(timezone.utc)
    assert remaining > timedelta(days=29)

    conn = db.connect(cfg.data_dir)
    try:
        conn.execute(
            "UPDATE tokens SET expires = ? WHERE token_hash = ?",
            (_iso(timedelta(seconds=-5)), hashed),
        )
        conn.commit()
    finally:
        conn.close()
    assert client.get("/api/auth/me", headers=headers).status_code == 401


def test_login_rate_limit_after_ten_failures(client, coder):
    for _ in range(10):
        response = client.post(
            "/api/auth/login",
            json={"username": CODER_USERNAME, "password": "wrong-password"},
        )
        assert response.status_code == 401

    blocked = client.post(
        "/api/auth/login",
        json={"username": CODER_USERNAME, "password": CODER_PASSWORD},
    )
    assert blocked.status_code == 429
    assert blocked.json()["detail"] == "rate_limited"
    assert int(blocked.headers["Retry-After"]) >= 1


def test_rate_limit_is_per_username(client, coder):
    for _ in range(10):
        client.post(
            "/api/auth/login",
            json={"username": CODER_USERNAME, "password": "wrong-password"},
        )
    other = client.post(
        "/api/auth/login",
        json={"username": "someone-else", "password": "wrong-password"},
    )
    assert other.status_code == 401  # not blocked for a different username


def test_logout_revokes_the_token(client, admin_token):
    headers = {"Authorization": f"Bearer {admin_token}"}
    assert client.post("/api/auth/logout", headers=headers).status_code == 200
    assert client.get("/api/auth/me", headers=headers).status_code == 401


def test_change_password(client, cfg, admin_token):
    headers = {"Authorization": f"Bearer {admin_token}"}
    second = client.post(
        "/api/auth/login",
        json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
    ).json()["token"]

    wrong = client.post(
        "/api/auth/password",
        json={"old": "nope", "new": "another-password"},
        headers=headers,
    )
    assert wrong.status_code == 400

    changed = client.post(
        "/api/auth/password",
        json={"old": ADMIN_PASSWORD, "new": "new-admin-password"},
        headers=headers,
    )
    assert changed.status_code == 200

    assert (
        client.post(
            "/api/auth/login",
            json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/api/auth/login",
            json={"username": ADMIN_USERNAME, "password": "new-admin-password"},
        ).status_code
        == 200
    )

    # the session that changed the password survives; the other one is revoked
    assert client.get("/api/auth/me", headers=headers).status_code == 200
    assert (
        client.get(
            "/api/auth/me", headers={"Authorization": f"Bearer {second}"}
        ).status_code
        == 401
    )
