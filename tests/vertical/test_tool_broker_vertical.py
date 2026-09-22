"""M2-03 vertical tests for open contracts, plans, resources, and JIT Admission."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from stata_research_agent.application.broker_execution import (
    BeginBrokerExecutionCommand,
    CompleteBrokerExecutionCommand,
)
from stata_research_agent.application.broker_execution_service import BrokerExecutionService
from stata_research_agent.application.control import CreateWorkspaceCommand, SubmitMessageCommand
from stata_research_agent.application.default_tool_contracts import python_run_contract
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
from stata_research_agent.application.produced_artifact_service import (
    ProducedArtifactService,
)
from stata_research_agent.application.provider_credentials import ResolvedProviderCredential
from stata_research_agent.application.sandbox_execution import (
    SandboxExecutionReceipt,
    SandboxExecutionRequest,
    SandboxOutputCandidate,
)
from stata_research_agent.application.tool_broker import (
    AdmitToolCallCommand,
    CreateDispatchPlanCommand,
    RawToolProposal,
    RecordExecutorExceptionCommand,
    RegisterToolContractCommand,
    ResourceClaimTemplate,
    ToolContractDefinition,
)
from stata_research_agent.application.tool_broker_service import ToolBrokerService
from stata_research_agent.application.turn_driver import ToolExecutionRequest
from stata_research_agent.application.turn_interaction import (
    AnswerWaitingCommand,
    ContinuePausedTurnCommand,
    ConvergePauseCommand,
    OpenWaitingCommand,
    RequestPauseCommand,
)
from stata_research_agent.application.turn_interaction_service import TurnInteractionService
from stata_research_agent.application.workspace_service import WorkspaceControlService
from stata_research_agent.artifacts.managed_store import FilesystemManagedArtifactStore
from stata_research_agent.domain.identifiers import CommandId, WorkspaceId
from stata_research_agent.domain.status import WaitReason
from stata_research_agent.interfaces.windows_sandbox_executor import WindowsSandboxExecutor
from stata_research_agent.persistence.broker_execution_store import (
    SqliteBrokerExecutionRepository,
)
from stata_research_agent.persistence.control_store import SqliteControlStore
from stata_research_agent.persistence.evaluation_store import SqliteEvaluationRepository
from stata_research_agent.persistence.model_gateway_store import SqliteModelGatewayRepository
from stata_research_agent.persistence.produced_artifact_store import (
    SqliteProducedArtifactRepository,
)
from stata_research_agent.persistence.tool_broker_store import SqliteToolBrokerRepository
from stata_research_agent.persistence.turn_interaction_store import (
    SqliteTurnInteractionRepository,
)
from stata_research_agent.persistence.workspace import WorkspaceDatabase
from stata_research_agent.runtime.sandbox_tool_executor import SandboxToolExecutor
from stata_research_agent.runtime.uuid_identity import UuidIdentityGenerator


class RecordingSandbox:
    def __init__(self, execution_root: Path) -> None:
        self._execution_root = execution_root

    def execute(self, request: SandboxExecutionRequest) -> SandboxExecutionReceipt:
        scratch = self._execution_root / ".stata-agent" / "staging" / request.attempt_id / "scratch"
        outputs = scratch / "outputs"
        outputs.mkdir(parents=True)
        program = scratch / ("program.py" if request.language == "python" else "program.ps1")
        program.write_text(request.code, encoding="utf-8")
        report = outputs / "report.json"
        report.write_text('{"statistic":2.5}', encoding="utf-8")
        import hashlib

        digest = hashlib.sha256(report.read_bytes()).hexdigest()
        return SandboxExecutionReceipt(
            "stata-agent.sandbox-receipt/v1",
            request.attempt_id,
            "base-container",
            "a" * 64,
            request.network_mode,
            (),
            (str(scratch),),
            False,
            "passed",
            "passed",
            0,
            "sandbox-ok",
            "",
            program,
            (SandboxOutputCandidate("report.json", report, report.stat().st_size, digest),),
        )


class Credential:
    def resolve_for_transport(
        self, credential_ref: str, *, provider_profile_id: str, endpoint: str
    ) -> ResolvedProviderCredential:
        del credential_ref
        return ResolvedProviderCredential(
            provider_profile_id,
            "credentialversion_test",
            endpoint,
            "transport-only-secret",
        )


class OneOutput:
    async def send(self, *, endpoint: str, request_json: str, credential: str) -> ProviderResponse:
        del endpoint, request_json, credential
        return ProviderResponse({"text": "inspect", "tool_calls": []})


def initialized_output(tmp_path: Path, *, tool_budget: int = 128, normalize: bool = False):
    database = WorkspaceDatabase(tmp_path / "workspace", WorkspaceId("ws_tool_broker"))
    database.create()
    connection = database.open(writable=True)
    identities = UuidIdentityGenerator()
    control = WorkspaceControlService(SqliteControlStore(connection), identities)
    control.create_workspace(
        CreateWorkspaceCommand(CommandId("cmd_tool_workspace"), WorkspaceId("ws_tool_broker"))
    )
    turn = control.submit_message(
        SubmitMessageCommand(CommandId("cmd_tool_turn"), "Inspect two artifacts")
    )
    turn_revision = 1
    if normalize:
        RuntimeEvaluationService(
            SqliteEvaluationRepository(connection), identities
        ).normalize_contract(
            NormalizeCompletionContractCommand(
                CommandId("cmd_tool_contract_normalize"),
                turn.turn_id,
                1,
                "Run the admitted tool and preserve its execution receipt.",
                (
                    ContractObligationCandidate(
                        "tool.execute",
                        "Execute tool",
                        ObligationProvenance.USER_EXPLICIT,
                        "message",
                        "tool-request",
                        RequirementLevel.REQUIRED,
                        "A durable Tool execution exists.",
                    ),
                ),
            )
        )
        turn_revision = 2
    output_id = new_output(
        connection,
        identities,
        turn,
        "cmd_tool_model_step",
        tool_budget=tool_budget,
        turn_revision=turn_revision,
    )
    return connection, identities, turn, output_id


def new_output(
    connection,
    identities,
    turn,
    command_id: str,
    *,
    tool_budget: int = 128,
    turn_revision: int = 1,
):
    output = asyncio.run(
        ModelGatewayService(
            SqliteModelGatewayRepository(connection), identities, Credential(), OneOutput()
        ).execute_step(
            StartModelStepCommand(
                CommandId(command_id),
                turn.turn_id,
                turn_revision,
                "system-v1",
                "Trace every tool.",
                "research-main",
                "skill-v1",
                "Inspect before acting.",
                "catalog-v1",
                (),
                (
                    ContextItemCandidate(
                        "message", "message", "msg_tool", "1", "remote_allowed", "inspect"
                    ),
                ),
                (),
                "permission-v1",
                {"artifact_read": True},
                "model-policy-v1",
                "fake",
                "test",
                "fake-model",
                "https://provider.example/v1/responses",
                "credential://fake",
                {},
                remaining_tool_budget=tool_budget,
            )
        )
    )
    assert output.assistant_output_id is not None
    return output.assistant_output_id


def read_contract(name: str = "community.inspect") -> ToolContractDefinition:
    return ToolContractDefinition(
        name,
        "1.0.0",
        "Community artifact inspection",
        "artifact.verify",
        {
            "type": "object",
            "properties": {
                "artifact_id": {"type": "string"},
                "detail": {"type": "string", "default": "summary"},
            },
            "required": ["artifact_id"],
            "additionalProperties": False,
        },
        {"type": "object"},
        "local_runtime",
        "pure_read",
        "parallel_safe",
        "replay_safe",
        "never",
        "not_interruptible",
        10.0,
        30.0,
        100_000,
        (ResourceClaimTemplate("artifact:{artifact_id}", "read", "artifact_id"),),
    )


def test_open_contract_calls_plan_in_parallel_reject_unknown_and_admit_jit(
    tmp_path: Path,
) -> None:
    connection, identities, turn, assistant_output_id = initialized_output(tmp_path)
    broker = ToolBrokerService(SqliteToolBrokerRepository(connection), identities)
    try:
        registered = broker.register_contract(
            RegisterToolContractCommand(
                CommandId("cmd_register_open_tool"), turn.turn_id, read_contract()
            )
        )
        assert registered.definition.tool_name == "community.inspect"
        dependencies = {
            "resource_identities": {
                "artifact:artifact_one": "artifact_one",
                "artifact:artifact_two": "artifact_two",
            }
        }
        plan = broker.create_dispatch_plan(
            CreateDispatchPlanCommand(
                CommandId("cmd_open_dispatch_plan"),
                assistant_output_id,
                "catalog-v1",
                dependencies,
                (
                    RawToolProposal("community.inspect", '{"artifact_id":"artifact_one"}', "p1"),
                    RawToolProposal("community.inspect", '{"artifact_id":"artifact_two"}', "p2"),
                    RawToolProposal("another.not-installed", "{}", "p3"),
                ),
            )
        )
        assert len(plan.scheduled_call_ids) == 2
        assert len(plan.rejected_call_ids) == 1
        assert plan.batch_count == 1
        assert [call.execution_batch_ordinal for call in plan.scheduled_calls] == [1, 1]
        assert [call.call_ordinal for call in plan.scheduled_calls] == [1, 2]
        batches = connection.execute(
            "SELECT execution_batch_ordinal FROM tool_dispatch_plan_entries ORDER BY call_ordinal"
        ).fetchall()
        assert [row[0] for row in batches] == [1, 1]
        assert (
            connection.execute("SELECT result_kind FROM canonical_tool_results").fetchone()[0]
            == "rejected"
        )

        admitted = broker.admit(
            AdmitToolCallCommand(
                CommandId("cmd_admit_open_call"),
                plan.scheduled_call_ids[0],
                1,
                "admission-v1",
                ("pure_read",),
                dependencies,
            )
        )
        assert admitted.status == "admitted"
        operation = connection.execute(
            "SELECT status, tool_call_id FROM operations WHERE operation_id = ?",
            (admitted.operation_id.value,),
        ).fetchone()
        assert tuple(operation) == ("admitted", plan.scheduled_call_ids[0].value)
        assert (
            connection.execute(
                "SELECT count(*) FROM tool_resource_leases WHERE lease_status = 'active'"
            ).fetchone()[0]
            == 1
        )
    finally:
        connection.close()


def test_no_progress_fingerprint_is_scoped_to_the_dependency_snapshot(
    tmp_path: Path,
) -> None:
    connection, identities, turn, assistant_output_id = initialized_output(tmp_path)
    broker = ToolBrokerService(SqliteToolBrokerRepository(connection), identities)
    bridge = BrokerExecutionService(SqliteBrokerExecutionRepository(connection), identities)
    dependency_v1 = {
        "resource_identities": {
            "artifact:artifact_one": "artifact_one",
            "result:current": "revision-one",
        }
    }
    dependency_v2 = {
        "resource_identities": {
            "artifact:artifact_one": "artifact_one",
            "result:current": "revision-two",
        }
    }

    def plan_for(output_id, dependencies, suffix: str):
        return broker.create_dispatch_plan(
            CreateDispatchPlanCommand(
                CommandId(f"cmd_no_progress_plan_{suffix}"),
                output_id,
                "catalog-v1",
                dependencies,
                (RawToolProposal("community.inspect", '{"artifact_id":"artifact_one"}'),),
            )
        )

    def fail(plan, dependencies, suffix: str) -> None:
        admitted = broker.admit(
            AdmitToolCallCommand(
                CommandId(f"cmd_no_progress_admit_{suffix}"),
                plan.scheduled_call_ids[0],
                1,
                "admission-v1",
                ("pure_read",),
                dependencies,
            )
        )
        handle = bridge.begin(
            BeginBrokerExecutionCommand(
                CommandId(f"cmd_no_progress_begin_{suffix}"),
                turn.turn_id,
                plan.scheduled_call_ids[0],
                admitted.operation_id,
            )
        )
        bridge.complete(
            CompleteBrokerExecutionCommand(
                CommandId(f"cmd_no_progress_complete_{suffix}"),
                handle,
                False,
                "fixture failure",
                {"reason": "fixture"},
            )
        )

    try:
        broker.register_contract(
            RegisterToolContractCommand(
                CommandId("cmd_no_progress_register"), turn.turn_id, read_contract()
            )
        )
        first = plan_for(assistant_output_id, dependency_v1, "one")
        fail(first, dependency_v1, "one")
        second_output = new_output(connection, identities, turn, "cmd_no_progress_step_two")
        second = plan_for(second_output, dependency_v1, "two")
        fail(second, dependency_v1, "two")

        third_output = new_output(connection, identities, turn, "cmd_no_progress_step_three")
        third = plan_for(third_output, dependency_v1, "three")
        with pytest.raises(ValueError, match="no-progress"):
            broker.admit(
                AdmitToolCallCommand(
                    CommandId("cmd_no_progress_admit_three"),
                    third.scheduled_call_ids[0],
                    1,
                    "admission-v1",
                    ("pure_read",),
                    dependency_v1,
                )
            )

        changed_output = new_output(
            connection, identities, turn, "cmd_no_progress_step_changed"
        )
        changed = plan_for(changed_output, dependency_v2, "changed")
        admitted = broker.admit(
            AdmitToolCallCommand(
                CommandId("cmd_no_progress_admit_changed"),
                changed.scheduled_call_ids[0],
                1,
                "admission-v1",
                ("pure_read",),
                dependency_v2,
            )
        )
        assert admitted.status == "admitted"
    finally:
        connection.close()


def test_no_progress_guard_blocks_identical_empty_success_stata_calls(
    tmp_path: Path,
) -> None:
    connection, identities, turn, assistant_output_id = initialized_output(tmp_path)
    broker = ToolBrokerService(SqliteToolBrokerRepository(connection), identities)
    bridge = BrokerExecutionService(SqliteBrokerExecutionRepository(connection), identities)
    dependencies = {"resource_identities": {"artifact:artifact_one": "artifact_one"}}

    def run_empty_success(output_id, suffix: str) -> None:
        plan = broker.create_dispatch_plan(
            CreateDispatchPlanCommand(
                CommandId(f"cmd_empty_plan_{suffix}"),
                output_id,
                "catalog-v1",
                dependencies,
                (RawToolProposal("stata.execute", '{"artifact_id":"artifact_one"}'),),
            )
        )
        admitted = broker.admit(
            AdmitToolCallCommand(
                CommandId(f"cmd_empty_admit_{suffix}"),
                plan.scheduled_call_ids[0],
                1,
                "admission-v1",
                ("pure_read",),
                dependencies,
            )
        )
        handle = bridge.begin(
            BeginBrokerExecutionCommand(
                CommandId(f"cmd_empty_begin_{suffix}"),
                turn.turn_id,
                plan.scheduled_call_ids[0],
                admitted.operation_id,
            )
        )
        bridge.complete(
            CompleteBrokerExecutionCommand(
                CommandId(f"cmd_empty_complete_{suffix}"),
                handle,
                True,
                "Stata execution completed",
                {
                    "structured": None,
                    "raw_output_excerpt": "(task finished with empty output)",
                },
            )
        )

    try:
        broker.register_contract(
            RegisterToolContractCommand(
                CommandId("cmd_empty_register"), turn.turn_id, read_contract("stata.execute")
            )
        )
        run_empty_success(assistant_output_id, "one")
        run_empty_success(
            new_output(connection, identities, turn, "cmd_empty_step_two"), "two"
        )
        third_output = new_output(connection, identities, turn, "cmd_empty_step_three")
        third = broker.create_dispatch_plan(
            CreateDispatchPlanCommand(
                CommandId("cmd_empty_plan_three"),
                third_output,
                "catalog-v1",
                dependencies,
                (RawToolProposal("stata.execute", '{"artifact_id":"artifact_one"}'),),
            )
        )
        with pytest.raises(ValueError, match="empty-success"):
            broker.admit(
                AdmitToolCallCommand(
                    CommandId("cmd_empty_admit_three"),
                    third.scheduled_call_ids[0],
                    1,
                    "admission-v1",
                    ("pure_read",),
                    dependencies,
                )
            )
    finally:
        connection.close()


def test_executor_exception_closes_admitted_operation_and_releases_resource(
    tmp_path: Path,
) -> None:
    connection, identities, turn, assistant_output_id = initialized_output(tmp_path)
    broker = ToolBrokerService(SqliteToolBrokerRepository(connection), identities)
    dependencies = {"resource_identities": {"artifact:artifact_one": "artifact_one"}}
    try:
        broker.register_contract(
            RegisterToolContractCommand(
                CommandId("cmd_exception_register"), turn.turn_id, read_contract()
            )
        )
        plan = broker.create_dispatch_plan(
            CreateDispatchPlanCommand(
                CommandId("cmd_exception_plan"),
                assistant_output_id,
                "catalog-v1",
                dependencies,
                (RawToolProposal("community.inspect", '{"artifact_id":"artifact_one"}'),),
            )
        )
        admitted = broker.admit(
            AdmitToolCallCommand(
                CommandId("cmd_exception_admit"),
                plan.scheduled_call_ids[0],
                1,
                "admission-v1",
                ("pure_read",),
                dependencies,
            )
        )

        outcome = broker.record_executor_exception(
            RecordExecutorExceptionCommand(
                CommandId("cmd_exception_record"),
                turn.turn_id,
                admitted.tool_call_id,
                admitted.operation_id,
                "ValueError",
                "formal post-estimation source Run has no promoted Result",
            )
        )

        assert outcome.status == "failed"
        assert connection.execute(
            "SELECT status FROM operations WHERE operation_id = ?",
            (admitted.operation_id.value,),
        ).fetchone()[0] == "failed"
        assert connection.execute(
            "SELECT count(*) FROM tool_resource_leases WHERE lease_status = 'active'"
        ).fetchone()[0] == 0
        result = connection.execute(
            """
            SELECT result_kind, structured_payload_json
            FROM canonical_tool_results WHERE tool_call_id = ?
            """,
            (admitted.tool_call_id.value,),
        ).fetchone()
        assert result["result_kind"] == "error"
        assert json.loads(str(result["structured_payload_json"])) == {
            "error_detail": "formal post-estimation source Run has no promoted Result",
            "error_kind": "ValueError",
            "execution_outcome": "definitely_not_started",
            "failure_phase": "executor_exception",
        }
    finally:
        connection.close()


def test_local_arbitrary_code_contract_cannot_omit_staged_os_isolation(
    tmp_path: Path,
) -> None:
    connection, identities, turn, _ = initialized_output(tmp_path)
    broker = ToolBrokerService(SqliteToolBrokerRepository(connection), identities)
    try:
        reviewed = python_run_contract()
        assert reviewed.execution_isolation == "sandboxed_staged_execution"
        with pytest.raises(ValueError, match="staged OS isolation"):
            broker.register_contract(
                RegisterToolContractCommand(
                    CommandId("cmd_register_unsafe_python"),
                    turn.turn_id,
                    replace(reviewed, execution_isolation="none"),
                )
            )
        assert (
            connection.execute(
                "SELECT count(*) FROM tool_contracts WHERE tool_name = 'python.run'"
            ).fetchone()[0]
            == 0
        )
    finally:
        connection.close()


def test_jit_admission_rechecks_dependencies_and_intake_side_effect_gate(
    tmp_path: Path,
) -> None:
    connection, identities, turn, assistant_output_id = initialized_output(tmp_path)
    broker = ToolBrokerService(SqliteToolBrokerRepository(connection), identities)
    try:
        broker.register_contract(
            RegisterToolContractCommand(
                CommandId("cmd_register_read_stale"), turn.turn_id, read_contract()
            )
        )
        dependencies = {"resource_identities": {"artifact:artifact_one": "artifact_one"}}
        plan = broker.create_dispatch_plan(
            CreateDispatchPlanCommand(
                CommandId("cmd_stale_plan"),
                assistant_output_id,
                "catalog-v1",
                dependencies,
                (RawToolProposal("community.inspect", '{"artifact_id":"artifact_one"}'),),
            )
        )
        with pytest.raises(ValueError, match="dependencies changed"):
            broker.admit(
                AdmitToolCallCommand(
                    CommandId("cmd_stale_admit"),
                    plan.scheduled_call_ids[0],
                    1,
                    "admission-v1",
                    ("pure_read",),
                    {"resource_identities": {"artifact:artifact_one": "new_revision"}},
                )
            )
        assert connection.execute("SELECT count(*) FROM operations").fetchone()[0] == 0

        write_definition = ToolContractDefinition(
            "community.mutate",
            "1.0.0",
            "Community mutation",
            "artifact.capture",
            {"type": "object", "properties": {}, "additionalProperties": False},
            {"type": "object"},
            "local_runtime",
            "workspace_write",
            "exclusive_scope",
            "non_replayable",
            "never",
            "not_interruptible",
            10.0,
            30.0,
            100_000,
            (ResourceClaimTemplate("workspace:main", "exclusive"),),
        )
        broker.register_contract(
            RegisterToolContractCommand(
                CommandId("cmd_register_write_intake"), turn.turn_id, write_definition
            )
        )
        write_dependencies = {"resource_identities": {}}
        write_output_id = new_output(connection, identities, turn, "cmd_tool_model_step_for_write")
        write_plan = broker.create_dispatch_plan(
            CreateDispatchPlanCommand(
                CommandId("cmd_write_plan"),
                write_output_id,
                "catalog-v2",
                write_dependencies,
                (RawToolProposal("community.mutate", "{}"),),
            )
        )
        with pytest.raises(ValueError, match="intake-only"):
            broker.admit(
                AdmitToolCallCommand(
                    CommandId("cmd_write_admit"),
                    write_plan.scheduled_call_ids[0],
                    1,
                    "admission-v1",
                    ("workspace_write",),
                    write_dependencies,
                )
            )
        assert connection.execute("SELECT count(*) FROM operations").fetchone()[0] == 0
    finally:
        connection.close()


def test_waiting_is_strict_admission_barrier_and_answer_increments_turn_revision(
    tmp_path: Path,
) -> None:
    connection, identities, turn, assistant_output_id = initialized_output(tmp_path)
    broker = ToolBrokerService(SqliteToolBrokerRepository(connection), identities)
    interaction = TurnInteractionService(SqliteTurnInteractionRepository(connection), identities)
    dependencies = {"resource_identities": {"artifact:artifact_one": "artifact_one"}}
    try:
        broker.register_contract(
            RegisterToolContractCommand(
                CommandId("cmd_wait_register"), turn.turn_id, read_contract()
            )
        )
        plan = broker.create_dispatch_plan(
            CreateDispatchPlanCommand(
                CommandId("cmd_wait_plan"),
                assistant_output_id,
                "catalog-v1",
                dependencies,
                (RawToolProposal("community.inspect", '{"artifact_id":"artifact_one"}'),),
            )
        )
        waiting = interaction.open_waiting(
            OpenWaitingCommand(
                CommandId("cmd_wait_open"),
                turn.turn_id,
                1,
                WaitReason.USER_CONFIRMATION,
                "Which specification should continue?",
                plan.scheduled_call_ids[0],
            )
        )
        assert waiting.turn_revision == 2
        with pytest.raises(ValueError, match="non-running"):
            broker.admit(
                AdmitToolCallCommand(
                    CommandId("cmd_wait_blocked_admit"),
                    plan.scheduled_call_ids[0],
                    2,
                    "admission-v1",
                    ("pure_read",),
                    dependencies,
                )
            )
        answered = interaction.answer_waiting(
            AnswerWaitingCommand(
                CommandId("cmd_wait_answer"),
                waiting.waiting_request_id,
                "Approve this inspection",
                "approve",
            )
        )
        assert answered.turn_revision == 3
        admitted = broker.admit(
            AdmitToolCallCommand(
                CommandId("cmd_wait_revalidated_admit"),
                plan.scheduled_call_ids[0],
                3,
                "admission-v1",
                ("pure_read",),
                dependencies,
            )
        )
        assert admitted.status == "admitted"
    finally:
        connection.close()


def test_user_pause_blocks_new_admission_and_cancels_pre_handoff_operation(
    tmp_path: Path,
) -> None:
    connection, identities, turn, assistant_output_id = initialized_output(tmp_path)
    broker = ToolBrokerService(SqliteToolBrokerRepository(connection), identities)
    interaction = TurnInteractionService(SqliteTurnInteractionRepository(connection), identities)
    dependencies = {
        "resource_identities": {
            "artifact:artifact_one": "artifact_one",
            "artifact:artifact_two": "artifact_two",
        }
    }
    try:
        broker.register_contract(
            RegisterToolContractCommand(
                CommandId("cmd_pause_register"), turn.turn_id, read_contract()
            )
        )
        plan = broker.create_dispatch_plan(
            CreateDispatchPlanCommand(
                CommandId("cmd_pause_plan"),
                assistant_output_id,
                "catalog-v1",
                dependencies,
                (
                    RawToolProposal("community.inspect", '{"artifact_id":"artifact_one"}'),
                    RawToolProposal("community.inspect", '{"artifact_id":"artifact_two"}'),
                ),
            )
        )
        first = broker.admit(
            AdmitToolCallCommand(
                CommandId("cmd_pause_first_admit"),
                plan.scheduled_call_ids[0],
                1,
                "admission-v1",
                ("pure_read",),
                dependencies,
            )
        )
        requested = interaction.request_pause(
            RequestPauseCommand(
                CommandId("cmd_pause_request"), turn.turn_id, 1, "Review current progress"
            )
        )
        assert requested.turn_revision == 2
        with pytest.raises(ValueError, match="pause intent"):
            broker.admit(
                AdmitToolCallCommand(
                    CommandId("cmd_pause_second_admit"),
                    plan.scheduled_call_ids[1],
                    2,
                    "admission-v1",
                    ("pure_read",),
                    dependencies,
                )
            )
        converged = interaction.converge_pause(
            ConvergePauseCommand(CommandId("cmd_pause_converge"), turn.turn_id)
        )
        assert converged.converged is True
        assert (
            connection.execute(
                "SELECT status FROM turns WHERE turn_id = ?", (turn.turn_id.value,)
            ).fetchone()[0]
            == "paused"
        )
        assert (
            connection.execute(
                "SELECT status FROM operations WHERE operation_id = ?", (first.operation_id.value,)
            ).fetchone()[0]
            == "failed"
        )
        assert (
            connection.execute(
                "SELECT result_kind FROM canonical_tool_results WHERE tool_call_id = ?",
                (plan.scheduled_call_ids[0].value,),
            ).fetchone()[0]
            == "cancelled"
        )
        continuation = interaction.continue_paused_turn(
            ContinuePausedTurnCommand(
                CommandId("cmd_pause_continue"),
                turn.turn_id,
                "I reviewed it; continue with the revised direction",
            )
        )
        assert continuation.successor_turn_id != turn.turn_id
        assert continuation.successor_status == "running"
        relation = connection.execute(
            """
            SELECT relation_kind, contract_decision,
                   predecessor_contract_revision_id = successor_contract_revision_id
            FROM turn_continuations WHERE turn_continuation_id = ?
            """,
            (continuation.continuation_id.value,),
        ).fetchone()
        assert tuple(relation) == (
            "user_pause_continuation",
            "retain_contract_revision",
            1,
        )
        assert (
            connection.execute(
                "SELECT status FROM turns WHERE turn_id = ?", (turn.turn_id.value,)
            ).fetchone()[0]
            == "paused"
        )
    finally:
        connection.close()


def test_tool_admission_budget_is_checked_and_consumed_in_same_uow(tmp_path: Path) -> None:
    connection, identities, turn, assistant_output_id = initialized_output(tmp_path, tool_budget=1)
    broker = ToolBrokerService(SqliteToolBrokerRepository(connection), identities)
    dependencies = {
        "resource_identities": {
            "artifact:artifact_one": "artifact_one",
            "artifact:artifact_two": "artifact_two",
        }
    }
    try:
        broker.register_contract(
            RegisterToolContractCommand(
                CommandId("cmd_budget_tool_register"), turn.turn_id, read_contract()
            )
        )
        plan = broker.create_dispatch_plan(
            CreateDispatchPlanCommand(
                CommandId("cmd_budget_tool_plan"),
                assistant_output_id,
                "catalog-v1",
                dependencies,
                (
                    RawToolProposal("community.inspect", '{"artifact_id":"artifact_one"}'),
                    RawToolProposal("community.inspect", '{"artifact_id":"artifact_two"}'),
                ),
            )
        )
        broker.admit(
            AdmitToolCallCommand(
                CommandId("cmd_budget_tool_first"),
                plan.scheduled_call_ids[0],
                1,
                "admission-v1",
                ("pure_read",),
                dependencies,
            )
        )
        revision_before = connection.execute(
            "SELECT max(workspace_revision) FROM workspace_commits"
        ).fetchone()[0]
        with pytest.raises(ValueError, match="budget is exhausted"):
            broker.admit(
                AdmitToolCallCommand(
                    CommandId("cmd_budget_tool_second"),
                    plan.scheduled_call_ids[1],
                    1,
                    "admission-v1",
                    ("pure_read",),
                    dependencies,
                )
            )
        assert (
            connection.execute("SELECT max(workspace_revision) FROM workspace_commits").fetchone()[
                0
            ]
            == revision_before
        )
        assert (
            connection.execute("SELECT used_tool_admissions FROM turn_budget_accounts").fetchone()[
                0
            ]
            == 1
        )
    finally:
        connection.close()


def test_pause_waits_for_handoff_operation_to_reach_durable_classification(
    tmp_path: Path,
) -> None:
    connection, identities, turn, assistant_output_id = initialized_output(tmp_path)
    broker = ToolBrokerService(SqliteToolBrokerRepository(connection), identities)
    interaction = TurnInteractionService(SqliteTurnInteractionRepository(connection), identities)
    dependencies = {"resource_identities": {"artifact:artifact_one": "artifact_one"}}
    try:
        broker.register_contract(
            RegisterToolContractCommand(
                CommandId("cmd_inflight_register"), turn.turn_id, read_contract()
            )
        )
        plan = broker.create_dispatch_plan(
            CreateDispatchPlanCommand(
                CommandId("cmd_inflight_plan"),
                assistant_output_id,
                "catalog-v1",
                dependencies,
                (RawToolProposal("community.inspect", '{"artifact_id":"artifact_one"}'),),
            )
        )
        admitted = broker.admit(
            AdmitToolCallCommand(
                CommandId("cmd_inflight_admit"),
                plan.scheduled_call_ids[0],
                1,
                "admission-v1",
                ("pure_read",),
                dependencies,
            )
        )
        current_revision = connection.execute(
            "SELECT max(workspace_revision) FROM workspace_commits"
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO operation_attempts VALUES (
                'attempt_pause_inflight', ?, 1, 1, 'handoff_committed', ?, NULL
            )
            """,
            (admitted.operation_id.value, current_revision),
        )
        connection.execute(
            "UPDATE operations SET status = 'handoff_committed' WHERE operation_id = ?",
            (admitted.operation_id.value,),
        )
        interaction.request_pause(
            RequestPauseCommand(CommandId("cmd_inflight_pause"), turn.turn_id, 1, "Pause safely")
        )
        first = interaction.converge_pause(
            ConvergePauseCommand(CommandId("cmd_inflight_converge_one"), turn.turn_id)
        )
        assert first.converged is False
        assert first.active_operation_count == 1
        assert (
            connection.execute(
                "SELECT status FROM turns WHERE turn_id = ?", (turn.turn_id.value,)
            ).fetchone()[0]
            == "running"
        )

        stable_revision = first.commit_revision.value
        connection.execute(
            """
            UPDATE operation_attempts
            SET status = 'outcome_unknown', terminal_revision = ?
            WHERE operation_id = ?
            """,
            (stable_revision, admitted.operation_id.value),
        )
        connection.execute(
            """
            UPDATE operations SET status = 'outcome_unknown', terminal_revision = ?
            WHERE operation_id = ?
            """,
            (stable_revision, admitted.operation_id.value),
        )
        second = interaction.converge_pause(
            ConvergePauseCommand(CommandId("cmd_inflight_converge_two"), turn.turn_id)
        )
        assert second.converged is True
        assert (
            connection.execute(
                "SELECT status FROM turns WHERE turn_id = ?", (turn.turn_id.value,)
            ).fetchone()[0]
            == "paused"
        )
    finally:
        connection.close()


