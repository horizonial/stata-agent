"""Port for bounded discovery of user-owned Stata data inside one Workspace."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from stata_research_agent.application.data_intake import WorkspaceDataCandidate


class WorkspaceDataCatalog(Protocol):
    def discover(self) -> tuple[WorkspaceDataCandidate, ...]: ...

    def resolve(self, relative_path: str) -> Path: ...
