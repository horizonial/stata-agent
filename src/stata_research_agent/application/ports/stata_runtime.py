"""Port owned by the application for a supervised external Stata executor."""

from __future__ import annotations

from typing import Protocol

from stata_research_agent.domain.stata_execution import (
    StataArtifactOutputRequest,
    StataRuntimeResult,
    StataSessionCloseResult,
)


class StataRuntime(Protocol):
    async def execute(
        self,
        *,
        session_id: str,
        code: str,
        timeout_seconds: float,
        operation_attempt_id: str | None = None,
        artifact_outputs: tuple[StataArtifactOutputRequest, ...] = (),
    ) -> StataRuntimeResult: ...

    async def close_session(self, *, session_id: str, reason: str) -> StataSessionCloseResult: ...
