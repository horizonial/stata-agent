"""Authoritative investigation queries locate Tool Call failures without hidden reasoning."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from stata_research_agent.application.broker_execution import (
    BeginBrokerExecutionCommand,
    CompleteBrokerExecutionCommand,
)
from stata_research_agent.application.broker_execution_service import BrokerExecutionService
from stata_research_agent.application.control import CreateWorkspaceCommand, SubmitMessageCommand
from stata_research_agent.application.model_gateway import (
    ContextItemCandidate,
    ProviderResponse,
    StartModelStepCommand,
)
from stata_research_agent.application.model_gateway_service import ModelGatewayService
from stata_research_agent.application.provider_credentials import ResolvedProviderCredential
from stata_research_agent.application.tool_broker import (
    AdmitToolCallCommand,
    CreateDispatchPlanCommand,
    RawToolProposal,
    RecordToolAdmissionBlockedCommand,
    RegisterToolContractCommand,
    ResourceClaimTemplate,
    ToolAdmissionBlockedError,
    ToolContractDefinition,
)
from stata_research_agent.application.tool_broker_service import ToolBrokerService
from stata_research_agent.application.workspace_service import WorkspaceControlService
from stata_research_agent.domain.identifiers import CommandId, WorkspaceId
from stata_research_agent.interfaces.api import WorkspaceHost, create_app
from stata_research_agent.persistence.broker_execution_store import (
    SqliteBrokerExecutionRepository,
)
from stata_research_agent.persistence.control_store import SqliteControlStore
from stata_research_agent.persistence.model_gateway_store import SqliteModelGatewayRepository
from stata_research_agent.persistence.tool_broker_store import SqliteToolBrokerRepository
from stata_research_agent.runtime.uuid_identity import UuidIdentityGenerator


class Credential:
    def resolve_for_transport(
        self, credential_ref: str, *, provider_profile_id: str, endpoint: str
    ) -> ResolvedProviderCredential:
        del credential_ref
        return ResolvedProviderCredential(
            provider_profile_id,
            "credentialversion_investigation",
            endpoint,
            "transport-only-secret",
        )


class PublicDecisionTransport:
    async def send(
        self, *, endpoint: str, request_json: str, credential: str
    ) -> ProviderResponse:
        del endpoint, request_json, credential
        return ProviderResponse(
            {
                "text": "Inspect both registered artifacts before choosing the next action.",
                "tool_calls": [],
            }
        )


def request(app, path: str) -> httpx.Response:
    async def send() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get(path)

    return asyncio.run(send())


def inspection_contract() -> ToolContractDefinition:
    return ToolContractDefinition(
        "community.inspect",
        "1.0.0",
        "Inspect an artifact",
        "artifact.verify",
        {
            "type": "object",
            "properties": {"artifact_id": {"type": "string"}},
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
        10,
        30,
        100_000,
        (ResourceClaimTemplate("artifact:{artifact_id}", "read", "artifact_id"),),
    )


def test_investigation_api_drills_from_turn_to_blocked_and_completed_tools(
    tmp_path: Path,
) -> None:
    host = WorkspaceHost(tmp_path / "workspaces")
    workspace_id = WorkspaceId("ws_investigation")
    database = host.database(workspace_id)
    database.create()
    connection = database.open(writable=True)
    identities = UuidIdentityGenerator()
    control = WorkspaceControlService(SqliteControlStore(connection), identities)
    control.create_workspace(CreateWorkspaceCommand(CommandId("cmd_inv_workspace"), workspace_id))
    turn = control.submit_message(
        SubmitMessageCommand(CommandId("cmd_inv_message"), "Inspect both artifacts")
    )
    output = asyncio.run(
        ModelGatewayService(
            SqliteModelGatewayRepository(connection),
            identities,
            Credential(),
            PublicDecisionTransport(),
        ).execute_step(
            StartModelStepCommand(
                CommandId("cmd_inv_model"),
                turn.turn_id,
                1,
                "system-v1",
                "Use registered tools and expose a concise public decision summary.",
                "research-main",
                "skill-v1",
                "Inspect before acting.",
                "catalog-v1",
                (),
                (
                    ContextItemCandidate(
                        "message",
                        "message",
                        "message_inv",
                        "1",
                        "remote_allowed",
                        "Inspect both artifacts",
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
            )
        )
    )
    assert output.assistant_output_id is not None
    broker = ToolBrokerService(SqliteToolBrokerRepository(connection), identities)
    broker.register_contract(
        RegisterToolContractCommand(
            CommandId("cmd_inv_register"), turn.turn_id, inspection_contract()
        )
    )
    dependencies = {
        "resource_identities": {
            "artifact:artifact_blocked": "artifact_blocked",
            "artifact:artifact_ok": "artifact_ok",
        }
    }
    plan = broker.create_dispatch_plan(
        CreateDispatchPlanCommand(
            CommandId("cmd_inv_plan"),
            output.assistant_output_id,
            "catalog-v1",
            dependencies,
            (
                RawToolProposal(
                    "community.inspect", '{"artifact_id":"artifact_blocked"}', "provider-1"
                ),
                RawToolProposal(
                    "community.inspect", '{"artifact_id":"artifact_ok"}', "provider-2"
                ),
            ),
        )
    )
    blocked_call, completed_call = plan.scheduled_call_ids
    with pytest.raises(ToolAdmissionBlockedError) as blocked:
        broker.admit(
            AdmitToolCallCommand(
                CommandId("cmd_inv_admit_blocked"),
                blocked_call,
                1,
                "admission-v1",
                (),
                dependencies,
            )
        )
    broker.record_admission_blocked(
        RecordToolAdmissionBlockedCommand(
            CommandId("cmd_inv_record_blocked"),
            turn.turn_id,
            blocked_call,
            blocked.value.reason_code,
        )
    )
    admitted = broker.admit(
        AdmitToolCallCommand(
            CommandId("cmd_inv_admit_ok"),
            completed_call,
            1,
            "admission-v1",
            ("pure_read",),
            dependencies,
        )
    )
    bridge = BrokerExecutionService(SqliteBrokerExecutionRepository(connection), identities)
    handle = bridge.begin(
        BeginBrokerExecutionCommand(
            CommandId("cmd_inv_begin"), turn.turn_id, completed_call, admitted.operation_id
        )
    )
    bridge.complete(
        CompleteBrokerExecutionCommand(
            CommandId("cmd_inv_complete"),
            handle,
            True,
            "Artifact inspection completed",
            {"available": True},
        )
    )
    connection.close()

    app = create_app(host)
    overview = request(
        app,
        f"/api/v1/workspaces/{workspace_id.value}/turns/{turn.turn_id.value}/investigation",
    )
    assert overview.status_code == 200
    overview_data = overview.json()["data"]
    assert overview_data["tool_call_ids"] == [blocked_call.value, completed_call.value]
    assert any(
        item["layer"] == "tool_admission" and item["object_id"] == blocked_call.value
        for item in overview_data["findings"]
    )

    blocked_detail = request(
        app,
        f"/api/v1/workspaces/{workspace_id.value}/turns/{turn.turn_id.value}"
        f"/tool-decisions/{blocked_call.value}",
    )
    assert blocked_detail.status_code == 200
    blocked_data = blocked_detail.json()["data"]
    assert blocked_data["assistant_public_text"].startswith("Inspect both")
    assert blocked_data["canonical_arguments"] == {"artifact_id": "artifact_blocked"}
    assert blocked_data["structural_diagnosis"] == "admission_blocked:effect_class_denied"
    assert blocked_data["status_history"][-1]["reason_code"] == "effect_class_denied"
    assert any(
        item["event_type"] == "tool.admission_blocked"
        for item in blocked_data["journal_references"]
    )

    completed_detail = request(
        app,
        f"/api/v1/workspaces/{workspace_id.value}/turns/{turn.turn_id.value}"
        f"/tool-decisions/{completed_call.value}",
    )
    assert completed_detail.status_code == 200
    completed_data = completed_detail.json()["data"]
    assert completed_data["structural_diagnosis"] == "no_structural_failure"
    assert completed_data["operations"][0]["status"] == "completed"
    assert completed_data["result_kind"] == "success"

    missing_turn = request(
        app,
        f"/api/v1/workspaces/{workspace_id.value}/turns/turn_missing/investigation",
    )
    assert missing_turn.status_code == 404
    missing_tool = request(
        app,
        f"/api/v1/workspaces/{workspace_id.value}/turns/{turn.turn_id.value}"
        "/tool-decisions/toolcall_missing",
    )
    assert missing_tool.status_code == 404
    read_connection = database.open(writable=False)
    try:
        revision_after_reads = read_connection.execute(
            "SELECT COALESCE(MAX(workspace_revision), 0) FROM workspace_commits"
        ).fetchone()[0]
    finally:
        read_connection.close()
    assert revision_after_reads == overview.json()["authoritative_revision"]