@pytest.mark.skipif(os.name != "nt", reason="BaseContainer is Windows-only")
def test_admitted_python_uses_base_container_and_commits_execution_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wxc = (
        Path(os.environ["TEMP"])
        / "stataagent-tq09-mxc"
        / "src"
        / "target"
        / "x86_64-pc-windows-msvc"
        / "release"
        / "wxc-exec.exe"
    )
    python = Path.home() / "miniconda3" / "python.exe"
    powershell = Path(r"C:\Program Files\PowerShell\7\pwsh.exe")
    if not all(path.is_file() for path in (wxc, python, powershell)):
        pytest.skip("reviewed MXC/Python/PowerShell runtime is unavailable")

    connection, identities, turn, assistant_output_id = initialized_output(tmp_path, normalize=True)
    broker = ToolBrokerService(SqliteToolBrokerRepository(connection), identities)
    dependencies = {"resource_identities": {}}
    execution_root = tmp_path / "workspace" / "runtime"
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-host-only-canary-123456789")
    try:
        broker.register_contract(
            RegisterToolContractCommand(
                CommandId("cmd_register_python_sandbox"),
                turn.turn_id,
                ToolContractDefinition(
                    "python.run",
                    "1.0.0",
                    "Sandboxed Python",
                    "python.execute",
                    {
                        "type": "object",
                        "properties": {
                            "code": {"type": "string"},
                            "inputs": {"type": "array", "default": []},
                            "network_mode": {"type": "string", "default": "block"},
                            "timeout_seconds": {"type": "integer", "default": 30},
                        },
                        "required": ["code"],
                        "additionalProperties": False,
                    },
                    {"type": "object"},
                    "local_runtime",
                    "write_or_unknown",
                    "exclusive_scope",
                    "non_replayable",
                    "never",
                    "terminatable_with_recovery",
                    30.0,
                    300.0,
                    1_000_000,
                    (ResourceClaimTemplate("execution-scope:main", "exclusive"),),
                    "sandboxed_staged_execution",
                ),
            )
        )
        authority = (tmp_path / "workspace" / "workspace.sqlite3").resolve()
        code = f"""
from pathlib import Path
import json
import os
import socket

authority = Path({str(authority)!r})
try:
    authority.read_bytes()
    authority_read = True
except Exception:
    authority_read = False
try:
    socket.create_connection(("1.1.1.1", 443), timeout=1).close()
    network_connected = True
except Exception:
    network_connected = False
report = {{
    "authority_read": authority_read,
    "network_connected": network_connected,
    "provider_secret_present": "DEEPSEEK_API_KEY" in os.environ,
}}
(Path(os.environ["SRA_OUTPUT_DIR"]) / "report.json").write_text(
    json.dumps(report), encoding="utf-8"
)
print("isolated-python-ok")
"""
        plan = broker.create_dispatch_plan(
            CreateDispatchPlanCommand(
                CommandId("cmd_python_sandbox_plan"),
                assistant_output_id,
                "catalog-v1",
                dependencies,
                (RawToolProposal("python.run", json.dumps({"code": code})),),
            )
        )
        admitted = broker.admit(
            AdmitToolCallCommand(
                CommandId("cmd_python_sandbox_admit"),
                plan.scheduled_call_ids[0],
                2,
                "admission-v1",
                ("write_or_unknown",),
                dependencies,
            )
        )
        executor = SandboxToolExecutor(
            WindowsSandboxExecutor(
                wxc_executable=wxc,
                python_executable=python,
                powershell_executable=powershell,
                execution_root=execution_root,
            ),
            BrokerExecutionService(SqliteBrokerExecutionRepository(connection), identities),
            identities,
            produced_artifacts=ProducedArtifactService(
                SqliteProducedArtifactRepository(connection),
                FilesystemManagedArtifactStore(
                    tmp_path / "workspace", execution_root=execution_root
                ),
                identities,
            ),
        )
        result = asyncio.run(
            executor.execute(
                ToolExecutionRequest(
                    turn.turn_id,
                    plan.scheduled_call_ids[0],
                    admitted.operation_id,
                    "python.run",
                    {"code": code, "inputs": [], "network_mode": "block", "timeout_seconds": 30},
                )
            )
        )
        assert result.success is True
        result_payload = json.loads(result.context_text)
        assert result_payload["isolation_tier"] == "base-container"
        assert result_payload["status"] == "completed"
        assert "host-only-canary" not in result.context_text
        assert len(result_payload["captured_artifacts"]) == 2

        attempt_id = result_payload["operation_attempt_id"]
        report_path = (
            execution_root
            / ".stata-agent"
            / "staging"
            / attempt_id
            / "scratch"
            / "outputs"
            / "report.json"
        )
        assert json.loads(report_path.read_text(encoding="utf-8")) == {
            "authority_read": False,
            "network_connected": False,
            "provider_secret_present": False,
        }
        receipt = connection.execute(
            """
            SELECT isolation_tier, network_mode, dacl_fallback_allowed,
                   staging_preflight, staging_postflight
            FROM sandbox_execution_receipts WHERE operation_id = ?
            """,
            (admitted.operation_id.value,),
        ).fetchone()
        assert tuple(receipt) == (
            "base-container",
            "block",
            0,
            "passed",
            "passed",
        )
        assert (
            connection.execute(
                "SELECT status FROM operations WHERE operation_id = ?",
                (admitted.operation_id.value,),
            ).fetchone()[0]
            == "completed"
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM tool_resource_leases WHERE lease_status = 'active'"
            ).fetchone()[0]
            == 0
        )
    finally:
        connection.close()


