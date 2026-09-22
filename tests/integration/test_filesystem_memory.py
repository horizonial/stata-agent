"""Transparent filesystem Memory and external-edit reconciliation."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from stata_research_agent.application.control import CreateWorkspaceCommand, SubmitMessageCommand
from stata_research_agent.application.memory import (
    CreateMemoryCommand,
    MemoryKind,
    MemoryOriginKind,
    MemorySource,
    MemorySourceRole,
)
from stata_research_agent.application.memory_service import MemoryService
from stata_research_agent.application.turn_driver import ToolExecutionRequest
from stata_research_agent.application.workspace_service import WorkspaceControlService
from stata_research_agent.domain.identifiers import (
    CommandId,
    OperationId,
    ToolCallId,
    TurnId,
    WorkspaceId,
)
from stata_research_agent.interfaces.memory_runtime import MemoryRecallExecutor
from stata_research_agent.persistence.control_store import SqliteControlStore
from stata_research_agent.persistence.filesystem_memory import FilesystemMemoryStore
from stata_research_agent.persistence.memory_recall_store import SqliteMemoryRecallRepository
from stata_research_agent.persistence.memory_store import SqliteMemoryRepository
from stata_research_agent.persistence.workspace import WorkspaceDatabase
from stata_research_agent.runtime.uuid_identity import UuidIdentityGenerator


class _MemoryBrokerBridge:
    def begin(self, _command):
        return SimpleNamespace()

    def complete(self, _command):
        return SimpleNamespace(status="succeeded")


def test_filesystem_memory_preserves_revisions_and_imports_external_edit(
    tmp_path: Path,
) -> None:
    workspace_id = WorkspaceId("ws_filesystem_memory")
    database = WorkspaceDatabase(tmp_path / "workspace", workspace_id)
    database.create()
    connection = database.open(writable=True)
    identities = UuidIdentityGenerator()
    try:
        control = WorkspaceControlService(SqliteControlStore(connection), identities)
        control.create_workspace(CreateWorkspaceCommand(CommandId("cmd_fs_memory"), workspace_id))
        message = control.submit_message(
            SubmitMessageCommand(CommandId("cmd_fs_memory_message"), "Use price in levels.")
        )
        created = MemoryService(SqliteMemoryRepository(connection), identities).create(
            CreateMemoryCommand(
                CommandId("cmd_fs_memory_create"),
                MemoryKind.RESEARCH_DECISION,
                "Primary outcome",
                "Use price in levels.",
                MemoryOriginKind.EXPLICIT_USER,
                (
                    MemorySource(
                        "message",
                        message.message_id.value,
                        str(message.commit_revision.value),
                        MemorySourceRole.USER_STATEMENT,
                    ),
                ),
            )
        )
        files = FilesystemMemoryStore(database.root)
        first = files.synchronize(connection)
        assert first.revision_files_written == 1
        assert (files.root / "MEMORY.md").is_file()
        immutable = files.root / "revisions" / f"{created.memory_revision_id.value}.md"
        immutable_payload = immutable.read_text(encoding="utf-8")
        current = next((files.root / "items").rglob(f"{created.memory_item_id.value}.md"))
        edited = current.read_text(encoding="utf-8").replace(
            "# Primary outcome\n\nUse price in levels.",
            "# Primary outcome\n\nUse log price as the primary outcome.",
        )
        current.write_text(edited, encoding="utf-8")

        report = files.reconcile_external_edits(connection, identities)
        assert not report.invalid_files
        state = connection.execute(
            """
            SELECT state.pointer_revision, revision.memory_revision_id,
                   revision.revision_number, revision.content, revision.origin_kind
            FROM memory_current_states AS state
            JOIN memory_revisions AS revision
              ON revision.memory_revision_id = state.current_revision_id
            WHERE state.memory_item_id = ?
            """,
            (created.memory_item_id.value,),
        ).fetchone()
        assert state is not None
        assert int(state["pointer_revision"]) == 2
        assert int(state["revision_number"]) == 2
        assert str(state["origin_kind"]) == "imported"
        assert str(state["content"]) == "Use log price as the primary outcome."
        assert immutable.read_text(encoding="utf-8") == immutable_payload
        assert (files.root / "revisions" / f"{state['memory_revision_id']}.md").is_file()
    finally:
        connection.close()


def test_progressive_recall_searches_then_opens_exact_revision(tmp_path: Path) -> None:
    workspace_id = WorkspaceId("ws_progressive_memory")
    database = WorkspaceDatabase(tmp_path / "workspace", workspace_id)
    database.create()
    connection = database.open(writable=True)
    identities = UuidIdentityGenerator()
    try:
        control = WorkspaceControlService(SqliteControlStore(connection), identities)
        control.create_workspace(
            CreateWorkspaceCommand(CommandId("cmd_progressive_memory"), workspace_id)
        )
        message = control.submit_message(
            SubmitMessageCommand(
                CommandId("cmd_progressive_memory_message"), "Use clustered errors."
            )
        )
        memory = MemoryService(SqliteMemoryRepository(connection), identities).create(
            CreateMemoryCommand(
                CommandId("cmd_progressive_memory_create"),
                MemoryKind.RESEARCH_CONSTRAINT,
                "Inference rule",
                "Always report clustered standard errors.",
                MemoryOriginKind.EXPLICIT_USER,
                (
                    MemorySource(
                        "message",
                        message.message_id.value,
                        str(message.commit_revision.value),
                        MemorySourceRole.USER_STATEMENT,
                    ),
                ),
            )
        )
        files = FilesystemMemoryStore(database.root)
        files.synchronize(connection)
        bridge = _MemoryBrokerBridge()
        recall = SqliteMemoryRecallRepository(connection, files, identities)
        search = MemoryRecallExecutor(
            recall,
            bridge,  # type: ignore[arg-type]
            identities,
            research_path_id=message.research_path_id.value,
            action="search",
        )
        searched = asyncio.run(
            search.execute(
                ToolExecutionRequest(
                    message.turn_id,
                    ToolCallId("toolcall_memory_search"),
                    OperationId("op_memory_search"),
                    "memory.search",
                    {"query": "clustered standard errors"},
                )
            )
        )
        search_payload = json.loads(searched.context_text)
        assert searched.success
        assert search_payload["hits"][0]["memory_item_id"] == memory.memory_item_id.value
        assert search_payload["hits"][0]["content"] is None

        opened = asyncio.run(
            MemoryRecallExecutor(
                recall,
                bridge,  # type: ignore[arg-type]
                identities,
                research_path_id=message.research_path_id.value,
                action="open",
            ).execute(
                ToolExecutionRequest(
                    TurnId(message.turn_id.value),
                    ToolCallId("toolcall_memory_open"),
                    OperationId("op_memory_open"),
                    "memory.open",
                    {"memory_item_ids": [memory.memory_item_id.value]},
                )
            )
        )
        open_payload = json.loads(opened.context_text)
        assert open_payload["hits"][0]["memory_revision_id"] == memory.memory_revision_id.value
        assert open_payload["hits"][0]["content"] == "Always report clustered standard errors."
    finally:
        connection.close()
