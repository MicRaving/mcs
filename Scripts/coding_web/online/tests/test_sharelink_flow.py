"""Share-link projects end to end (D11): owner, invitations, coders, accidents.

The owner is signed in with his own Nextcloud account.  He creates a public
share link for the project folder (OCS), invites two coders who have **no**
Nextcloud account, and their app instances join with the invitation text.  The
project is the same folder, so the same sync engine, the same tags and the same
honour-system rules apply - link mode only changes *how* the coder
authenticates (``anonymous`` + the link password) and *who* the project thinks
they are (the invitation's app identity).

The tests also pin the accident guards: a withdrawn or replaced invitation
turns the project read-only without touching a single local file, the same
invitation on two computers is surfaced as a warning, and a deleted link or a
changed password says so instead of looking like a network failure.
"""

from __future__ import annotations

import json
import os

import pytest

import app as app_module
import routes_online
from online import invite as invite_mod
from online import keyring_store, sharelink
from online.errors import ForbiddenError
from online.nextcloud import LINK_GONE_MESSAGE, READONLY_MESSAGE
from online.store import OnlineStore
from online.tests.webdav_fake import WebDAVFake
from studio_state import S

OWNER_PW = "own-app-password"
PROJECT = {"members": [{"username": "own", "display_name": "Owner", "role": "owner",
                        "tags": ["O"], "tasks": []}]}


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
    except Exception:                           # noqa: BLE001
        pass
    S.online = False
    S.store = None
    routes_online.set_store(None)
    routes_online._ACTIVE.clear()
    routes_online._REOPEN.update(running=False, failed=None)
    if original and os.path.isdir(original):
        S.configure(original)


@pytest.fixture
def keyring(monkeypatch):
    kr = FakeKeyring()
    monkeypatch.setattr(keyring_store, "_keyring", kr)
    return kr


@pytest.fixture
def client():
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


@pytest.fixture
def share(tmp_path):
    root = tmp_path / "share"
    for rel, data in {"Project/project.json": b'{"scheme": "x"}\n',
                      "Text/K1.txt": b"paper one\n",
                      "Text/K2.txt": b"paper two\n"}.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    (root / ".studio").mkdir()
    (root / ".studio" / "members.json").write_text(json.dumps(PROJECT), encoding="utf-8")
    fake = WebDAVFake(str(root))
    fake.state.users = {"own": OWNER_PW}        # the owner's own dav path
    url = fake.start()
    try:
        yield fake, url, tmp_path
    finally:
        fake.stop()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _owner_connect(client, url):
    r = client.post("/api/online/connect", json={
        "kind": "nextcloud", "url": url, "username": "own",
        "app_password": OWNER_PW, "folder": "", "webdav_root": url})
    assert r.status_code == 200, r.get_json()
    pid = r.get_json()["projects"][0]["id"]
    r = client.post("/api/online/open", json={"project_id": pid})
    assert r.status_code == 200, r.get_json()
    return pid


def _create_link(client):
    r = client.post("/api/online/link", json={"action": "create"})
    assert r.status_code == 200, r.get_json()
    data = r.get_json()
    assert data["available"] is True, data
    assert data["link"]["public_root"].endswith("/public.php/dav/files/" + data["link"]["token"])
    assert data["password_stored"] is True
    return data["link"]


def _invite(client, display, tags):
    r = client.post("/api/online/link/invite",
                    json={"display_name": display, "tags": tags, "tasks": ["coding", "settle"]})
    assert r.status_code == 200, r.get_json()
    data = r.get_json()
    assert data["invitation"].startswith("csinvite1.")
    assert invite_mod.parse_invite(data["invitation"])["password"]
    return data


def _join(base, text, passphrase=None):
    inv = invite_mod.parse_invite(text, passphrase)
    store = OnlineStore(base_dir=str(base), debounce=0, autostart=False, use_keyring=False)
    store.connect_nextcloud(inv["root"], inv["member"]["username"], inv["password"], folder="",
                            webdav_root=inv["root"], auth_user="anonymous", login="link",
                            invite_id=inv["member"].get("invite_id"))
    pid = store.client.project_id
    store.open(pid, start=False)
    return store


