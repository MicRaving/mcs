"""Blobs/manifest/push/conflicts integration tests (INTERFACES §3.6)."""

from __future__ import annotations

import hashlib
import json
import time

from conftest import (
    CODER2_PASSWORD,
    CODER2_USERNAME,
    CODER_PASSWORD,
    CODER_USERNAME,
    join_project,
    push_entries,
    push_file,
    put_blob,
)

from studio_server import db
from studio_server.blobs import BlobStore


def manifest(client, headers, pid, since=0):
    response = client.get(
        f"/api/p/{pid}/manifest", params={"since": since}, headers=headers
    )
    assert response.status_code == 200, response.text
    return response.json()


def paths(body):
    return [entry["path"] for entry in body["files"]]


def entry(body, path):
    return next(row for row in body["files"] if row["path"] == path)


def push_one(client, headers, pid, path, data, base_hash=None):
    digest = put_blob(client, headers, pid, data)
    body = push_entries(
        client,
        headers,
        pid,
        [{"path": path, "base_hash": base_hash, "new_hash": digest, "size": len(data)}],
    )
    return digest, body["results"][0], body["seq"]


# ── manifest / accepted ────────────────────────────────────────────────


def test_manifest_starts_empty(client, project, admin_headers):
    body = manifest(client, admin_headers, project["id"])
    assert body["seq"] == 0
    assert body["files"] == []
    assert isinstance(body["visibility_epoch"], str) and body["visibility_epoch"]


def test_accepted_push_increments_seq_and_appears_in_since(client, project, admin_headers):
    pid = project["id"]
    data = b'{"Records": []}\n'
    digest, result, head = push_one(client, admin_headers, pid, "Extraction/K1.json", data)
    assert result == {"path": "Extraction/K1.json", "status": "accepted", "seq": 1, "hash": digest}
    assert head == 1

    first = manifest(client, admin_headers, pid)
    assert first["seq"] == 1
    row = entry(first, "Extraction/K1.json")
    assert row["hash"] == digest
    assert row["size"] == len(data)
    assert row["kind"] == "shared"
    assert row["key"] == "K1"
    assert row["seq"] == 1
    assert row["deleted"] is False

    _, second, _ = push_one(client, admin_headers, pid, "Extraction/K2.json", b"{}")
    assert second["seq"] == 2
    delta = manifest(client, admin_headers, pid, since=1)
    assert paths(delta) == ["Extraction/K2.json"]
    assert delta["seq"] == 2


def test_delete_marks_deleted_in_manifest(client, project, admin_headers):
    pid = project["id"]
    digest, _, _ = push_one(client, admin_headers, pid, "Extraction/Gone.json", b"{}")
    result = push_entries(
        client,
        admin_headers,
        pid,
        [{"path": "Extraction/Gone.json", "base_hash": digest, "new_hash": None}],
    )["results"][0]
    assert result["status"] == "accepted"
    assert result["hash"] is None

    row = entry(manifest(client, admin_headers, pid), "Extraction/Gone.json")
    assert row["deleted"] is True
    assert row["hash"] is None
    assert row["seq"] == 2


