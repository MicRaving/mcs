"""The owner side of a share link: passwords, URLs, OCS calls, keyring (``sharelink``).

Measured on a real Nextcloud 34 (2026-11-26): ``POST
/ocs/v2.php/apps/files_sharing/api/v1/shares?format=json`` with
``OCS-APIRequest: true`` and ``path=/<folder>&shareType=3&permissions=15&password=…``
returns ``ocs.data.{id, token, url}``; ``PUT`` changes the password and
``DELETE`` revokes the link.  The local fake implements that slice, and these
tests use it for the calls and a plain WebDAV share for the fallback.
"""

from __future__ import annotations

import json
import os
import string

import pytest

from online import keyring_store, sharelink
from online.nextcloud import NextcloudClient
from online.tests.webdav_fake import WebDAVFake


class FakeKeyring:
    def __init__(self):
        self.store: dict = {}

    def set_password(self, service, account, secret):
        self.store[(service, account)] = secret

    def get_password(self, service, account):
        return self.store.get((service, account))

    def delete_password(self, service, account):
        self.store.pop((service, account), None)


@pytest.fixture
def keyring(monkeypatch):
    kr = FakeKeyring()
    monkeypatch.setattr(keyring_store, "_keyring", kr)
    return kr


@pytest.fixture
def share(tmp_path):
    root = tmp_path / "share"
    (root / "Project").mkdir(parents=True)
    (root / "Project" / "project.json").write_text('{"scheme": "x"}\n', encoding="utf-8")
    fake = WebDAVFake(str(root))
    url = fake.start()
    try:
        yield fake, url
    finally:
        fake.stop()


# ---------------------------------------------------------------------------
# passwords
# ---------------------------------------------------------------------------

def test_random_password_is_policy_safe_and_not_a_constant():
    seen = set()
    for _ in range(20):
        pw = sharelink.random_password(24)
        assert len(pw) == 24
        assert any(c in string.ascii_lowercase for c in pw)
        assert any(c in string.ascii_uppercase for c in pw)
        assert any(c in string.digits for c in pw)
        assert any(c in sharelink._PW_SYMBOL for c in pw)
        seen.add(pw)
    assert len(seen) == 20


def test_a_short_request_is_lifted_to_a_usable_length():
    assert len(sharelink.random_password(4)) >= 12


# ---------------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------------

def test_share_urls_are_understood_in_every_form_nextcloud_shows():
    token = "AbCdEf123456"
    for raw in (f"https://cloud.example.org/s/{token}",
                f"https://cloud.example.org/index.php/s/{token}",
                f"https://cloud.example.org/s/{token}/download",
                f"https://cloud.example.org/public.php/dav/files/{token}",
                f"https://cloud.example.org/public.php/dav/files/{token}/"):
        got = sharelink.parse_share_url(raw)
        assert got["token"] == token, raw
        assert got["public_root"] == f"https://cloud.example.org/public.php/dav/files/{token}"
        assert got["url"] == f"https://cloud.example.org/s/{token}"


def test_a_bare_token_is_resolved_against_the_connected_nextcloud():
    got = sharelink.parse_share_url("/s/tok123", "https://cloud.example.org")
    assert got["token"] == "tok123"
    assert got["public_root"].startswith("https://cloud.example.org/public.php/dav/files/")


def test_a_link_that_is_not_a_share_link_is_refused_with_a_readable_message():
    with pytest.raises(sharelink.ShareLinkError) as exc:
        sharelink.parse_share_url("https://cloud.example.org/apps/files/?dir=/Coding")
    assert exc.value.code == "bad_link"
    assert "/s/" in str(exc.value)
    with pytest.raises(sharelink.ShareLinkError):
        sharelink.parse_share_url("")


def test_public_root_and_share_url_roundtrip():
    root = sharelink.public_root_for("https://cloud.example.org", "tok")
    assert root == "https://cloud.example.org/public.php/dav/files/tok"
    assert sharelink.share_url_for(root) == "https://cloud.example.org/s/tok"
    assert sharelink.is_public_root(root) is True
    assert sharelink.is_public_root("https://cloud.example.org/remote.php/dav/files/a") is False


