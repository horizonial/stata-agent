"""Host-level Workspace scheduling over authoritative SQLite Turn state."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from stata_research_agent.application.ports.memory_curator import (
    WorkspaceMemoryMaintenanceRunner,
)
from stata_research_agent.application.ports.turn_dispatch import (
    WorkspaceTurnAuthorityResolver,
    WorkspaceTurnRunner,
)
from stata_research_agent.domain.identifiers import TurnId, WorkspaceId


@dataclass(slots=True)
class _WorkspaceWake:
    event: asyncio.Event
    task: asyncio.Task[None]


class WorkspaceTurnSupervisor:
    """Run one scheduler loop per Workspace while permitting Workspace parallelism.

    Dispatch is only a wake hint.  Every loop iteration re-reads the SQLite write lane and Turn
    status, so duplicate delivery does not grant execution authority and process memory is never
    the sole source of runnable work.
    """

    def __init__(
        self,
        authorities: WorkspaceTurnAuthorityResolver,
        runner: WorkspaceTurnRunner,
        memory_maintenance: WorkspaceMemoryMaintenanceRunner | None = None,
    ) -> None:
        self._authorities = authorities
        self._runner = runner
        self._memory_maintenance = memory_maintenance
        self._workspaces: dict[str, _WorkspaceWake] = {}
        self._maintenance_tasks: dict[str, asyncio.Task[None]] = {}
        self._closed = False

    def dispatch(self, workspace_id: WorkspaceId, turn_id: TurnId) -> None:
        del turn_id  # identity is re-read from the authoritative Workspace write lane
        if self._closed:
            raise RuntimeError("Workspace Turn supervisor is closed")
        current = self._workspaces.get(workspace_id.value)
        if current is not None and not current.task.done():
            current.event.set()
            return
        event = asyncio.Event()
        event.set()
        task = asyncio.create_task(
            self._run_workspace(workspace_id, event),
            name=f"workspace-turn-supervisor:{workspace_id.value}",
        )
        self._workspaces[workspace_id.value] = _WorkspaceWake(event, task)

    async def _run_workspace(self, workspace_id: WorkspaceId, event: asyncio.Event) -> None:
        try:
            while not self._closed:
                event.clear()
                await self._drain_workspace(workspace_id)
                self._schedule_memory_maintenance(workspace_id)
                if not event.is_set():
                    return
        finally:
            current = self._workspaces.get(workspace_id.value)
            if current is not None and current.task is asyncio.current_task():
                del self._workspaces[workspace_id.value]

    async def _drain_workspace(self, workspace_id: WorkspaceId) -> None:
        authority = self._authorities.scheduling_authority(workspace_id)
        while not self._closed:
            active = authority.active_write_turn()
            if active is None:
                active = authority.activate_next_queued_turn()
                if active is None:
                    return
            turn_id, status = active
            if status == "waiting":
                return
            if status != "running":
                return
            await self._runner.run_turn(workspace_id, turn_id)
            after = authority.active_write_turn()
            if after is not None and after[0] == turn_id and after[1] == "running":
                # A runner that returned without committing a stable state must not be spun or
                # silently re-executed. Recovery/attention owns this condition.
                return

    def _schedule_memory_maintenance(self, workspace_id: WorkspaceId) -> None:
        if self._memory_maintenance is None or self._closed:
            return
        current = self._maintenance_tasks.get(workspace_id.value)
        if current is not None and not current.done():
            return
        task = asyncio.create_task(
            self._run_memory_maintenance(workspace_id),
            name=f"workspace-memory-maintenance:{workspace_id.value}",
        )
        self._maintenance_tasks[workspace_id.value] = task

    async def _run_memory_maintenance(self, workspace_id: WorkspaceId) -> None:
        try:
            assert self._memory_maintenance is not None
            await self._memory_maintenance.run_once(workspace_id)
        except Exception:
            # Memory is an optional continuity aid. A Curator failure must never fail or
            # delay the foreground research Turn; durable jobs/attempts remain inspectable.
            return
        finally:
            current = self._maintenance_tasks.get(workspace_id.value)
            if current is asyncio.current_task():
                del self._maintenance_tasks[workspace_id.value]

    async def close(self) -> None:
        self._closed = True
        wakes = tuple(self._workspaces.values())
        for wake in wakes:
            wake.event.set()
        if wakes:
            await asyncio.gather(*(wake.task for wake in wakes), return_exceptions=True)
        maintenance = tuple(self._maintenance_tasks.values())
        if maintenance:
            for task in maintenance:
                task.cancel()
            await asyncio.gather(*maintenance, return_exceptions=True)
        self._workspaces.clear()
        self._maintenance_tasks.clear()
