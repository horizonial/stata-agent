"""Adapter from an admitted generic Tool Call to the Stata operation protocol."""

from __future__ import annotations

import json

from stata_research_agent.domain.identifiers import CommandId

from .ports.identity import IdentityGenerator
from .stata_operation import ExecuteStataCommand
from .stata_operation_service import StataOperationService
from .turn_driver import ToolExecutionRequest, ToolExecutionResult


class StataToolExecutor:
    def __init__(
        self,
        service: StataOperationService,
        identities: IdentityGenerator,
        *,
        authoritative_session_id: str | None = None,
    ) -> None:
        self._service = service
        self._identities = identities
        self._authoritative_session_id = authoritative_session_id

    async def execute(self, request: ToolExecutionRequest) -> ToolExecutionResult:
        if request.tool_name != "stata.run":
            raise ValueError(f"unsupported admitted tool: {request.tool_name}")
        code = request.arguments.get("code")
        proposed_session_id = request.arguments.get("session_id")
        if not isinstance(code, str):
            raise ValueError("stata.run requires string code")
        if self._authoritative_session_id is None:
            if not isinstance(proposed_session_id, str):
                raise ValueError("stata.run requires string session_id")
            session_id = proposed_session_id
        else:
            if (
                proposed_session_id is not None
                and proposed_session_id != self._authoritative_session_id
            ):
                raise ValueError(
                    "stata.run session_id does not match the authoritative Execution Scope"
                )
            session_id = self._authoritative_session_id
        timeout = request.arguments.get("timeout_seconds", 300.0)
        if not isinstance(timeout, (int, float)):
            raise ValueError("stata.run timeout_seconds must be numeric")
        if request.remaining_time_seconds is not None:
            timeout = min(float(timeout), max(0.1, request.remaining_time_seconds))
        outcome = await self._service.execute(
            ExecuteStataCommand(
                self._identities.new(CommandId),
                request.turn_id,
                session_id,
                code,
                float(timeout),
                tool_call_id=request.tool_call_id,
                admitted_operation_id=request.operation_id,
            )
        )
        payload = {
            "operation_id": outcome.operation_id.value,
            "operation_attempt_id": outcome.attempt_id.value,
            "status": outcome.status,
            "execution_status": (
                None if outcome.execution_status is None else outcome.execution_status.value
            ),
            "completion_manifest_id": (
                None if outcome.manifest_id is None else outcome.manifest_id.value
            ),
        }
        return ToolExecutionResult(
            outcome.status == "completed",
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        )