def test_re_push_of_a_landed_version_is_a_noop(client, project, cfg, admin_headers):
    pid = project["id"]
    path = "Extraction/Idem.json"
    data = b'{"v": 1}'
    digest = put_blob(client, admin_headers, pid, data)
    first = push_entries(
        client,
        admin_headers,
        pid,
        [{"path": path, "base_hash": None, "new_hash": digest}],
    )
    assert first["results"][0] == {
        "path": path,
        "status": "accepted",
        "seq": 1,
        "hash": digest,
    }
    assert first["seq"] == 1

    # a retry that carries the base it now believes in
    retry = push_entries(
        client,
        admin_headers,
        pid,
        [{"path": path, "base_hash": digest, "new_hash": digest}],
    )
    assert retry["results"][0] == {
        "path": path,
        "status": "accepted",
        "seq": 1,
        "hash": digest,
    }
    assert retry["seq"] == 1

    # a retry whose response was lost before the local head was recorded
    stale_retry = push_entries(
        client,
        admin_headers,
        pid,
        [{"path": path, "base_hash": None, "new_hash": digest}],
    )
    assert stale_retry["results"][0]["seq"] == 1
    assert stale_retry["seq"] == 1

    # deleting twice is also one change
    deleted = push_entries(
        client,
        admin_headers,
        pid,
        [{"path": path, "base_hash": digest, "new_hash": None}],
    )
    assert deleted["results"][0] == {
        "path": path,
        "status": "accepted",
        "seq": 2,
        "hash": None,
    }
    delete_again = push_entries(
        client,
        admin_headers,
        pid,
        [{"path": path, "base_hash": digest, "new_hash": None}],
    )
    assert delete_again["results"][0] == {
        "path": path,
        "status": "accepted",
        "seq": 2,
        "hash": None,
    }
    assert delete_again["seq"] == 2

    conn = db.connect(cfg.data_dir)
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM changes WHERE project_id = ? AND path = ?",
            (pid, path),
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 2


def test_merge_that_lands_on_the_server_version_is_a_noop(client, project, cfg, admin_headers):
    pid = project["id"]
    path = "Extraction/MergeNoop.json"
    base = json.dumps({"a": 1, "b": 2}, indent=2).encode() + b"\n"
    digest, _, _ = push_one(client, admin_headers, pid, path, base)
    server = json.dumps({"a": 1, "b": 3}, indent=2).encode() + b"\n"
    _, result, _ = push_one(client, admin_headers, pid, path, server, base_hash=digest)
    head_hash = result["hash"]

    # the client sends its untouched base against the stale base: the merge
    # result is exactly the server head, so no change row is written
    body = push_entries(
        client,
        admin_headers,
        pid,
        [{"path": path, "base_hash": digest, "new_hash": digest}],
    )
    merged = body["results"][0]
    assert merged == {
        "path": path,
        "status": "merged",
        "seq": 2,
        "hash": head_hash,
    }
    assert body["seq"] == 2

    conn = db.connect(cfg.data_dir)
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM changes WHERE project_id = ? AND path = ?",
            (pid, path),
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 2


def test_conflict_and_rejected_results_carry_the_current_head_hash(
    client, project, cfg, admin_headers, coder
):
    pid = project["id"]
    path = "Extraction/Head.json"
    _, result, _ = push_one(client, admin_headers, pid, path, b'{"v": 1}')
    head_hash = result["hash"]

    # a conflict reports the untouched server head
    other = put_blob(client, admin_headers, pid, b'{"v": 2}')
    conflict = push_entries(
        client,
        admin_headers,
        pid,
        [{"path": path, "base_hash": None, "new_hash": other}],
    )["results"][0]
    assert conflict["status"] == "conflict"
    assert conflict["hash"] == head_hash

    # a missing blob on a path that has a head reports that head too
    missing = push_entries(
        client,
        admin_headers,
        pid,
        [{"path": path, "base_hash": head_hash, "new_hash": "b" * 64}],
    )["results"][0]
    assert missing["status"] == "rejected"
    assert missing["reason"] == "missing_blob"
    assert missing["hash"] == head_hash

    # a forbidden write reports the head the client must restore
    owner_push = push_file(
        client, admin_headers, pid, "Results/reanalysis.xlsx", b"owner xlsx"
    )
    assert owner_push["status"] == "accepted"
    anna = join_project(
        client, admin_headers, pid, coder, CODER_USERNAME, CODER_PASSWORD,
        tags=["Anna"], tasks=["coding"],
    )
    coder_blob = put_blob(client, admin_headers, pid, b"coder xlsx")
    refused = push_entries(
        client,
        anna,
        pid,
        [{"path": "Results/reanalysis.xlsx", "base_hash": None, "new_hash": coder_blob}],
    )["results"][0]
    assert refused["status"] == "rejected"
    assert refused["reason"] == "forbidden"
    assert refused["hash"] == owner_push["hash"]


