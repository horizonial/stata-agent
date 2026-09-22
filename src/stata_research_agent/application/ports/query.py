"""Read-only authoritative query port."""

from typing import Protocol

from stata_research_agent.application.queries import (
    WorkspaceBootstrapSnapshot,
    WorkspaceExecutionSnapshot,
)


class WorkspaceQuery(Protocol):
    def execution_snapshot(self) -> WorkspaceExecutionSnapshot: ...

    def bootstrap_snapshot(self) -> WorkspaceBootstrapSnapshot: ...
