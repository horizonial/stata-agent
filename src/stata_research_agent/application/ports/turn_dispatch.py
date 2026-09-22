"""Wake-only port from committed public commands to the host Turn scheduler."""

from typing import Protocol

from stata_research_agent.domain.identifiers import TurnId, WorkspaceId


class TurnDispatcher(Protocol):
    """Notify the runtime that authoritative Turn state may now be runnable."""

    def dispatch(self, workspace_id: WorkspaceId, turn_id: TurnId) -> None: ...


class WorkspaceTurnAuthority(Protocol):
    """Authoritative scheduler view for one Workspace write lane."""

    def active_write_turn(self) -> tuple[TurnId, str] | None: ...

    def activate_next_queued_turn(self) -> tuple[TurnId, str] | None: ...


class WorkspaceTurnAuthorityResolver(Protocol):
    def scheduling_authority(self, workspace_id: WorkspaceId) -> WorkspaceTurnAuthority: ...


class WorkspaceTurnRunner(Protocol):
    async def run_turn(self, workspace_id: WorkspaceId, turn_id: TurnId) -> None: ...