# ── rejections ─────────────────────────────────────────────────────────


def test_excluded_path_rejected(client, project, admin_headers):
    pid = project["id"]
    _, result, head = push_one(client, admin_headers, pid, "temp/scratch.json", b"{}")
    assert result == {
        "path": "temp/scratch.json",
        "status": "rejected",
        "reason": "excluded",
        "hash": None,
    }
    assert head == 0
    assert manifest(client, admin_headers, pid)["files"] == []


def test_non_owner_cannot_push_derived(client, project, cfg, admin_headers, coder):
    pid = project["id"]
    anna = join_project(
        client, admin_headers, pid, coder, CODER_USERNAME, CODER_PASSWORD,
        tags=["Anna"], tasks=["coding"],
    )
    digest = put_blob(client, admin_headers, pid, b"xlsx-ish")
    result = push_entries(
        client,
        anna,
        pid,
        [{"path": "Results/reanalysis.xlsx", "base_hash": None, "new_hash": digest}],
    )["results"][0]
    assert result["status"] == "rejected"
    assert result["reason"] == "forbidden"
    assert manifest(client, admin_headers, pid)["files"] == []


def test_owned_file_needs_its_own_tag(client, project, cfg, admin_headers, coder):
    pid = project["id"]
    anna = join_project(
        client, admin_headers, pid, coder, CODER_USERNAME, CODER_PASSWORD,
        tags=["Anna"], tasks=["coding"],
    )
    digest = put_blob(client, admin_headers, pid, b'{"mine": false}')
    wrong_tag = push_entries(
        client,
        anna,
        pid,
        [{"path": "Codings/K1 - Bob.json", "base_hash": None, "new_hash": digest}],
    )["results"][0]
    assert wrong_tag["status"] == "rejected"
    assert wrong_tag["reason"] == "forbidden"

    own_tag = push_entries(
        client,
        anna,
        pid,
        [{"path": "Codings/K1 - Anna.json", "base_hash": None, "new_hash": digest}],
    )["results"][0]
    assert own_tag["status"] == "accepted"


def test_missing_blob_rejected(client, project, admin_headers):
    pid = project["id"]
    absent = "a" * 64
    body = push_entries(
        client,
        admin_headers,
        pid,
        [{"path": "Extraction/Nope.json", "base_hash": None, "new_hash": absent}],
    )
    assert body["results"][0] == {
        "path": "Extraction/Nope.json",
        "status": "rejected",
        "reason": "missing_blob",
        "hash": None,
    }
    assert body["seq"] == 0


# ── visibility through the manifest ────────────────────────────────────


def test_blinding_hides_other_tag_until_done_then_epoch_changes(
    client, project, cfg, admin_headers, coder
):
    pid = project["id"]
    anna = join_project(
        client, admin_headers, pid, coder, CODER_USERNAME, CODER_PASSWORD,
        tags=["Anna"], tasks=["coding"],
    )
    push_one(client, admin_headers, pid, "Codings/K1 - Anna.json", b'{"tag": "Anna"}')
    push_one(client, admin_headers, pid, "Codings/K1 - Bob.json", b'{"tag": "Bob"}')

    before = manifest(client, admin_headers, pid)
    before_anna = manifest(client, anna, pid)
    assert paths(before_anna) == ["Codings/K1 - Anna.json"]

    head_before = before_anna["seq"]
    done = client.post(
        f"/api/p/{pid}/done",
        json={"key": "K1", "stage": "coding", "tag": "Anna"},
        headers=anna,
    )
    assert done.status_code == 200, done.text

    after_anna = manifest(client, anna, pid)
    assert paths(after_anna) == [
        "Codings/K1 - Anna.json",
        "Codings/K1 - Bob.json",
    ]
    assert after_anna["visibility_epoch"] != before_anna["visibility_epoch"]
    # a re-request with the old `since` cannot see it (it is an old seq) — the
    # changed epoch is what makes the client pull with since=0
    assert paths(manifest(client, anna, pid, since=head_before)) == []
    # the owner always sees both and its epoch is stable across the coder's done
    assert paths(before) == paths(manifest(client, admin_headers, pid))


