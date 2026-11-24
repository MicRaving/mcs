"""The AI coding routes are **on demand** in an online project (INTERFACES §4).

``Codings/<Key> - SB|MM|DS.json`` are the independent reliability routes
(AGENTS.md) and ~264 MB of a co-author's first-login download, so
``protocol.is_core`` is False for them.  These tests drive the shipping Flask
app against the engine's own FakeServer and check the three promises that make
that safe:

1. **Listings stay right without downloading**: ``/api/papers`` counts the route
   as a coder of the study (``n_coders``, ``codings[]``) from the manifest -
   including an offline mirror - and the file is not on disk afterwards.
2. **The whole-corpus view fetches it in one batch**: the agreement/IRR view
   (``GET /api/agreement/reliability``) pulls the missing route files once,
   with progress, and the numbers equal those of a plain local project holding
   the same files.
3. **Offline is explicit**: when a route cannot be fetched, the view names it
   (``not_available_offline``) instead of quietly scoring a smaller set.
"""

from __future__ import annotations

import json
import os

import pytest

from online.tests.conftest import PID

import app as app_module
import routes_online
from studio_state import S

ROUTE_FILE = "Codings/K1 - SB.json"
HUMAN_FILE = "Codings/K1 - A.json"

#: Consensus coding (``Extraction/``), the human coder and the AI route.  Two of
#: the three experiments agree between A and SB, so the field yields a real AC.
EXTRACTION = [
    {"Study_ID": "K1_Exp1", "Age_Group": 1, "Setting": 1},
    {"Study_ID": "K1_Exp2", "Age_Group": 2, "Setting": 1},
    {"Study_ID": "K1_Exp3", "Age_Group": 1, "Setting": 2},
]
CODER_A = [
    {"Study_ID": "K1_Exp1", "Age_Group": 1, "Setting": 1},
    {"Study_ID": "K1_Exp2", "Age_Group": 2, "Setting": 1},
    {"Study_ID": "K1_Exp3", "Age_Group": 2, "Setting": 2},
]
ROUTE_SB = [
    {"Study_ID": "K1_Exp1", "Age_Group": 1, "Setting": 1},
    {"Study_ID": "K1_Exp2", "Age_Group": 1, "Setting": 1},
    {"Study_ID": "K1_Exp3", "Age_Group": 2, "Setting": 2},
]


def _jbytes(obj) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, indent=2) + "\n").encode()


def _seed(server) -> None:
    """Put the consensus, the human coder and the on-demand route on the server."""
    server.put_file(PID, "Extraction/K1.json", _jbytes(EXTRACTION))
    server.put_file(PID, HUMAN_FILE, _jbytes(CODER_A))
    server.put_file(PID, ROUTE_FILE, _jbytes(ROUTE_SB))


@pytest.fixture(autouse=True)
def _isolate_app_state():
    """Every test starts local and leaves the app on the folder it found."""
    original = S.ROOT
    routes_online.set_store(None)
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


def _open_synced(client, make_node, server):
    """Open an online project as the owner and pull the core tier once."""
    _seed(server)
    node = make_node("own")
    _open(client, node)
    assert client.post("/api/online/sync").status_code == 200
    return node


# ---------------------------------------------------------------------------
# 1. a listing counts the route without downloading it
# ---------------------------------------------------------------------------

def test_paper_list_counts_the_on_demand_route_without_fetching_it(client, make_node, server):
    node = _open_synced(client, make_node, server)
    assert os.path.isfile(node.path(HUMAN_FILE))          # core: pulled on open
    assert not os.path.exists(node.path(ROUTE_FILE))      # on demand: not pulled

    papers = {p["key"]: p for p in client.get("/api/papers").get_json()["papers"]}
    k1 = papers["K1"]
    assert k1["n_coders"] == 2
    assert [c["coder"] for c in k1["codings"]] == ["A", "SB"]
    sb = next(c for c in k1["codings"] if c["coder"] == "SB")
    assert sb["local"] is False
    # the list must not have downloaded what it only counts
    assert not os.path.exists(node.path(ROUTE_FILE))

    # the Home view's coder count is a manifest listing too
    coders = client.get("/api/project/summary").get_json()["metrics"]["coders"]
    assert coders == 2


def test_route_file_is_still_an_owned_coding_file(client, make_node, server):
    """On demand is a cache tier, not a new class: blinding/writes are unchanged."""
    from online_shared import protocol
    fc = protocol.classify(ROUTE_FILE)
    assert (fc.kind, fc.tag, fc.stage) == ("owned", "SB", "coding")
    assert protocol.is_core(ROUTE_FILE) is False
    assert protocol.is_core(HUMAN_FILE) is True


