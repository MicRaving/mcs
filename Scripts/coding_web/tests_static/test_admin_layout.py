"""The project admin is one page inside Settings, with one place for people.

The owner's report (2026-11-26): a separate modal with six tabs was hard to
use, and "Add member" plus "Server users" were two lists of the same people.
"""

from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "static"


def _text(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8", errors="replace")


def test_settings_has_one_admin_entry_and_panel():
    html = _text("index.html")
    assert 'data-section="admin"' in html
    assert 'id="st-panel-admin"' in html and 'id="st-admin-body"' in html
    # the other sections stay what they were
    assert 'data-section="project"' in html and 'data-section="updates"' in html


def test_the_admin_is_rendered_inline_not_as_a_modal():
    js = _text("admin_online.js")
    # one renderer into a container; no overlay/dialog any more
    assert "function renderInline(" in js
    assert "window.csOnlineAdmin = { render: renderInline, close }" in js
    for gone in ('role: "dialog"', 'aria-modal', ".oa-tabs", "function selectTab("):
        assert gone not in js, gone
    settings = _text("settings.js")
    assert 'if (state.section === "admin") loadAdmin();' in settings
    assert "csOnlineAdmin.render" in settings


def test_people_are_managed_in_exactly_one_place():
    js = _text("admin_online.js")
    assert "function addPersonForm(" in js
    assert '"Add a person"' in js
    # the two old duplicate lists are gone
    assert "function usersTable(" not in js
    assert "function createUserForm(" not in js
    assert '"Server users"' not in js
    assert '"Add member"' not in js
    # the account switch lives on the same person row
    assert "Disable account" in js and "Enable account" in js
    # the Nextcloud project explains where accounts come from
    assert "Co-authors with their own Nextcloud account" in js


def test_locks_live_with_blinding_and_leases():
    js = _text("admin_online.js")
    assert "function lockListSection(" in js
    assert "VIEWS.locks" not in js
    assert "Blinding & locks" in js


def test_the_header_entry_opens_settings_not_a_modal():
    js = _text("online.js")
    assert "window.CS.openSettings(\"admin\")" in js
    settings = _text("settings.js")
    assert "window.CS.openSettings = function" in settings
