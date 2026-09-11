from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import stata_agent.ui as ui
from stata_agent.application.settings import SettingsService
from stata_agent.settings.local_repository import LocalSettingsRepository
from stata_agent.settings.secret_store import InMemorySecretStore


@pytest.fixture
def secured_client(tmp_path, monkeypatch):
    service = SettingsService(
        LocalSettingsRepository(tmp_path / "settings.json"), environ={}, dotenv={}, secret_store=InMemorySecretStore()
    )
    monkeypatch.setattr(ui.app.state, "settings_service", service, raising=False)
    monkeypatch.setattr(ui.app.state, "settings_allowed_hosts", {"testserver"}, raising=False)
    with TestClient(ui.app) as client:
        yield client


def test_mutation_rejects_missing_origin_and_csrf(secured_client):
    token = secured_client.get("/api/settings").json()["csrf_token"]
    payload = json.dumps({"expected_revision": 0, "changes": {"ui.theme": "dark"}})
    missing_origin = secured_client.patch(
        "/api/settings", headers={"Content-Type": "application/json", "X-Settings-CSRF": token}, content=payload
    )
    assert missing_origin.status_code == 403
    assert missing_origin.json()["error"]["code"] == "settings_origin_required"

    missing_csrf = secured_client.patch(
        "/api/settings", headers={"Content-Type": "application/json", "Origin": "http://testserver"}, content=payload
    )
    assert missing_csrf.status_code == 403
    assert missing_csrf.json()["error"]["code"] == "settings_csrf_failed"


def test_mutation_rejects_wrong_host_content_type_and_oversized_body(secured_client):
    token = secured_client.get("/api/settings").json()["csrf_token"]
    base = {"Origin": "http://testserver", "X-Settings-CSRF": token, "Content-Type": "application/json"}
    wrong_host = secured_client.patch(
        "/api/settings", headers={**base, "Host": "evil.example"}, json={"expected_revision": 0, "changes": {}}
    )
    assert wrong_host.status_code == 403
    assert wrong_host.json()["error"]["code"] == "settings_host_forbidden"

    wrong_type = secured_client.patch(
        "/api/settings", headers={**base, "Content-Type": "text/plain"}, content="{}"
    )
    assert wrong_type.status_code == 415
    assert wrong_type.json()["error"]["code"] == "settings_content_type"

    oversized = secured_client.patch(
        "/api/settings", headers={**base, "Content-Length": str(70 * 1024)}, content="{}"
    )
    assert oversized.status_code == 413
    assert oversized.json()["error"]["code"] == "settings_body_too_large"

    wrong_origin = secured_client.patch(
        "/api/settings", headers={**base, "Origin": "http://localhost"}, json={"expected_revision": 0, "changes": {}}
    )
    assert wrong_origin.status_code == 403
    assert wrong_origin.json()["error"]["code"] == "settings_origin_forbidden"


def test_environment_managed_patch_cannot_shadow_value(tmp_path, monkeypatch):
    service = SettingsService(
        LocalSettingsRepository(tmp_path / "settings.json"),
        environ={"STATA_AGENT_LIVE": "true"},
        dotenv={},
        secret_store=InMemorySecretStore(),
    )
    monkeypatch.setattr(ui.app.state, "settings_service", service, raising=False)
    monkeypatch.setattr(ui.app.state, "settings_allowed_hosts", {"testserver"}, raising=False)
    with TestClient(ui.app) as client:
        token = client.get("/api/settings").json()["csrf_token"]
        response = client.patch(
            "/api/settings",
            headers={"Origin": "http://testserver", "X-Settings-CSRF": token, "Content-Type": "application/json"},
            json={"expected_revision": 0, "changes": {"provider.live_enabled": False}},
        )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "settings_environment_managed"
