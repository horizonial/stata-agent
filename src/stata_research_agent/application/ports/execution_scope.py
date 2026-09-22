"""Inward port for resolving an authoritative Workspace execution target."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from stata_research_agent.domain.identifiers import (
    ExecutionScopeId,
    TurnId,
    WorkspaceId,
)


@dataclass(frozen=True, slots=True)
class AuthorizedExecutionScope:
    workspace_id: WorkspaceId
    execution_scope_id: ExecutionScopeId
    workspace_root: Path
    active_write_turn_id: TurnId


class ExecutionScopeAuthority(Protocol):
    def active_write_scope(self, turn_id: TurnId) -> AuthorizedExecutionScope: ...
