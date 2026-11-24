"""Tests for the browser-mode route policy (PLAN_online_only §7, WP2).

The policy must be **total** over the app's URL map: every endpoint the app
registers is classified here, and an unknown route is refused by default.  The
``install`` hook must be inert unless ``BROWSER_MODE`` is on.
"""

from __future__ import annotations

import pytest

import app as app_module
import browser_policy as bp

APP = app_module.app


@pytest.fixture(scope="module", autouse=True)
def _install_policy():
    bp.install(APP)
    APP.config["BROWSER_MODE"] = False
    APP.config.pop("BROWSER_ROLE", None)
    yield
    APP.config["BROWSER_MODE"] = False


# ---------------------------------------------------------------- totality
def test_every_registered_route_is_classified():
    """A new route cannot slip through: the table must cover the whole url_map."""
    rules = list(APP.url_map.iter_rules())
    assert rules, "the app registered no routes"
    missing, bad = [], []
    for rule in rules:
        if rule.endpoint not in bp.CLASSIFICATION:
            missing.append(rule.endpoint)
            continue
        cls = bp.classification(rule.endpoint)
        if cls not in (bp.ALLOW, bp.OWNER_ONLY, bp.DESKTOP_ONLY):
            bad.append((rule.endpoint, cls))
        # every method the route carries resolves through the public function
        for method in sorted(rule.methods or ()):
            if method in ("HEAD", "OPTIONS"):
                continue
            allowed, message = bp.browser_route_allowed(rule.endpoint, method, "owner")
            if cls == bp.DESKTOP_ONLY and (allowed or not message):
                bad.append((rule.endpoint, method, allowed, message))
    assert not missing, f"endpoints missing from browser_policy.CLASSIFICATION: {missing}"
    assert not bad, f"endpoints with an invalid classification: {bad}"


def test_table_has_no_dead_entries():
    registered = {rule.endpoint for rule in APP.url_map.iter_rules()}
    dead = sorted(set(bp.CLASSIFICATION) - registered)
    assert not dead, f"browser_policy classifies endpoints the app no longer serves: {dead}"


def test_summary_counts_match_the_table():
    counts = bp.summary()
    assert counts["endpoints"] == len(bp.CLASSIFICATION)
    assert sum(counts["counts"].values()) == len(bp.CLASSIFICATION)
    assert len(counts["desktop_only"]) == counts["counts"][bp.DESKTOP_ONLY]
    assert len(counts["owner_only"]) == counts["counts"][bp.OWNER_ONLY]


# ---------------------------------------------------------------- verdicts
def test_unknown_endpoint_is_denied_by_default():
    allowed, message = bp.browser_route_allowed("not.a.route", "GET", "owner")
    assert allowed is False
    assert message
    status, body = bp.denial("not.a.route", "GET", "owner")
    assert status == 403
    assert body["error"] == "desktop_only"
    assert "not classified" in body["message"]


def test_unknown_role_is_denied():
    allowed, message = bp.browser_route_allowed("core.api_papers", "GET", "admin")
    assert allowed is False
    assert "unknown role" in message


def test_me_docket_and_missing_role_resolve_to_the_member_role():
    docket = {"user": {"name": "x", "role": "owner"}, "projects": []}
    assert bp.browser_route_allowed("screening.api_screening_merge", "POST", docket) == (True, "")
    docket = {"user": {"role": "coder"}}
    assert bp.browser_route_allowed("screening.api_screening_merge", "POST", docket)[0] is False
    # an unresolved role is a member, never the owner
    assert bp.browser_route_allowed("core.api_papers", "GET", None) == (True, "")
    assert bp.browser_route_allowed("screening.api_screening_merge", "POST", None)[0] is False


def test_allow_route_is_open_to_every_role():
    for role in bp.ROLES:
        assert bp.browser_route_allowed("core.api_coding", "GET", role) == (True, "")


def test_desktop_only_is_refused_for_every_role():
    for role in bp.ROLES:
        allowed, message = bp.browser_route_allowed("core.api_set_root", "POST", role)
        assert allowed is False
        assert message
        status, body = bp.denial("core.api_set_root", "POST", role)
        assert status == 403
        assert body["error"] == "desktop_only"


