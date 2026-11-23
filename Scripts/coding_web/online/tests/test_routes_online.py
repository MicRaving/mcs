"""End-to-end tests of the ``routes_online`` blueprint (Flask test client).

The online side runs against the engine's own in-memory ``FakeServer`` through
``online.tests.conftest``'s node fixture, so the tests exercise the real
``OnlineStore``/``Syncer`` - not a stub - while the Flask app is the shipping
app from ``app.py``.  Each test installs the node's store with
``routes_online.set_store`` (the seam the app uses for its process-wide store).
"""

from __future__ import annotations

import os
import time

import pytest

from online.tests.conftest import PID, SERVER_URL

import app as app_module
import routes_online
from studio_state import S

PDF = b"%PDF-1.4\n% a minimal test pdf\n%%EOF\n"


@pytest.fixture(autouse=True)
def _isolate_app_state():
    """Every test starts local and leaves the app on the folder it found."""
    original = S.ROOT
    routes_online.set_store(None)
    with routes_online._PUBLISH_LOCK:
        routes_online._PUBLISH.update(
            running=False, ok=None, source_root=None, started=None, finished=None,
            total=0, hashed=0, uploaded=0, pushed=0, skipped=0, conflicts=0,
            errors=[], message="")
    S.online = False
    S.store = None
    yield
    S.online = False
    S.store = None
    routes_online.set_store(None)
    if original and os.path.isdir(original):
        S.configure(original)


@pytest.fixture
def client():
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


def _open(client, node, project_id=PID):
    routes_online.set_store(node.store)
    resp = client.post("/api/online/open", json={"project_id": project_id})
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()


# ---------------------------------------------------------------------------
# status / connect / open / close
# ---------------------------------------------------------------------------

def test_status_is_local_for_a_folder_project(client):
    data = client.get("/api/online/status").get_json()
    assert data["active"] is False
    assert data["mode"] == "local"
    assert data["pending"] == 0


def test_status_is_active_after_open(client, make_node):
    node = make_node("a")
    _open(client, node)
    data = client.get("/api/online/status").get_json()
    assert data["active"] is True
    assert data["mode"] in ("online", "offline", "syncing")
    assert data["project_id"] == PID
    assert data["me"]["role"] == "coder"


def test_connect_lists_projects(client, make_node):
    node = make_node("a")
    routes_online.set_store(node.store)
    resp = client.post("/api/online/connect",
                       json={"server": SERVER_URL, "username": "a", "password": "pw"})
    assert resp.status_code == 200, resp.get_json()
    assert any(p["id"] == PID for p in resp.get_json()["projects"])


def test_open_switches_root_and_close_restores_it(client, make_node):
    original = S.ROOT
    node = make_node("a")
    opened = _open(client, node)
    assert os.path.normcase(S.ROOT) == os.path.normcase(node.root)
    assert os.path.normcase(opened["root"]) == os.path.normcase(node.root)
    assert S.online is True
    assert client.post("/api/online/sync").status_code == 200
    assert os.path.isfile(os.path.join(S.ROOT, "Text", "K1.txt"))

    resp = client.post("/api/online/close")
    assert resp.status_code == 200, resp.get_json()
    assert os.path.normcase(S.ROOT) == os.path.normcase(original)
    assert S.online is False
    assert S.store is None


# ---------------------------------------------------------------------------
# coder identity (audit §4)
# ---------------------------------------------------------------------------

def test_coder_tag_outside_member_tags_is_refused(client, make_node):
    node = make_node("a")                       # tags: ["A"]
    _open(client, node)
    client.post("/api/online/sync")

    body = {"key": "K1", "coder": "ZZZ", "records": [{"Study_ID": "K1"}]}
    resp = client.post("/api/save", json=body)
    assert resp.status_code == 403, resp.get_json()
    assert resp.get_json()["error"] == "forbidden"

    # a read route that carries ?coder= is guarded too
    resp = client.get("/api/fulltext/K1", query_string={"coder": "ZZZ"})
    assert resp.status_code == 403

    body["coder"] = "A"
    resp = client.post("/api/save", json=body)
    assert resp.status_code == 200, resp.get_json()


def test_owner_may_use_any_tag(client, make_node):
    node = make_node("own")
    _open(client, node)
    client.post("/api/online/sync")
    resp = client.post("/api/save",
                       json={"key": "K1", "coder": "A", "records": [{"Study_ID": "K1"}]})
    assert resp.status_code == 200, resp.get_json()


