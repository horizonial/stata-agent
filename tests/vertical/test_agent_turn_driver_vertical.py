"""M2-06 autonomous Agent loop over real Worker, Broker, Stata bridge, and Stop Guard."""

from __future__ import annotations

import asyncio
from pathlib import Path

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
    ToolExecutionRequest,
    ToolExecutionResult,
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
from stata_research_agent.domain.status import StopGuardDecision, WaitReason
from stata_research_agent.persistence.control_store import SqliteControlStore
from stata_research_agent.persistence.evaluation_store import SqliteEvaluationRepository
from stata_research_agent.persistence.model_gateway_store import SqliteModelGatewayRepository
from stata_research_agent.persistence.stata_operation_store import (
    SqliteStataOperationRepository,
)
from stata_research_agent.persistence.tool_broker_store import SqliteToolBrokerRepository
from stata_research_agent.persistence.workspace import WorkspaceDatabase
from stata_research_agent.runtime.agent_turn_driver import AgentTurnDriver
from stata_research_agent.runtime.model_evaluation_coordinator import (
    ModelEvaluationCoordinator,
)
from stata_research_agent.runtime.uuid_identity import UuidIdentityGenerator

PROJECT_ROOT = Path(__file__).parents[2]
PYTHON = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"


class Credential:
    def resolve_for_transport(
        self, credential_ref: str, *, provider_profile_id: str, endpoint: str
    ) -> ResolvedProviderCredential:
        del credential_ref
        return ResolvedProviderCredential(
            provider_profile_id, "credentialversion_test", endpoint, "driver-test-secret"
        )


