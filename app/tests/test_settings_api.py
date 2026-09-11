from __future__ import annotations

import json
import os

import pytest
from fastapi.testclient import TestClient

import stata_agent.ui as ui
from stata_agent.application.settings import SettingsService
from stata_agent.settings.local_repository import LocalSettingsRepository
from stata_agent.settings.secret_store import InMemorySecretStore
from stata_agent.storage.sqlite_store import SQLiteStore


@pytest.fixture
def client(tmp_path, monkeypatch):
    service = SettingsService(
        LocalSettingsRepository(tmp_path / "settings.json"),
        environ={},
        dotenv={},
        secret_store=InMemorySecretStore(),
    )
    monkeypatch.setattr(ui.app.state, "settings_service", service, raising=False)
    monkeypatch.setattr(ui.app.state, "settings_allowed_hosts", {"testserver"}, raising=False)
    with TestClient(ui.app) as test_client:
        yield test_client


def headers(token: str, *, content_type: str = "application/json") -> dict[str, str]:
    return {
        "Origin": "http://testserver",
        "X-Settings-CSRF": token,
        "Content-Type": content_type,
    }


def test_get_patch_and_secret_projection_never_echoes_secret(client):
    response = client.get("/api/settings")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["revision"] == 0
    token = body["csrf_token"]

    changed = client.patch(
        "/api/settings",
        headers=headers(token),
        content=json.dumps({"expected_revision": 0, "changes": {"ui.theme": "dark"}}),
    )
    assert changed.status_code == 200
    assert changed.json()["revision"] == 1

    marker = "SECRET_MARKER_SETTINGS_API"
    secret = client.put(
        "/api/settings/secrets/deepseek",
        headers=headers(token),
        content=json.dumps({"value": marker}),
    )
    assert secret.status_code == 200
    assert marker not in secret.text
    assert client.get("/api/settings").json()["secrets"]["deepseek"]["configured"] is True


def test_provider_catalog_drives_page_credentials_for_non_legacy_provider(client):
    initial = client.get("/api/settings").json()
    catalog = initial["provider_catalog"]
    openai = next(item for item in catalog if item["id"] == "openai")
    assert openai["models"]
    assert "api_key_env" not in json.dumps(openai)
    token = initial["csrf_token"]
    response = client.put(
        "/api/settings/secrets/openai",
        headers=headers(token),
        content=json.dumps({"value": "openai-page-secret"}),
    )
    assert response.status_code == 200
    assert "openai-page-secret" not in response.text
    refreshed = client.get("/api/settings").json()
    assert refreshed["secrets"]["openai"] == {
        "provider": "openai",
        "configured": True,
        "source": "credential",
        "editable": True,
    }


def test_patch_requires_privacy_ack_and_stale_revision_is_stable(client):
    token = client.get("/api/settings").json()["csrf_token"]
    body = {"expected_revision": 0, "changes": {"privacy.mode": "approved_remote"}}
    denied = client.patch("/api/settings", headers=headers(token), content=json.dumps(body))
    assert denied.status_code == 422
    assert denied.json()["error"]["code"] == "settings_privacy_acknowledgement_required"

    body["privacy_acknowledgement"] = True
    accepted = client.patch("/api/settings", headers=headers(token), content=json.dumps(body))
    assert accepted.status_code == 200
    stale = client.patch(
        "/api/settings",
        headers=headers(token),
        content=json.dumps({"expected_revision": 0, "changes": {"ui.theme": "light"}}),
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "settings_revision_conflict"


def test_secret_delete_accepts_empty_body(client):
    token = client.get("/api/settings").json()["csrf_token"]
    put = client.put("/api/settings/secrets/qwen", headers=headers(token), json={"value": "temporary-secret"})
    assert put.status_code == 200
    deleted = client.delete("/api/settings/secrets/qwen", headers=headers(token))
    assert deleted.status_code == 200
    assert deleted.json()["secret"]["configured"] is False


def test_backup_download_and_verify_are_bounded_and_do_not_restore(client, tmp_path, monkeypatch):
    database = tmp_path / "ledger.sqlite3"
    store = SQLiteStore(str(database), writer_id="settings-api", takeover=True)
    store.close()
    monkeypatch.setattr(ui, "DEFAULT_DB", database)
    assets = database.parent / ".attachments" / "objects"
    assets.mkdir(parents=True)
    (assets / "large.bin").write_bytes(os.urandom(128 * 1024))
    token = client.get("/api/settings").json()["csrf_token"]
    response = client.post("/api/settings/backup", headers=headers(token))
    assert response.status_code == 200
    assert len(response.content) > 64 * 1024
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-type"].startswith("application/zip")
    verified = client.post(
        "/api/settings/backup/verify",
        headers={"Origin": "http://testserver", "X-Settings-CSRF": token},
        files={"bundle": ("backup.zip", response.content, "application/zip")},
    )
    assert verified.status_code == 200
    assert verified.json()["verified"] is True
    assert set(verified.json()["manifest"]) == {"schema", "database_schema_version", "file_count"}