def test_owner_only_needs_the_owner_role():
    assert bp.browser_route_allowed("screening.api_screening_merge", "POST", "owner") == (True, "")
    for role in ("coder", "viewer"):
        allowed, message = bp.browser_route_allowed("screening.api_screening_merge", "POST", role)
        assert allowed is False
        assert "owner-only" in message
        status, body = bp.denial("screening.api_screening_merge", "POST", role)
        assert status == 403
        assert body["error"] == "forbidden"
        assert body["message"] == message


@pytest.mark.parametrize("endpoint", [
    "analysis.api_analysis_run",        # R5
    "repro.api_repro_run",              # user-pushed code
    "repro.api_repro_dossier_edit",     # in-place append into Results/
    "scheme.api_put_scheme",            # read-only scheme library in the image
    "online.api_connect",               # the gateway owns the session
    "online.api_admin_publish",         # source_root is a server path
    "settings.api_update_apply",        # replaces the executable
])
def test_the_plan_s_desktop_only_routes_are_refused(endpoint):
    allowed, message = bp.browser_route_allowed(endpoint, "POST", "owner")
    assert allowed is False
    assert message


@pytest.mark.parametrize("endpoint", [
    "analysis.api_analysis_run_status",   # reading the result stays allowed
    "repro.api_repro_run_status",
    "online.api_admin_publish_progress",
    "online.api_cache_get",
])
def test_reading_a_desktop_only_action_s_result_is_allowed(endpoint):
    assert bp.browser_route_allowed(endpoint, "GET", "owner") == (True, "")


# ---------------------------------------------------------------- the hook
def _client():
    return APP.test_client()


def test_install_is_idempotent():
    before = len(APP.before_request_funcs.get(None) or [])
    bp.install(APP)
    assert len(APP.before_request_funcs.get(None) or []) == before


def test_browser_mode_off_does_not_change_behaviour():
    APP.config["BROWSER_MODE"] = False
    APP.config["BROWSER_ROLE"] = "coder"
    resp = _client().post("/api/root", json={"root": ""})
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "folder does not exist"
    # a route the policy would refuse on is reachable again, i.e. the guard is inert
    resp = _client().post("/api/repro/dossier/edit", json={})
    assert resp.status_code != 403 or resp.get_json().get("error") != "desktop_only"


def test_browser_mode_on_refuses_desktop_only_routes():
    APP.config["BROWSER_MODE"] = True
    APP.config["BROWSER_ROLE"] = "coder"
    resp = _client().post("/api/root", json={"root": ""})
    assert resp.status_code == 403
    body = resp.get_json()
    assert body["error"] == "desktop_only"
    assert "desktop-only" in body["message"]
    resp = _client().post("/api/pick-root", json={})
    assert resp.status_code == 403
    assert resp.get_json()["error"] == "desktop_only"


def test_browser_mode_on_keeps_allowed_routes_working():
    APP.config["BROWSER_MODE"] = True
    APP.config["BROWSER_ROLE"] = "coder"
    resp = _client().get("/api/root")
    assert resp.status_code == 200
    assert resp.get_json().get("ok") is not False


def test_browser_mode_on_owner_only_by_role():
    APP.config["BROWSER_MODE"] = True
    APP.config["BROWSER_ROLE"] = "coder"
    resp = _client().post("/api/screening/merge", json={})
    assert resp.status_code == 403
    assert resp.get_json()["error"] == "forbidden"
    APP.config["BROWSER_ROLE"] = "owner"
    resp = _client().post("/api/screening/merge", json={})
    assert resp.status_code == 400          # the view's own "coder required"
    assert resp.get_json().get("error") != "forbidden"


def test_browser_mode_on_unknown_role_refuses_allowed_routes():
    APP.config["BROWSER_MODE"] = True
    APP.config["BROWSER_ROLE"] = "intruder"
    resp = _client().get("/api/root")
    assert resp.status_code == 403
    assert resp.get_json()["error"] == "forbidden"
