"""Assignment endpoints (INTERFACES §3.9)."""

from __future__ import annotations

import pytest
from conftest import CODER_PASSWORD, CODER_USERNAME, join_project

from studio_server.assign import plan_assignment

KEYS = [f"K{i:03d}" for i in range(12)]
CODERS = [{"tag": "A", "share": 1}, {"tag": "B", "share": 1}, {"tag": "C", "share": 1}]


def plan_body(**overrides):
    body = {
        "stage": "coding",
        "keys": list(KEYS),
        "coders": [dict(coder) for coder in CODERS],
        "overlap_pct": 25,
        "overlap_mode": "pairs",
        "seed": 7,
    }
    body.update(overrides)
    return body


def preview(client, headers, pid, body):
    return client.post(f"/api/p/{pid}/assignments/preview", json=body, headers=headers)


def commit(client, headers, pid, body):
    return client.post(f"/api/p/{pid}/assignments/commit", json=body, headers=headers)


def test_preview_equals_the_pure_planner(client, project, admin_headers):
    pid = project["id"]
    response = preview(client, admin_headers, pid, plan_body())
    assert response.status_code == 200, response.text
    expected = plan_assignment(
        KEYS, [("A", 1), ("B", 1), ("C", 1)], 25, "pairs", 7
    )
    assert response.json() == expected


def test_preview_without_a_seed_fills_one_in(client, project, admin_headers):
    pid = project["id"]
    body = plan_body()
    body.pop("seed")
    first = preview(client, admin_headers, pid, body)
    assert first.status_code == 200, first.text
    seed = first.json()["params"]["seed"]
    assert isinstance(seed, int)

    # replaying the returned seed reproduces the plan exactly
    replay = preview(client, admin_headers, pid, plan_body(seed=seed))
    assert replay.json() == first.json()


def test_commit_stores_the_planned_rows(client, project, admin_headers):
    pid = project["id"]
    body = plan_body()
    expected = plan_assignment(KEYS, [("A", 1), ("B", 1), ("C", 1)], 25, "pairs", 7)

    response = commit(client, admin_headers, pid, body)
    assert response.status_code == 200, response.text
    run_id = response.json()["run_id"]
    assert run_id

    listed = client.get(f"/api/p/{pid}/assignments", headers=admin_headers)
    assert listed.status_code == 200
    rows = listed.json()["assignments"]
    pairs = {(row["key"], row["tag"]) for row in rows}
    assert pairs == {
        (key, tag) for key, tags in expected["assignments"].items() for tag in tags
    }
    assert all(row["run_id"] == run_id for row in rows)
    assert all(row["stage"] == "coding" for row in rows)

    # filters
    only_a = client.get(
        f"/api/p/{pid}/assignments", params={"tag": "A"}, headers=admin_headers
    ).json()["assignments"]
    assert only_a and all(row["tag"] == "A" for row in only_a)
    other_stage = client.get(
        f"/api/p/{pid}/assignments", params={"stage": "abstract"}, headers=admin_headers
    ).json()["assignments"]
    assert other_stage == []


def test_commit_is_reproducible_with_the_same_seed(client, project, admin_headers):
    pid = project["id"]
    first = commit(client, admin_headers, pid, plan_body()).json()["run_id"]
    second = commit(client, admin_headers, pid, plan_body()).json()["run_id"]
    assert first != second
    rows = client.get(
        f"/api/p/{pid}/assignments", headers=admin_headers
    ).json()["assignments"]
    first_rows = {(row["key"], row["tag"]) for row in rows if row["run_id"] == first}
    second_rows = {(row["key"], row["tag"]) for row in rows if row["run_id"] == second}
    assert first_rows == second_rows


def test_member_sees_only_its_own_assignments(client, project, cfg, admin_headers, coder):
    pid = project["id"]
    anna = join_project(
        client, admin_headers, pid, coder, CODER_USERNAME, CODER_PASSWORD,
        tags=["A"], tasks=["coding"],
    )
    commit(client, admin_headers, pid, plan_body())

    mine = client.get(f"/api/p/{pid}/assignments", headers=anna).json()["assignments"]
    assert all(row["tag"] == "A" for row in mine)

    everyone = client.get(f"/api/p/{pid}/assignments", headers=admin_headers).json()[
        "assignments"
    ]
    assert len(everyone) > len(mine)
    assert {(row["key"], row["tag"]) for row in mine} == {
        (row["key"], row["tag"]) for row in everyone if row["tag"] == "A"
    }

    # a tag the member does not carry yields nothing
    assert (
        client.get(
            f"/api/p/{pid}/assignments", params={"tag": "B"}, headers=anna
        ).json()["assignments"]
        == []
    )


