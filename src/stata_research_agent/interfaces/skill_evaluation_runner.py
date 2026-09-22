"""Production independent Skill evaluator backed by the configured Provider."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any, Protocol, cast

from stata_research_agent.application.model_configuration import (
    WorkspaceModelConfigurationService,
)
from stata_research_agent.application.model_gateway import ProviderDispatchError, ProviderResponse
from stata_research_agent.application.provider_credentials import ProviderCredentialService
from stata_research_agent.application.sensitive_output import SensitiveOutputGate
from stata_research_agent.application.skill_evaluation import (
    EvaluateSkillCommand,
    SkillEvaluationJudgment,
    SkillEvaluationOutcome,
    SkillEvaluationVerdict,
    SkillImprovementKind,
    SkillImprovementProposalDraft,
)
from stata_research_agent.domain.identifiers import (
    CommandId,
    SkillEvaluationAttemptId,
    SkillEvaluationRunId,
    SkillImprovementProposalId,
    WorkspaceId,
)
from stata_research_agent.interfaces.openai_compatible_transport import (
    OpenAICompatibleChatTransport,
)
from stata_research_agent.persistence.atomic_commit import canonical_json
from stata_research_agent.persistence.skill_evaluation_query import SqliteSkillEvaluationQuery
from stata_research_agent.persistence.skill_evaluation_store import (
    SqliteSkillEvaluationRepository,
)
from stata_research_agent.persistence.workspace import WorkspaceDatabase
from stata_research_agent.runtime.uuid_identity import UuidIdentityGenerator

_PROMPT = """
You are an independent evaluator of a reusable Skill used by a Stata research agent.
Evaluate operational guidance quality, not the truth of the research conclusion.
The evidence contains exact Skill version identities and explicit user feedback that
co-occurred with use. This is observational evidence, never causal attribution.

Return no tool calls. Put one JSON object in the top-level `text` string:
{
  "verdict": "one allowed verdict listed below",
  "rationale": "concise evidence-calibrated explanation",
  "limitations": ["at least one explicit limitation"],
  "proposals": [{
    "kind": "revise|merge|retire|keep_observing",
    "title": "short title",
    "rationale": "why this is worth user review",
    "suggested_instruction_body": "required for revise/merge; full replacement guidance",
    "merge_target_skill_name": "required only for merge"
  }]
}

Allowed verdicts: insufficient_evidence, candidate_preferred, baseline_preferred,
mixed, no_material_difference.