def test_abstract_stage_hidden_until_setting_visible(client, project, cfg, admin_headers, coder):
    pid = project["id"]
    anna = join_project(
        client, admin_headers, pid, coder, CODER_USERNAME, CODER_PASSWORD,
        tags=["Anna"], tasks=["coding"],
    )
    push_one(client, admin_headers, pid, "Screening/Bob.json", b'{"screened": true}')
    before = manifest(client, anna, pid)
    assert paths(before) == []

    patched = client.patch(
        f"/api/p/{pid}/settings",
        json={"blinding": {"abstract": "visible"}},
        headers=admin_headers,
    )
    assert patched.status_code == 200, patched.text

    after = manifest(client, anna, pid)
    assert paths(after) == ["Screening/Bob.json"]
    assert after["visibility_epoch"] != before["visibility_epoch"]


def test_ai_tag_follows_the_same_blinding_rule(client, project, cfg, admin_headers, coder2):
    pid = project["id"]
    sb = join_project(
        client, admin_headers, pid, coder2, CODER2_USERNAME, CODER2_PASSWORD,
        tags=["SB"], tasks=["coding"],
    )
    push_one(client, admin_headers, pid, "Codings/K9 - MM.json", b'{"route": "MM"}')
    push_one(client, admin_headers, pid, "Codings/K9 - SB.json", b'{"route": "SB"}')

    assert paths(manifest(client, sb, pid)) == ["Codings/K9 - SB.json"]
    client.post(
        f"/api/p/{pid}/done",
        json={"key": "K9", "stage": "coding", "tag": "SB"},
        headers=sb,
    )
    assert paths(manifest(client, sb, pid)) == [
        "Codings/K9 - MM.json",
        "Codings/K9 - SB.json",
    ]


# ── merge / conflict ───────────────────────────────────────────────────


def test_stale_base_with_disjoint_edits_merges(client, project, admin_headers):
    pid = project["id"]
    path = "Extraction/Merge.json"
    base = json.dumps({"a": 1, "b": 2}, indent=2).encode() + b"\n"
    digest, result, _ = push_one(client, admin_headers, pid, path, base)
    assert result["status"] == "accepted"

    server_bytes = json.dumps({"a": 1, "b": 3}, indent=2).encode() + b"\n"
    _, result, _ = push_one(client, admin_headers, pid, path, server_bytes, base_hash=digest)
    assert result["status"] == "accepted"
    server_hash = result["hash"]

    client_bytes = json.dumps({"a": 9, "b": 2}, indent=2).encode() + b"\n"
    client_hash = put_blob(client, admin_headers, pid, client_bytes)
    merged_body = push_entries(
        client,
        admin_headers,
        pid,
        [{"path": path, "base_hash": digest, "new_hash": client_hash}],
    )
    merged = merged_body["results"][0]
    assert merged["status"] == "merged"
    assert merged["hash"] != server_hash
    assert merged["seq"] == 3

    row = entry(manifest(client, admin_headers, pid), path)
    assert row["hash"] == merged["hash"]

    fetched = client.get(f"/api/p/{pid}/blobs/{merged['hash']}", headers=admin_headers)
    assert fetched.status_code == 200
    assert json.loads(fetched.content) == {"a": 9, "b": 3}
    # the stale base is still readable as a past version
    assert client.get(f"/api/p/{pid}/blobs/{digest}", headers=admin_headers).status_code == 200


