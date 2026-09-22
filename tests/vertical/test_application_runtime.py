"""M4 host scheduler: per-Workspace concurrency and authoritative queue draining."""

from __future__ import annotations

import asyncio
from pathlib import Path

from stata_research_agent.application.control import (
    CompleteTurnCommand,
    CreateWorkspaceCommand,
    SubmitMessageCommand,
)
from stata_research_agent.application.workspace_service import WorkspaceControlService
from stata_research_agent.domain.identifiers import CommandId, TurnId, WorkspaceId
from stata_research_agent.domain.status import TurnStatus
from stata_research_agent.persistence.control_store import SqliteControlStore
from stata_research_agent.persistence.workspace import WorkspaceDatabase
from stata_research_agent.persistence.workspace_scheduler import (
    SqliteWorkspaceTurnAuthority,
)
from stata_research_agent.runtime.application_runtime import WorkspaceTurnSupervisor
from stata_research_agent.runtime.uuid_identity import UuidIdentityGenerator


class Host:
    def __init__(self, root: Path) -> None:
        self.root = root

    def database(self, workspace_id: WorkspaceId) -> WorkspaceDatabase:
        return WorkspaceDatabase(self.root / workspace_id.value, workspace_id)

    def scheduling_authority(self, workspace_id: WorkspaceId) -> SqliteWorkspaceTurnAuthority:
        return SqliteWorkspaceTurnAuthority(self.database(workspace_id), UuidIdentityGenerator())


def initialize(host: Host, workspace_id: WorkspaceId):
    database = host.database(workspace_id)
    database.create()
    connection = database.open(writable=True)
    try:
        service = WorkspaceControlService(SqliteControlStore(connection), UuidIdentityGenerator())
        service.create_workspace(
            CreateWorkspaceCommand(CommandId(f"cmd_create_{workspace_id.value}"), workspace_id)
        )
        running = service.submit_message(
            SubmitMessageCommand(
                CommandId(f"cmd_running_{workspace_id.value}"), "Run the first Turn"
            )
        )
        return database, running
    finally:
        connection.close()


class ConcurrencyRunner:
    def __init__(self, host: Host, first_turns: set[str]) -> None:
        self.host = host
        self.first_turns = first_turns
        self.started: set[str] = set()
        self.completed: list[str] = []
        self.both_started = asyncio.Event()
        self.release = asyncio.Event()

    async def run_turn(self, workspace_id: WorkspaceId, turn_id: TurnId) -> None:
        self.started.add(turn_id.value)
        if self.first_turns.issubset(self.started):
            self.both_started.set()
        if turn_id.value in self.first_turns:
            await self.release.wait()
        connection = self.host.database(workspace_id).open(writable=True)
        try:
            WorkspaceControlService(
                SqliteControlStore(connection), UuidIdentityGenerator()
            ).complete_turn(
                CompleteTurnCommand(
                    CommandId(f"cmd_complete_{turn_id.value}"),
                    turn_id,
                    TurnStatus.SUCCEEDED,
                )
            )
        finally:
            connection.close()
        self.completed.append(turn_id.value)


def test_supervisor_runs_workspaces_in_parallel_and_drains_one_workspace_queue(
    tmp_path: Path,
) -> None:
    host = Host(tmp_path / "host")
    database_a, running_a = initialize(host, WorkspaceId("ws_runtime_a"))
    database_b, running_b = initialize(host, WorkspaceId("ws_runtime_b"))
    connection_a = database_a.open(writable=True)
    try:
        queued_a = WorkspaceControlService(
            SqliteControlStore(connection_a), UuidIdentityGenerator()
        ).submit_message(SubmitMessageCommand(CommandId("cmd_runtime_queued_a"), "Run second"))
    finally:
        connection_a.close()

    async def scenario() -> None:
        runner = ConcurrencyRunner(host, {running_a.turn_id.value, running_b.turn_id.value})
        supervisor = WorkspaceTurnSupervisor(host, runner)
        supervisor.dispatch(WorkspaceId("ws_runtime_a"), running_a.turn_id)
        supervisor.dispatch(WorkspaceId("ws_runtime_b"), running_b.turn_id)
        await asyncio.wait_for(runner.both_started.wait(), timeout=2)
        runner.release.set()
        for _ in range(100):
            if queued_a.turn_id.value in runner.completed:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("queued Workspace Turn was not activated")
        await supervisor.close()
        assert runner.completed.index(running_a.turn_id.value) < runner.completed.index(
            queued_a.turn_id.value
        )
        assert set(runner.completed) == {
            running_a.turn_id.value,
            queued_a.turn_id.value,
            running_b.turn_id.value,
        }

    asyncio.run(scenario())
