"""Saved online connections (owner report 2026-11-25).

"Saving online project is missing. I need to type in the connection
credentials again every time." / "It does not show up on my list of recently
opened projects."

* an opened online project is remembered in the app config - kind, server,
  user, folder, project - **never** the secret, which goes to the OS keyring
  (here: a fake keyring; the file fallback when there is none);
* the app start reopens the last online project without asking; a refused
  secret leaves ``reopen_failed`` (the page opens the dialog prefilled);
* the recent list (``/api/projects``) carries the online projects;
* "Forget" and "Log out" remove the stored secret.
"""

from __future__ import annotations

import json
import os

import pytest

import app as app_module
import routes_online
from online import keyring_store
from online.store import OnlineStore
from online.tests.conftest import PID, SERVER_URL, FakeTransport
from online.tests.webdav_fake import WebDAVFake
from studio_state import S, config_path, load_config

MEMBERS = {"members": [{"username": "a", "role": "owner", "tags": ["A"],
                        "tasks": ["coding", "settle", "harmonize", "reproduce"]}]}
APP_PASSWORD = "nc-app-password-123"


class FakeKeyring:
    def __init__(self):
        self.store: dict = {}

    def set_password(self, service, account, secret):
        self.store[(service, account)] = secret

    def get_password(self, service, account):
        return self.store.get((service, account))

    def delete_password(self, service, account):
        self.store.pop((service, account), None)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Local start, a fresh config file, the app's folder restored afterwards."""
    original = S.ROOT
    monkeypatch.setenv("CODINGSTUDIO_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setenv("CODINGSTUDIO_ONLINE_DIR", str(tmp_path / "online"))
    routes_online.set_store(None)
    routes_online._ACTIVE.clear()
    routes_online._REOPEN.update(running=False, failed=None)
    S.online = False
    S.store = None
    yield
    try:
        if routes_online._STORE is not None:
            routes_online._STORE.close()
    except Exception:                       # noqa: BLE001
        pass
    S.online = False
    S.store = None
    routes_online.set_store(None)
    routes_online._ACTIVE.clear()
    routes_online._REOPEN.update(running=False, failed=None)
    if original and os.path.isdir(original):
        S.configure(original)


@pytest.fixture
def client():
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


@pytest.fixture
def keyring(monkeypatch):
    kr = FakeKeyring()
    monkeypatch.setattr(keyring_store, "_keyring", kr)
    return kr


@pytest.fixture
def share(tmp_path):
    root = tmp_path / "share"
    for rel, data in {"Project/project.json": b'{"scheme": "x"}\n',
                      "Text/K1.txt": b"paper one\n", "Text/K2.txt": b"paper two\n",
                      "Papers/K3.pdf": b"%PDF-1.4\n%%EOF\n",
                      "Extraction/K1.json": b'{"Study_ID": "K1"}\n'}.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    (root / ".studio").mkdir()
    (root / ".studio" / "members.json").write_text(json.dumps(MEMBERS), encoding="utf-8")
    fake = WebDAVFake(str(root))
    url = fake.start()
    yield url
    fake.stop()


def _connect_and_open(client, url, password=APP_PASSWORD):
    r = client.post("/api/online/connect", json={
        "kind": "nextcloud", "url": url, "username": "a", "app_password": password,
        "folder": "", "webdav_root": url})
    assert r.status_code == 200, r.get_json()
    pid = r.get_json()["projects"][0]["id"]
    r = client.post("/api/online/open", json={"project_id": pid})
    assert r.status_code == 200, r.get_json()
    return pid, r.get_json()


def _restart():
    """The app process ends (store closed) and starts again (a new store)."""
    store = routes_online._STORE
    if store is not None:
        store.close()
    routes_online.set_store(None)
    routes_online._ACTIVE.clear()
    S.clear_online()


# ---------------------------------------------------------------------------

def test_open_remembers_the_connection_without_the_secret(client, share, keyring):
    pid, opened = _connect_and_open(client, share)
    conn = opened["connection"]
    assert conn["kind"] == "nextcloud" and conn["project_id"] == pid
    assert conn["host"] == "127.0.0.1" and conn["username"] == "a"
    raw = open(config_path(), encoding="utf-8").read()
    assert APP_PASSWORD not in raw
    cfg = load_config()
    assert cfg["last_project"] == {"kind": "online", "id": conn["id"]}
    saved = cfg["online_connections"][0]
    assert not any(k in saved for k in ("password", "app_password", "token"))
    # the secret is in the keyring, and no plain copy is left in the mirror
    assert APP_PASSWORD in keyring.store.values()
    assert not os.path.exists(os.path.join(S.ROOT, ".studio", "token"))
    data = client.get("/api/online/connections").get_json()
    assert [c["id"] for c in data["connections"]] == [conn["id"]]
    assert data["active"] == conn["id"]


def test_app_start_reopens_the_last_online_project_without_asking(client, share, keyring):
    pid, opened = _connect_and_open(client, share)
    mirror = S.ROOT
    _restart()
    assert S.online is False
    assert routes_online.reopen_last_on_start() is True
    assert S.online is True
    assert os.path.normcase(S.ROOT) == os.path.normcase(mirror)
    st = client.get("/api/online/status").get_json()
    assert st["active"] is True and st["backend"] == "nextcloud"
    assert st["connection_id"] == opened["connection"]["id"]


def test_a_refused_secret_leaves_the_dialog_prefilled_never_the_password(client, share, keyring):
    _connect_and_open(client, share)
    _restart()
    for k in list(keyring.store):
        keyring.store[k] = "revoked"                     # the app password was revoked
    from online.tests import webdav_fake
    # the fake accepts any password: make it refuse this one
    orig = webdav_fake._Handler._begin

    def strict(self):
        import base64
        raw = self.headers.get("Authorization") or ""
        if raw and base64.b64decode(raw[6:]).decode().endswith(":revoked"):
            self._req_body = b""
            self._send(401, b"no", {"WWW-Authenticate": 'Basic realm="x"'})
            return None
        return orig(self)

    webdav_fake._Handler._begin = strict
    try:
        assert routes_online.reopen_last_on_start() is False
    finally:
        webdav_fake._Handler._begin = orig
    assert S.online is False
    st = client.get("/api/online/status").get_json()
    failed = st["reopen_failed"]
    assert failed["error"] == "unauthorized"
    conn = failed["connection"]
    assert conn["username"] == "a" and conn["host"] == "127.0.0.1" and conn["backend"] == "nextcloud"
    assert "revoked" not in json.dumps(st)


def test_recent_list_carries_the_online_project(client, share, keyring, tmp_path):
    local = tmp_path / "local-project"
    (local / "Papers").mkdir(parents=True)
    (local / "Text").mkdir()
    S.configure(str(local))                          # a recent folder
    pid, opened = _connect_and_open(client, share)
    data = client.get("/api/projects").get_json()
    kinds = [(p["kind"], p.get("current")) for p in data["projects"]]
    assert kinds[0] == ("online", True)              # the open project first
    row = data["projects"][0]
    assert row["backend"] == "nextcloud" and row["host"] == "127.0.0.1"
    assert data["current"] == "online:" + opened["connection"]["id"]
    assert any(p["kind"] == "local" and os.path.normcase(p["path"]) == os.path.normcase(str(local))
               for p in data["projects"])
    # the mirror itself never becomes a "recent folder"
    assert not any(p["kind"] == "local" and os.path.normcase(p["path"]) == os.path.normcase(S.ROOT)
                   for p in data["projects"])
    client.post("/api/project/recent", json={"path": S.ROOT})
    assert not any(os.path.normcase(p) == os.path.normcase(S.ROOT)
                   for p in load_config().get("recent_projects") or [])
    # after closing it, it is an ordinary (not current) row, reopened by id
    client.post("/api/online/close")
    data = client.get("/api/projects").get_json()
    online = [p for p in data["projects"] if p["kind"] == "online"]
    assert online and online[0]["current"] is False
    r = client.post("/api/online/reopen", json={"id": online[0]["id"]})
    assert r.status_code == 200, r.get_json()
    assert S.online is True


def test_forget_removes_the_secret_and_the_entry(client, share, keyring):
    _pid, opened = _connect_and_open(client, share)
    cid = opened["connection"]["id"]
    client.post("/api/online/close")
    assert load_config()["last_project"] == {"kind": "local"}      # closed by hand: no reopen
    r = client.delete(f"/api/online/connections/{cid}")
    assert r.status_code == 200
    assert keyring.store == {}
    assert client.get("/api/online/connections").get_json()["connections"] == []
    assert client.post("/api/online/reopen", json={"id": cid}).status_code == 404


def test_logout_removes_the_secret_keyring_and_file(client, share, monkeypatch):
    monkeypatch.setattr(keyring_store, "_keyring", None)           # the file fallback
    _pid, _opened = _connect_and_open(client, share)
    token_file = os.path.join(S.ROOT, ".studio", "token")
    assert os.path.exists(token_file)
    r = client.post("/api/online/logout")
    assert r.status_code == 200
    assert not os.path.exists(token_file)
    assert load_config()["last_project"] == {"kind": "local"}
    # nothing to reopen with: the connect dialog is the way back
    cid = client.get("/api/online/connections").get_json()["connections"][0]["id"]
    r = client.post("/api/online/reopen", json={"id": cid})
    assert r.status_code == 401
    assert r.get_json()["connection"]["id"] == cid


def test_the_listing_refreshes_the_paper_keys_without_a_manual_sync(client, share, keyring):
    _connect_and_open(client, share)
    store = routes_online._STORE
    assert store.syncer.on_listing is not None
    store.syncer.tick(force=True)                   # what the background thread does
    assert {"K1", "K2", "K3"} <= set(S.KEYS)
    st = client.get("/api/online/status").get_json()
    assert st["listing_rev"] >= 1


def test_server_mode_token_is_reused_on_start(client, make_node):
    node = make_node("a")
    routes_online.set_store(node.store)
    r = client.post("/api/online/open", json={"project_id": PID})
    assert r.status_code == 200, r.get_json()
    conn = r.get_json()["connection"]
    assert conn["kind"] == "server" and conn["server"] == SERVER_URL
    assert "pw" not in open(config_path(), encoding="utf-8").read().replace("project", "")
    node.store.close()
    routes_online.set_store(None)
    S.clear_online()
    # a new process: a fresh store on the same mirror base, same fake server
    store = OnlineStore(base_dir=node.base, transport_factory=lambda _s: node.transport, debounce=0,
                        autostart=False, use_keyring=False)
    routes_online.set_store(store)
    assert routes_online.reopen_last_on_start() is True
    assert S.online is True and store.username == "a"
    assert client.get("/api/online/status").get_json()["project_id"] == PID


def test_a_saved_connection_never_holds_a_secret():
    from studio_state import record_online_connection

    with pytest.raises(ValueError):
        record_online_connection({"kind": "server", "server": "https://x", "username": "a",
                                  "project_id": "p", "password": "pw"})


def test_prefetch_after_the_project_was_closed_is_a_quiet_no_op(client, share, keyring, monkeypatch):
    _connect_and_open(client, share)
    store = routes_online._STORE
    logged = []
    import studio_state
    monkeypatch.setattr(studio_state, "log", lambda m: logged.append(m))
    store.close()                                   # the window closed meanwhile
    res = S.ensure_many(["Codings/K1 - SB.json"], label="agreement: coder files")
    assert res.get("closed") is True and res["missing"] == []
    assert not any("prefetch" in m for m in logged), logged


def test_keyring_migrates_a_fallback_file(tmp_path, keyring):
    d = str(tmp_path / ".studio")
    os.makedirs(d)
    # a file an older build wrote (no keyring then)
    keyring_store.save_token("https://x", "a", "secret", d, use_keyring=False)
    assert os.path.exists(os.path.join(d, "token"))
    assert keyring_store.load_token("https://x", "a", d) == "secret"
    assert keyring.store and not os.path.exists(os.path.join(d, "token"))
    assert keyring_store.load_token("https://x", "a", d) == "secret"


@pytest.mark.skipif(os.name != "nt" or os.environ.get("CS_TEST_WINCRED") != "1",
                    reason="touches the real Windows Credential Manager (opt-in: CS_TEST_WINCRED=1)")
def test_windows_credential_manager_roundtrip():
    cred = keyring_store._WinCred()
    service = "CodingStudio-online-selftest"
    cred.set_password(service, "a@https://x", "s3cr3t-ü")
    try:
        assert cred.get_password(service, "a@https://x") == "s3cr3t-ü"
    finally:
        cred.delete_password(service, "a@https://x")
    assert cred.get_password(service, "a@https://x") is None
