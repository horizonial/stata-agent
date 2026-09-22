"""Authoritative active-runtime budgets survive Turn process recreation."""

from __future__ import annotations

import pytest

from stata_research_agent.application.control import (
    CreateWorkspaceCommand,
    SubmitMessageCommand,
)
from stata_research_agent.application.workspace_service import WorkspaceControlService
from stata_research_agent.domain.identifiers import CommandId, WorkspaceId
from stata_research_agent.persistence.control_store import SqliteControlStore
from stata_research_agent.persistence.turn_runtime_budget_store import (
    SqliteTurnRuntimeBudgetLedger,
)
from stata_research_agent.persistence.workspace import WorkspaceDatabase
from stata_research_agent.runtime.uuid_identity import UuidIdentityGenerator


def test_runtime_budget_is_frozen_accumulated_and_reloaded(tmp_path) -> None:
    workspace_id = WorkspaceId("ws_runtime_budget")
    database = WorkspaceDatabase(tmp_path / workspace_id.value, workspace_id)
    database.create()
    connection = database.open(writable=True)
    identities = UuidIdentityGenerator()
    try:
        control = WorkspaceControlService(SqliteControlStore(connection), identities)
        control.create_workspace(
            CreateWorkspaceCommand(CommandId("cmd_runtime_workspace"), workspace_id)
        )
        turn = control.submit_message(
            SubmitMessageCommand(CommandId("cmd_runtime_turn"), "Inspect auto data")
        )

        first_process = SqliteTurnRuntimeBudgetLedger(connection, identities)
        assert first_process.remaining_seconds(
            turn.turn_id,
            policy_revision="turn-runtime-v1",
            configured_max_seconds=10.0,
        ) == pytest.approx(10.0)
        first_process.consume_seconds(turn.turn_id, 3.25)

        resumed_process = SqliteTurnRuntimeBudgetLedger(connection, identities)
        assert resumed_process.remaining_seconds(
            turn.turn_id,
            policy_revision="turn-runtime-v1",
            configured_max_seconds=10.0,
        ) == pytest.approx(6.75)
        resumed_process.consume_seconds(turn.turn_id, 100.0)
        assert resumed_process.remaining_seconds(
            turn.turn_id,
            policy_revision="turn-runtime-v1",
            configured_max_seconds=10.0,
        ) == 0.0

        row = connection.execute(
            """
            SELECT policy_revision, max_wall_clock_seconds,
                   consumed_wall_clock_seconds, segment_count
            FROM turn_runtime_budgets WHERE turn_id = ?
            """,
            (turn.turn_id.value,),
        ).fetchone()
        assert tuple(row) == ("turn-runtime-v1", 10.0, 10.0, 2)
        events = [
            str(item[0])
            for item in connection.execute(
                """
                SELECT event_type FROM journal_entries
                WHERE object_type = 'turn' AND object_id = ?
                  AND event_type LIKE 'turn.runtime_budget_%'
                ORDER BY workspace_revision, ordinal
                """,
                (turn.turn_id.value,),
            ).fetchall()
        ]
        assert events == [
            "turn.runtime_budget_initialized",
            "turn.runtime_budget_consumed",
            "turn.runtime_budget_consumed",
        ]

        with pytest.raises(ValueError, match="policy cannot change"):
            resumed_process.remaining_seconds(
                turn.turn_id,
                policy_revision="turn-runtime-v2",
                configured_max_seconds=10.0,
            )
        with pytest.raises(ValueError, match="maximum cannot change"):
            resumed_process.remaining_seconds(
                turn.turn_id,
                policy_revision="turn-runtime-v1",
                configured_max_seconds=20.0,
            )
    finally:
        connection.close()
