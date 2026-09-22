"""Real Windows BaseContainer controls for the formal arbitrary-code executor."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from stata_research_agent.application.sandbox_execution import (
    SandboxExecutionError,
    SandboxExecutionRequest,
    SandboxInput,
)
from stata_research_agent.interfaces.windows_sandbox_executor import (
    WindowsSandboxExecutor,
)

pytestmark = pytest.mark.skipif(os.name != "nt", reason="BaseContainer is Windows-only")


def reviewed_runtime() -> tuple[Path, Path, Path]:
    wxc = (
        Path(os.environ["TEMP"])
        / "stataagent-tq09-mxc"
        / "src"
        / "target"
        / "x86_64-pc-windows-msvc"
        / "release"
        / "wxc-exec.exe"
    )
    python = Path.home() / "miniconda3" / "python.exe"
    powershell = Path(r"C:\Program Files\PowerShell\7\pwsh.exe")
    if not all(path.is_file() for path in (wxc, python, powershell)):
        pytest.skip("reviewed MXC/Python/PowerShell runtime is unavailable")
    return wxc, python, powershell


def executor(tmp_path: Path) -> WindowsSandboxExecutor:
    wxc, python, powershell = reviewed_runtime()
    return WindowsSandboxExecutor(
        wxc_executable=wxc,
        python_executable=python,
        powershell_executable=powershell,
        execution_root=tmp_path / "runtime",
    )


def test_multi_hardlink_input_is_rejected_before_program_creation(tmp_path: Path) -> None:
    original = tmp_path / "input.txt"
    alias = tmp_path / "input-alias.txt"
    original.write_text("authorized bytes", encoding="utf-8")
    os.link(original, alias)

    with pytest.raises(SandboxExecutionError, match="source failed alias validation"):
        executor(tmp_path).execute(
            SandboxExecutionRequest(
                "attempt_hardlink_rejected",
                "python",
                "raise SystemExit('must not execute')",
                (SandboxInput(original, "input.txt"),),
                "block",
                30,
            )
        )

    program = (
        tmp_path
        / "runtime"
        / ".stata-agent"
        / "staging"
        / "attempt_hardlink_rejected"
        / "scratch"
        / "program.py"
    )
    assert not program.exists()


def test_powershell_runs_in_base_container_with_scrubbed_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authority = tmp_path / "authority.sqlite"
    authority.write_text("authority-canary", encoding="utf-8")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-host-secret-must-not-cross")
    escaped_authority = str(authority).replace("'", "''")
    script = f"""
$authorityRead = $false
try {{
  Get-Content -LiteralPath '{escaped_authority}' -ErrorAction Stop | Out-Null
  $authorityRead = $true
}} catch {{}}
$report = @{{
  authority_read = $authorityRead
  provider_secret_present = [bool]$env:DEEPSEEK_API_KEY
}}
$outputPath = Join-Path $env:SRA_OUTPUT_DIR 'report.json'
$report | ConvertTo-Json -Compress | Set-Content -LiteralPath $outputPath -Encoding utf8
Write-Output 'isolated-powershell-ok'
"""
    receipt = executor(tmp_path).execute(
        SandboxExecutionRequest(
            "attempt_powershell_isolated", "powershell", script, (), "block", 30
        )
    )

    assert receipt.isolation_tier == "base-container"
    assert receipt.dacl_fallback_allowed is False
    assert receipt.exit_code == 0
    assert receipt.safe_stdout.strip() == "isolated-powershell-ok"
    report_candidate = next(
        item for item in receipt.output_candidates if item.relative_path == "report.json"
    )
    assert json.loads(report_candidate.source_path.read_text(encoding="utf-8-sig")) == {
        "authority_read": False,
        "provider_secret_present": False,
    }
    assert authority.read_text(encoding="utf-8") == "authority-canary"


def test_sensitive_stdout_is_blocked_after_isolated_execution(tmp_path: Path) -> None:
    with pytest.raises(SandboxExecutionError, match="CREDENTIAL_OUTPUT_BLOCKED"):
        executor(tmp_path).execute(
            SandboxExecutionRequest(
                "attempt_sensitive_stdout",
                "python",
                "print('sk-never-share-this-token')",
                (),
                "block",
                30,
            )
        )
