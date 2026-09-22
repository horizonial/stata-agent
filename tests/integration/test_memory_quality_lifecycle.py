"""Multi-conversation Memory quality regression on authoritative storage."""

from __future__ import annotations

from stata_research_agent.application.control import (
    CompleteTurnCommand,
    CreateWorkspaceCommand,
    SubmitMessageCommand,
)
from stata_research_agent.application.memory import (
    CreateMemoryCommand,
    MemoryKind,
    MemoryOriginKind,
    MemorySource,
    MemorySourceRole,
    RetractMemoryCommand,
)
from stata_research_agent.application.memory_service import MemoryService
from stata_research_agent.application.workspace_service import WorkspaceControlService
from stata_research_agent.domain.identifiers import CommandId, WorkspaceId
from stata_research_agent.domain.status import TurnStatus
from stata_research_agent.persistence.context_authority import SqliteContextAuthorityReader
from stata_research_agent.persistence.control_store import SqliteControlStore
from stata_research_agent.persistence.memory_store import SqliteMemoryRepository
from stata_research_agent.persistence.workspace import WorkspaceDatabase
from stata_research_agent.runtime.uuid_identity import UuidIdentityGenerator


def _user_source(message) -> MemorySource:
    return MemorySource(
        "message",
        message.message_id.value,
        str(message.commit_revision.value),
        MemorySourceRole.USER_STATEMENT,
    )


def _recalled_memory_contents(connection, turn_id) -> tuple[str, ...]:
    return tuple(
        candidate.item.content
        for candidate in SqliteContextAuthorityReader(connection).collect(turn_id)
        if candidate.item.source_object_type == "memory_revision"
    )


def test_memory_remains_precise_across_correction_and_many_conversations(tmp_path) -> None:
    workspace_id = WorkspaceId("ws_memory_quality_lifecycle")
    database = WorkspaceDatabase(tmp_path / workspace_id.value, workspace_id)
    database.create()
    connection = database.open(writable=True)
    identities = UuidIdentityGenerator()
    try:
        control = WorkspaceControlService(SqliteControlStore(connection), identities)
        control.create_workspace(
            CreateWorkspaceCommand(CommandId("cmd_memory_quality_workspace"), workspace_id)
        )
        current = control.submit_message(
            SubmitMessageCommand(
                CommandId("cmd_memory_quality_initial"),
                "Use clustered standard errors and log price as the outcome.",
            )
        )
        memory = MemoryService(SqliteMemoryRepository(connection), identities)
        constraint = memory.create(
            CreateMemoryCommand(
                CommandId("cmd_memory_quality_constraint"),
                MemoryKind.RESEARCH_CONSTRAINT,
                "Standard error rule",
                "Always report clustered standard errors.",
                MemoryOriginKind.EXPLICIT_USER,
                (_user_source(current),),
            )
        )
        old_decision = memory.create(
            CreateMemoryCommand(
                CommandId("cmd_memory_quality_old_decision"),
                MemoryKind.RESEARCH_DECISION,
                "Outcome transformation",
                "Use log price as the primary outcome.",
                MemoryOriginKind.EXPLICIT_USER,
                (_user_source(current),),
            )
        )
        active_decision_content = "Use log price as the primary outcome."
        for ordinal in range(1, 13):
            recalled = _recalled_memory_contents(connection, current.turn_id)
            assert any("clustered standard errors" in item for item in recalled)
            assert any(active_decision_content in item for item in recalled)

            control.complete_turn(
                CompleteTurnCommand(
                    CommandId(f"cmd_memory_quality_complete_{ordinal}"),
                    current.turn_id,
                    TurnStatus.SUCCEEDED,
                )
            )
            prompt = (
                "Correction: do not log price; keep price in levels."
                if ordinal == 6
                else f"Continue the research, conversation {ordinal}."
            )
            current = control.submit_message(
                SubmitMessageCommand(
                    CommandId(f"cmd_memory_quality_message_{ordinal}"), prompt
                )
            )
            if ordinal == 6:
                memory.retract(
                    RetractMemoryCommand(
                        CommandId("cmd_memory_quality_retract_old"),
                        old_decision.memory_item_id,
                        old_decision.pointer_revision,
                        "The user corrected the outcome transformation.",
                    )
                )
                memory.create(
                    CreateMemoryCommand(
                        CommandId("cmd_memory_quality_new_decision"),
                        MemoryKind.RESEARCH_DECISION,
                        "Outcome transformation",
                        "Keep price in levels as the primary outcome.",
                        MemoryOriginKind.EXPLICIT_USER,
                        (_user_source(current),),
                    )
                )
                active_decision_content = "Keep price in levels as the primary outcome."

        final_recalled = _recalled_memory_contents(connection, current.turn_id)
        expected_markers = ("clustered standard errors", "price in levels")
        true_positive = sum(
            any(marker in content for content in final_recalled) for marker in expected_markers
        )
        precision = true_positive / len(final_recalled)
        recall = true_positive / len(expected_markers)
        assert precision == recall == 1.0
        assert all("log price" not in item for item in final_recalled)
        assert constraint.memory_revision_id.value in {
            candidate.item.source_object_id
            for candidate in SqliteContextAuthorityReader(connection).collect(current.turn_id)
            if candidate.item.source_object_type == "memory_revision"
        }
    finally:
        connection.close()