For merge, use only a Skill listed in available_active_skills and provide the complete merged
replacement body for the evaluated Skill. Proposals are advisory only. Never claim a Skill was
changed, merged, activated, or retired.
Preserve research-method freedom: do not turn empirical methods into a whitelist/workflow.
Focus on clarity, control, traceability, data fidelity, and reusable interaction preferences.
When evidence is sparse or confounded, prefer insufficient_evidence/keep_observing.
Maximum 4 proposals.
""".strip()

_BLOCKED = re.compile(
    r"ignore\s+(all\s+)?previous|override\s+system\s+prompt|"
    r"bypass\s+(trace|evidence|confirmation|approval)|"
    r"fabricat(e|ion)|invent\s+(results?|numbers?|evidence)",
    re.I,
)


class WorkspaceDatabaseResolver(Protocol):
    def database(self, workspace_id: WorkspaceId) -> WorkspaceDatabase: ...


class ProductionSkillEvaluationRunner:
    EVALUATOR_REVISION = "independent-skill-evaluator-v1"

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

    async def evaluate(
        self, workspace_id: WorkspaceId, command: EvaluateSkillCommand
    ) -> SkillEvaluationOutcome:
        configuration = self._model_configuration.resolve(workspace_id.value)
        credential = self._credentials.resolve_for_transport(
            configuration.credential_ref,
            provider_profile_id=configuration.provider_profile_id,
            endpoint=configuration.endpoint,
        )
        database = self._databases.database(workspace_id)
        connection = database.open(writable=True)
        try:
            repository = SqliteSkillEvaluationRepository(connection)
            preparation = repository.prepare(
                command=command,
                run_id=self._identities.new(SkillEvaluationRunId),
                attempt_id=self._identities.new(SkillEvaluationAttemptId),
                provider_profile_id=configuration.provider_profile_id,
                credential_version_id=credential.credential_version_id,
                model_name=configuration.model_name,
                request_builder=lambda evidence: self._request(configuration.model_name, evidence),
            )
            if preparation.replayed:
                return SqliteSkillEvaluationQuery(connection).outcome(
                    preparation.run_id, replayed=True
                )
            try:
                response = await self._transport.send(
                    endpoint=configuration.endpoint,
                    request_json=preparation.request_json,
                    credential=credential.secret,
                )
                inspected = self._sensitive.inspect_json(
                    "skill.evaluator.response",
                    response.output,
                    protected_values=(credential.secret,),
                )
                if inspected.verdict != "safe":
                    raise ValueError("skill_evaluator_sensitive_output")
                safe_output = cast(Mapping[str, Any], inspected.safe_value)
                judgment = self._parse_judgment(safe_output)
                response = ProviderResponse(
                    safe_output,
                    response.usage_kind,
                    response.input_tokens,
                    response.output_tokens,
                    response.cached_input_tokens,
                    response.uncached_input_tokens,
                    response.finish_reason,
                )
            except ProviderDispatchError as error:
                repository.mark_failed(
                    command_id=self._phase_command("fail", preparation.run_id),
                    preparation=preparation,
                    error_code=error.code,
                    delivery_unknown=error.delivery_unknown,
                )
                raise
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                repository.mark_failed(
                    command_id=self._phase_command("invalid", preparation.run_id),
                    preparation=preparation,
                    error_code="skill_evaluation_output_contract_invalid",
                    delivery_unknown=False,
                )
                raise ValueError("independent Skill evaluation output was invalid") from error
            return repository.finalize(
                command_id=self._phase_command("finalize", preparation.run_id),
                preparation=preparation,
                response=response,
                judgment=judgment,
                proposal_ids=tuple(
                    self._identities.new(SkillImprovementProposalId) for _ in judgment.proposals
                ),
                evaluator_revision=self.EVALUATOR_REVISION,
            )
        finally:
            connection.close()

    @staticmethod
    def _request(model_name: str, evidence: Mapping[str, Any]) -> str:
        return canonical_json(
            {
                "model": model_name,
                "normalized_input": {
                    "system": {"content": _PROMPT},
                    "main_skill": {"content": ""},
                    "context": [evidence],
                    "tools": {},
                    "runtime": {"purpose": "independent_skill_evaluation"},
                },
                "policy": {"temperature": 0, "max_tokens": 3200},
            }
        )

    @staticmethod
    def _parse_judgment(output: Mapping[str, Any]) -> SkillEvaluationJudgment:
        raw_text = output.get("text")
        if not isinstance(raw_text, str):
            raise ValueError("Skill evaluator text is missing")
        parsed = json.loads(raw_text)
        if not isinstance(parsed, dict):
            raise ValueError("Skill evaluator result must be an object")
        verdict = str(parsed.get("verdict", ""))
        allowed_verdicts = {
            "insufficient_evidence",
            "candidate_preferred",
            "baseline_preferred",
            "mixed",
            "no_material_difference",
        }
        if verdict not in allowed_verdicts:
            raise ValueError("Skill evaluator verdict is invalid")
        rationale = str(parsed.get("rationale", ""))[:4000]
        raw_limitations = parsed.get("limitations")
        raw_proposals = parsed.get("proposals", [])
        if not isinstance(raw_limitations, list) or not isinstance(raw_proposals, list):
            raise ValueError("Skill evaluator collections are invalid")
        limitations = tuple(str(item)[:1000] for item in raw_limitations[:12] if str(item).strip())
        proposals: list[SkillImprovementProposalDraft] = []
        allowed_kinds = {"revise", "merge", "retire", "keep_observing"}
        for raw in raw_proposals[:4]:
            if not isinstance(raw, dict):
                raise ValueError("Skill improvement proposal must be an object")
            kind = str(raw.get("kind", ""))
            if kind not in allowed_kinds:
                raise ValueError("Skill improvement proposal kind is invalid")
            body = raw.get("suggested_instruction_body")
            normalized_body = None if body is None else str(body)[:16384]
            if normalized_body is not None and _BLOCKED.search(normalized_body):
                raise ValueError("Skill improvement proposal contains blocked guidance")
            merge_target = raw.get("merge_target_skill_name")
            proposals.append(
                SkillImprovementProposalDraft(
                    cast(SkillImprovementKind, kind),
                    str(raw.get("title", ""))[:240],
                    str(raw.get("rationale", ""))[:4000],
                    normalized_body,
                    None if merge_target is None else str(merge_target)[:64],
                )
            )
        return SkillEvaluationJudgment(
            cast(SkillEvaluationVerdict, verdict),
            rationale,
            limitations,
            tuple(proposals),
        )

    @staticmethod
    def _phase_command(phase: str, run_id: SkillEvaluationRunId) -> CommandId:
        return CommandId(f"cmd_skill_evaluation_{phase}_{run_id.value.removeprefix('skilleval_')}")
