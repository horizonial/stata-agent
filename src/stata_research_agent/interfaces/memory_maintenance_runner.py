"""Production model-backed Project Memory Curator."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Protocol, cast

from stata_research_agent.application.memory import MemoryKind, MemoryLifecycle
from stata_research_agent.application.memory_curator import (
    MemoryCandidateProposal,
    MemoryExtractionOutput,
    MemoryMaintenanceOutcome,
    MemoryMaintenanceWindow,
    MemorySourceMessage,
    SkillEvolutionProposal,
)
from stata_research_agent.application.model_configuration import (
    WorkspaceModelConfigurationService,
)
from stata_research_agent.application.model_gateway import ProviderDispatchError, ProviderResponse
from stata_research_agent.application.provider_credentials import ProviderCredentialService
from stata_research_agent.application.sensitive_output import SensitiveOutputGate
from stata_research_agent.domain.identifiers import (
    CommandId,
    MemoryMaintenanceJobId,
    MemoryProviderAttemptId,
    MessageId,
    WorkspaceId,
)
from stata_research_agent.interfaces.openai_compatible_transport import (
    OpenAICompatibleChatTransport,
)
from stata_research_agent.persistence.atomic_commit import canonical_json
from stata_research_agent.persistence.filesystem_memory import FilesystemMemoryStore
from stata_research_agent.persistence.memory_curator_store import (
    SqliteMemoryMaintenanceRepository,
)
from stata_research_agent.persistence.workspace import WorkspaceDatabase
from stata_research_agent.runtime.uuid_identity import UuidIdentityGenerator


class WorkspaceDatabaseResolver(Protocol):
    def database(self, workspace_id: WorkspaceId) -> WorkspaceDatabase: ...


_CURATOR_PROMPT = """
You are the background Project Memory Curator for a Stata research agent.
Extract only durable project continuity from the supplied completed conversation window.
Memory is not Evidence and must never contain invented numeric results or claims.

Put a JSON-encoded object in the top-level `text` string and return no tool calls.
The encoded object must have:
{
  "episode_summary": "brief factual summary",
  "candidates": [{
    "source_message_id": "msg_...",
    "kind": "one registered Memory kind",
    "title": "short title",
    "content": "durable statement",
    "supporting_quote": "exact contiguous quote copied from that user message",
    "suggested_lifecycle": "active|proposed"
  }],
  "skill_candidates": [{
    "skill_name": "lowercase-hyphenated-name",
    "description": "what the Skill does and when it applies",
    "instruction_body": "concise reusable guidance",
    "rationale": "why these repeated memories form reusable guidance",
    "source_memory_item_ids": ["memoryitem_...", "memoryitem_..."]
  }]
}

Use active only for an explicit research decision, constraint, feedback, or unresolved question
whose content is directly supported by the exact quote. Use proposed for preferences, procedures,
generalizations, or any inference. Omit transient requests, greetings, generated results, secrets,
and anything already represented by the active-memory index. Maximum 24 candidates.