def _sync(store):
    return store.sync_now()


def _remote(fake, rel):
    path = os.path.join(fake.root, *rel.split("/"))
    return open(path, "rb").read() if os.path.exists(path) else None


def _flask_store():
    return routes_online._STORE


# ---------------------------------------------------------------------------
# 1. the whole flow: create, invite, join, code, converge
# ---------------------------------------------------------------------------

def test_owner_creates_a_link_and_two_coders_converge_without_accounts(client, share, keyring):
    fake, url, tmp = share
    _owner_connect(client, url)
    link = _create_link(client)

    alice = _invite(client, "Alice", ["AL"])
    bob = _invite(client, "Bob", ["BO"])
    assert alice["member"]["username"] != bob["member"]["username"]
    assert alice["member"]["tags"] == ["AL"] and bob["member"]["tags"] == ["BO"]

    a = _join(tmp / "a", alice["invitation"])
    b = _join(tmp / "b", bob["invitation"])
    try:
        assert a.me()["role"] == "coder" and a.me()["tags"] == ["AL"]
        assert a.me().get("login") == "link"
        for store, rel, body, who in ((a, "Codings/K1 - AL.json", {"coder": "AL"}, "Alice"),
                                      (b, "Codings/K1 - BO.json", {"coder": "BO"}, "Bob")):
            full = os.path.join(store.root, *rel.split("/"))
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8") as fh:
                json.dump(body, fh)
            _sync(store)
        assert json.loads(_remote(fake, "Codings/K1 - AL.json")) == {"coder": "AL"}
        assert json.loads(_remote(fake, "Codings/K1 - BO.json")) == {"coder": "BO"}

        # the owner sees both
        _sync(_flask_store())
        assert _flask_store().listdir("Codings")
        paths = {e["path"] for e in _flask_store().listdir("Codings")}
        assert {"Codings/K1 - AL.json", "Codings/K1 - BO.json"} <= paths

        # A may not write B's tag (same rule as an account member)
        bad = os.path.join(a.root, *"Codings/K2 - BO.json".split("/"))
        os.makedirs(os.path.dirname(bad), exist_ok=True)
        with open(bad, "w", encoding="utf-8") as fh:
            json.dump({"coder": "BO", "forged": True}, fh)
        _sync(a)
        assert _remote(fake, "Codings/K2 - BO.json") is None
        assert a.status()["pending"] == 0
        # nothing was pushed and nothing was lost: the file is still in A's mirror
        assert os.path.exists(bad)
    finally:
        a.close()
        b.close()
    # the link document holds no password anywhere on the share
    raw = _remote(fake, ".studio/link.json")
    assert raw and b"password" not in raw.lower()
    assert json.loads(raw)["public_root"] == link["public_root"]
    # nor in the app config
    from studio_state import config_path
    assert "password" not in open(config_path(), encoding="utf-8").read().lower()


def test_the_link_password_cannot_open_the_owners_files(client, share, keyring):
    fake, url, _tmp = share
    _owner_connect(client, url)
    link = _create_link(client)
    pw = sharelink.link_password(link["public_root"])
    assert pw
    # anonymous + the link password opens exactly the shared folder
    from online.webdav import WebDAV
    dav = WebDAV(link["public_root"], "anonymous", pw, allow_insecure_http=True)
    try:
        assert dav.exists("Text/K1.txt")
    finally:
        dav.close()
    # the same password on the owner's own dav path is refused
    from online.errors import AuthError
    own = WebDAV(f"{url}/remote.php/dav/files/own", "own", pw, allow_insecure_http=True)
    try:
        with pytest.raises(AuthError):
            own.stat("")
    finally:
        own.close()
    right = WebDAV(f"{url}/remote.php/dav/files/own", "own", OWNER_PW, allow_insecure_http=True)
    try:
        assert right.exists("Text/K1.txt")
    finally:
        right.close()


