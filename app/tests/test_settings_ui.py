from __future__ import annotations

from pathlib import Path


UI_ROOT = Path(__file__).parents[1] / "src" / "stata_agent" / "webui"


def read(name: str) -> str:
    return (UI_ROOT / name).read_text(encoding="utf-8")


def test_settings_navigation_and_current_research_are_separate():
    script = read("app.js")
    index = read("index.html")
    assert 'data-nav-page="settings"' in index
    assert '["settings", "应用设置", "settings"]' in script
    assert '["research", "当前研究", "result"]' in script
    assert "settingsSecretAction" in script
    assert "renderProviderCredentialEditor" in script
    assert "provider_catalog" in script
    assert 'renderSecretRow("deepseek")' not in script
    assert 'renderSecretRow("qwen")' not in script
    assert "settings_privacy_acknowledgement_required" in script
    assert "settings_revision_conflict" in script
    assert "settings-save-bar" in script
    assert "verifySettingsBackup" in script
    assert 'action: "settings-diagnostics"' in script
    assert "validateSettingsDraft" in script
    assert "storage.database" in script
    assert "settings-reset-context" in script
    assert "beforeunload" in script
    assert "applyTheme" in script
    assert "innerHTML" not in script
    assert '"agent.interactive_max_steps": [2, 128]' in script
    assert '"agent.goal_max_steps": [2, 256]' in script
    assert '"agent.max_tool_calls": [1, 512]' in script
    assert "单条消息的内部模型调用上限" in script
    assert 'hasDurableCheckpoint() ? "从停点续跑" : "继续处理"' in script


def test_settings_ui_has_safe_secret_and_restart_contract():
    script = read("app.js")
    styles = read("styles.css")
    assert 'type: "password"' in script
    assert "state.settingsToken" in script
    assert "pending_restart" in script
    assert "由环境管理" in script
    assert "settings-save-bar" in styles
    assert "settings-grid" in styles
    assert ':root[data-theme="dark"]' in styles
    assert "settingsCredentialProvider" in script
    assert "optionalEndpoint" in script
    assert "安全凭据存储不可用" in script
