from __future__ import annotations

import threading

from stata_agent.application.settings import SettingsService
from stata_agent.settings.local_repository import LocalSettingsRepository
from stata_agent.settings.secret_store import InMemorySecretStore
from stata_agent.toolkit import ToolContext
from stata_agent.tools.stata_client import server_params
import stata_agent.ui as ui


def test_stata_server_params_accepts_request_scoped_directory(tmp_path):
    params = server_params(tmp_path / "stata-mcp")
    assert params.cwd == str(tmp_path / "stata-mcp")
    assert params.command.endswith(".venv\\Scripts\\python.exe")


def test_tool_context_carries_frozen_live_gate():
    token = threading.Event()
    context = ToolContext(live_provider_enabled=False, cancellation=token)
    assert context.live_provider_enabled is False
    assert context.cancellation is token


def test_privacy_audit_is_append_only_and_idempotent(tmp_path):
    from stata_agent.events.schema import EVENT_PRIVACY
    from stata_agent.storage.sqlite_store import SQLiteStore

    store = SQLiteStore(str(tmp_path / "ledger.sqlite3"), writer_id="settings-runtime")
    try:
        ui._privacy_audit_before_provider(
            store,
            "ui",
            privacy_mode="mixed_sanitized",
            settings_revision=3,
            request_id="request-1",
        )
        ui._privacy_audit_before_provider(
            store,
            "ui",
            privacy_mode="mixed_sanitized",
            settings_revision=4,
            request_id="request-2",
        )
        events = [event for event in store.scan("ui") if event.event_type == EVENT_PRIVACY]
        assert len(events) == 1
        assert events[0].payload == {
            "scope": "application",
            "old_mode": "local_strict",
            "new_mode": "mixed_sanitized",
            "settings_revision": 3,
            "workspace_id": ui._workspace_id("ui"),
        }
    finally:
        store.close()


def test_injected_settings_service_resolves_next_request_values(tmp_path):
    service = SettingsService(
        LocalSettingsRepository(tmp_path / "settings.json"),
        environ={},
        dotenv={},
        secret_store=InMemorySecretStore(),
    )
    before = service.resolve()
    result = service.apply_patch(expected_revision=before.revision, changes={"ui.theme": "dark"})
    assert result.effective["ui.theme"].value == "dark"
    assert result.revision == 1


def test_provider_readiness_uses_snapshot_privacy_and_live_flags(tmp_path):
    from stata_agent.providers.registry import live_available

    secret_store = InMemorySecretStore()
    secret_store.set("deepseek", "snapshot-provider-key")
    service = SettingsService(
        LocalSettingsRepository(tmp_path / "settings.json"),
        environ={},
        dotenv={},
        secret_store=secret_store,
    )
    initial = service.resolve()
    service.apply_patch(
        expected_revision=initial.revision,
        changes={"provider.live_enabled": True, "privacy.mode": "approved_remote"},
    )
    effective = service.resolve()

    assert live_available(effective, secret_store=secret_store) is True


def test_agent_loop_limits_are_frozen_from_effective_settings(tmp_path):
    service = SettingsService(
        LocalSettingsRepository(tmp_path / "settings.json"),
        environ={},
        dotenv={},
        secret_store=InMemorySecretStore(),
    )
    result = service.apply_patch(
        expected_revision=0,
        changes={
            "agent.interactive_max_steps": 24,
            "agent.goal_max_steps": 80,
            "agent.max_tool_calls": 160,
        },
    )

    assert ui._agent_loop_limits(result.effective) == (24, 80, 160)