def test_coder_files_reads_a_local_project_from_the_disk(tmp_path):
    """The local (non-online) branch of the same listing: disk, no manifest."""
    from studio_lib import coder_files
    root = _local_project(tmp_path)
    (root / "Codings" / "K1 - harmonizer - A.json").write_text("{}", encoding="utf-8")
    S.online = False
    S.store = None
    S.configure(str(root))
    files = coder_files("K1")
    assert [c["coder"] for c in files] == ["A", "SB"]
    assert all(c["local"] is True and c["modified"] for c in files)


# ---------------------------------------------------------------------------
# 2. the agreement view fetches the route, and the numbers match a local project
# ---------------------------------------------------------------------------

def _local_project(tmp_path):
    """A plain folder project holding the same three files (no server)."""
    root = tmp_path / "local-project"
    for sub in ("Papers", "Text", "Extraction", "Codings"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    (root / "Text" / "K1.txt").write_text("paper text K1\n", encoding="utf-8")
    (root / "Extraction" / "K1.json").write_bytes(_jbytes(EXTRACTION))
    (root / HUMAN_FILE).write_bytes(_jbytes(CODER_A))
    (root / ROUTE_FILE).write_bytes(_jbytes(ROUTE_SB))
    return root


def _view(client):
    resp = client.get("/api/agreement/reliability")
    assert resp.status_code == 200, resp.get_data(as_text=True)[:400]
    return resp.get_json()


def _numbers(view):
    """The comparable part of the agreement answer."""
    return (view["overall"], view["raters"],
            {f["name"]: f["ac1"] for f in view["fields"] if f["ac1"] is not None})


def test_agreement_fetches_the_route_once_and_matches_the_local_numbers(
        client, make_node, server, tmp_path):
    node = _open_synced(client, make_node, server)
    assert not os.path.exists(node.path(ROUTE_FILE))

    online = _view(client)
    # the route arrived, in one batch, and the view says so
    assert os.path.isfile(node.path(ROUTE_FILE))
    assert online["not_available_offline"] == []
    assert online["prefetch"]["needed"] >= 1
    assert online["prefetch"]["fetched"] >= 2            # A (already local) + SB
    assert set(online["raters"]) == {"A", "SB"}
    assert online["overall"] is not None

    # a second visit needs nothing: the batch is not repeated per page load
    again = _view(client)
    assert again["prefetch"]["needed"] == 0
    assert _numbers(again) == _numbers(online)

    # the same files in a local project give the same numbers
    S.online = False
    S.store = None
    S.configure(str(_local_project(tmp_path)))
    local = _view(client)
    assert local["not_available_offline"] == []
    assert local["prefetch"]["needed"] == 0
    assert _numbers(local) == _numbers(online)


def test_the_status_reports_the_prefetch(client, make_node, server):
    _open_synced(client, make_node, server)
    _view(client)
    status = client.get("/api/online/status").get_json()
    assert status["active"] is True
    prefetch = status["prefetch"]
    assert prefetch["running"] is False and prefetch["phase"] == "done"
    assert prefetch["total"] >= 1 and prefetch["missing"] == 0


# ---------------------------------------------------------------------------
# 3. offline: the view names the route it could not fetch
# ---------------------------------------------------------------------------

def test_offline_agreement_names_the_route_it_could_not_fetch(
        client, make_node, server):
    node = _open_synced(client, make_node, server)
    assert not os.path.exists(node.path(ROUTE_FILE))

    node.transport.offline = True
    data = _view(client)                                  # still an answer, not a 503
    assert data["not_available_offline"] == [ROUTE_FILE]
    assert data["prefetch"]["offline"] is True
    assert data["prefetch"]["missing"] == [ROUTE_FILE]
    # the route was not scored - and the answer says which one is missing
    assert "SB" not in data["raters"]
    assert not os.path.exists(node.path(ROUTE_FILE))

    # a reload offline still reports it (no cached "everything was fine")
    assert _view(client)["not_available_offline"] == [ROUTE_FILE]

    # back online: the next visit fetches it and the warning disappears
    node.transport.offline = False
    back = _view(client)
    assert back["not_available_offline"] == []
    assert os.path.isfile(node.path(ROUTE_FILE))
    assert set(back["raters"]) == {"A", "SB"}


def test_offline_paper_list_still_counts_the_route(client, make_node, server):
    """A listing is a manifest read: offline, the coder count does not drop."""
    node = _open_synced(client, make_node, server)
    node.transport.offline = True
    papers = {p["key"]: p for p in client.get("/api/papers").get_json()["papers"]}
    assert papers["K1"]["n_coders"] == 2
    assert not os.path.exists(node.path(ROUTE_FILE))