def test_sandbox_executor_captures_exact_code_and_outputs_before_completion(
    tmp_path: Path,
) -> None:
    connection, identities, turn, assistant_output_id = initialized_output(tmp_path, normalize=True)
    broker = ToolBrokerService(SqliteToolBrokerRepository(connection), identities)
    dependencies = {"resource_identities": {}}
    execution_root = tmp_path / "workspace" / "runtime"
    contract = python_run_contract()
    try:
        broker.register_contract(
            RegisterToolContractCommand(
                CommandId("cmd_register_python_capture"), turn.turn_id, contract
            )
        )
        plan = broker.create_dispatch_plan(
            CreateDispatchPlanCommand(
                CommandId("cmd_python_capture_plan"),
                assistant_output_id,
                "catalog-v1",
                dependencies,
                (RawToolProposal("python.run", json.dumps({"code": "print(2.5)"})),),
            )
        )
        admitted = broker.admit(
            AdmitToolCallCommand(
                CommandId("cmd_python_capture_admit"),
                plan.scheduled_call_ids[0],
                2,
                "admission-v1",
                ("write_or_unknown",),
                dependencies,
            )
        )
        managed_store = FilesystemManagedArtifactStore(
            tmp_path / "workspace", execution_root=execution_root
        )
        executor = SandboxToolExecutor(
            RecordingSandbox(execution_root),
            BrokerExecutionService(SqliteBrokerExecutionRepository(connection), identities),
            identities,
            produced_artifacts=ProducedArtifactService(
                SqliteProducedArtifactRepository(connection), managed_store, identities
            ),
        )
        result = asyncio.run(
            executor.execute(
                ToolExecutionRequest(
                    turn.turn_id,
                    plan.scheduled_call_ids[0],
                    admitted.operation_id,
                    "python.run",
                    {"code": "print(2.5)"},
                )
            )
        )

        assert result.success is True
        payload = json.loads(result.context_text)
        assert [item["role"] for item in payload["captured_artifacts"]] == [
            "executable",
            "output",
        ]
        assert [item["artifact_kind"] for item in payload["captured_artifacts"]] == [
            "code",
            "table",
        ]
        rows = connection.execute(
            """
            SELECT artifact_kind, media_type, producer_attempt_id
            FROM artifacts WHERE producer_attempt_id = ? ORDER BY artifact_kind
            """,
            (payload["operation_attempt_id"],),
        ).fetchall()
        assert {(str(row[0]), str(row[1])) for row in rows} == {
            ("code", "text/x-python"),
            ("table", "application/json"),
        }
        assert {str(row[2]) for row in rows} == {payload["operation_attempt_id"]}
        assert (
            connection.execute(
                "SELECT count(*) FROM artifact_verification_receipts WHERE reason_code = ?",
                ("sandbox_capture_verified",),
            ).fetchone()[0]
            == 2
        )
        canonical = connection.execute(
            "SELECT artifact_references_json FROM canonical_tool_results"
        ).fetchone()[0]
        assert len(json.loads(str(canonical))) == 2
    finally:
        connection.close()
