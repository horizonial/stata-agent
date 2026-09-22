"""Admitted progressive-recall tools for Project Memory."""

from __future__ import annotations

import json

from stata_research_agent.application.broker_execution import (
    BeginBrokerExecutionCommand,
    CompleteBrokerExecutionCommand,
)
from stata_research_agent.application.broker_execution_service import BrokerExecutionService
from stata_research_agent.application.ports.identity import IdentityGenerator
from stata_research_agent.application.turn_driver import ToolExecutionRequest, ToolExecutionResult
from stata_research_agent.domain.identifiers import CommandId
from stata_research_agent.persistence.memory_recall_store import SqliteMemoryRecallRepository


class MemoryRecallExecutor:
    """Search a Memory index or open exact current revisions through the Tool Broker."""

    def __init__(
        self,
        repository: SqliteMemoryRecallRepository,
        bridge: BrokerExecutionService,
        identities: IdentityGenerator,
        *,
        research_path_id: str,
        action: str,
    ) -> None:
        if action not in {"search", "open"}:
            raise ValueError("unknown Memory recall action")
        self._repository = repository
        self._bridge = bridge
        self._identities = identities
        self._research_path_id = research_path_id
        self._action = action

    async def execute(self, request: ToolExecutionRequest) -> ToolExecutionResult:
        handle = self._bridge.begin(
            BeginBrokerExecutionCommand(
                self._identities.new(CommandId),
                request.turn_id,
                request.tool_call_id,
                request.operation_id,
            )
        )
        try:
            self._repository.reconcile_external_edits()
            if self._action == "search":
                hits = self._repository.search(
                    request.arguments, research_path_id=self._research_path_id
                )
                summary = f"Found {len(hits)} relevant current Memory items"
            else:
                hits = self._repository.open(
                    request.arguments, research_path_id=self._research_path_id
                )
                summary = f"Opened {len(hits)} exact current Memory revisions"
            payload: dict[str, object] = {
                "boundary": (
                    "Project Memory is advisory context, not statistical Evidence or current "
                    "Research State. Current user instructions and authoritative research facts "
                    "take precedence."
                ),
                "action": self._action,
                "hits": hits,
            }
            success = True
        except Exception as error:
            payload = {"error_type": type(error).__name__, "message": str(error)}
            success = False
            summary = "Project Memory recall failed"
        outcome = self._bridge.complete(
            CompleteBrokerExecutionCommand(
                self._identities.new(CommandId), handle, success, summary, payload
            )
        )
        payload["status"] = outcome.status
        return ToolExecutionResult(
            success, json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        )