class TwoStepResearchModel:
    def __init__(self) -> None:
        self.calls = 0

    async def send(self, *, endpoint: str, request_json: str, credential: str) -> ProviderResponse:
        del endpoint, credential
        self.calls += 1
        if self.calls == 1:
            assert "Inspect auto data" in request_json
            return ProviderResponse(
                {
                    "text": "I will inspect the data with Stata.",
                    "plan": {
                        "summary": "Inspect then finish",
                        "structured_plan": {"nodes": ["inspect", "finish"]},
                    },
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
        assert "completion_manifest_id" in request_json
        return ProviderResponse(
            {
                "text": "The requested traceable Stata inspection is complete.",
                "tool_calls": [],
                "completion": {
                    "disposition": "succeed",
                    "summary": "The required Stata inspection completed.",
                },
            }
        )


class AmbiguousCompletionModel:
    async def send(self, *, endpoint: str, request_json: str, credential: str) -> ProviderResponse:
        del endpoint, request_json, credential
        return ProviderResponse(
            {
                "text": "Two variable meanings remain defensible.",
                "tool_calls": [],
                "evaluation": {
                    "verdict": "unknown",
                    "findings": ["research_semantic_ambiguity"],
                },
                "completion": {
                    "disposition": "succeed",
                    "summary": "The user must choose the variable meaning.",
                },
            }
        )


class PartialCompletionModel:
    async def send(self, *, endpoint: str, request_json: str, credential: str) -> ProviderResponse:
        del endpoint, request_json, credential
        return ProviderResponse(
            {
                "text": "I cannot complete the required empirical check in this Turn.",
                "tool_calls": [],
                "completion": {
                    "disposition": "partial",
                    "summary": "The required check remains explicitly incomplete.",
                },
            }
        )


class ImmediateCompletionModel:
    def __init__(self) -> None:
        self.calls = 0

    async def send(self, *, endpoint: str, request_json: str, credential: str) -> ProviderResponse:
        del endpoint, request_json, credential
        self.calls += 1
        return ProviderResponse(
            {
                "text": "The requested work is complete.",
                "tool_calls": [],
                "completion": {
                    "disposition": "succeed",
                    "summary": "All deterministic completion requirements are satisfied.",
                },
            }
        )


class AdmissionBlockedThenPartialModel:
    def __init__(self) -> None:
        self.calls = 0

    async def send(self, *, endpoint: str, request_json: str, credential: str) -> ProviderResponse:
        del endpoint, credential
        self.calls += 1
        if self.calls == 1:
            return ProviderResponse(
                {
                    "text": "I will inspect the data with Stata.",
                    "tool_calls": [
                        {
                            "name": "stata.run",
                            "arguments": {"session_id": "scope-main", "code": "describe"},
                        }
                    ],
                }
            )
        assert "effect_class_denied" in request_json
        return ProviderResponse(
            {
                "text": "The proposed Stata call was not authorized in this Turn.",
                "tool_calls": [],
                "completion": {
                    "disposition": "partial",
                    "summary": "No Stata execution was authorized.",
                },
            }
        )


class MutableClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class DeadlineCrossingModel:
    def __init__(self, clock: MutableClock) -> None:
        self.clock = clock

    async def send(self, *, endpoint: str, request_json: str, credential: str) -> ProviderResponse:
        del endpoint, request_json, credential
        self.clock.now = 11.0
        return ProviderResponse(
            {
                "text": "I will run Stata now.",
                "tool_calls": [
                    {
                        "name": "stata.run",
                        "arguments": {"session_id": "scope-main", "code": "describe"},
                    }
                ],
            }
        )


class MustNotExecute:
    async def execute(self, request: ToolExecutionRequest) -> ToolExecutionResult:
        del request
        raise AssertionError("supervised ambiguity path must not execute a tool")


class SuccessfulRuntime:
    async def execute(self, **kwargs):
        del kwargs
        return StataRuntimeResult(
            envelope_schema_version="stata-mcp.envelope/v1",
            text="Contains data from auto.dta; 74 observations",
            structured={"N": 74.0, "variables": 12.0},
            receipt=StataExecutionReceipt(
                schema_version="stata.execution-receipt/v1alpha1",
                executor_instance_id="driver-executor",
                session_id="scope-main",
                session_generation=1,
                exec_seq=1,
                execution_status=StataExecutionStatus.SUCCEEDED,
                rc=0,
                raw_output_status="complete",
                structured_result_status="complete",
                command_hash="driver-command",
                data_signature="74:12:driver",
                session_reset=False,
                runtime_environment={"stata_version": "18"},
                supervision_proof={"supervised": True},
            ),
            is_error=False,
        )

    async def close_session(self, *, session_id: str, reason: str):
        del reason
        return StataSessionCloseResult(
            "stata.session-control/v1alpha1", "driver-executor", session_id, True, {}
        )


def stata_contract() -> ToolContractDefinition:
    return ToolContractDefinition(
        "stata.run",
        "1.0.0",
        "Run Stata",
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


def test_autonomous_two_step_turn_reaches_stop_guard_success(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_id = WorkspaceId("ws_agent_driver")
    database = WorkspaceDatabase(workspace_root, workspace_id)
    database.create()
    connection = database.open(writable=True)
    identities = UuidIdentityGenerator()
    control = WorkspaceControlService(SqliteControlStore(connection), identities)
    initialized = control.create_workspace(
        CreateWorkspaceCommand(CommandId("cmd_driver_workspace"), workspace_id)
    )
    turn = control.submit_message(
        SubmitMessageCommand(CommandId("cmd_driver_turn"), "Inspect auto data")
    )
    evaluator = RuntimeEvaluationService(SqliteEvaluationRepository(connection), identities)
    normalized = evaluator.normalize_contract(
        NormalizeCompletionContractCommand(
            CommandId("cmd_driver_normalize"),
            turn.turn_id,
            1,
            "Inspect auto data using a traceable Stata operation.",
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
            CommandId("cmd_driver_register_stata"), turn.turn_id, stata_contract()
        )
    )
    gateway = ModelGatewayService(
        SqliteModelGatewayRepository(connection),
        identities,
        Credential(),
        TwoStepResearchModel(),
    )
    stata_service = StataOperationService(
        SqliteStataOperationRepository(connection),
        SuccessfulRuntime(),
        identities,
        FilesystemCompletionManifestStore(workspace_root),
        FilesystemManagedArtifactStore(workspace_root),
    )
    driver = AgentTurnDriver(
        gateway,
        broker,
        evaluator,
        StataToolExecutor(stata_service, identities),
        identities,
        PYTHON,
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
                    "Use tools, inspect their results, then propose completion.",
                    "catalog-v1",
                    (
                        {
                            "name": "stata.run",
                            "input_schema": stata_contract().input_schema,
                        },
                    ),
                    "permission-v1",
                    {"stata_execute": True},
                    "model-policy-v1",
                    "test-provider",
                    "test",
                    "test-model",
                    "https://provider.invalid/responses",
                    "credential://test",
                    {},
                ),
                (
                    ContextItemCandidate(
                        "user_message",
                        "message",
                        turn.message_id.value,
                        "1",
                        "remote_allowed",
                        "Inspect auto data",
                    ),
                ),
                {"resource_identities": {"stata-session:scope-main": "scope-main"}},
                ("workspace_write",),
                {"stata.run": normalized.obligation_ids},
                4,
            )
        )
    )
    try:
        assert outcome.status == "succeeded"
        assert outcome.executed_steps == 2
        assert outcome.tool_executions == 1
        assert outcome.stop_guard is not None
        assert outcome.stop_guard.decision is StopGuardDecision.TERMINATE
        assert connection.execute("SELECT count(*) FROM operations").fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT count(*) FROM canonical_tool_results WHERE result_kind = 'success'"
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute("SELECT coverage_status FROM goal_coverages").fetchone()[0]
            == "satisfied"
        )
        budgets = connection.execute(
            """
            SELECT remaining_step_budget, remaining_tool_budget
            FROM context_manifests
            JOIN steps USING (step_id)
            ORDER BY step_ordinal
            """
        ).fetchall()
        assert [tuple(row) for row in budgets] == [(3, 128), (2, 127)]
    finally:
        connection.close()