# ---------------------------------------------------------------------------
# 2. auth identity vs coder identity
# ---------------------------------------------------------------------------

def test_link_mode_authenticates_anonymous_but_acts_as_the_member(client, share, keyring):
    fake, url, tmp = share
    _owner_connect(client, url)
    _create_link(client)
    invited = _invite(client, "Alice", ["AL"])
    a = _join(tmp / "a", invited["invitation"])
    try:
        cl = a.client
        assert cl.login_mode == "link"
        assert cl.auth_user == "anonymous" and cl.dav.username == "anonymous"
        assert cl.username == invited["member"]["username"]
        assert invited["member"]["username"] not in cl.root_url
        assert "link" in repr(cl) and invite_mod.parse_invite(invited["invitation"])["password"] not in repr(cl)

        fake.resets()                       # only this client's requests from here on
        lock = a.acquire_lock("K1")
        assert lock["user"] == cl.username
        a.mark_done("K1", "coding", "AL")
        a.presence()
        # every request this client made carried the anonymous Basic-auth user
        users = {user for user, _m, _p in fake.state.requests}
        assert users == {"anonymous"}, users
        # while locks / done / presence carry the member identity
        from online.nextcloud import done_path, lock_path, presence_path
        held = json.loads(_remote(fake, lock_path("K1")))
        assert held["user"] == cl.username
        assert json.loads(_remote(fake, done_path("AL")))["tag"] == "AL"
        row = json.loads(_remote(fake, presence_path(a.mirror.state["client_id"])))
        assert row["user"] == cl.username and row.get("login") == "link"
        presence = [json.loads(_remote(fake, ".studio/presence/" + name))
                    for name in os.listdir(os.path.join(fake.root, ".studio", "presence"))
                    if name.endswith(".json")]
        assert {p["user"] for p in presence} == {cl.username}
        assert all(p.get("login") == "link" for p in presence)
    finally:
        a.close()


def test_a_link_member_cannot_administer_the_project(client, share, keyring):
    fake, url, tmp = share
    _owner_connect(client, url)
    _create_link(client)
    a = _join(tmp / "a", _invite(client, "Alice", ["AL"])["invitation"])
    try:
        pid = a.client.project_id
        with pytest.raises(ForbiddenError):
            a.client.update_settings(pid, {"lock_ttl_h": 1})
        with pytest.raises(ForbiddenError):
            a.client.put_member(pid, 1, {"role": "owner"})
        with pytest.raises(ForbiddenError):
            a.client.invite_member(pid, {"display_name": "Mallory", "tags": ["M"]})
        with pytest.raises(ForbiddenError):
            a.client.put_link(pid, {"public_root": "https://x/y"})
        assert a.status()["readonly"] is False
    finally:
        a.close()


# ---------------------------------------------------------------------------
# 3. withdrawal, re-issue, outbox
# ---------------------------------------------------------------------------

