"""Read-only Workspace file tools expose research files without host-path escape."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any

from stata_research_agent.application.broker_execution import (
    BrokerExecutionHandle,
    BrokerExecutionOutcome,
)
from stata_research_agent.application.turn_driver import ToolExecutionRequest
from stata_research_agent.domain.identifiers import (
    OperationAttemptId,
    OperationId,
    ToolCallId,
    TurnId,
)
from stata_research_agent.domain.revisions import WorkspaceRevision
from stata_research_agent.interfaces.workspace_file_tools import (
    ArtifactLedgerQueryExecutor,
    WorkspaceFileExecutor,
    workspace_list_artifacts_contract,
    workspace_list_files_contract,
    workspace_read_text_contract,
)
from stata_research_agent.runtime.uuid_identity import UuidIdentityGenerator


class _Bridge:
    def begin(self, command: Any) -> BrokerExecutionHandle:
        return BrokerExecutionHandle(
            command.operation_id,
            OperationAttemptId("attempt_workspace_file_test"),
            command.tool_call_id,
            False,
        )

    def complete(self, command: Any) -> BrokerExecutionOutcome:
        return BrokerExecutionOutcome(
            command.handle.operation_id,
            command.handle.attempt_id,
            "completed" if command.success else "failed",
            WorkspaceRevision(1),
            False,
        )


def _request(tool_name: str, arguments: dict[str, object]) -> ToolExecutionRequest:
    return ToolExecutionRequest(
        TurnId("turn_workspace_file_test"),
        ToolCallId("toolcall_workspace_file_test"),
        OperationId("op_workspace_file_test"),
        tool_name,
        arguments,
    )


def test_workspace_file_contracts_are_pure_read_and_bounded() -> None:
    listed = workspace_list_files_contract()
    read = workspace_read_text_contract()

    assert listed.effect_class == read.effect_class == "pure_read"
    assert listed.replay_class == read.replay_class == "replay_safe"
    assert read.input_schema["properties"]["max_lines"]["maximum"] == 400
    assert workspace_list_artifacts_contract().effect_class == "pure_read"


def test_workspace_file_executor_lists_and_reads_do_file(tmp_path: Path) -> None:
    do_file = tmp_path / "package" / "replicate.do"
    do_file.parent.mkdir()
    do_file.write_text("version 18\nuse data.dta, clear\nregress y x\n", encoding="utf-8")
    (tmp_path / ".stata-agent").mkdir()
    (tmp_path / ".stata-agent" / "secret.txt").write_text("hidden", encoding="utf-8")
    (tmp_path / "workspace.sqlite3").write_text("ledger", encoding="utf-8")
    (tmp_path / "workspace.sqlite3-wal").write_text("journal", encoding="utf-8")
    bridge = _Bridge()
    identities = UuidIdentityGenerator()

    listed = asyncio.run(
        WorkspaceFileExecutor(
            tmp_path, bridge, identities, action="list"  # type: ignore[arg-type]
        ).execute(_request("workspace.list_files", {"recursive": True}))
    )
    excerpt = asyncio.run(
        WorkspaceFileExecutor(
            tmp_path, bridge, identities, action="read"  # type: ignore[arg-type]
        ).execute(
            _request(
                "workspace.read_text",
                {"relative_path": "package/replicate.do", "start_line": 2, "max_lines": 2},
            )
        )
    )

    listed_payload = json.loads(listed.context_text)
    excerpt_payload = json.loads(excerpt.context_text)
    assert listed.success is True
    assert [item["relative_path"] for item in listed_payload["files"]] == [
        "package/replicate.do"
    ]
    assert excerpt.success is True
    assert excerpt_payload["content"] == "use data.dta, clear\nregress y x"
    assert excerpt_payload["total_lines"] == 3
    assert len(excerpt_payload["sha256"]) == 64


def test_workspace_file_executor_rejects_private_and_parent_paths(tmp_path: Path) -> None:
    (tmp_path / ".stata-agent").mkdir()
    (tmp_path / ".stata-agent" / "secret.txt").write_text("hidden", encoding="utf-8")
    bridge = _Bridge()
    executor = WorkspaceFileExecutor(
        tmp_path,
        bridge,  # type: ignore[arg-type]
        UuidIdentityGenerator(),
        action="read",
    )

    private = asyncio.run(
        executor.execute(
            _request("workspace.read_text", {"relative_path": ".stata-agent/secret.txt"})
        )
    )
    escaped = asyncio.run(
        executor.execute(_request("workspace.read_text", {"relative_path": "../outside.do"}))
    )
    ledger = asyncio.run(
        executor.execute(
            _request("workspace.read_text", {"relative_path": "workspace.sqlite3"})
        )
    )

    assert private.success is False
    assert escaped.success is False
    assert ledger.success is False
    assert "private Workspace" in json.loads(private.context_text)["message"]
    assert "unsafe" in json.loads(escaped.context_text)["message"]
    assert "private Workspace" in json.loads(ledger.context_text)["message"]


def test_artifact_ledger_query_returns_identity_without_managed_path() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE artifacts(
            artifact_id TEXT, artifact_kind TEXT, media_type TEXT, size_bytes INTEGER,
            content_hash TEXT, producer_attempt_id TEXT, created_revision INTEGER
        );
        CREATE TABLE artifact_states(
            artifact_id TEXT, availability TEXT, verified_at TEXT
        );
        CREATE TABLE operation_attempts(operation_attempt_id TEXT, operation_id TEXT);
        CREATE TABLE artifact_candidate_sources(artifact_id TEXT, artifact_candidate_id TEXT);
        CREATE TABLE completion_manifest_artifacts(
            artifact_candidate_id TEXT, output_slot TEXT, producer_locator TEXT
        );
        INSERT INTO artifacts VALUES(
            'artifact_figure', 'document', 'image/png', 2048,
            'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
            'attempt_figure', 9
        );
        INSERT INTO artifact_states VALUES('artifact_figure', 'available', 'now');
        INSERT INTO operation_attempts VALUES('attempt_figure', 'op_figure');
        INSERT INTO artifact_candidate_sources VALUES('artifact_figure', 'candidate_figure');
        INSERT INTO completion_manifest_artifacts VALUES(
            'candidate_figure', 'figure.main', 'stata-command://example#figure.main'
        );
        """
    )
    result = asyncio.run(
        ArtifactLedgerQueryExecutor(
            connection,
            _Bridge(),  # type: ignore[arg-type]
            UuidIdentityGenerator(),
        ).execute(
            _request(
                "research.list_artifacts",
                {"artifact_kind": "document", "media_type": "image/png"},
            )
        )
    )

    payload = json.loads(result.context_text)
    assert result.success is True
    assert payload["artifacts"] == [
        {
            "artifact_id": "artifact_figure",
            "artifact_kind": "document",
            "media_type": "image/png",
            "size_bytes": 2048,
            "sha256": "a" * 64,
            "availability": "available",
            "verified_at": "now",
            "producer_operation_id": "op_figure",
            "output_slot": "figure.main",
            "producer_locator": "stata-command://example#figure.main",
            "created_revision": 9,
        }
    ]
    assert "managed_handle" not in payload["artifacts"][0]
    connection.close()