def test_turn_deadline_blocks_new_tool_admission_after_model_returns(tmp_path: Path) -> None:
    workspace_id = WorkspaceId("ws_agent_deadline")
    database = WorkspaceDatabase(tmp_path / "deadline", workspace_id)
    database.create()
    connection = database.open(writable=True)
    identities = UuidIdentityGenerator()
    control = WorkspaceControlService(SqliteControlStore(connection), identities)
    initialized = control.create_workspace(
        CreateWorkspaceCommand(CommandId("cmd_deadline_workspace"), workspace_id)
    )
    turn = control.submit_message(
        SubmitMessageCommand(CommandId("cmd_deadline_turn"), "Inspect auto data")
    )
    clock = MutableClock()
    driver = AgentTurnDriver(
        ModelGatewayService(
            SqliteModelGatewayRepository(connection),
            identities,
            Credential(),
            DeadlineCrossingModel(clock),
        ),
        ToolBrokerService(SqliteToolBrokerRepository(connection), identities),
        RuntimeEvaluationService(SqliteEvaluationRepository(connection), identities),
        MustNotExecute(),
        identities,
        PYTHON,
        monotonic_clock=clock,
    )
    outcome = asyncio.run(
        driver.run(
            TurnDriverConfig(
                turn.turn_id,
                1,
                workspace_id.value,
                initialized.main_scope_id.value,
                initialized.main_path_id.value,
                TurnDriverModelConfig(
                    "system-v1",
                    "You are a traceable Stata research agent.",
                    "research-main",
                    "skill-v1",
                    "Use tools only while runtime budget remains.",
                    "catalog-v1",
                    (),
                    "permission-v1",
                    {},
                    "model-policy-v1",
                    "test-provider",
                    "test",
                    "test-model",
                    "https://provider.invalid/responses",
                    "credential://test",
                    {},
                ),
                (),
                {"resource_identities": {}},
                (),
                {},
                max_wall_clock_seconds=10.0,
            )
        )
    )
    try:
        assert outcome.status == "runtime_deadline_exceeded"
        assert outcome.executed_steps == 1
        assert outcome.tool_executions == 0
        assert connection.execute("SELECT count(*) FROM operations").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM tool_dispatch_plans").fetchone()[0] == 0
    finally:
        connection.close()


def test_admission_block_is_durable_and_reaches_next_model_step(tmp_path: Path) -> None:
    workspace_id = WorkspaceId("ws_agent_admission_block")
    database = WorkspaceDatabase(tmp_path / "admission-block", workspace_id)
    database.create()
    connection = database.open(writable=True)
    identities = UuidIdentityGenerator()
    control = WorkspaceControlService(SqliteControlStore(connection), identities)
    initialized = control.create_workspace(
        CreateWorkspaceCommand(CommandId("cmd_block_workspace"), workspace_id)
    )
    turn = control.submit_message(
        SubmitMessageCommand(CommandId("cmd_block_turn"), "Inspect auto data")
    )
    broker = ToolBrokerService(SqliteToolBrokerRepository(connection), identities)
    broker.register_contract(
        RegisterToolContractCommand(
            CommandId("cmd_block_register_stata"), turn.turn_id, stata_contract()
        )
    )
    driver = AgentTurnDriver(
        ModelGatewayService(
            SqliteModelGatewayRepository(connection),
            identities,
            Credential(),
            AdmissionBlockedThenPartialModel(),
        ),
        broker,
        RuntimeEvaluationService(SqliteEvaluationRepository(connection), identities),
        MustNotExecute(),
        identities,
        PYTHON,
    )
    outcome = asyncio.run(
        driver.run(
            TurnDriverConfig(
                turn.turn_id,
                1,
                workspace_id.value,
                initialized.main_scope_id.value,
                initialized.main_path_id.value,
                TurnDriverModelConfig(
                    "system-v1",
                    "You are a traceable Stata research agent.",
                    "research-main",
                    "skill-v1",
                    "Use only authorized tools.",
                    "catalog-v1",
                    (
                        {
                            "name": "stata.run",
                            "input_schema": stata_contract().input_schema,
                        },
                    ),
                    "permission-v1",
                    {},
                    "model-policy-v1",
                    "test-provider",
                    "test",
                    "test-model",
                    "https://provider.invalid/responses",
                    "credential://test",
                    {},
                ),
                (),
                {"resource_identities": {"stata-session:scope-main": "scope-main"}},
                (),
                {},
                3,
            )
        )
    )
    try:
        assert outcome.status == "budget_exhausted"
        assert outcome.executed_steps >= 2
        assert outcome.tool_executions == 0
        assert connection.execute("SELECT count(*) FROM operations").fetchone()[0] == 0
        blocked = connection.execute(
            """
            SELECT proposal_status, reason_code
            FROM tool_call_status_history
            ORDER BY status_ordinal DESC LIMIT 1
            """
        ).fetchone()
        assert tuple(blocked) == ("scheduled", "effect_class_denied")
        journal = connection.execute(
            """
            SELECT event_type, json_extract(payload_json, '$.reason_code')
            FROM journal_entries
            WHERE event_type = 'tool.admission_blocked'
            """
        ).fetchone()
        assert tuple(journal) == ("tool.admission_blocked", "effect_class_denied")
    finally:
        connection.close()