def test_same_field_conflict_leaves_server_head_unchanged(client, project, admin_headers):
    pid = project["id"]
    path = "Extraction/Conflict.json"
    base, result, _ = push_one(client, admin_headers, pid, path, b'{"a": 1}')
    server_hash = result["hash"]
    _, result, _ = push_one(client, admin_headers, pid, path, b'{"a": 2}', base_hash=base)
    head_hash = result["hash"]
    assert head_hash != server_hash

    client_hash = put_blob(client, admin_headers, pid, b'{"a": 3}')
    conflict = push_entries(
        client,
        admin_headers,
        pid,
        [{"path": path, "base_hash": base, "new_hash": client_hash}],
    )["results"][0]
    assert conflict["status"] == "conflict"
    assert isinstance(conflict["conflict_id"], int)

    # server state untouched
    assert entry(manifest(client, admin_headers, pid), path)["hash"] == head_hash

    rows = client.get(
        f"/api/p/{pid}/conflicts", params={"state": "open"}, headers=admin_headers
    ).json()["conflicts"]
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == conflict["conflict_id"]
    assert row["path"] == path
    assert row["base_hash"] == base
    assert row["server_hash"] == head_hash
    assert row["client_hash"] == client_hash
    assert row["state"] == "open"

    resolved = client.post(
        f"/api/p/{pid}/conflicts/{row['id']}/resolve",
        json={"resolution": "theirs"},
        headers=admin_headers,
    )
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["conflict"]["state"] == "resolved"
    assert resolved.json()["conflict"]["resolution"] == "theirs"
    # "theirs" only closes the conflict
    assert entry(manifest(client, admin_headers, pid), path)["hash"] == head_hash


def test_resolve_mine_and_merged_write_the_new_head(client, project, admin_headers):
    pid = project["id"]

    def make_conflict(path):
        base, _, _ = push_one(client, admin_headers, pid, path, b'{"v": 0}')
        push_one(client, admin_headers, pid, path, b'{"v": 1}', base_hash=base)
        client_hash = put_blob(client, admin_headers, pid, b'{"v": 2}')
        result = push_entries(
            client,
            admin_headers,
            pid,
            [{"path": path, "base_hash": base, "new_hash": client_hash}],
        )["results"][0]
        assert result["status"] == "conflict"
        return client_hash, result["conflict_id"]

    client_hash, conflict_id = make_conflict("Extraction/Mine.json")
    resolved = client.post(
        f"/api/p/{pid}/conflicts/{conflict_id}/resolve",
        json={"resolution": "mine"},
        headers=admin_headers,
    )
    assert resolved.status_code == 200, resolved.text
    assert entry(manifest(client, admin_headers, pid), "Extraction/Mine.json")["hash"] == client_hash

    merged_hash = put_blob(client, admin_headers, pid, b'{"v": 3}')
    _, conflict_id = make_conflict("Extraction/Merged.json")
    resolved = client.post(
        f"/api/p/{pid}/conflicts/{conflict_id}/resolve",
        json={"resolution": "merged", "new_hash": merged_hash},
        headers=admin_headers,
    )
    assert resolved.status_code == 200, resolved.text
    assert (
        entry(manifest(client, admin_headers, pid), "Extraction/Merged.json")["hash"]
        == merged_hash
    )


