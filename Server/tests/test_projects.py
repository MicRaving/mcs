"""Admin accounts, projects, settings, members and audit (§3.4)."""

from __future__ import annotations

from conftest import CODER_PASSWORD, CODER_USERNAME, auth, create_user, login
from studio_server import db, STAGES, TASKS


def _add_member(client, headers, pid, user_id, role="coder", tags=None, tasks=None):
    response = client.put(
        f"/api/p/{pid}/members/{user_id}",
        json={
            "role": role,
            "tags": ["Anna"] if tags is None else tags,
            "tasks": ["coding"] if tasks is None else tasks,
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()["member"]


def _coder_token(client, pid, coder, admin_headers):
    _add_member(client, admin_headers, pid, coder["id"])
    return login(client, CODER_USERNAME, CODER_PASSWORD)


# ── admin accounts ─────────────────────────────────────────────────────


def test_admin_creates_and_edits_users(client, admin_headers):
    created = client.post(
        "/api/admin/users",
        json={
            "username": "anna",
            "display_name": "Anna",
            "email": "anna@example.org",
            "password": "anna-password",
            "is_admin": False,
        },
        headers=admin_headers,
    )
    assert created.status_code == 201, created.text
    user = created.json()["user"]
    assert user["username"] == "anna"
    assert user["is_admin"] is False
    assert user["disabled"] is False

    listed = client.get("/api/admin/users", headers=admin_headers)
    assert listed.status_code == 200
    assert {row["username"] for row in listed.json()["users"]} >= {"admin", "anna"}

    edited = client.patch(
        f"/api/admin/users/{user['id']}",
        json={"display_name": "Anna B.", "is_admin": True},
        headers=admin_headers,
    )
    assert edited.status_code == 200
    assert edited.json()["user"]["display_name"] == "Anna B."
    assert edited.json()["user"]["is_admin"] is True

    assert login(client, "anna", "anna-password")


def test_duplicate_username_is_conflict(client, admin_headers):
    payload = {"username": "dup", "password": "dup-password"}
    assert client.post("/api/admin/users", json=payload, headers=admin_headers).status_code == 201
    again = client.post("/api/admin/users", json=payload, headers=admin_headers)
    assert again.status_code == 409
    assert again.json()["detail"] == "conflict"


def test_non_admin_cannot_manage_users(client, project, coder):
    headers = auth(login(client, CODER_USERNAME, CODER_PASSWORD))
    assert client.get("/api/admin/users", headers=headers).status_code == 403
    response = client.post(
        "/api/admin/users",
        json={"username": "sneaky", "password": "sneaky-password"},
        headers=headers,
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "forbidden"


# ── projects ───────────────────────────────────────────────────────────


def test_create_project_makes_creator_owner(client, project, admin_headers, admin):
    assert project["id"]
    assert project["name"] == "Meta-analysis"

    detail = client.get(f"/api/p/{project['id']}", headers=admin_headers)
    assert detail.status_code == 200
    body = detail.json()
    assert body["id"] == project["id"]
    assert body["protocol"] == 1
    assert body["me"]["role"] == "owner"
    assert set(body["me"]["tasks"]) == set(TASKS)

    members = client.get(f"/api/p/{project['id']}/members", headers=admin_headers).json()[
        "members"
    ]
    mine = [m for m in members if m["user_id"] == admin["id"]]
    assert len(mine) == 1
    assert mine[0]["role"] == "owner"


def test_project_settings_defaults(client, project, admin_headers):
    settings = client.get(f"/api/p/{project['id']}", headers=admin_headers).json()["settings"]
    assert settings["blinding"] == {stage: "blinded" for stage in STAGES}
    assert settings["lock_ttl_h"] == 24
    assert settings["max_offline_lease_h"] == 72


def test_non_admin_cannot_create_project(client, coder):
    headers = auth(login(client, CODER_USERNAME, CODER_PASSWORD))
    response = client.post("/api/projects", json={"name": "Nope"}, headers=headers)
    assert response.status_code == 403


def test_non_member_cannot_read_project(client, project, coder):
    headers = auth(login(client, CODER_USERNAME, CODER_PASSWORD))
    response = client.get(f"/api/p/{project['id']}", headers=headers)
    assert response.status_code == 403
    assert response.json()["detail"] == "forbidden"

    missing = client.get("/api/p/does-not-exist", headers=headers)
    assert missing.status_code == 404
    assert missing.json()["detail"] == "not_found"


def test_member_sees_project_with_own_permissions(client, project, coder, admin_headers):
    token = _coder_token(client, project["id"], coder, admin_headers)
    detail = client.get(f"/api/p/{project['id']}", headers=auth(token))
    assert detail.status_code == 200
    me = detail.json()["me"]
    assert me["role"] == "coder"
    assert me["tags"] == ["Anna"]
    assert me["tasks"] == ["coding"]

    projects = client.get("/api/projects", headers=auth(token)).json()["projects"]
    assert projects == [{"id": project["id"], "name": project["name"], "role": "coder"}]


def test_settings_patch_merges_and_is_owner_only(client, project, coder, admin_headers):
    patched = client.patch(
        f"/api/p/{project['id']}/settings",
        json={"blinding": {"coding": "visible"}, "lock_ttl_h": 12},
        headers=admin_headers,
    )
    assert patched.status_code == 200, patched.text
    settings = patched.json()["settings"]
    assert settings["blinding"]["coding"] == "visible"
    assert settings["blinding"]["abstract"] == "blinded"
    assert settings["blinding"]["fulltext"] == "blinded"
    assert settings["lock_ttl_h"] == 12
    assert settings["max_offline_lease_h"] == 72

    # stored, not just echoed
    again = client.get(f"/api/p/{project['id']}", headers=admin_headers).json()["settings"]
    assert again["blinding"]["coding"] == "visible"

    token = _coder_token(client, project["id"], coder, admin_headers)
    forbidden = client.patch(
        f"/api/p/{project['id']}/settings",
        json={"lock_ttl_h": 1},
        headers=auth(token),
    )
    assert forbidden.status_code == 403


def test_settings_validation(client, project, admin_headers):
    bad_value = client.patch(
        f"/api/p/{project['id']}/settings",
        json={"blinding": {"coding": "sometimes"}},
        headers=admin_headers,
    )
    assert bad_value.status_code == 400
    assert bad_value.json()["detail"] == "bad_request"

    bad_ttl = client.patch(
        f"/api/p/{project['id']}/settings",
        json={"lock_ttl_h": 0},
        headers=admin_headers,
    )
    assert bad_ttl.status_code == 400


# ── members ────────────────────────────────────────────────────────────


def test_members_put_and_delete_are_owner_only(client, project, coder, admin_headers):
    member = _add_member(client, admin_headers, project["id"], coder["id"])
    assert member["role"] == "coder"
    assert member["tags"] == ["Anna"]
    assert member["tasks"] == ["coding"]

    token = auth(login(client, CODER_USERNAME, CODER_PASSWORD))
    denied_put = client.put(
        f"/api/p/{project['id']}/members/{coder['id']}",
        json={"role": "owner", "tags": [], "tasks": []},
        headers=token,
    )
    assert denied_put.status_code == 403
    denied_delete = client.delete(
        f"/api/p/{project['id']}/members/{coder['id']}", headers=token
    )
    assert denied_delete.status_code == 403

    promoted = client.put(
        f"/api/p/{project['id']}/members/{coder['id']}",
        json={"role": "viewer", "tags": ["Anna", "Bob"], "tasks": ["settle"]},
        headers=admin_headers,
    )
    assert promoted.status_code == 200
    assert promoted.json()["member"]["role"] == "viewer"
    assert promoted.json()["member"]["tags"] == ["Anna", "Bob"]
    assert promoted.json()["member"]["tasks"] == ["settle"]

    removed = client.delete(
        f"/api/p/{project['id']}/members/{coder['id']}", headers=admin_headers
    )
    assert removed.status_code == 200
    members = client.get(f"/api/p/{project['id']}/members", headers=admin_headers).json()[
        "members"
    ]
    assert all(m["user_id"] != coder["id"] for m in members)


def test_member_payload_validation(client, project, coder, admin_headers):
    bad_role = client.put(
        f"/api/p/{project['id']}/members/{coder['id']}",
        json={"role": "editor", "tags": [], "tasks": []},
        headers=admin_headers,
    )
    assert bad_role.status_code == 400

    bad_task = client.put(
        f"/api/p/{project['id']}/members/{coder['id']}",
        json={"role": "coder", "tags": [], "tasks": ["teleport"]},
        headers=admin_headers,
    )
    assert bad_task.status_code == 400

    unknown_user = client.put(
        f"/api/p/{project['id']}/members/99999",
        json={"role": "coder", "tags": [], "tasks": []},
        headers=admin_headers,
    )
    assert unknown_user.status_code == 404


def test_last_owner_cannot_be_removed_or_demoted(client, project, admin, admin_headers):
    removed = client.delete(
        f"/api/p/{project['id']}/members/{admin['id']}", headers=admin_headers
    )
    assert removed.status_code == 400

    demoted = client.put(
        f"/api/p/{project['id']}/members/{admin['id']}",
        json={"role": "coder", "tags": [], "tasks": []},
        headers=admin_headers,
    )
    assert demoted.status_code == 400


def test_server_admin_is_owner_of_every_project(client, cfg, project):
    other = create_user(cfg, "root2", "root2-password", is_admin=True)
    headers = auth(login(client, "root2", "root2-password"))

    detail = client.get(f"/api/p/{project['id']}", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["me"]["role"] == "owner"
    assert set(detail.json()["me"]["tasks"]) == set(TASKS)

    listed = client.get("/api/projects", headers=headers).json()["projects"]
    assert {"id": project["id"], "name": project["name"], "role": "owner"} in listed


# ── audit ──────────────────────────────────────────────────────────────


def test_audit_records_member_and_settings_changes(
    client, project, coder, admin_headers
):
    client.patch(
        f"/api/p/{project['id']}/settings",
        json={"max_offline_lease_h": 48},
        headers=admin_headers,
    )
    _add_member(client, admin_headers, project["id"], coder["id"])
    client.delete(
        f"/api/p/{project['id']}/members/{coder['id']}", headers=admin_headers
    )

    response = client.get(f"/api/p/{project['id']}/audit?limit=50", headers=admin_headers)
    assert response.status_code == 200
    entries = response.json()["audit"]
    actions = [entry["action"] for entry in entries]
    assert "project.create" in actions
    assert "settings.update" in actions
    assert "member.set" in actions
    assert "member.remove" in actions
    assert all(entry["ts"].endswith("Z") for entry in entries)

    settings_entry = next(e for e in entries if e["action"] == "settings.update")
    assert settings_entry["detail"] == {"changed": ["max_offline_lease_h"]}

    limited = client.get(f"/api/p/{project['id']}/audit?limit=2", headers=admin_headers)
    assert len(limited.json()["audit"]) == 2


def test_audit_is_owner_only(client, project, coder, admin_headers):
    token = _coder_token(client, project["id"], coder, admin_headers)
    assert (
        client.get(f"/api/p/{project['id']}/audit", headers=auth(token)).status_code
        == 403
    )


def test_audit_detail_survives_plain_text(client, cfg, project, admin_headers):
    conn = db.connect(cfg.data_dir)
    try:
        db.audit(conn, project["id"], None, "import", "plain text detail")
    finally:
        conn.close()
    entries = client.get(
        f"/api/p/{project['id']}/audit", headers=admin_headers
    ).json()["audit"]
    entry = next(e for e in entries if e["action"] == "import")
    assert entry["detail"] == "plain text detail"
