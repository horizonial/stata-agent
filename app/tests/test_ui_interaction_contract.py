"""Browserless contracts for the operator-facing UI state and safety surface."""

from __future__ import annotations

from pathlib import Path


UI_ROOT = Path(__file__).parents[1] / "src" / "stata_agent" / "webui"


def _read(name: str) -> str:
    return (UI_ROOT / name).read_text(encoding="utf-8")


def test_run_control_keeps_stop_and_disconnect_non_terminal() -> None:
    script = _read("app.js")

    assert 'const RUN_STATES = new Set(["idle", "running", "cancelling", "disconnected", "failed", "cancelled", "uncertain", "paused", "completed"])' in script
    assert 'const ACTIVE_RUN_STATES = new Set(["running", "cancelling", "disconnected"])' in script
    assert "waitForTerminalConfirmation" in script
    assert "state.runState === \"cancelling\"" in script
    assert "terminalStateFor" in script
    assert 'if (status === "cancelled" || status === "canceled") return "cancelled"' in script
    assert 'if (status === "paused") return "paused"' in script
    assert "if (!canSend())" in script
    assert "workspaceGeneration" in script
    assert "isCurrentWorkspaceGeneration" in script


def test_failure_and_attachment_projections_are_allow_listed() -> None:
    script = _read("app.js")

    assert "normalizeFailure" in script
    assert "support_action" in script
    assert "download-diagnostics" in script
    assert "retry-message" in script
    assert "safeAttachmentProjection" in script
    assert "result.retryable" in script
    assert "renderAttachmentManifest" in script
    assert "renderAttachmentStatusList" in script
    assert 'attachment_id: attachmentId' in script
    assert "storage_key" not in script
    assert "sha256" not in script
    assert "innerHTML" not in script
    assert "document.write" not in script
    assert "eval(" not in script


def test_approval_dialog_and_markdown_table_accessibility_contract() -> None:
    script = _read("app.js")
    index = _read("index.html")
    styles = _read("styles.css")

    assert "activateOverlay" in script
    assert "trapOverlayFocus" in script
    assert 'aria-required' in script
    assert "decision-note-error" in index
    assert 'aria-describedby="decision-description"' in index
    assert "正在保存审批决定" in script
    assert "请填写具体修改要求后再提交" in script
    assert "md-table-wrap" in script and "md-table-wrap" in styles
    assert 'setAttribute("scope", "col")' in script
    assert '<caption' in script or 'el("caption"' in script
    assert "escaped" in script
    assert 'line.trim().startsWith("```")' in script