def test_withdrawing_an_invitation_is_read_only_and_keeps_every_edit(client, share, keyring):
    fake, url, tmp = share
    _owner_connect(client, url)
    _create_link(client)
    bob = _invite(client, "Bob", ["BO"])
    b = _join(tmp / "b", bob["invitation"])
    try:
        rel = "Codings/K3 - BO.json"
        full = os.path.join(b.root, *rel.split("/"))
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            json.dump({"v": 1}, fh)
        _sync(b)
        assert _remote(fake, rel) is not None

        r = client.delete(f"/api/online/link/invite/{bob['member']['username']}")
        assert r.status_code == 200, r.get_json()
        assert r.get_json()["member"]["revoked"] is True

        # a new local edit while withdrawn: never pushed, never deleted
        b.client._invalidate_project()      # the app re-reads members.json every 5 s
        with open(full, "w", encoding="utf-8") as fh:
            json.dump({"v": 2}, fh)
        _sync(b)
        st = b.status()
        assert st["readonly"] is True and st["readonly_reason"] == "withdrawn"
        assert st["readonly_message"] == READONLY_MESSAGE
        assert st["pending"] >= 1
        assert json.loads(_remote(fake, rel)) == {"v": 1}
        assert json.loads(open(full, encoding="utf-8").read()) == {"v": 2}
        with pytest.raises(ForbiddenError):
            b.acquire_lock("K1")
    finally:
        b.close()

    # re-issue: the old invitation stays read-only, the new one works
    r = client.post(f"/api/online/link/invite/{bob['member']['username']}/reissue", json={})
    assert r.status_code == 200, r.get_json()
    fresh = r.get_json()
    assert fresh["invitation"] != bob["invitation"]

    old = _join(tmp / "b", bob["invitation"])
    try:
        _sync(old)
        assert old.status()["readonly"] is True
        assert old.status()["readonly_reason"] == "replaced"
    finally:
        old.close()

    renewed = _join(tmp / "b", fresh["invitation"])
    try:
        _sync(renewed)
        assert renewed.status()["pending"] == 0
        assert renewed.status()["readonly"] is False
        # the edit made while withdrawn finally lands on the share
        assert json.loads(_remote(fake, "Codings/K3 - BO.json")) == {"v": 2}
    finally:
        renewed.close()


# ---------------------------------------------------------------------------
# 4. the same invitation on two computers
# ---------------------------------------------------------------------------

def test_the_same_invitation_on_two_computers_is_warned_about(client, share, keyring):
    fake, url, tmp = share
    _owner_connect(client, url)
    _create_link(client)
    invited = _invite(client, "Alice", ["AL"])
    one = _join(tmp / "one", invited["invitation"])
    two = _join(tmp / "two", invited["invitation"])
    try:
        one.presence(key="K1", tab="coding")
        seen = two.presence(key="K1", tab="coding")
        mine = [row for row in seen if row.get("same_identity")]
        assert mine, seen
        assert mine[0]["user"] == invited["member"]["username"]
        assert mine[0]["ts_iso"] and mine[0]["display_name"]
        # it is a warning, not a block: the project still writes
        assert two.status()["readonly"] is False
    finally:
        one.close()
        two.close()


# ---------------------------------------------------------------------------
# 5. the link is deleted / its password changed
# ---------------------------------------------------------------------------

def test_deleting_the_link_says_so_and_keeps_the_mirror(client, share, keyring):
    fake, url, tmp = share
    _owner_connect(client, url)
    link = _create_link(client)
    a = _join(tmp / "a", _invite(client, "Alice", ["AL"])["invitation"])
    try:
        _sync(a)                                  # the mirror now holds the project
        local = os.path.join(a.root, "Codings", "K9 - AL.json")
        os.makedirs(os.path.dirname(local), exist_ok=True)
        with open(local, "w", encoding="utf-8") as fh:
            json.dump({"offline": True}, fh)
        assert _remote(fake, "Text/K1.txt")
        r = client.delete("/api/online/link")
        assert r.status_code == 200, r.get_json()
        assert link["token"] not in fake.state.shares
        _sync(a)
        a.client._invalidate_project()      # the app re-reads members.json every 5 s
        st = a.status()
        assert st["link_error"] == LINK_GONE_MESSAGE
        assert a.mirror is not None and os.path.isdir(a.root)
        assert os.path.exists(os.path.join(a.root, "Text", "K1.txt"))
        assert json.loads(open(local, encoding="utf-8").read()) == {"offline": True}
        assert st["pending"] >= 1
        # the members were withdrawn at the same time
        assert st["readonly"] is True
    finally:
        a.close()
    # the owner's panel no longer has a link
    data = client.get("/api/online/link").get_json()
    assert data["link"] is None


