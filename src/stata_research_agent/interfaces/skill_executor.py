"""Filesystem adapter for progressive, auditable loading of research Skills."""

from __future__ import annotations

import json
from pathlib import Path

from stata_research_agent.application.broker_execution import (
    BeginBrokerExecutionCommand,
    CompleteBrokerExecutionCommand,
)
from stata_research_agent.application.broker_execution_service import (
    BrokerExecutionService,
)
from stata_research_agent.application.ports.identity import IdentityGenerator
from stata_research_agent.application.turn_driver import (
    ToolExecutionRequest,
    ToolExecutionResult,
)
from stata_research_agent.domain.identifiers import CommandId

from .workspace_skills import FilesystemMainSkillCatalog


class LoadSpecializedSkillExecutor:
    def __init__(
        self,
        catalog: FilesystemMainSkillCatalog,
        workspace_root: Path,
        bridge: BrokerExecutionService,
        identities: IdentityGenerator,
    ) -> None:
        self._catalog = catalog
        self._workspace_root = workspace_root
        self._bridge = bridge
        self._identities = identities

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
            skill = self._catalog.load_specialized(
                self._workspace_root, str(request.arguments["skill_name"])
            )
            if skill.revision != str(request.arguments["expected_revision"]):
                raise ValueError("specialized Skill changed after catalog selection")
            payload: dict[str, object] = {
                "name": skill.name,
                "description": skill.description,
                "revision": skill.revision,
                "content_sha256": skill.content_sha256,
                "source_kind": skill.source_kind,
                "content": skill.content,
            }
            success = True
            summary = f"Loaded registered specialized Skill {skill.name}"
        except Exception as error:
            payload = {
                "error_type": type(error).__name__,
                "message": str(error),
            }
            success = False
            summary = "Specialized Skill load failed"
        outcome = self._bridge.complete(
            CompleteBrokerExecutionCommand(
                self._identities.new(CommandId),
                handle,
                success,
                summary,
                payload,
            )
        )
        payload["status"] = outcome.status
        return ToolExecutionResult(
            success,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        )
