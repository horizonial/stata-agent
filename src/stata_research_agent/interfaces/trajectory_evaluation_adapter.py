"""Deterministic Agent-loop trajectory evaluation over the production Turn Driver."""

from __future__ import annotations

import asyncio
import json
import shutil
from hashlib import sha256
from pathlib import Path
from typing import Any

from stata_research_agent.application.control import (
    CreateWorkspaceCommand,
    SubmitMessageCommand,
)
from stata_research_agent.application.evaluation import (
    ContractObligationCandidate,
    NormalizeCompletionContractCommand,
    ObligationProvenance,
    RequirementLevel,
)
from stata_research_agent.application.evaluation_service import RuntimeEvaluationService
from stata_research_agent.application.model_gateway import (
    ContextItemCandidate,
    ProviderResponse,
)
from stata_research_agent.application.model_gateway_service import ModelGatewayService
from stata_research_agent.application.product_evaluation import (
    EvaluationLevel,
    EvaluationPayloadReference,
    EvaluationScenario,
    EvaluationScore,
    GraderKind,
    GraderSpec,
    ProductEvaluationError,
    ScoreVerdict,
    TrialBundle,
)
from stata_research_agent.application.provider_credentials import ResolvedProviderCredential
from stata_research_agent.application.stata_operation_service import StataOperationService
from stata_research_agent.application.stata_tool_executor import StataToolExecutor
from stata_research_agent.application.tool_broker import (
    RegisterToolContractCommand,
    ResourceClaimTemplate,
    ToolContractDefinition,
)
from stata_research_agent.application.tool_broker_service import ToolBrokerService
from stata_research_agent.application.turn_driver import (
    TurnDriverConfig,
    TurnDriverModelConfig,
)
from stata_research_agent.application.workspace_service import WorkspaceControlService
from stata_research_agent.artifacts.completion_manifest_store import (
    FilesystemCompletionManifestStore,
)
from stata_research_agent.artifacts.managed_store import FilesystemManagedArtifactStore
from stata_research_agent.domain.identifiers import CommandId, WorkspaceId
from stata_research_agent.domain.stata_execution import (
    StataExecutionReceipt,
    StataExecutionStatus,
    StataRuntimeResult,
    StataSessionCloseResult,
)
from stata_research_agent.persistence.control_store import SqliteControlStore
from stata_research_agent.persistence.evaluation_store import SqliteEvaluationRepository
from stata_research_agent.persistence.model_gateway_store import SqliteModelGatewayRepository
from stata_research_agent.persistence.stata_operation_store import (
    SqliteStataOperationRepository,
)
from stata_research_agent.persistence.tool_broker_store import SqliteToolBrokerRepository
from stata_research_agent.persistence.trajectory_evaluation_query import (
    SqliteTrajectoryEvaluationQuery,
)
from stata_research_agent.persistence.workspace import WorkspaceDatabase
from stata_research_agent.runtime.agent_turn_driver import AgentTurnDriver
from stata_research_agent.runtime.uuid_identity import UuidIdentityGenerator


class _Credential:
    def resolve_for_transport(
        self,
        credential_ref: str,
        *,
        provider_profile_id: str,
        endpoint: str,
    ) -> ResolvedProviderCredential:
        del credential_ref
        return ResolvedProviderCredential(
            provider_profile_id,
            "credentialversion_eval",
            endpoint,
            "trajectory-evaluation-secret",
        )


class _TwoStepModel:
    def __init__(self) -> None:
        self.calls = 0

    async def send(
        self,
        *,
        endpoint: str,
        request_json: str,
        credential: str,
    ) -> ProviderResponse:
        del endpoint, credential
        self.calls += 1
        if self.calls == 1:
            if "Inspect the traceable Stata data" not in request_json:
                raise ProductEvaluationError("first Agent context omitted the user request")
            return ProviderResponse(
                {
                    "text": "I will inspect the data through the supervised Stata tool.",
                    "tool_calls": [
                        {
                            "name": "stata.run",
                            "arguments": {
                                "session_id": "scope-main",
                                "code": "describe",
                            },
                        }
                    ],
                }
            )
        if "completion_manifest_id" not in request_json:
            raise ProductEvaluationError("second Agent context omitted the Tool Result")
        return ProviderResponse(
            {
                "text": "The required traceable Stata inspection is complete.",
                "tool_calls": [],
                "completion": {
                    "disposition": "succeed",
                    "summary": "The required Stata inspection completed.",
                },
            }
        )


