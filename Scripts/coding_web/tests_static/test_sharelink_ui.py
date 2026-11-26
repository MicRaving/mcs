"""Static UI guards for share-link projects (D11).

The frontend has no DOM test harness (the app is plain scripts loaded by
``index.html``), so this suite pins the wiring that a broken rename would
silently remove, and checks that every static script still parses.

* the connect dialog offers "Join with an invitation" (paste, file, drop) and
  shows the passphrase field only when the invitation needs one;
* the read-only / link-gone banner exists and is fed by ``status.readonly`` and
  ``status.link_error``;
* a presence row carrying ``same_identity`` produces the "also in use on
  another computer" warning;
* the project-admin Members tab carries the share-link panel, the invitation
  copy/save actions and the withdraw/re-issue controls;
* every ``static/*.js`` passes ``node --check`` (skipped when node is absent).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "static"


def _text(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8", errors="replace")


def test_the_connect_dialog_can_join_with_an_invitation():
    js = _text("online.js")
    for needle in ("Join with an invitation", "online-invite-text", "online-invite-file",
                   "online-invite-passphrase", "online-join-btn", "api/online/invite",
                   "api/online/join", "csinvite1", ".csinvite", "Open file…"):
        assert needle in js, needle
    # the passphrase is asked for only when the server says it is needed
    assert "needs_passphrase" in js
    assert "passField.hidden = !info.needs_passphrase" in js
    # a dropped file is read into the same box
    assert 'addEventListener("drop"' in js


def test_the_read_only_and_link_gone_banner_is_wired():
    js = _text("online.js")
    assert "online-banner" in js
    assert "renderReadonlyBanner" in js
    assert "status.readonly" in js or "st.readonly" in js
    assert "st.link_error" in js
    assert "read-only" in js
    css = _text("online.css")
    assert ".cs-online-banner" in css


def test_the_duplicate_identity_warning_is_surfaced():
    js = _text("online.js")
    assert "same_identity" in js
    assert "also in use on another computer" in js
    assert "ask the owner for your own invitation" in js


def test_the_admin_panel_carries_the_share_link_section():
    js = _text("admin_online.js")
    for needle in ('section("Share link"', "Create a share link", "Use this link",
                   "api/online/link/invite", "Renew link password", "Delete link",
                   "Re-issue", "Withdraw", "Copy invitation", "Save as file"):
        assert needle in js, needle
    assert "Protect the invitation with a passphrase" in js
    # the link section is only rendered for a Nextcloud backend
    assert 'status.backend === "nextcloud"' in js
    assert ".oa-invite" in _text("admin_online.css")


def test_the_saved_connection_badge_says_nextcloud_link():
    assert "Nextcloud link" in _text("online.js")


@pytest.mark.parametrize("path", sorted(STATIC.glob("*.js")), ids=lambda p: p.name)
def test_static_scripts_parse(path: Path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    out = subprocess.run([node, "--check", str(path)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, f"{path.name}:\n{out.stdout}\n{out.stderr}"
