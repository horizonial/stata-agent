"""Admitted high-level research Tool adapter for the M2 autonomous vertical."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass

from stata_research_agent.application.broker_execution import (
    BeginBrokerExecutionCommand,
    CompleteBrokerExecutionCommand,
)
from stata_research_agent.application.broker_execution_service import BrokerExecutionService
from stata_research_agent.application.ports.identity import IdentityGenerator
from stata_research_agent.application.research_workflow import RunResearchToWordCommand
from stata_research_agent.application.research_workflow_service import ResearchToWordService
from stata_research_agent.application.turn_driver import (
    ToolExecutionRequest,
    ToolExecutionResult,
)
from stata_research_agent.domain.identifiers import CommandId


@dataclass(frozen=True, slots=True)
class ResearchWorkflowExecutor:
    workflow: ResearchToWordService
    bridge: BrokerExecutionService
    identities: IdentityGenerator
    workflow_command: RunResearchToWordCommand | None = None
    command_factory: Callable[[ToolExecutionRequest], RunResearchToWordCommand] | None = None

    def __post_init__(self) -> None:
        if (self.workflow_command is None) == (self.command_factory is None):
            raise ValueError("provide exactly one research workflow command source")

    async def execute(self, request: ToolExecutionRequest) -> ToolExecutionResult:
        if request.tool_name != "research.run_to_word":
            raise ValueError(f"unsupported admitted tool: {request.tool_name}")
        handle = self.bridge.begin(
            BeginBrokerExecutionCommand(
                self.identities.new(CommandId),
                request.turn_id,
                request.tool_call_id,
                request.operation_id,
            )
        )
        try:
            command = self.workflow_command
            if command is None:
                assert self.command_factory is not None
                command = self.command_factory(request)
            result = await self.workflow.run(command)
        except Exception as error:
            error_detail = (
                str(error)[:500]
                if isinstance(error, (ValueError, RuntimeError))
                else type(error).__name__
            )
            completed = self.bridge.complete(
                CompleteBrokerExecutionCommand(
                    self.identities.new(CommandId),
                    handle,
                    False,
                    f"Research-to-Word workflow failed: {type(error).__name__}",
                    {
                        "error_kind": type(error).__name__,
                        "error_detail": error_detail,
                    },
                )
            )
            return ToolExecutionResult(
                completed.status == "completed",
                json.dumps(
                    {
                        "error_kind": type(error).__name__,
                        "error_detail": error_detail,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
        payload = {
            "result_id": result.result_id.value,
            "table_render_receipt_id": result.table_render_receipt_id.value,
            "document_revision_id": result.document_revision_id.value,
            "docx_artifact_id": result.docx_artifact_id,
            "delivery_verdict": result.delivery_verdict,
        }
        completed = self.bridge.complete(
            CompleteBrokerExecutionCommand(
                self.identities.new(CommandId),
                handle,
                True,
                "Research-to-Word workflow completed",
                payload,
                (result.docx_artifact_id,),
            )
        )
        return ToolExecutionResult(
            completed.status == "completed",
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        )