class _SuccessfulStataRuntime:
    async def execute(self, **kwargs: Any) -> StataRuntimeResult:
        session_id = str(kwargs["session_id"])
        return StataRuntimeResult(
            "stata-mcp.envelope/v1",
            "Contains data; 74 observations",
            {"N": 74.0, "variables": 12.0},
            StataExecutionReceipt(
                "stata.execution-receipt/v1alpha1",
                "trajectory-eval-executor",
                session_id,
                1,
                1,
                StataExecutionStatus.SUCCEEDED,
                0,
                "complete",
                "complete",
                "trajectory-command-hash",
                "74:12:trajectory",
                False,
                {"stata_version": "18", "evaluation_fixture": True},
                {"supervised": True},
            ),
            False,
        )

    async def close_session(self, *, session_id: str, reason: str) -> StataSessionCloseResult:
        del reason
        return StataSessionCloseResult(
            "stata.session-control/v1alpha1",
            "trajectory-eval-executor",
            session_id,
            True,
            {},
        )


def _stata_contract() -> ToolContractDefinition:
    return ToolContractDefinition(
        "stata.run",
        "1.0.0",
        "Run one supervised Stata inspection",
        "stata.execute",
        {
            "type": "object",
            "properties": {
                "code": {"type": "string"},
                "session_id": {"type": "string"},
            },
            "required": ["code", "session_id"],
            "additionalProperties": False,
        },
        {"type": "object"},
        "local_runtime",
        "workspace_write",
        "exclusive_scope",
        "non_replayable",
        "never",
        "not_interruptible",
        300,
        3600,
        1_000_000,
        (ResourceClaimTemplate("stata-session:{session_id}", "exclusive", "session_id"),),
    )


