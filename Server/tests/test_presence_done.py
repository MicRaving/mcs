"""Presence heartbeats and done marks (INTERFACES §3.8)."""

from __future__ import annotations

from conftest import (
    CODER_PASSWORD,
    CODER_USERNAME,
    join_project,
)

from studio_server import db


def _join(client, admin_headers, pid, user, username, password, tags):
    return join_project(
        client, admin_headers, pid, user, username, password, tags=tags, tasks=["coding"]
    )


def _presence(client, headers, pid, client_id, **extra):
    return client.post(
        f"/api/p/{pid}/presence", json={"client_id": client_id, **extra}, headers=headers
    )


# ── presence ───────────────────────────────────────────────────────────


def test_presence_reports_the_other_clients(client, project, admin_headers, coder):
    pid = project["id"]
    anna = _join(client, admin_headers, pid, coder, CODER_USERNAME, CODER_PASSWORD, ["Anna"])

    first = _presence(
        client, admin_headers, pid, "owner-laptop", key="K1", tab="coding", app_version="1.2"
    )
    assert first.status_code == 200
    assert first.json() == {"others": []}

    second = _presence(client, anna, pid, "anna-laptop", key="K2", tab="screening")
    assert second.status_code == 200
    others = second.json()["others"]
    assert len(others) == 1
    assert others[0]["user"] == "admin"
    assert others[0]["key"] == "K1"
    assert others[0]["tab"] == "coding"
    assert others[0]["ts"].endswith("Z")

    # the owner now sees Anna; a repeated heartbeat does not duplicate
    again = _presence(client, anna, pid, "anna-laptop", key="K3")
    assert [row["user"] for row in again.json()["others"]] == ["admin"]
    assert again.status_code == 200
    seen = _presence(client, admin_headers, pid, "owner-laptop").json()["others"]
    assert [(row["user"], row["key"]) for row in seen] == [("coder", "K3")]


def test_presence_prunes_by_ttl(client, project, cfg, admin_headers):
    pid = project["id"]
    conn = db.connect(cfg.data_dir)
    try:
        conn.execute(
            "INSERT INTO presence (project_id, user_id, client_id, key, tab,"
            " app_version, ts) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (pid, 1, "stale-client", "K0", "coding", "0.1", "2000-01-01T00:00:00Z"),
        )
        conn.commit()
    finally:
        conn.close()

    response = _presence(client, admin_headers, pid, "owner-laptop")
    assert response.status_code == 200
    assert response.json()["others"] == []

    conn = db.connect(cfg.data_dir)
    try:
        rows = conn.execute(
            "SELECT client_id FROM presence WHERE project_id = ?", (pid,)
        ).fetchall()
    finally:
        conn.close()
    assert [row["client_id"] for row in rows] == ["owner-laptop"]


def test_presence_delete_on_clean_close(client, project, admin_headers, coder):
    pid = project["id"]
    anna = _join(client, admin_headers, pid, coder, CODER_USERNAME, CODER_PASSWORD, ["Anna"])
    _presence(client, admin_headers, pid, "owner-laptop")
    _presence(client, anna, pid, "anna-laptop")

    left = client.delete(f"/api/p/{pid}/presence/anna-laptop", headers=anna)
    assert left.status_code == 200
    assert _presence(client, admin_headers, pid, "owner-laptop").json()["others"] == []

    # a non-owner cannot delete somebody else's client row
    _presence(client, admin_headers, pid, "owner-laptop")
    other = client.delete(f"/api/p/{pid}/presence/owner-laptop", headers=anna)
    assert other.status_code == 200
    assert _presence(client, anna, pid, "anna-laptop").json()["others"] != []


def test_presence_requires_client_id(client, project, admin_headers):
    response = client.post(
        f"/api/p/{project['id']}/presence", json={"client_id": "  "}, headers=admin_headers
    )
    assert response.status_code == 400


# ── done ───────────────────────────────────────────────────────────────