def test_supervised_turn_waits_when_evaluation_requires_researcher_decision(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path / "supervised"
    workspace_id = WorkspaceId("ws_agent_supervised")
    database = WorkspaceDatabase(workspace_root, workspace_id)
    database.create()
    connection = database.open(writable=True)
    identities = UuidIdentityGenerator()
    control = WorkspaceControlService(SqliteControlStore(connection), identities)
    initialized = control.create_workspace(
        CreateWorkspaceCommand(CommandId("cmd_supervised_workspace"), workspace_id)
    )
    turn = control.submit_message(
        SubmitMessageCommand(CommandId("cmd_supervised_turn"), "Choose a variable meaning")
    )
    evaluator = RuntimeEvaluationService(SqliteEvaluationRepository(connection), identities)
    evaluator.normalize_contract(
        NormalizeCompletionContractCommand(
            CommandId("cmd_supervised_normalize"),
            turn.turn_id,
            1,
            "Resolve the variable meaning with the researcher.",
            (
                ContractObligationCandidate(
                    "consider.variable.meaning",
                    "Consider variable meaning",
                    ObligationProvenance.AGENT_NORMALIZATION,
                    "message",
                    turn.message_id.value,
                    RequirementLevel.OPTIONAL,
                    "The ambiguity is surfaced to the researcher.",
                ),
            ),
        )
    )
    driver = AgentTurnDriver(
        ModelGatewayService(
            SqliteModelGatewayRepository(connection),
            identities,
            Credential(),
            AmbiguousCompletionModel(),
        ),
        ToolBrokerService(SqliteToolBrokerRepository(connection), identities),
        evaluator,
        MustNotExecute(),
        identities,
        PYTHON,
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
                    "Escalate unresolved research semantics.",
                    "catalog-v1",
                    (),
                    "permission-v1",
                    {},
                    "model-policy-v1",
                    "test-provider",
                    "test",
                    "test-model",
                    "https://provider.invalid/responses",
                    "credential://test",
                    {},
                ),
                (
                    ContextItemCandidate(
                        "user_message",
                        "message",
                        turn.message_id.value,
                        "1",
                        "remote_allowed",
                        "Choose a variable meaning",
                    ),
                ),
                {"resource_identities": {}},
                (),
                {},
                2,
            )
        )
    )
    try:
        assert outcome.status == "waiting"
        assert outcome.stop_guard is not None
        assert outcome.stop_guard.decision is StopGuardDecision.WAIT
        assert outcome.stop_guard.wait_reason is WaitReason.USER_CONFIRMATION
        assert (
            connection.execute(
                "SELECT active_write_turn_id FROM workspace_write_lane WHERE singleton_id = 1"
            ).fetchone()[0]
            == turn.turn_id.value
        )
        assert connection.execute("SELECT status FROM waiting_requests").fetchone()[0] == "open"
    finally:
        connection.close()


