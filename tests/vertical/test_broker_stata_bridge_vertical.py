"""M2-06 bridge: one admitted Tool Operation is the Stata execution identity."""

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
    StartModelStepCommand,
)
from stata_research_agent.application.model_gateway_service import ModelGatewayService
from stata_research_agent.application.provider_credentials import ResolvedProviderCredential
from stata_research_agent.application.stata_operation import ExecuteStataCommand
from stata_research_agent.application.stata_operation_service import StataOperationService
from stata_research_agent.application.tool_broker import (
    AdmitToolCallCommand,
    CreateDispatchPlanCommand,
    RawToolProposal,
    RegisterToolContractCommand,
    ResourceClaimTemplate,
    ToolContractDefinition,
)
from stata_research_agent.application.tool_broker_service import ToolBrokerService
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
from stata_research_agent.persistence.workspace import WorkspaceDatabase
from stata_research_agent.runtime.uuid_identity import UuidIdentityGenerator


class Credential:
    def resolve_for_transport(
        self, credential_ref: str, *, provider_profile_id: str, endpoint: str
    ) -> ResolvedProviderCredential:
        del credential_ref
        return ResolvedProviderCredential(
            provider_profile_id, "credentialversion_test", endpoint, "test-secret"
        )


class OneAssistantOutput:
    async def send(self, *, endpoint: str, request_json: str, credential: str) -> ProviderResponse:
        del endpoint, request_json, credential
        return ProviderResponse({"text": "Run Stata describe", "tool_calls": []})


class SuccessfulStataRuntime:
    async def execute(self, **kwargs):
        del kwargs
        return StataRuntimeResult(
            envelope_schema_version="stata-mcp.envelope/v1",
            text="74 observations",
            structured={"N": 74.0},
            receipt=StataExecutionReceipt(
                schema_version="stata.execution-receipt/v1alpha1",
                executor_instance_id="executor-bridge",
                session_id="scope-main",
                session_generation=1,
                exec_seq=1,
                execution_status=StataExecutionStatus.SUCCEEDED,
                rc=0,
                raw_output_status="complete",
                structured_result_status="complete",
                command_hash="bridge-command",
                data_signature="74:12:bridge",
                session_reset=False,
                runtime_environment={"stata_version": "18"},
                supervision_proof={"supervised": True},
            ),
            is_error=False,
        )

    async def close_session(self, *, session_id: str, reason: str):
        del reason
        return StataSessionCloseResult(
            "stata.session-control/v1alpha1",
            "executor-bridge",
            session_id,
            True,
            {},
        )


def test_admitted_stata_operation_is_handed_off_finalized_and_resolved(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path / "workspace"
    database = WorkspaceDatabase(workspace_root, WorkspaceId("ws_broker_stata"))
    database.create()
    connection = database.open(writable=True)
    identities = UuidIdentityGenerator()
    control = WorkspaceControlService(SqliteControlStore(connection), identities)
    control.create_workspace(
        CreateWorkspaceCommand(CommandId("cmd_bridge_workspace"), WorkspaceId("ws_broker_stata"))
    )
    turn = control.submit_message(
        SubmitMessageCommand(CommandId("cmd_bridge_turn"), "Inspect the dataset in Stata")
    )
    evaluator = RuntimeEvaluationService(SqliteEvaluationRepository(connection), identities)
    evaluator.normalize_contract(
        NormalizeCompletionContractCommand(
            CommandId("cmd_bridge_contract"),
            turn.turn_id,
            1,
            "Inspect the data with a traceable Stata command.",
            (
                ContractObligationCandidate(
                    "inspect.data",
                    "Inspect data",
                    ObligationProvenance.USER_EXPLICIT,
                    "message",
                    "bridge-request",
                    RequirementLevel.REQUIRED,
                    "A Stata execution receipt exists.",
                ),
            ),
        )
    )
    gateway = ModelGatewayService(
        SqliteModelGatewayRepository(connection),
        identities,
        Credential(),
        OneAssistantOutput(),
    )
    step = asyncio.run(
        gateway.execute_step(
            StartModelStepCommand(
                CommandId("cmd_bridge_step"),
                turn.turn_id,
                2,
                "system-v1",
                "Use registered tools.",
                "research-main",
                "skill-v1",
                "Inspect, then report.",
                "catalog-v1",
                (),
                (
                    ContextItemCandidate(
                        "message",
                        "message",
                        "bridge-request",
                        "1",
                        "remote_allowed",
                        "Inspect the dataset in Stata",
                    ),
                ),
                (),
                "permission-v1",
                {"stata_execute": True},
                "model-policy-v1",
                "test",
                "test",
                "test-model",
                "https://provider.invalid/responses",
                "credential://test",
                {},
            )
        )
    )
    assert step.assistant_output_id is not None
    broker = ToolBrokerService(SqliteToolBrokerRepository(connection), identities)
    broker.register_contract(
        RegisterToolContractCommand(
            CommandId("cmd_bridge_register_tool"),
            turn.turn_id,
            ToolContractDefinition(
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
            ),
        )
    )
    dependencies = {"resource_identities": {"stata-session:scope-main": "scope-main"}}
    plan = broker.create_dispatch_plan(
        CreateDispatchPlanCommand(
            CommandId("cmd_bridge_plan"),
            step.assistant_output_id,
            "catalog-v1",
            dependencies,
            (RawToolProposal("stata.run", '{"code":"describe","session_id":"scope-main"}'),),
        )
    )
    admitted = broker.admit(
        AdmitToolCallCommand(
            CommandId("cmd_bridge_admit"),
            plan.scheduled_call_ids[0],
            2,
            "admission-v1",
            ("workspace_write",),
            dependencies,
        )
    )
    service = StataOperationService(
        SqliteStataOperationRepository(connection),
        SuccessfulStataRuntime(),
        identities,
        FilesystemCompletionManifestStore(workspace_root),
        FilesystemManagedArtifactStore(workspace_root),
    )
    outcome = asyncio.run(
        service.execute(
            ExecuteStataCommand(
                CommandId("cmd_bridge_execute"),
                turn.turn_id,
                "scope-main",
                "describe",
                tool_call_id=plan.scheduled_call_ids[0],
                admitted_operation_id=admitted.operation_id,
            )
        )
    )
    try:
        assert outcome.operation_id == admitted.operation_id
        assert outcome.status == "completed"
        assert connection.execute("SELECT count(*) FROM operations").fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT proposal_status FROM tool_calls WHERE tool_call_id = ?",
                (plan.scheduled_call_ids[0].value,),
            ).fetchone()[0]
            == "resolved"
        )
        result = connection.execute(
            "SELECT result_kind, structured_payload_json FROM canonical_tool_results"
        ).fetchone()
        assert result[0] == "success"
        assert outcome.manifest_id.value in result[1]
        assert (
            connection.execute(
                "SELECT count(*) FROM tool_resource_leases WHERE lease_status = 'active'"
            ).fetchone()[0]
            == 0
        )
    finally:
        connection.close()