def test_done_marks_and_filters(client, project, admin_headers, coder):
    pid = project["id"]
    anna = _join(client, admin_headers, pid, coder, CODER_USERNAME, CODER_PASSWORD, ["Anna"])

    marked = client.post(
        f"/api/p/{pid}/done",
        json={"key": "K1", "stage": "coding", "tag": "Anna"},
        headers=anna,
    )
    assert marked.status_code == 200, marked.text
    assert marked.json()["done"]["tag"] == "Anna"
    assert marked.json()["done"]["key"] == "K1"

    client.post(
        f"/api/p/{pid}/done",
        json={"key": "K2", "stage": "abstract", "tag": "Anna"},
        headers=anna,
    )
    # a repeat is idempotent, not a duplicate
    client.post(
        f"/api/p/{pid}/done",
        json={"key": "K1", "stage": "coding", "tag": "Anna"},
        headers=anna,
    )

    all_rows = client.get(f"/api/p/{pid}/done", headers=admin_headers).json()["done"]
    assert [(row["key"], row["stage"], row["tag"]) for row in all_rows] == [
        ("K1", "coding", "Anna"),
        ("K2", "abstract", "Anna"),
    ]
    by_tag = client.get(
        f"/api/p/{pid}/done", params={"tag": "Anna"}, headers=admin_headers
    ).json()["done"]
    assert len(by_tag) == 2
    by_stage = client.get(
        f"/api/p/{pid}/done", params={"stage": "abstract"}, headers=admin_headers
    ).json()["done"]
    assert [row["key"] for row in by_stage] == ["K2"]
    assert (
        client.get(f"/api/p/{pid}/done", params={"tag": "Nobody"}, headers=admin_headers)
        .json()["done"]
        == []
    )


def test_done_requires_own_tag_but_owner_marks_any(client, project, admin_headers, coder):
    pid = project["id"]
    anna = _join(client, admin_headers, pid, coder, CODER_USERNAME, CODER_PASSWORD, ["Anna"])

    refused = client.post(
        f"/api/p/{pid}/done",
        json={"key": "K1", "stage": "coding", "tag": "Bob"},
        headers=anna,
    )
    assert refused.status_code == 403
    assert refused.json()["detail"] == "forbidden"

    owner = client.post(
        f"/api/p/{pid}/done",
        json={"key": "K1", "stage": "coding", "tag": "Bob"},
        headers=admin_headers,
    )
    assert owner.status_code == 200


def test_done_validates_stage_and_key(client, project, admin_headers, coder):
    pid = project["id"]
    anna = _join(client, admin_headers, pid, coder, CODER_USERNAME, CODER_PASSWORD, ["Anna"])
    bad_stage = client.post(
        f"/api/p/{pid}/done",
        json={"key": "K1", "stage": "everything", "tag": "Anna"},
        headers=anna,
    )
    assert bad_stage.status_code == 400
    empty_key = client.post(
        f"/api/p/{pid}/done",
        json={"key": "", "stage": "coding", "tag": "Anna"},
        headers=anna,
    )
    assert empty_key.status_code == 400


def test_reopen_is_owner_only(client, project, admin_headers, coder):
    pid = project["id"]
    anna = _join(client, admin_headers, pid, coder, CODER_USERNAME, CODER_PASSWORD, ["Anna"])
    client.post(
        f"/api/p/{pid}/done",
        json={"key": "K1", "stage": "coding", "tag": "Anna"},
        headers=anna,
    )

    refused = client.request(
        "DELETE",
        f"/api/p/{pid}/done",
        json={"key": "K1", "stage": "coding", "tag": "Anna"},
        headers=anna,
    )
    assert refused.status_code == 403

    reopened = client.request(
        "DELETE",
        f"/api/p/{pid}/done",
        json={"key": "K1", "stage": "coding", "tag": "Anna"},
        headers=admin_headers,
    )
    assert reopened.status_code == 200
    assert reopened.json() == {"ok": True, "deleted": True}
    assert client.get(f"/api/p/{pid}/done", headers=admin_headers).json()["done"] == []


def test_done_changes_the_member_visibility_epoch(client, project, admin_headers, coder):
    pid = project["id"]
    anna = _join(client, admin_headers, pid, coder, CODER_USERNAME, CODER_PASSWORD, ["Anna"])
    before = client.get(f"/api/p/{pid}/manifest", headers=anna).json()["visibility_epoch"]
    client.post(
        f"/api/p/{pid}/done",
        json={"key": "K1", "stage": "coding", "tag": "Anna"},
        headers=anna,
    )
    after = client.get(f"/api/p/{pid}/manifest", headers=anna).json()["visibility_epoch"]
    assert after != before

    # another member's marks do not move this member's epoch
    bob = client.post(
        f"/api/p/{pid}/done",
        json={"key": "K1", "stage": "coding", "tag": "Bob"},
        headers=admin_headers,
    )
    assert bob.status_code == 200
    unchanged = client.get(f"/api/p/{pid}/manifest", headers=anna).json()["visibility_epoch"]
    assert unchanged == after
