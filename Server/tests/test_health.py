"""Health endpoint and the client/server protocol guard (§3.1, §3.10)."""

from __future__ import annotations

from studio_server import PROTOCOL_VERSION, SERVER_VERSION


def test_health_needs_no_auth(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["version"] == SERVER_VERSION
    assert body["protocol"] == PROTOCOL_VERSION
    # the browser-mode toggle is part of the contract (off by default in tests)
    assert body["browser_mode"] == "off"


def test_health_accepts_matching_protocol(client):
    response = client.get("/api/health", headers={"X-Studio-Protocol": str(PROTOCOL_VERSION)})
    assert response.status_code == 200


def test_protocol_mismatch_is_426(client):
    response = client.get("/api/health", headers={"X-Studio-Protocol": "2"})
    assert response.status_code == 426
    assert response.json()["detail"] == "protocol_mismatch"
    assert response.json()["message"]
    assert response.headers["upgrade"] == f"studio/{PROTOCOL_VERSION}"


def test_mismatch_applies_to_authenticated_routes(client):
    response = client.get("/api/auth/me", headers={"X-Studio-Protocol": "0"})
    assert response.status_code == 426
    assert response.json()["detail"] == "protocol_mismatch"


def test_unauthorized_error_shape(client):
    response = client.get("/api/auth/me")
    assert response.status_code == 401
    body = response.json()
    assert body["detail"] == "unauthorized"
    assert isinstance(body["message"], str) and body["message"]


def test_unknown_route_uses_contract_shape(client):
    response = client.get("/api/does-not-exist")
    assert response.status_code == 404
    assert response.json()["detail"] == "not_found"
