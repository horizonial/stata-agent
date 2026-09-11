"""Focused tests for catalog validation and effective source projection."""

from __future__ import annotations

from pathlib import Path

import pytest

from stata_agent.application.settings import (
    SETTINGS_CATALOG,
    EffectiveSettings,
    SettingsEnvironmentManagedError,
    SettingsPatch,
    SettingsReadOnlyError,
    SettingsRevisionConflict,
    SettingsService,
    SettingsValidationError,
)
from stata_agent.settings.local_repository import LocalSettingsRepository


def make_service(
    tmp_path: Path,
    *,
    environ: dict[str, str] | None = None,
    dotenv: dict[str, str] | None = None,
) -> SettingsService:
    return SettingsService(
        LocalSettingsRepository(tmp_path / "settings.json"),
        environ=environ or {},
        dotenv=dotenv or {},
    )


def test_catalog_has_typed_defaults_and_apply_modes():
    assert SETTINGS_CATALOG["privacy.mode"].default == "local_strict"
    assert SETTINGS_CATALOG["privacy.mode"].enum == (
        "local_strict",
        "mixed_sanitized",
        "approved_remote",
    )
    assert SETTINGS_CATALOG["ui.theme"].apply_mode == "immediate"
    assert SETTINGS_CATALOG["stata.mcp_dir"].apply_mode == "restart"
    assert SETTINGS_CATALOG["provider.deepseek.api_key"].sensitive
    assert "openai" in SETTINGS_CATALOG["provider.primary"].enum
    assert "gpt-4o-mini" in SETTINGS_CATALOG["provider.model"].enum
    assert SETTINGS_CATALOG["agent.interactive_max_steps"].default == 16
    assert SETTINGS_CATALOG["agent.interactive_max_steps"].minimum == 2
    assert SETTINGS_CATALOG["agent.interactive_max_steps"].maximum == 128
    assert SETTINGS_CATALOG["agent.goal_max_steps"].default == 64
    assert SETTINGS_CATALOG["agent.max_tool_calls"].default == 128
    assert SETTINGS_CATALOG["agent.interactive_max_steps"].apply_mode == "next_request"


def test_precedence_default_dotenv_user_environment(tmp_path: Path):
    service = make_service(
        tmp_path,
        environ={"STATA_AGENT_PRIVACY": "approved_remote"},
        dotenv={"STATA_AGENT_PRIVACY": "mixed_sanitized", "STATA_AGENT_LIVE": "true"},
    )
    first = service.resolve()
    assert first["privacy.mode"].value == "approved_remote"
    assert first["privacy.mode"].source == "environment"
    assert not first["privacy.mode"].editable
    assert first["provider.live_enabled"].value is True
    assert first["provider.live_enabled"].source == "dotenv"
    assert first["provider.live_enabled"].editable

    result = SettingsService(
        service.repository,
        environ={},
        dotenv={"STATA_AGENT_PRIVACY": "mixed_sanitized"},
    ).apply_patch(
        SettingsPatch(expected_revision=0, changes={"privacy.mode": "approved_remote"})
    )
    assert result.revision == 1
    effective = SettingsService(
        service.repository,
        environ={"STATA_AGENT_PRIVACY": "local_strict"},
        dotenv={"STATA_AGENT_PRIVACY": "mixed_sanitized"},
    ).resolve()
    assert effective["privacy.mode"].value == "local_strict"
    assert effective["privacy.mode"].source == "environment"


def test_user_override_is_persisted_without_dotenv_mutation(tmp_path: Path):
    dotenv = {"STATA_AGENT_LIVE": "true"}
    service = make_service(tmp_path, dotenv=dotenv)
    result = service.apply_patch(expected_revision=0, changes={"provider.live_enabled": False})

    assert result.revision == 1
    assert result.effective["provider.live_enabled"].value is False
    assert result.effective["provider.live_enabled"].source == "user"
    assert dotenv == {"STATA_AGENT_LIVE": "true"}
    assert "STATA_AGENT_LIVE" not in service.repository.path.read_text(encoding="utf-8")


def test_provider_model_selection_is_profile_bound(tmp_path: Path):
    service = make_service(tmp_path)
    result = service.apply_patch(
        expected_revision=0,
        changes={
            "provider.primary": "openai",
            "provider.model": "gpt-4o-mini",
            "provider.base_url": "",
        },
    )
    assert result.effective["provider.primary"].value == "openai"
    assert result.effective["provider.model"].value == "gpt-4o-mini"
    assert result.effective["provider.base_url"].value == ""

    with pytest.raises(SettingsValidationError):
        service.apply_patch(
            expected_revision=result.revision,
            changes={"provider.model": "deepseek-chat"},
        )
    assert service.resolve().revision == result.revision