def test_renewing_the_password_invalidates_the_sent_invitations(client, share, keyring):
    fake, url, tmp = share
    _owner_connect(client, url)
    link = _create_link(client)
    old = _invite(client, "Dora", ["DO"])
    a = _join(tmp / "a", old["invitation"])
    try:
        r = client.post("/api/online/link/renew", json={})
        assert r.status_code == 200, r.get_json()
        data = r.get_json()
        assert data["invitations"], data
        assert data["invitations"][0]["member"]["username"] == old["member"]["username"]
        # the new password is stored, never returned
        assert sharelink.link_password(link["public_root"])
        assert "password" not in json.dumps(data).lower()
        _sync(a)
        assert a.status()["link_error"] == LINK_GONE_MESSAGE
    finally:
        a.close()
    fresh = _join(tmp / "a2", data["invitations"][0]["invitation"])
    try:
        assert fresh.status()["readonly"] is False
        _sync(fresh)
        assert fresh.status()["mode"] in ("online", "syncing")
    finally:
        fresh.close()
    # the renewed invitation works with the *new* password, the old one does not
    old_inv = invite_mod.parse_invite(old["invitation"])
    from online.errors import AuthError
    from online.webdav import WebDAV
    for pw, ok in ((old_inv["password"], False), (sharelink.link_password(link["public_root"]), True)):
        dav = WebDAV(link["public_root"], "anonymous", pw, allow_insecure_http=True)
        try:
            if ok:
                assert dav.exists("Text/K1.txt")
            else:
                with pytest.raises(AuthError):
                    dav.stat("")
        finally:
            dav.close()


# ---------------------------------------------------------------------------
# 6. joining twice / the routes
# ---------------------------------------------------------------------------

def test_importing_the_same_invitation_twice_reuses_the_mirror(client, share, keyring):
    fake, url, tmp = share
    _owner_connect(client, url)
    _create_link(client)
    invited = _invite(client, "Alice", ["AL"])
    first = _join(tmp / "a", invited["invitation"])
    root = first.root
    first.close()
    again = _join(tmp / "a", invited["invitation"])
    try:
        assert os.path.normcase(again.root) == os.path.normcase(root)
        assert again.status()["readonly"] is False
    finally:
        again.close()


def test_the_invite_route_describes_and_then_joins(client, share, keyring):
    fake, url, tmp = share
    _owner_connect(client, url)
    _create_link(client)
    protected = client.post("/api/online/link/invite", json={
        "display_name": "Eve", "tags": ["EV"], "tasks": ["coding"], "passphrase": "correct horse"}).get_json()
    assert protected["protected"] is True

    r = client.post("/api/online/invite", json={"text": protected["invitation"]})
    assert r.status_code == 200
    assert r.get_json()["needs_passphrase"] is True
    assert r.get_json()["display_name"] == "Eve"

    r = client.post("/api/online/join", json={"text": protected["invitation"], "passphrase": "wrong"})
    assert r.status_code == 400 and r.get_json()["error"] == "wrong_passphrase"
    assert routes_online._STORE.username == "own"          # nothing was switched

    # the owner's own session is still open; join in a fresh process-like store
    routes_online.set_store(None)
    S.clear_online()
    r = client.post("/api/online/join", json={"text": protected["invitation"],
                                             "passphrase": "correct horse"})
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["status"]["login"] == "link"
    assert body["status"]["readonly"] is False
    assert body["connection"]["login"] == "link"
    assert body["connection"]["backend"] == "nextcloud"
    assert S.online is True
    assert client.get("/api/online/status").get_json()["me"]["tags"] == ["EV"]
    assert "correct horse" not in json.dumps(body)


def test_a_link_member_is_refused_by_the_admin_routes(client, share, keyring):
    fake, url, tmp = share
    _owner_connect(client, url)
    _create_link(client)
    invited = _invite(client, "Alice", ["AL"])
    routes_online.set_store(None)
    S.clear_online()
    r = client.post("/api/online/join", json={"text": invited["invitation"]})
    assert r.status_code == 200, r.get_json()
    assert client.get("/api/online/admin/members").status_code == 403
    assert client.post("/api/online/link", json={"action": "create"}).status_code == 403