# ---------------------------------------------------------------------------
# done: local folder project and online project
# ---------------------------------------------------------------------------

def test_done_works_for_a_local_folder_project(client, tmp_path):
    local = tmp_path / "local-project"
    (local / "Papers").mkdir(parents=True)
    (local / "Text").mkdir()
    S.configure(str(local))
    assert S.online is False

    resp = client.post("/api/online/done", json={"key": "K1", "stage": "coding", "tag": "A"})
    assert resp.status_code == 200, resp.get_json()
    assert (local / ".studio" / "done.json").is_file()

    rows = client.get("/api/online/done", query_string={"stage": "coding"}).get_json()["done"]
    assert {"key": "K1", "stage": "coding", "tag": "A"} in rows
    # idempotent
    client.post("/api/online/done", json={"key": "K1", "stage": "coding", "tag": "A"})
    rows = client.get("/api/online/done").get_json()["done"]
    assert len(rows) == 1


def test_done_works_for_an_online_project(client, make_node, server):
    node = make_node("a")
    _open(client, node)
    resp = client.post("/api/online/done", json={"key": "K1", "stage": "coding", "tag": "A"})
    assert resp.status_code == 200, resp.get_json()
    assert ("A", "K1", "coding") in server.projects[PID]["done"]

    rows = client.get("/api/online/done",
                      query_string={"stage": "coding", "tag": "A"}).get_json()["done"]
    assert any(r["key"] == "K1" and r["tag"] == "A" and r["stage"] == "coding" for r in rows)


def test_done_offline_answers_ok_lists_the_mark_and_sends_it_on_reconnect(client, make_node, server):
    """Finish paper works offline: 200, the mark is listed, and it lands later."""
    node = make_node("a")
    _open(client, node)
    assert client.post("/api/online/sync").status_code == 200

    node.transport.offline = True
    resp = client.post("/api/online/done", json={"key": "K1", "stage": "coding", "tag": "A"})
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["ok"] is True
    assert ("A", "K1", "coding") not in server.projects[PID]["done"]

    status = client.get("/api/online/status").get_json()
    assert status["mode"] == "offline"
    assert status["pending"] == 1 and status["done_pending"] == 1

    # the done list answers from the queue, not with a 503
    resp = client.get("/api/online/done", query_string={"stage": "coding", "tag": "A"})
    assert resp.status_code == 200, resp.get_json()
    rows = resp.get_json()["done"]
    assert [(r["key"], r["tag"]) for r in rows if r.get("pending")] == [("K1", "A")]

    # a sync round that cannot reach the server keeps the mark
    assert client.post("/api/online/sync").status_code == 200
    assert client.get("/api/online/status").get_json()["done_pending"] == 1

    node.transport.offline = False
    assert client.post("/api/online/sync").status_code == 200
    assert ("A", "K1", "coding") in server.projects[PID]["done"]
    status = client.get("/api/online/status").get_json()
    assert status["mode"] == "online"
    assert status["pending"] == 0 and status["done_pending"] == 0


# ---------------------------------------------------------------------------
# locks
# ---------------------------------------------------------------------------

def test_lock_held_by_another_member_maps_to_409(client, make_node):
    node_a = make_node("a")
    _open(client, node_a)
    resp = client.post("/api/online/locks/K1", json={})
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["lock"]["key"] == "K1"

    node_b = make_node("b")
    _open(client, node_b)
    resp = client.post("/api/online/locks/K1", json={})
    assert resp.status_code == 409, resp.get_json()
    body = resp.get_json()
    assert body["error"] == "locked"
    assert body["holder"]["user"] == "a"

    # the holder may release it
    _open(client, node_a)
    assert client.delete("/api/online/locks/K1").status_code == 200


def test_locks_pass_the_server_identity_through_and_flag_mine(client, make_node):
    """`mine` is decided server-side from the username, never from a name."""
    node_a = make_node("a")
    _open(client, node_a)
    lock = client.post("/api/online/locks/K1", json={}).get_json()["lock"]
    assert lock["mine"] is True
    assert lock["user"] == "a" and lock["display_name"] == "A"
    assert lock["client_id"] == node_a.mirror.state["client_id"]

    node_b = make_node("b")
    _open(client, node_b)
    rows = client.get("/api/online/locks").get_json()["locks"]
    row = next(r for r in rows if r["key"] == "K1")
    assert row["mine"] is False
    assert row["user"] == "a" and row["display_name"] == "A"
    assert row["client_id"] == node_a.mirror.state["client_id"]
    assert "expires" in row and "offline" in row and "expired" in row


