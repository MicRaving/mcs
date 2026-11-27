"""The bundled CA store and the connection test.

The macOS report of 2026-11-26 ("the project cannot be reached" when joining an
invitation, and the in-app update failing) is what a frozen build without a
trust store looks like: every HTTPS call fails the same way.  ``online.tls``
pins the CA file; ``online.netdiag`` says which step failed instead of
collapsing everything into "offline".
"""

from __future__ import annotations

import json
import os

import pytest

from online import netdiag, tls
from online.tests import webdav_fake


def test_the_context_has_certificates():
    ctx = tls.context()
    assert len(ctx.get_ca_certs()) > 10
    assert ctx.verify_mode.name == "CERT_REQUIRED" and ctx.check_hostname


def test_a_named_ca_file_wins(monkeypatch, tmp_path):
    import ssl

    pem = tmp_path / "one.pem"
    # a real (self-signed) certificate, so create_default_context accepts it
    os.system(f'openssl req -x509 -newkey rsa:2048 -nodes -keyout {tmp_path}/k.pem '
              f'-out {pem} -days 1 -subj /CN=test 2>/dev/null')
    if not pem.exists():
        pytest.skip("openssl CLI not available")
    monkeypatch.setenv("CODINGSTUDIO_CA_FILE", str(pem))
    monkeypatch.setattr(tls, "_CONTEXT", None)
    try:
        assert tls.ca_file() == str(pem)
        assert isinstance(tls.context(), ssl.SSLContext)
    finally:
        monkeypatch.undo()
        tls._CONTEXT = None


def test_diagnose_reaches_a_working_share(tmp_path):
    root = tmp_path / "share"
    root.mkdir()
    (root / "Text").mkdir()
    (root / "Text" / "a.txt").write_text("x", encoding="utf-8")
    fake = webdav_fake.WebDAVFake(str(root))
    fake.state.users = {"anna": "pw"}
    url = fake.start()
    try:
        result = netdiag.run(url, "anna", "pw")
        assert result["ok"] is True, result
        steps = {s["step"]: s for s in result["steps"]}
        assert set(steps) >= {"address", "dns", "tcp", "webdav", "listing"}
        assert steps["listing"]["entries"] >= 2
        assert result["tls"]["ca_count"] > 10
        assert "hint" not in result
    finally:
        fake.stop()


def test_diagnose_names_a_refused_password(tmp_path):
    root = tmp_path / "share"
    root.mkdir()
    fake = webdav_fake.WebDAVFake(str(root))
    fake.state.users = {"anna": "right"}
    url = fake.start()
    try:
        # an account URL, where the fake (like Nextcloud) enforces the password
        result = netdiag.run(f"{url}/remote.php/dav/files/anna", "anna", "wrong")
        assert result["ok"] is False
        steps = {s["step"]: s for s in result["steps"]}
        assert steps["tcp"]["ok"] and steps["webdav"]["ok"] is False
        assert steps["webdav"]["status"] in (401, 403)
        assert "deleted" in result["hint"]
    finally:
        fake.stop()


def test_diagnose_names_a_name_that_does_not_resolve():
    result = netdiag.run("https://no-such-host.invalid/remote.php/dav", "a", "b")
    assert result["ok"] is False
    steps = {s["step"]: s for s in result["steps"]}
    assert steps["dns"]["ok"] is False
    assert "cannot find" in result["hint"]
    assert json.dumps(result)              # serialisable for the route