def test_the_owner_can_show_an_invitation_again_without_rotating_it(client, share, keyring):
    fake, url, tmp = share
    _owner_connect(client, url)
    _create_link(client)
    made = _invite(client, "Fay", ["FA"])
    r = client.get(f"/api/online/link/invite/{made['member']['username']}")
    assert r.status_code == 200, r.get_json()
    again = r.get_json()
    assert again["invitation"] == made["invitation"]          # the same invitation
    assert again["filename"].endswith(".csinvite")
    assert "Join with an invitation" in again["instructions"]
    r = client.get("/api/online/link/invite/nobody")
    assert r.status_code == 404


def test_a_link_connection_is_reopened_on_start_without_asking(client, share, keyring):
    fake, url, tmp = share
    _owner_connect(client, url)
    _create_link(client)
    invited = _invite(client, "Greta", ["GR"])
    routes_online.set_store(None)
    S.clear_online()
    r = client.post("/api/online/join", json={"text": invited["invitation"]})
    assert r.status_code == 200, r.get_json()
    cid = r.get_json()["connection"]["id"]
    mirror = S.ROOT

    # the app process ends and starts again
    routes_online._STORE.close()
    routes_online.set_store(None)
    routes_online._ACTIVE.clear()
    S.clear_online()
    assert routes_online.reopen_last_on_start() is True
    assert S.online is True
    assert os.path.normcase(S.ROOT) == os.path.normcase(mirror)
    st = client.get("/api/online/status").get_json()
    assert st["login"] == "link" and st["connection_id"] == cid
    assert st["readonly"] is False
    assert st["me"]["tags"] == ["GR"]
    # the link password is in the credential store, never in the config
    from studio_state import config_path
    assert "password" not in open(config_path(), encoding="utf-8").read().lower()


def test_coder_identities_are_unique_and_a_tag_cannot_be_reused(client, share, keyring):
    fake, url, tmp = share
    _owner_connect(client, url)
    _create_link(client)
    first = _invite(client, "Anna Berger", ["ANB"])
    # the same display name gets a different app identity, silently
    second = _invite(client, "Anna Berger", ["ANB2"])
    assert second["member"]["username"] != first["member"]["username"]
    # a tag another coder already owns is refused with a message a person can act on
    r = client.post("/api/online/link/invite",
                    json={"display_name": "Someone Else", "tags": ["ANB"], "tasks": ["coding"]})
    assert r.status_code == 400, r.get_json()
    message = r.get_json()["message"]
    assert "ANB" in message and "different tag" in message
    # the account owner's row in members.json keeps exactly its old fields
    raw = json.loads(open(os.path.join(fake.root, ".studio", "members.json"), encoding="utf-8").read())
    owner = raw["members"][0]
    assert "login" not in owner and "invite_id" not in owner and "user_id" not in owner
    link_rows = [m for m in raw["members"][1:]]
    assert all(m["login"] == "link" and m["invite_id"] and m["revoked"] is False
               for m in link_rows)


def test_the_owner_can_paste_a_link_created_in_nextcloud(client, share, keyring):
    fake, url, tmp = share
    _owner_connect(client, url)
    from online import sharelink
    ocs = sharelink.OcsClient(url, "own", OWNER_PW, allow_insecure_http=True)
    try:
        made = ocs.create_share("/", "pasted-link-pw")
    finally:
        ocs.close()
    # the wrong password is refused with a sentence, before anything is stored
    r = client.post("/api/online/link", json={"action": "attach",
                                             "url": f"{url}/s/{made['token']}",
                                             "password": "not-the-password"})
    assert r.status_code == 400, r.get_json()
    assert "not accepted" in r.get_json()["message"]
    # an unreachable host is explained, not a generic network error
    r = client.post("/api/online/link", json={"action": "attach",
                                             "url": "http://127.0.0.1:1/s/tok",
                                             "password": "whatever"})
    assert r.status_code == 400, r.get_json()
    assert "cannot be reached" in r.get_json()["message"]
    r = client.post("/api/online/link", json={"action": "attach",
                                             "url": f"{url}/s/{made['token']}",
                                             "password": "pasted-link-pw"})
    assert r.status_code == 200, r.get_json()
    data = r.get_json()
    assert data["link"]["token"] == made["token"]
    assert data["link"]["share_id"] == str(made["share_id"])
    assert data["password_stored"] is True
    # and an invitation can be generated from it without typing the password again
    invited = _invite(client, "Hana", ["HA"])
    assert invite_mod.parse_invite(invited["invitation"])["root"].endswith(made["token"])


