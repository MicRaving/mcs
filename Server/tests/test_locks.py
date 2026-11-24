"""Study-key locks (INTERFACES §3.7)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from conftest import CODER2_PASSWORD, CODER2_USERNAME, CODER_PASSWORD, CODER_USERNAME, join_project

import studio_server.api_locks as locks_module


def _join(client, admin_headers, pid, user, username, password, tags, tasks):
    return join_project(
        client, admin_headers, pid, user, username, password, tags=tags, tasks=tasks
    )


def _acquire(client, headers, pid, key="K1", **body):
    return client.post(f"/api/p/{pid}/locks/{key}", json=body, headers=headers)


def _future(hours: float) -> str:
    moment = datetime.now(timezone.utc) + timedelta(hours=hours)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _hours_between(start: str, end: str) -> float:
    parse = lambda value: datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(  # noqa: E731
        tzinfo=timezone.utc
    )
    return (parse(end) - parse(start)).total_seconds() / 3600.0


def test_acquire_list_and_release(client, project, cfg, admin_headers, coder):
    pid = project["id"]
    anna = _join(client, admin_headers, pid, coder, CODER_USERNAME, CODER_PASSWORD,
                 ["Anna"], ["settle"])
    response = _acquire(client, anna, pid, "K1", client_id="laptop")
    assert response.status_code == 200, response.text
    lock = response.json()
    assert lock["key"] == "K1"
    assert lock["user"] == "coder"
    assert lock["client_id"] == "laptop"
    assert lock["offline"] is False
    assert lock["expired"] is False
    assert not lock["expires"].startswith("2000")
    assert 23.9 < _hours_between(lock["acquired"], lock["expires"]) < 24.1

    listed = client.get(f"/api/p/{pid}/locks", headers=admin_headers)
    assert listed.status_code == 200
    assert isinstance(listed.json(), list)
    assert [row["key"] for row in listed.json()] == ["K1"]
    assert listed.json()[0]["display_name"] == ""

    released = client.delete(f"/api/p/{pid}/locks/K1", headers=anna)
    assert released.status_code == 200
    assert client.get(f"/api/p/{pid}/locks", headers=admin_headers).json() == []
    assert client.delete(f"/api/p/{pid}/locks/K1", headers=anna).status_code == 404


def test_two_users_race_one_wins(client, project, cfg, admin_headers, coder, coder2):
    pid = project["id"]
    anna = _join(client, admin_headers, pid, coder, CODER_USERNAME, CODER_PASSWORD,
                 ["Anna"], ["settle"])
    bob = _join(client, admin_headers, pid, coder2, CODER2_USERNAME, CODER2_PASSWORD,
                ["Bob"], ["settle"])

    first = _acquire(client, anna, pid, "K1", client_id="a")
    assert first.status_code == 200
    second = _acquire(client, bob, pid, "K1", client_id="b")
    assert second.status_code == 409
    body = second.json()
    assert body["detail"] == "locked"
    assert body["holder"]["user"] == "coder"
    assert body["holder"]["client_id"] == "a"

    # the holder may "acquire" again — that is a renewal
    renewed = _acquire(client, anna, pid, "K1", client_id="a2")
    assert renewed.status_code == 200
    assert renewed.json()["client_id"] == "a2"
    assert renewed.json()["acquired"] == first.json()["acquired"]


def test_expiry_lets_the_next_user_in(client, project, cfg, admin_headers, coder, coder2, monkeypatch):
    pid = project["id"]
    anna = _join(client, admin_headers, pid, coder, CODER_USERNAME, CODER_PASSWORD,
                 ["Anna"], ["settle"])
    bob = _join(client, admin_headers, pid, coder2, CODER2_USERNAME, CODER2_PASSWORD,
                ["Bob"], ["settle"])
    assert _acquire(client, anna, pid, "K1").status_code == 200

    # 25 h later the 24 h lock is gone
    monkeypatch.setattr(locks_module, "utcnow", lambda: _future(25))
    taken = _acquire(client, bob, pid, "K1")
    assert taken.status_code == 200, taken.text
    assert taken.json()["user"] == "coder2"


def test_offline_lease_is_capped(client, project, admin_headers, coder):
    pid = project["id"]
    anna = _join(client, admin_headers, pid, coder, CODER_USERNAME, CODER_PASSWORD,
                 ["Anna"], ["harmonize"])
    client.patch(
        f"/api/p/{pid}/settings",
        json={"max_offline_lease_h": 72},
        headers=admin_headers,
    )
    response = _acquire(client, anna, pid, "K1", offline_hours=1000)
    assert response.status_code == 200, response.text
    lock = response.json()
    assert lock["offline"] is True
    assert 71.9 < _hours_between(lock["acquired"], lock["expires"]) <= 72.1

    short = _acquire(client, anna, pid, "K2", offline_hours=5)
    assert short.status_code == 200
    assert 4.9 < _hours_between(short.json()["acquired"], short.json()["expires"]) < 5.1

    # a plain renewal returns to the online TTL
    online = _acquire(client, anna, pid, "K1")
    assert online.status_code == 200
    assert online.json()["offline"] is False
    assert 23.9 < _hours_between(online.json()["acquired"], online.json()["expires"]) < 24.1


def test_lock_ttl_setting_is_used(client, project, admin_headers, coder):
    pid = project["id"]
    anna = _join(client, admin_headers, pid, coder, CODER_USERNAME, CODER_PASSWORD,
                 ["Anna"], ["reproduce"])
    client.patch(
        f"/api/p/{pid}/settings", json={"lock_ttl_h": 12}, headers=admin_headers
    )
    lock = _acquire(client, anna, pid, "K1").json()
    assert 11.9 < _hours_between(lock["acquired"], lock["expires"]) < 12.1


def test_bad_offline_hours(client, project, admin_headers, coder):
    pid = project["id"]
    anna = _join(client, admin_headers, pid, coder, CODER_USERNAME, CODER_PASSWORD,
                 ["Anna"], ["settle"])
    assert _acquire(client, anna, pid, "K1", offline_hours=0).status_code == 400
    assert _acquire(client, anna, pid, "K1", offline_hours=-3).status_code == 400


def test_locking_needs_a_shared_write_task(client, project, cfg, admin_headers, coder):
    pid = project["id"]
    coder_only = _join(client, admin_headers, pid, coder, CODER_USERNAME, CODER_PASSWORD,
                       ["Anna"], ["coding"])
    refused = _acquire(client, coder_only, pid, "K1")
    assert refused.status_code == 403
    assert refused.json()["detail"] == "forbidden"

    # the owner always may
    assert _acquire(client, admin_headers, pid, "K1").status_code == 200


def test_force_release_is_owner_only(client, project, cfg, admin_headers, coder, coder2):
    pid = project["id"]
    anna = _join(client, admin_headers, pid, coder, CODER_USERNAME, CODER_PASSWORD,
                 ["Anna"], ["settle"])
    bob = _join(client, admin_headers, pid, coder2, CODER2_USERNAME, CODER2_PASSWORD,
                ["Bob"], ["settle"])
    assert _acquire(client, anna, pid, "K1").status_code == 200

    # another coder cannot release it, with or without force
    assert client.delete(f"/api/p/{pid}/locks/K1", headers=bob).status_code == 403
    assert (
        client.delete(f"/api/p/{pid}/locks/K1", params={"force": 1}, headers=bob).status_code
        == 403
    )
    # the owner must ask explicitly
    assert client.delete(f"/api/p/{pid}/locks/K1", headers=admin_headers).status_code == 403
    forced = client.delete(
        f"/api/p/{pid}/locks/K1", params={"force": 1}, headers=admin_headers
    )
    assert forced.status_code == 200
    assert client.get(f"/api/p/{pid}/locks", headers=admin_headers).json() == []

    audit = client.get(f"/api/p/{pid}/audit?limit=50", headers=admin_headers).json()["audit"]
    actions = [row["action"] for row in audit]
    assert "lock.acquire" in actions
    assert "lock.force_release" in actions


def test_holder_can_release_without_force(client, project, cfg, admin_headers, coder):
    pid = project["id"]
    anna = _join(client, admin_headers, pid, coder, CODER_USERNAME, CODER_PASSWORD,
                 ["Anna"], ["settle"])
    assert _acquire(client, anna, pid, "K1").status_code == 200
    assert client.delete(f"/api/p/{pid}/locks/K1", headers=anna).status_code == 200


@pytest.mark.parametrize("key", ["", " "])
def test_lock_key_validation(client, project, admin_headers, key):
    response = client.post(
        f"/api/p/{project['id']}/locks/%20", json={}, headers=admin_headers
    )
    # the client normalises empty keys to a blank path segment; either way the
    # server rejects it rather than creating a nameless lock
    assert response.status_code in (400, 404, 422)
    assert client.get(f"/api/p/{project['id']}/locks", headers=admin_headers).json() == []