def test_effective_settings_are_immutable_and_restart_is_projected(tmp_path: Path):
    service = make_service(tmp_path)
    result = service.apply_patch(expected_revision=0, changes={"ui.port": 8100})

    assert isinstance(result.effective, EffectiveSettings)
    assert result.effective.restart_required
    assert "ui.port" in result.effective.pending_keys
    with pytest.raises(TypeError):
        result.effective.values["ui.port"] = result.effective["ui.port"]  # type: ignore[index]


def test_environment_managed_shadow_save_is_rejected_atomically(tmp_path: Path):
    service = make_service(tmp_path, environ={"STATA_AGENT_LIVE": "true"})

    with pytest.raises(SettingsEnvironmentManagedError) as error:
        service.apply_patch(
            expected_revision=0,
            changes={"provider.live_enabled": False, "privacy.mode": "mixed_sanitized"},
        )

    assert error.value.code == "settings_environment_managed"
    assert not service.repository.path.exists()


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("provider.live_enabled", 1),
        ("context.max_input_tokens", True),
        ("context.max_input_tokens", 0),
        ("agent.interactive_max_steps", 1),
        ("agent.interactive_max_steps", 129),
        ("agent.goal_max_steps", 1),
        ("agent.goal_max_steps", 257),
        ("agent.max_tool_calls", 0),
        ("agent.max_tool_calls", 513),
        ("privacy.mode", "not-a-mode"),
        ("provider.deepseek.base_url", "http://example.com"),
        ("provider.deepseek.base_url", "https://user:password@example.com"),
        ("ui.port", 80),
    ],
)
def test_invalid_patch_values_are_rejected_without_partial_write(tmp_path: Path, key: str, value: object):
    service = make_service(tmp_path)

    with pytest.raises(SettingsValidationError):
        service.apply_patch(expected_revision=0, changes={key: value})

    assert not service.repository.path.exists()


def test_context_budget_relation_is_validated_before_write(tmp_path: Path):
    service = make_service(tmp_path)

    with pytest.raises(SettingsValidationError):
        service.apply_patch(
            expected_revision=0,
            changes={
                "context.max_input_tokens": 100,
                "context.reserve_output_tokens": 101,
            },
        )
    assert not service.repository.path.exists()


def test_unknown_sensitive_and_read_only_keys_are_rejected(tmp_path: Path):
    service = make_service(tmp_path)

    with pytest.raises(SettingsValidationError):
        service.apply_patch(expected_revision=0, changes={"not.a.setting": 1})
    with pytest.raises(SettingsValidationError):
        service.apply_patch(expected_revision=0, changes={"provider.deepseek.api_key": "secret-marker"})
    with pytest.raises(SettingsReadOnlyError):
        service.apply_patch(expected_revision=0, changes={"ui.language": "zh-CN"})
    assert not service.repository.path.exists()


def test_invalid_environment_fails_closed_and_reports_health(tmp_path: Path):
    service = make_service(tmp_path, environ={"STATA_AGENT_PRIVACY": "unsafe"})

    effective = service.resolve()

    setting = effective["privacy.mode"]
    assert setting.value == "local_strict"
    assert setting.source == "environment"
    assert not setting.editable
    assert setting.status == "error"
    assert any(item.code == "settings_invalid_value" for item in effective.health)


def test_secret_projection_never_contains_secret_value(tmp_path: Path):
    marker = "secret-marker-that-must-not-escape"
    service = make_service(tmp_path, environ={"DEEPSEEK_API_KEY": marker})

    setting = service.resolve()["provider.deepseek.api_key"]
    serialized = service.resolve().as_dict()

    assert setting.sensitive
    assert setting.value is None
    assert setting.source == "environment"
    assert marker not in repr(serialized)


def test_stale_service_patch_reports_revision_conflict(tmp_path: Path):
    service = make_service(tmp_path)
    service.apply_patch(expected_revision=0, changes={"ui.theme": "dark"})

    with pytest.raises(SettingsRevisionConflict):
        service.apply_patch(expected_revision=0, changes={"ui.theme": "light"})