def test_worker_partial_completion_is_preserved_by_stop_guard(tmp_path: Path) -> None:
    workspace_id = WorkspaceId("ws_agent_partial")
    database = WorkspaceDatabase(tmp_path / "partial", workspace_id)
    database.create()
    connection = database.open(writable=True)
    identities = UuidIdentityGenerator()
    control = WorkspaceControlService(SqliteControlStore(connection), identities)
    initialized = control.create_workspace(
        CreateWorkspaceCommand(CommandId("cmd_partial_workspace"), workspace_id)
    )
    turn = control.submit_message(
        SubmitMessageCommand(CommandId("cmd_partial_turn"), "Try the required check")
    )
    evaluator = RuntimeEvaluationService(SqliteEvaluationRepository(connection), identities)
    evaluator.normalize_contract(
        NormalizeCompletionContractCommand(
            CommandId("cmd_partial_normalize"),
            turn.turn_id,
            1,
            "Attempt one required empirical check.",
            (
                ContractObligationCandidate(
                    "check.required",
                    "Complete the required check",
                    ObligationProvenance.USER_EXPLICIT,
                    "message",
                    turn.message_id.value,
                    RequirementLevel.REQUIRED,
                    "A formal check exists.",
                ),
            ),
        )
    )
    driver = AgentTurnDriver(
        ModelGatewayService(
            SqliteModelGatewayRepository(connection),
            identities,
            Credential(),
            PartialCompletionModel(),
        ),
        ToolBrokerService(SqliteToolBrokerRepository(connection), identities),
        evaluator,
        MustNotExecute(),
        identities,
        PYTHON,
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
                    "You are a traceable research agent.",
                    "research-main",
                    "skill-v1",
                    "Report partial completion honestly.",
                    "catalog-v1",
                    (),
                    "permission-v1",
                    {},
                    "model-policy-v1",
                    "test-provider",
                    "test",
                    "test-model",
                    "https://provider.invalid/responses",
                    "credential://test",
                    {},
                ),
                (),
                {"resource_identities": {}},
                (),
                {},
                2,
            )
        )
    )
    try:
        assert outcome.status == "partial"
        assert outcome.stop_guard is not None
        assert outcome.stop_guard.terminal_disposition.value == "partial"
        assert connection.execute(
            "SELECT status FROM turns WHERE turn_id = ?", (turn.turn_id.value,)
        ).fetchone()[0] == "partial"
        assert connection.execute(
            "SELECT active_write_turn_id FROM workspace_write_lane WHERE singleton_id = 1"
        ).fetchone()[0] is None
    finally:
        connection.close()


def test_completion_on_final_step_reaches_stop_guard_without_extra_evaluator_step(
    tmp_path: Path,
) -> None:
    workspace_id = WorkspaceId("ws_agent_final_step_completion")
    database = WorkspaceDatabase(tmp_path / "final-step", workspace_id)
    database.create()
    connection = database.open(writable=True)
    identities = UuidIdentityGenerator()
    control = WorkspaceControlService(SqliteControlStore(connection), identities)
    initialized = control.create_workspace(
        CreateWorkspaceCommand(CommandId("cmd_final_step_workspace"), workspace_id)
    )
    turn = control.submit_message(
        SubmitMessageCommand(CommandId("cmd_final_step_turn"), "Complete immediately")
    )
    evaluator = RuntimeEvaluationService(SqliteEvaluationRepository(connection), identities)
    evaluator.normalize_contract(
        NormalizeCompletionContractCommand(
            CommandId("cmd_final_step_contract"),
            turn.turn_id,
            1,
            "Complete immediately.",
            (),
        )
    )
    model = ImmediateCompletionModel()
    gateway = ModelGatewayService(
        SqliteModelGatewayRepository(connection),
        identities,
        Credential(),
        model,
    )
    driver = AgentTurnDriver(
        gateway,
        ToolBrokerService(SqliteToolBrokerRepository(connection), identities),
        evaluator,
        MustNotExecute(),
        identities,
        PYTHON,
        model_evaluation=ModelEvaluationCoordinator(gateway),
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
                    "You are a traceable research agent.",
                    "research-main",
                    "skill-v1",
                    "Complete the request.",
                    "catalog-v1",
                    (),
                    "permission-v1",
                    {},
                    "model-policy-v1",
                    "test-provider",
                    "test",
                    "test-model",
                    "https://provider.invalid/responses",
                    "credential://test",
                    {},
                ),
                (),
                {"resource_identities": {}},
                (),
                {},
                1,
            )
        )
    )
    try:
        assert outcome.status == "succeeded"
        assert outcome.executed_steps == 1
        assert outcome.stop_guard is not None
        assert outcome.stop_guard.decision is StopGuardDecision.TERMINATE
        assert model.calls == 1
    finally:
        connection.close()