class TurnLoopTrajectoryEvaluationAdapter:
    """Drive a real two-Step Agent Turn with deterministic Provider and Stata fixtures."""

    scenario_id = "agent.trajectory.turn-loop-success"

    def __init__(self, project_root: Path, worker_python: Path) -> None:
        self._project_root = project_root.resolve()
        self._worker_python = worker_python.resolve()

    def execute(
        self,
        scenario: EvaluationScenario,
        *,
        trial_id: str,
        trial_root: Path,
    ) -> TrialBundle:
        self._validate_scenario(scenario)
        if trial_root.exists() and any(trial_root.iterdir()):
            raise ProductEvaluationError("trajectory Trial bundle directory is not empty")
        trial_root.mkdir(parents=True, exist_ok=True)
        reference_source = self._verified_reference(scenario)
        reference_path = trial_root / "trajectory-reference.json"
        shutil.copy2(reference_source, reference_path)
        workspace_root = trial_root / "workspace"
        workspace_id = WorkspaceId(f"ws_{trial_id.replace('-', '_')}")
        database = WorkspaceDatabase(workspace_root, workspace_id)
        database.create()
        connection = database.open(writable=True)
        identities = UuidIdentityGenerator()
        try:
            control = WorkspaceControlService(SqliteControlStore(connection), identities)
            initialized = control.create_workspace(
                CreateWorkspaceCommand(CommandId(f"cmd_{trial_id}_workspace"), workspace_id)
            )
            turn = control.submit_message(
                SubmitMessageCommand(
                    CommandId(f"cmd_{trial_id}_turn"),
                    "Inspect the traceable Stata data and stop only after the Tool Result exists.",
                )
            )
            evaluator = RuntimeEvaluationService(SqliteEvaluationRepository(connection), identities)
            normalized = evaluator.normalize_contract(
                NormalizeCompletionContractCommand(
                    CommandId(f"cmd_{trial_id}_normalize"),
                    turn.turn_id,
                    1,
                    "Inspect data through a traceable Stata operation.",
                    (
                        ContractObligationCandidate(
                            "stata.inspection",
                            "Run traceable Stata inspection",
                            ObligationProvenance.USER_EXPLICIT,
                            "message",
                            turn.message_id.value,
                            RequirementLevel.REQUIRED,
                            "A successful Stata operation and Completion Manifest exist.",
                        ),
                    ),
                )
            )
            broker = ToolBrokerService(SqliteToolBrokerRepository(connection), identities)
            broker.register_contract(
                RegisterToolContractCommand(
                    CommandId(f"cmd_{trial_id}_register"), turn.turn_id, _stata_contract()
                )
            )
            driver = AgentTurnDriver(
                ModelGatewayService(
                    SqliteModelGatewayRepository(connection),
                    identities,
                    _Credential(),
                    _TwoStepModel(),
                ),
                broker,
                evaluator,
                StataToolExecutor(
                    StataOperationService(
                        SqliteStataOperationRepository(connection),
                        _SuccessfulStataRuntime(),
                        identities,
                        FilesystemCompletionManifestStore(workspace_root),
                        FilesystemManagedArtifactStore(workspace_root),
                    ),
                    identities,
                ),
                identities,
                self._worker_python,
            )
            outcome = asyncio.run(
                driver.run(
                    TurnDriverConfig(
                        turn.turn_id,
                        2,
                        workspace_id.value,
                        initialized.main_scope_id.value,
                        initialized.main_path_id.value,
                        TurnDriverModelConfig(
                            "system-v1",
                            "You are a traceable Stata research agent.",
                            "research-main",
                            "skill-v1",
                            "Use supervised tools and stop only after checking results.",
                            "catalog-v1",
                            (
                                {
                                    "name": "stata.run",
                                    "input_schema": _stata_contract().input_schema,
                                },
                            ),
                            "permission-v1",
                            {"stata_execute": True},
                            "model-policy-v1",
                            "deterministic-eval",
                            "test",
                            "two-step-model",
                            "https://provider.invalid/responses",
                            "credential://trajectory-eval",
                            {},
                        ),
                        (
                            ContextItemCandidate(
                                "user_message",
                                "message",
                                turn.message_id.value,
                                "1",
                                "remote_allowed",
                                (
                                    "Inspect the traceable Stata data and stop only after the "
                                    "Tool Result exists."
                                ),
                            ),
                        ),
                        {"resource_identities": {"stata-session:scope-main": "scope-main"}},
                        ("workspace_write",),
                        {"stata.run": normalized.obligation_ids},
                        4,
                    )
                )
            )
            report = self._trajectory_report(connection, outcome)
            observations = self._observations(report)
            report_path = trial_root / "trajectory-report.json"
            report_bytes = self._json(report).encode("utf-8")
            report_path.write_bytes(report_bytes)
            trace_path = trial_root / "trace.jsonl"
            self._export_trace(connection, trace_path)
            artifact_manifest = trial_root / "artifact-manifest.json"
            artifact_manifest.write_text(
                self._json(
                    {
                        "schema_version": "stata-research-agent.eval-artifacts/v1",
                        "artifacts": [
                            {"role": "trajectory_report", "locator": "trajectory-report.json"},
                            {
                                "role": "trajectory_reference",
                                "locator": "trajectory-reference.json",
                            },
                            {"role": "trace", "locator": "trace.jsonl"},
                            {
                                "role": "workspace_database",
                                "locator": "workspace/workspace.sqlite3",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
                newline="\n",
            )
            return TrialBundle(
                trial_id,
                scenario.scenario_id,
                scenario.revision,
                scenario.scenario_sha256,
                workspace_root,
                database.database_path,
                trace_path,
                artifact_manifest,
                tuple(sorted(observations)),
                ("evaluation_payload:trajectory-report",),
                (
                    EvaluationPayloadReference(
                        "trajectory-report",
                        "application/json",
                        report_path,
                        sha256(report_bytes).hexdigest(),
                    ),
                    EvaluationPayloadReference(
                        "trajectory-reference",
                        "application/json",
                        reference_path,
                        sha256(reference_path.read_bytes()).hexdigest(),
                    ),
                ),
            )
        finally:
            connection.close()

    @staticmethod
    def _trajectory_report(connection: Any, outcome: Any) -> dict[str, Any]:
        facts = SqliteTrajectoryEvaluationQuery(connection).turn_loop_report()
        return {
            "schema_version": "stata-research-agent.trajectory-report/v1",
            "outcome": {
                "status": outcome.status,
                "executed_steps": outcome.executed_steps,
                "tool_executions": outcome.tool_executions,
                "stop_guard_decision": (
                    outcome.stop_guard.decision.value if outcome.stop_guard else None
                ),
            },
            **facts,
        }

    @staticmethod
    def _observations(report: dict[str, Any]) -> set[str]:
        observations: set[str] = set()
        if report["outcome"]["status"] == "succeeded":
            observations.add("turn.succeeded")
        if len(report["steps"]) == 2:
            observations.add("turn.two_steps_completed")
        if len(report["admissions"]) == len(report["operations"]) == 1:
            observations.add("tool.admitted_before_execution")
        if (
            len(report["tool_results"]) == 1
            and report["tool_results"][0]["result_kind"] == "success"
        ):
            observations.add("tool.result_committed")
        if report["goal_coverage"] and report["goal_coverage"]["coverage_status"] == "satisfied":
            observations.add("goal.coverage_satisfied")
        if report["stop_guard"] and report["stop_guard"]["decision"] == "terminate":
            observations.add("stop_guard.terminate_after_evidence")
        return observations

    @staticmethod
    def _export_trace(connection: Any, path: Path) -> None:
        rows = SqliteTrajectoryEvaluationQuery(connection).journal_entries()
        path.write_text(
            "".join(
                json.dumps(
                    {
                        "journal_entry_id": row["journal_entry_id"],
                        "workspace_revision": row["workspace_revision"],
                        "ordinal": row["ordinal"],
                        "event_type": row["event_type"],
                        "object_type": row["object_type"],
                        "object_id": row["object_id"],
                        "payload": json.loads(str(row["payload_json"])),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
                for row in rows
            ),
            encoding="utf-8",
            newline="\n",
        )

    @staticmethod
    def _validate_scenario(scenario: EvaluationScenario) -> None:
        if scenario.scenario_id != TurnLoopTrajectoryEvaluationAdapter.scenario_id:
            raise ProductEvaluationError("trajectory adapter received another scenario")
        if scenario.level is not EvaluationLevel.AGENT_LOOP:
            raise ProductEvaluationError("Turn trajectory scenario must be agent_loop level")

    def _verified_reference(self, scenario: EvaluationScenario) -> Path:
        references = [
            fixture for fixture in scenario.fixtures if fixture.role == "trajectory_reference"
        ]
        if len(references) != 1:
            raise ProductEvaluationError("trajectory scenario requires one reference fixture")
        fixture = references[0]
        path = (self._project_root / fixture.locator).resolve()
        if not path.is_relative_to(self._project_root) or not path.is_file():
            raise ProductEvaluationError("trajectory reference fixture is unavailable")
        if sha256(path.read_bytes()).hexdigest() != fixture.sha256:
            raise ProductEvaluationError("trajectory reference fixture digest mismatch")
        return path

    @staticmethod
    def _json(value: object) -> str:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )


class TurnLoopTrajectoryGrader:
    """Recompute causal Turn invariants from the immutable Workspace database."""

    grader_id = "trajectory.turn_loop_authority"
    revision = "turn-loop-authority/v1"
    kind = GraderKind.TRACE

    def grade(
        self,
        scenario: EvaluationScenario,
        spec: GraderSpec,
        bundle: TrialBundle,
    ) -> EvaluationScore:
        del scenario
        query = SqliteTrajectoryEvaluationQuery.open_readonly(bundle.workspace_database)
        try:
            facts = query.turn_loop_authority_facts()
            steps = facts["steps"]
            call = facts["tool_calls"][0] if len(facts["tool_calls"]) == 1 else None
            admission = facts["admissions"][0] if len(facts["admissions"]) == 1 else None
            operation = facts["operations"][0] if len(facts["operations"]) == 1 else None
            result = facts["tool_results"][0] if len(facts["tool_results"]) == 1 else None
            coverage = facts["goal_coverage"]
            stop = facts["stop_guard"]
            passed = bool(
                len(steps) == 2
                and all(row["status"] == "completed" for row in steps)
                and call is not None
                and call["proposal_status"] == "resolved"
                and admission is not None
                and operation is not None
                and admission["tool_call_id"] == call["tool_call_id"]
                and admission["operation_id"] == operation["operation_id"]
                and admission["admitted_revision"] == operation["created_revision"]
                and operation["status"] == "completed"
                and result is not None
                and operation["terminal_revision"] <= result["created_revision"]
                and result["tool_call_id"] == call["tool_call_id"]
                and result["result_kind"] == "success"
                and coverage is not None
                and coverage["required_total"] == coverage["required_satisfied"] == 1
                and coverage["coverage_status"] == "satisfied"
                and stop is not None
                and stop["decision"] == "terminate"
                and stop["terminal_disposition"] == "succeed"
                and result["created_revision"] <= stop["created_revision"]
                and facts["turn_status"] == "succeeded"
            )
            detail = {
                "step_count": len(steps),
                "call_status": call["proposal_status"] if call else None,
                "operation_status": operation["status"] if operation else None,
                "result_kind": result["result_kind"] if result else None,
                "coverage_status": coverage["coverage_status"] if coverage else None,
                "stop_decision": stop["decision"] if stop else None,
                "turn_status": facts["turn_status"],
            }
        finally:
            query.close()
        return EvaluationScore(
            self.grader_id,
            self.revision,
            self.kind,
            spec.role,
            ScoreVerdict.PASS if passed else ScoreVerdict.FAIL,
            passed,
            json.dumps(detail, sort_keys=True),
            (
                "workspace_database",
                "trace",
                "evaluation_payload:trajectory-report",
            ),
        )