# ---------------------------------------------------------------------------
# OCS
# ---------------------------------------------------------------------------

def test_ocs_create_renew_and_delete_the_link(share):
    fake, url = share
    ocs = sharelink.OcsClient(url, "own", "pw", allow_insecure_http=True)
    try:
        assert ocs.available() is True
        created = ocs.create_share("/", "first-password")
        assert created["share_id"] and created["token"]
        assert fake.state.shares[created["token"]]["password"] == "first-password"
        assert fake.state.shares[created["token"]]["permissions"] == 15
        ocs.update_password(created["share_id"], "second-password")
        assert fake.state.shares[created["token"]]["password"] == "second-password"
        listed = ocs.list_shares("/")
        assert any(str(row.get("token")) == created["token"] for row in listed)
        ocs.delete_share(created["share_id"])
        assert created["token"] not in fake.state.shares
        # deleting an already gone share is the wanted state, not an error
        ocs.delete_share(created["share_id"])
    finally:
        ocs.close()


def test_a_plain_webdav_share_has_no_ocs(tmp_path):
    root = tmp_path / "plain"
    root.mkdir()
    (root / "x.txt").write_text("x", encoding="utf-8")
    fake = WebDAVFake(str(root), ocs=False)
    url = fake.start()
    try:
        ocs = sharelink.OcsClient(url, "a", "b", allow_insecure_http=True)
        try:
            assert ocs.available() is False
            with pytest.raises(sharelink.ShareLinkUnavailable):
                ocs.create_share("/", "pw")
        finally:
            ocs.close()
    finally:
        fake.stop()


def test_ocs_refuses_a_wrong_share_id_without_deleting_anything(share):
    fake, url = share
    ocs = sharelink.OcsClient(url, "own", "pw", allow_insecure_http=True)
    try:
        made = ocs.create_share("/", "pw")
        with pytest.raises(sharelink.ShareLinkError) as exc:
            ocs.update_password("99999", "other")
        assert exc.value.code == "ocs_404"
        assert fake.state.shares[made["token"]]["password"] == "pw"
    finally:
        ocs.close()


# ---------------------------------------------------------------------------
# the link password: keyring keyed by the link root
# ---------------------------------------------------------------------------

def test_the_link_password_lives_in_the_keyring_keyed_by_the_public_root(keyring):
    root = sharelink.public_root_for("https://cloud.example.org", "tok")
    sharelink.save_link_password(root, "s3cret-link-pw")
    assert sharelink.link_password(root) == "s3cret-link-pw"
    assert keyring.store and all(root in account for _service, account in keyring.store)
    sharelink.forget_link_password(root)
    assert sharelink.link_password(root) is None


def test_the_file_fallback_goes_into_its_own_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(keyring_store, "_keyring", None)
    root = sharelink.public_root_for("https://cloud.example.org", "tok")
    fallback = str(tmp_path / ".studio" / "link")
    sharelink.save_link_password(root, "pw-in-a-file", fallback_dir=fallback)
    assert os.path.exists(os.path.join(fallback, "token"))
    assert sharelink.link_password(root, fallback_dir=fallback) == "pw-in-a-file"
    # never the connection token's own file
    assert not os.path.exists(str(tmp_path / ".studio" / "token"))


# ---------------------------------------------------------------------------
# .studio/link.json
# ---------------------------------------------------------------------------

def test_link_document_roundtrip_on_the_share(share):
    fake, url = share
    client = NextcloudClient(url, "own", "pw", webdav_root_url=url, allow_insecure_http=True)
    try:
        assert client.link(client.project_id) is None
        doc = {"share_id": "7", "token": "tok", "created_at": "2026-11-26T12:00:00Z",
               "public_root": sharelink.public_root_for(url, "tok"), "password": "must not be written"}
        saved = client.put_link(client.project_id, doc)
        assert "password" not in saved
        on_disk = json.loads(open(os.path.join(fake.root, ".studio", "link.json"), "rb").read())
        assert "password" not in on_disk
        again = client.link(client.project_id)
        assert again["public_root"] == doc["public_root"] and again["share_id"] == "7"
        client.delete_link(client.project_id)
        assert client.link(client.project_id) is None
    finally:
        client.close()