def test_a_server_without_the_sharing_api_falls_back_to_pasting_a_link(tmp_path, keyring, monkeypatch):
    from online.tests.webdav_fake import WebDAVFake
    root = tmp_path / "plain"
    (root / "Project").mkdir(parents=True)
    (root / "Project" / "project.json").write_text('{"scheme": "x"}\n', encoding="utf-8")
    (root / ".studio").mkdir()
    (root / ".studio" / "members.json").write_text(json.dumps(PROJECT), encoding="utf-8")
    fake = WebDAVFake(str(root), ocs=False)       # LRZ Sync+Share / an old server
    url = fake.start()
    monkeypatch.setenv("CODINGSTUDIO_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setenv("CODINGSTUDIO_ONLINE_DIR", str(tmp_path / "online"))
    import app as app_module
    app_module.app.config["TESTING"] = True
    client = app_module.app.test_client()
    try:
        _owner_connect(client, url)
        r = client.post("/api/online/link", json={"action": "create"})
        assert r.status_code == 400
        body = r.get_json()
        assert body["error"] == "ocs_unavailable"
        assert "paste" in body["message"]
        data = client.get("/api/online/link").get_json()
        assert data["available"] is False
        assert data["reason"]
        assert data["link"] is None
        # the paste flow still works against a plain WebDAV share (no OCS)
        public = sharelink.public_root_for(url, "handmade")
        fake.state.shares["handmade"] = {"id": "90", "token": "handmade",
                                         "password": "pw90", "path": "/", "permissions": 15}
        r = client.post("/api/online/link", json={"action": "attach",
                                                 "url": f"{url}/s/handmade", "password": "pw90"})
        assert r.status_code == 200, r.get_json()
        assert r.get_json()["link"]["public_root"] == public
    finally:
        fake.stop()


def test_a_broken_invitation_is_a_readable_error_not_a_crash(client):
    r = client.post("/api/online/join", json={"text": "hello, join my project"})
    assert r.status_code == 400
    assert r.get_json()["error"] == "not_invitation"
    assert "invitation" in r.get_json()["message"].lower()


def test_diagnose_explains_an_invitation_that_cannot_be_reached(client, share, keyring):
    """The join dialog's "Test connection": the route runs the checks with the
    invitation's own link and password and names the step that failed."""
    fake, url, _tmp = share
    _owner_connect(client, url)
    _create_link(client)
    alice = _invite(client, "Alice", ["AL"])
    # a working invitation: every step passes
    r = client.post("/api/online/diagnose", json={"text": alice["invitation"]})
    assert r.status_code == 200, r.get_json()
    ok = r.get_json()
    assert ok["ok"] is True, ok
    assert [s["step"] for s in ok["steps"]][:3] == ["address", "dns", "tcp"]
    # the answer never echoes the link password
    assert "password" not in json.dumps(ok).lower()

    # deleted link -> the webdav step fails, the transport steps pass
    r = client.delete("/api/online/link")
    assert r.status_code == 200
    r = client.post("/api/online/diagnose", json={"text": alice["invitation"]})
    bad = r.get_json()
    steps = {s["step"]: s for s in bad["steps"]}
    assert bad["ok"] is False
    assert steps["tcp"]["ok"] is True
    assert steps["webdav"]["ok"] is False
    assert "hint" in bad and bad["hint"]


def test_diagnose_needs_an_invitation_or_url(client):
    r = client.post("/api/online/diagnose", json={})
    assert r.status_code == 400
    assert r.get_json()["error"] == "bad_request"
