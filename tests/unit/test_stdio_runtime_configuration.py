from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from stata_research_agent.domain.stata_execution import (
    StataArtifactOutputRequest,
    StataExecutionStatus,
)
from stata_research_agent.stata.stdio_runtime import (
    StataRuntimeContractError,
    StdioStataRuntime,
)


def runtime(*, source_root: Path | None) -> StdioStataRuntime:
    return StdioStataRuntime(
        python_executable=Path("python.exe"),
        mcp_source_root=source_root,
        working_directory=Path("workspace"),
        stata_home=Path("stata"),
    )


def test_installed_mode_does_not_inherit_pythonpath(monkeypatch) -> None:
    monkeypatch.setenv("PYTHONPATH", "untrusted-adapter")

    environment = runtime(source_root=None)._server_environment()

    assert "PYTHONPATH" not in environment
    assert environment["PYTHONNOUSERSITE"] == "1"
    assert environment["PYTHONDONTWRITEBYTECODE"] == "1"
    assert environment["STATA_HOME"].endswith("stata")
    assert runtime(source_root=None)._server_arguments() == [
        "-I",
        "-B",
        "-m",
        "stata_mcp.server",
    ]


def test_development_mode_sets_exact_source_root(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PYTHONPATH", "untrusted-adapter")
    source_root = tmp_path / "reviewed-source"

    environment = runtime(source_root=source_root)._server_environment()

    assert environment["PYTHONPATH"] == str(source_root.resolve())
    assert environment["PYTHONNOUSERSITE"] == "1"
    assert environment["PYTHONDONTWRITEBYTECODE"] == "1"
    assert runtime(source_root=source_root)._server_arguments() == [
        "-P",
        "-B",
        "-m",
        "stata_mcp.server",
    ]


def _task_result(*, execution_status: str, rc: int, artifact_status: str) -> object:
    return SimpleNamespace(
        meta={
            "envelope_schema_version": "stata-mcp.envelope/v1",
            "artifact_output_contract_status": artifact_status,
            "artifact_outputs": [],
            "execution_receipt": {
                "schema_version": "stata.execution-receipt/v1alpha1",
                "executor_instance_id": "executor-test",
                "session_id": "scope-main",
                "session_generation": 1,
                "exec_seq": 4,
                "execution_status": execution_status,
                "rc": rc,
                "raw_output_status": "complete",
                "structured_result_status": "not_requested",
                "command_hash": "abc",
                "data_signature": None,
                "session_reset": False,
                "runtime_environment": {"stata_version": "18"},
                "supervision_proof": {"worker_pid": 1},
            },
        },
        content=(SimpleNamespace(type="text", text="esttab returned an error"),),
        structured_content=None,
        is_error=rc != 0,
    )


def test_known_command_failure_with_missing_output_remains_a_definitive_result() -> None:
    parsed = runtime(source_root=None)._parse_runtime_result(
        _task_result(
            execution_status="command_failed", rc=111, artifact_status="missing_required"
        ),
        (
            StataArtifactOutputRequest(
                "table.main", "tables/main.rtf", "table", "application/rtf"
            ),
        ),
    )

    assert parsed.receipt.execution_status is StataExecutionStatus.COMMAND_FAILED
    assert parsed.artifacts == ()


def test_success_without_required_output_is_a_contract_violation() -> None:
    with pytest.raises(StataRuntimeContractError, match="missing_required"):
        runtime(source_root=None)._parse_runtime_result(
            _task_result(
                execution_status="succeeded", rc=0, artifact_status="missing_required"
            ),
            (
                StataArtifactOutputRequest(
                    "table.main", "tables/main.rtf", "table", "application/rtf"
                ),
            ),
        )