Optionally propose at most 4 skill_candidates, but only when at least two distinct entries from
different user interactions in the active-memory index show a stable recurring preference,
feedback pattern, or project
procedure. Never derive a Skill from research decisions, constraints, unresolved questions,
numeric results, or a single example. A Skill candidate is review-only: do not claim it is
installed or active. Keep it instruction-only; never request scripts, credentials, prompt
overrides, audit bypasses, fabricated results, or destructive actions. Otherwise return [].
""".strip()


class ProductionMemoryMaintenanceRunner:
    """Run at most one recoverable Curator job for an idle Workspace."""

    CURATOR_REVISION = "memory-curator-v1"

    def __init__(
        self,
        databases: WorkspaceDatabaseResolver,
        model_configuration: WorkspaceModelConfigurationService,
        credentials: ProviderCredentialService,
        *,
        transport: OpenAICompatibleChatTransport | None = None,
        sensitive_output_gate: SensitiveOutputGate | None = None,
    ) -> None:
        self._databases = databases
        self._model_configuration = model_configuration
        self._credentials = credentials
        self._transport = transport or OpenAICompatibleChatTransport()
        self._sensitive = sensitive_output_gate or SensitiveOutputGate()
        self._identities = UuidIdentityGenerator()

    async def run_once(self, workspace_id: WorkspaceId) -> MemoryMaintenanceOutcome | None:
        database = self._databases.database(workspace_id)
        connection = database.open(writable=True)
        try:
            repository = SqliteMemoryMaintenanceRepository(connection, self._identities)
            repository.recover_interrupted()
            memory_files = FilesystemMemoryStore(database.root)
            memory_files.reconcile_external_edits(connection, self._identities)
            window = repository.discover_window()
            if window is None or not window.messages:
                repository.apply_retention_policy(
                    command_id=self._identities.new(CommandId),
                    observed_at=datetime.now(UTC).isoformat(),
                )
                memory_files.synchronize(connection)
                return None
            job = repository.enqueue(
                command_id=self._identities.new(CommandId),
                job_id=self._identities.new(MemoryMaintenanceJobId),
                window=window,
                curator_revision=self.CURATOR_REVISION,
            )
            if job.attempt_count >= 3:
                return MemoryMaintenanceOutcome(job.job_id, "failed")

            configuration = self._model_configuration.resolve(workspace_id.value)
            credential = self._credentials.resolve_for_transport(
                configuration.credential_ref,
                provider_profile_id=configuration.provider_profile_id,
                endpoint=configuration.endpoint,
            )
            request_json = self._request(configuration.model_name, window)
            attempt_id = self._identities.new(MemoryProviderAttemptId)
            repository.prepare_attempt(
                command_id=self._identities.new(CommandId),
                job_id=job.job_id,
                attempt_id=attempt_id,
                provider_profile_id=configuration.provider_profile_id,
                credential_version_id=credential.credential_version_id,
                endpoint_origin=repository.endpoint_origin(configuration.endpoint),
                model_name=configuration.model_name,
                request_json=request_json,
            )
            repository.mark_dispatch_started(
                command_id=self._identities.new(CommandId),
                job_id=job.job_id,
                attempt_id=attempt_id,
            )
            try:
                response = await self._transport.send(
                    endpoint=configuration.endpoint,
                    request_json=request_json,
                    credential=credential.secret,
                )
                inspected = self._sensitive.inspect_json(
                    "memory.curator.response",
                    response.output,
                    protected_values=(credential.secret,),
                )
                if inspected.verdict != "safe":
                    raise ValueError("memory_curator_sensitive_output")
                safe_output = cast(Mapping[str, Any], inspected.safe_value)
                extraction = self._parse_extraction(safe_output, window.messages)
                response = ProviderResponse(
                    safe_output,
                    response.usage_kind,
                    response.input_tokens,
                    response.output_tokens,
                    response.cached_input_tokens,
                    response.uncached_input_tokens,
                )
            except ProviderDispatchError as error:
                repository.fail(
                    command_id=self._identities.new(CommandId),
                    job_id=job.job_id,
                    attempt_id=attempt_id,
                    error_code=error.code,
                    delivery_unknown=error.delivery_unknown,
                )
                return MemoryMaintenanceOutcome(
                    job.job_id, "delivery_unknown" if error.delivery_unknown else "failed"
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                repository.fail(
                    command_id=self._identities.new(CommandId),
                    job_id=job.job_id,
                    attempt_id=attempt_id,
                    error_code="memory_output_contract_invalid",
                    delivery_unknown=False,
                )
                return MemoryMaintenanceOutcome(job.job_id, "failed")

            outcome = repository.finalize(
                command_id=self._identities.new(CommandId),
                job=job,
                attempt_id=attempt_id,
                response=response,
                extraction=extraction,
            )
            repository.apply_retention_policy(
                command_id=self._identities.new(CommandId),
                observed_at=datetime.now(UTC).isoformat(),
            )
            memory_files.synchronize(connection)
            return outcome
        finally:
            connection.close()

    @staticmethod
    def _request(model_name: str, window: MemoryMaintenanceWindow) -> str:
        payload = {
            "messages": [
                {
                    "message_id": message.message_id.value,
                    "created_revision": message.created_revision,
                    "content": message.content,
                }
                for message in window.messages
            ],
            "assistant_texts": list(window.assistant_texts),
            "active_memory_index": list(window.active_memory_index),
            "source_window": [window.source_start_revision, window.source_end_revision],
        }
        return canonical_json(
            {
                "model": model_name,
                "normalized_input": {
                    "system": {"content": _CURATOR_PROMPT},
                    "main_skill": {"content": ""},
                    "context": [payload],
                    "tools": {},
                    "runtime": {"purpose": "project_memory_maintenance"},
                },
                "policy": {"temperature": 0, "max_tokens": 2400},
            }
        )

    @staticmethod
    def _parse_extraction(
        output: Mapping[str, Any], source_messages: tuple[MemorySourceMessage, ...]
    ) -> MemoryExtractionOutput:
        raw_text = output.get("text")
        if not isinstance(raw_text, str):
            raise ValueError("Memory Curator text is missing")
        parsed = json.loads(raw_text)
        if not isinstance(parsed, dict):
            raise ValueError("Memory Curator result must be an object")
        summary = parsed.get("episode_summary")
        raw_candidates = parsed.get("candidates")
        raw_skills = parsed.get("skill_candidates", [])
        if (
            not isinstance(summary, str)
            or not summary.strip()
            or not isinstance(raw_candidates, list)
            or not isinstance(raw_skills, list)
        ):
            raise ValueError("Memory Curator result shape is invalid")
        allowed_ids = {message.message_id.value for message in source_messages}
        candidates: list[MemoryCandidateProposal] = []
        for raw in raw_candidates[:24]:
            if not isinstance(raw, dict):
                raise ValueError("Memory candidate must be an object")
            source_id = raw.get("source_message_id")
            if not isinstance(source_id, str) or source_id not in allowed_ids:
                raise ValueError("Memory candidate source is outside the extraction window")
            candidate = MemoryCandidateProposal(
                source_message_id=MessageId(source_id),
                kind=MemoryKind(str(raw.get("kind", ""))),
                title=str(raw.get("title", ""))[:240],
                content=str(raw.get("content", ""))[:4000],
                supporting_quote=str(raw.get("supporting_quote", ""))[:4000],
                suggested_lifecycle=MemoryLifecycle(str(raw.get("suggested_lifecycle", ""))),
            )
            candidates.append(candidate)
        skill_candidates: list[SkillEvolutionProposal] = []
        for raw in raw_skills[:4]:
            if not isinstance(raw, dict):
                raise ValueError("Skill evolution candidate must be an object")
            raw_sources = raw.get("source_memory_item_ids")
            if not isinstance(raw_sources, list) or not all(
                isinstance(item, str) for item in raw_sources
            ):
                raise ValueError("Skill evolution sources must be Memory item ids")
            skill_candidates.append(
                SkillEvolutionProposal(
                    skill_name=str(raw.get("skill_name", ""))[:64],
                    description=str(raw.get("description", ""))[:500],
                    instruction_body=str(raw.get("instruction_body", ""))[:16384],
                    rationale=str(raw.get("rationale", ""))[:2000],
                    source_memory_item_ids=tuple(raw_sources[:16]),
                )
            )
        return MemoryExtractionOutput(summary[:4000], tuple(candidates), tuple(skill_candidates))
