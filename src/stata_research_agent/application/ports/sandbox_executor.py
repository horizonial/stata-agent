"""Inward port for staged arbitrary-code execution."""

from typing import Protocol

from stata_research_agent.application.sandbox_execution import (
    SandboxExecutionReceipt,
    SandboxExecutionRequest,
)


class SandboxExecutor(Protocol):
    def execute(self, request: SandboxExecutionRequest) -> SandboxExecutionReceipt: ...