def test_manual_run(client, project, admin_headers):
    pid = project["id"]
    response = client.post(
        f"/api/p/{pid}/assignments/manual",
        json={"stage": "coding", "keys": ["K1", "K2"], "tags": ["A", "B"]},
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text
    run_id = response.json()["run_id"]

    rows = client.get(
        f"/api/p/{pid}/assignments", params={"stage": "coding"}, headers=admin_headers
    ).json()["assignments"]
    assert {(row["key"], row["tag"]) for row in rows} == {
        ("K1", "A"),
        ("K1", "B"),
        ("K2", "A"),
        ("K2", "B"),
    }
    runs = client.get(f"/api/p/{pid}/assignments/runs", headers=admin_headers).json()["runs"]
    row = next(run for run in runs if run["run_id"] == run_id)
    assert row["params"]["mode"] == "manual"
    assert row["params"]["tags"] == ["A", "B"]
    assert row["undone"] is False


def test_manual_validation(client, project, admin_headers):
    pid = project["id"]
    assert (
        client.post(
            f"/api/p/{pid}/assignments/manual",
            json={"stage": "coding", "keys": [], "tags": ["A"]},
            headers=admin_headers,
        ).status_code
        == 400
    )
    assert (
        client.post(
            f"/api/p/{pid}/assignments/manual",
            json={"stage": "coding", "keys": ["K1"], "tags": []},
            headers=admin_headers,
        ).status_code
        == 400
    )


def test_runs_and_undo(client, project, admin_headers):
    pid = project["id"]
    run_id = commit(client, admin_headers, pid, plan_body()).json()["run_id"]

    runs = client.get(f"/api/p/{pid}/assignments/runs", headers=admin_headers).json()["runs"]
    row = next(run for run in runs if run["run_id"] == run_id)
    assert row["stage"] == "coding"
    assert row["params"]["seed"] == 7
    assert row["params"]["overlap_pct"] == 25

    undone = client.delete(f"/api/p/{pid}/assignments/runs/{run_id}", headers=admin_headers)
    assert undone.status_code == 200

    assert client.get(f"/api/p/{pid}/assignments", headers=admin_headers).json()[
        "assignments"
    ] == []
    runs = client.get(f"/api/p/{pid}/assignments/runs", headers=admin_headers).json()["runs"]
    assert next(run for run in runs if run["run_id"] == run_id)["undone"] is True

    again = client.delete(f"/api/p/{pid}/assignments/runs/{run_id}", headers=admin_headers)
    assert again.status_code == 400
    missing = client.delete(
        f"/api/p/{pid}/assignments/runs/does-not-exist", headers=admin_headers
    )
    assert missing.status_code == 404


def test_assignment_endpoints_are_owner_only(client, project, cfg, admin_headers, coder):
    pid = project["id"]
    anna = join_project(
        client, admin_headers, pid, coder, CODER_USERNAME, CODER_PASSWORD,
        tags=["A"], tasks=["coding"],
    )
    assert preview(client, anna, pid, plan_body()).status_code == 403
    assert commit(client, anna, pid, plan_body()).status_code == 403
    assert (
        client.post(
            f"/api/p/{pid}/assignments/manual",
            json={"stage": "coding", "keys": ["K1"], "tags": ["A"]},
            headers=anna,
        ).status_code
        == 403
    )
    assert client.get(f"/api/p/{pid}/assignments/runs", headers=anna).status_code == 403
    assert (
        client.delete(
            f"/api/p/{pid}/assignments/runs/x", headers=anna
        ).status_code
        == 403
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"coders": []},
        {"overlap_mode": "triples"},
        {"overlap_pct": 101},
        {"stage": "everything"},
        {"keys": ["K1", "K1"]},
    ],
)
def test_preview_validation(client, project, admin_headers, overrides):
    response = preview(client, admin_headers, project["id"], plan_body(**overrides))
    assert response.status_code == 400
    assert response.json()["detail"] == "bad_request"