# ---------------------------------------------------------------------------
# manifest listings + on-demand fetch
# ---------------------------------------------------------------------------

def test_manifest_pdf_is_listed_and_fetched_by_the_paper_route(client, make_node, server):
    node = make_node("a")
    _open(client, node)
    client.post("/api/online/sync")
    server.put_file(PID, "Papers/K2.pdf", PDF)
    assert client.post("/api/online/sync").status_code == 200

    listing = client.get("/api/papers").get_json()["papers"]
    keys = {p["key"] for p in listing}
    assert "K2" in keys
    assert any(p["key"] == "K2" and p["has_pdf"] for p in listing)

    local_pdf = os.path.join(node.root, "Papers", "K2.pdf")
    assert not os.path.exists(local_pdf)          # known, but not downloaded

    resp = client.get("/paper/K2")
    assert resp.status_code == 200, resp.data[:200]
    assert resp.data == PDF
    assert os.path.isfile(local_pdf)              # ensure_local fetched it

    # the supplement listing reads the manifest too
    server.put_file(PID, "Supplements/K2 Supplement 1.txt", b"appendix\n")
    client.post("/api/online/sync")
    client.get("/api/papers")
    supps = client.get("/api/pdfinfo/K2").get_json()["supplements"]
    assert any(s["file"] == "K2 Supplement 1.txt" for s in supps)


# ---------------------------------------------------------------------------
# unwritable files stay local
# ---------------------------------------------------------------------------

def test_unwritable_file_edit_is_not_pushed(client, make_node, server):
    node = make_node("a")                          # coder, not owner
    _open(client, node)
    client.post("/api/online/sync")

    rel = "Results/local-only.txt"
    full = node.path(rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as fh:
        fh.write("only on this machine\n")

    assert client.post("/api/online/sync").status_code == 200
    assert server.projects[PID]["files"].get(rel) is None
    assert rel in node.mirror.state["local_only"]
    assert os.path.isfile(full)


# ---------------------------------------------------------------------------
# admin passthrough
# ---------------------------------------------------------------------------

def test_admin_passthrough_is_forbidden_for_a_coder(client, make_node):
    node = make_node("a")
    _open(client, node)
    calls = [
        client.get("/api/online/admin/members"),
        client.get("/api/online/admin/settings"),
        client.get("/api/online/admin/users"),
        client.get("/api/online/admin/audit"),
        client.post("/api/online/admin/publish", json={"source_root": S.ROOT}),
        client.put("/api/online/admin/members/2", json={"role": "owner", "tags": [], "tasks": []}),
    ]
    for resp in calls:
        assert resp.status_code == 403, resp.get_json()
        assert resp.get_json()["error"] == "forbidden"


# ---------------------------------------------------------------------------
# admin publish
# ---------------------------------------------------------------------------

def test_publish_pushes_every_file_and_skips_excluded(client, make_node, server, tmp_path):
    node = make_node("own")
    _open(client, node)

    source = tmp_path / "source-project"
    wanted = {
        "Text/K9.txt": b"paper K9\n",
        "Papers/K9.pdf": b"%PDF-1.4\nK9\n",
        "Codings/K9 - O.json": b'[{"Study_ID": "K9"}]\n',
        "Datasets/K9/K9 - harmonized.csv": b"pid,dv_x\n1,2\n",
        "Results/report.csv": b"a,b\n1,2\n",
    }
    excluded = {
        "temp/scratch.txt": b"x",
        "archive/old.txt": b"y",
        ".studio/done.json": b"{}",
        "Codings/__pycache__/x.pyc": b"z",
    }
    for rel, data in {**wanted, **excluded}.items():
        path = source.joinpath(*rel.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    resp = client.post("/api/online/admin/publish", json={"source_root": str(source)})
    assert resp.status_code == 200, resp.get_json()

    deadline = time.time() + 20
    progress = client.get("/api/online/admin/publish").get_json()
    while progress["running"] and time.time() < deadline:
        time.sleep(0.05)
        progress = client.get("/api/online/admin/publish").get_json()
    assert progress["running"] is False, progress
    assert progress["ok"] is True, progress

    for rel, data in wanted.items():
        assert server.file_bytes(PID, rel) == data, rel
    for rel in excluded:
        assert server.file_bytes(PID, rel) is None, rel
    assert progress["pushed"] >= len(wanted)