def test_conflicts_are_scoped_to_their_owner(
    client, project, cfg, admin_headers, coder, coder2
):
    pid = project["id"]
    anna = join_project(
        client, admin_headers, pid, coder, CODER_USERNAME, CODER_PASSWORD,
        tags=["Anna"], tasks=["coding"],
    )
    # Anna writes her own coding file twice; the owner updates the head first
    base, _, _ = push_one(client, admin_headers, pid, "Codings/K - Anna.json", b'{"v": 0}')
    _, result, _ = push_one(
        client, admin_headers, pid, "Codings/K - Anna.json", b'{"v": 1}', base_hash=base
    )
    client_hash = put_blob(client, admin_headers, pid, b'{"v": 2}')
    conflict = push_entries(
        client,
        anna,
        pid,
        [{"path": "Codings/K - Anna.json", "base_hash": base, "new_hash": client_hash}],
    )["results"][0]
    assert conflict["status"] == "conflict"

    own = client.get(
        f"/api/p/{pid}/conflicts", params={"state": "open"}, headers=anna
    ).json()["conflicts"]
    assert [row["id"] for row in own] == [conflict["conflict_id"]]

    # the owner sees everybody's conflicts and may resolve them
    all_rows = client.get(
        f"/api/p/{pid}/conflicts", params={"state": "all"}, headers=admin_headers
    ).json()["conflicts"]
    assert any(row["id"] == conflict["conflict_id"] for row in all_rows)
    owner_resolve = client.post(
        f"/api/p/{pid}/conflicts/{conflict['conflict_id']}/resolve",
        json={"resolution": "theirs"},
        headers=admin_headers,
    )
    assert owner_resolve.status_code == 200

    # another coder cannot see or resolve Anna's conflicts
    bob = join_project(
        client, admin_headers, pid, coder2, CODER2_USERNAME, CODER2_PASSWORD,
        tags=["Bob"], tasks=["coding"],
    )
    # Anna opens a second conflict (stale base again)
    second = push_entries(
        client,
        anna,
        pid,
        [{"path": "Codings/K - Anna.json", "base_hash": base, "new_hash": client_hash}],
    )["results"][0]
    assert second["status"] == "conflict"

    other = client.get(
        f"/api/p/{pid}/conflicts", params={"state": "all"}, headers=bob
    ).json()["conflicts"]
    assert other == []
    forbidden = client.post(
        f"/api/p/{pid}/conflicts/{second['conflict_id']}/resolve",
        json={"resolution": "theirs"},
        headers=bob,
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["detail"] == "forbidden"


# ── settle / locks ─────────────────────────────────────────────────────


def test_settle_requires_a_done_mark(client, project, cfg, admin_headers, coder):
    pid = project["id"]
    settler = join_project(
        client, admin_headers, pid, coder, CODER_USERNAME, CODER_PASSWORD,
        tags=["Anna"], tasks=["settle"],
    )
    data = b'{"Records": []}'
    digest = put_blob(client, admin_headers, pid, data)
    entry_body = {
        "path": "Extraction/S1.json",
        "base_hash": None,
        "new_hash": digest,
    }
    refused = push_entries(client, settler, pid, [entry_body])["results"][0]
    assert refused == {
        "path": "Extraction/S1.json",
        "status": "rejected",
        "reason": "forbidden",
        "hash": None,
    }

    client.post(
        f"/api/p/{pid}/done",
        json={"key": "S1", "stage": "coding", "tag": "Anna"},
        headers=settler,
    )
    accepted = push_entries(client, settler, pid, [entry_body])["results"][0]
    assert accepted["status"] == "accepted"


def test_lock_holder_blocks_other_users_until_expiry(
    client, project, cfg, admin_headers, coder, coder2
):
    pid = project["id"]
    anna = join_project(
        client, admin_headers, pid, coder, CODER_USERNAME, CODER_PASSWORD,
        tags=["Anna"], tasks=["coding", "settle"],
    )
    bob = join_project(
        client, admin_headers, pid, coder2, CODER2_USERNAME, CODER2_PASSWORD,
        tags=["Bob"], tasks=["coding", "settle"],
    )
    for who, tag in ((anna, "Anna"), (bob, "Bob")):
        client.post(
            f"/api/p/{pid}/done",
            json={"key": "L1", "stage": "coding", "tag": tag},
            headers=who,
        )

    acquired = client.post(
        f"/api/p/{pid}/locks/L1", json={"client_id": "anna-laptop"}, headers=anna
    )
    assert acquired.status_code == 200, acquired.text
    assert acquired.json()["user"] == "coder"

    digest = put_blob(client, admin_headers, pid, b'{"locked": true}')
    blocked = push_entries(
        client,
        bob,
        pid,
        [{"path": "Extraction/L1.json", "base_hash": None, "new_hash": digest}],
    )["results"][0]
    assert blocked == {
        "path": "Extraction/L1.json",
        "status": "rejected",
        "reason": "locked",
        "hash": None,
    }

    # the holder may write
    holder = push_entries(
        client,
        anna,
        pid,
        [{"path": "Extraction/L1.json", "base_hash": None, "new_hash": digest}],
    )["results"][0]
    assert holder["status"] == "accepted"

    # expire the lock in the database: the other writer is no longer blocked
    conn = db.connect(cfg.data_dir)
    try:
        conn.execute(
            "UPDATE locks SET expires = '2000-01-01T00:00:00Z' WHERE key = 'L1'"
        )
        conn.commit()
    finally:
        conn.close()

    updated = put_blob(client, admin_headers, pid, b'{"locked": false}')
    after = push_entries(
        client,
        bob,
        pid,
        [
            {
                "path": "Extraction/L1.json",
                "base_hash": holder["hash"],
                "new_hash": updated,
            }
        ],
    )["results"][0]
    assert after["status"] == "accepted"


def test_holder_push_renews_the_lock(client, project, cfg, admin_headers, coder):
    pid = project["id"]
    anna = join_project(
        client, admin_headers, pid, coder, CODER_USERNAME, CODER_PASSWORD,
        tags=["Anna"], tasks=["coding", "settle"],
    )
    client.post(
        f"/api/p/{pid}/done",
        json={"key": "R1", "stage": "coding", "tag": "Anna"},
        headers=anna,
    )
    client.post(f"/api/p/{pid}/locks/R1", json={"client_id": "anna-laptop"}, headers=anna)

    conn = db.connect(cfg.data_dir)
    try:
        conn.execute(
            "UPDATE locks SET acquired = '2000-01-01T00:00:00Z',"
            " renewed = '2000-01-01T00:00:00Z',"
            " expires = '2000-01-02T00:00:00Z' WHERE key = 'R1'"
        )
        conn.commit()
    finally:
        conn.close()

    digest = put_blob(client, admin_headers, pid, b"{}")
    result = push_entries(
        client,
        anna,
        pid,
        [{"path": "Extraction/R1.json", "base_hash": None, "new_hash": digest}],
    )["results"][0]
    assert result["status"] == "accepted"

    locks = client.get(f"/api/p/{pid}/locks", headers=admin_headers).json()
    row = next(lock for lock in locks if lock["key"] == "R1")
    assert row["user"] == "coder"
    assert row["renewed"] > "2000-01-01T00:00:00Z"
    assert row["expires"] > "2000-01-02T00:00:00Z"
    assert row["expired"] is False


# ── bulk publish ───────────────────────────────────────────────────────


def test_owner_bulk_publish_of_2000_entries_is_fast(client, project, cfg, admin_headers):
    pid = project["id"]
    store = BlobStore(cfg.data_dir)
    entries = []
    for index in range(2000):
        data = f'{{"index": {index}}}\n'.encode()
        digest = hashlib.sha256(data).hexdigest()
        store.write_bytes(digest, data)
        key = f"Study {index:04d}"
        entries.append(
            {
                "path": f"Datasets/{key}/{key} - prepared.csv",
                "base_hash": None,
                "new_hash": digest,
                "size": len(data),
            }
        )

    started = time.perf_counter()
    body = push_entries(client, admin_headers, pid, entries)
    elapsed = time.perf_counter() - started
    print(f"\nbulk publish of 2000 entries: {elapsed:.2f}s")

    assert len(body["results"]) == 2000
    assert all(result["status"] == "accepted" for result in body["results"])
    assert body["seq"] == 2000
    assert elapsed < 20.0
