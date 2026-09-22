from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path

import httpx
from fastapi import FastAPI

from stata_research_agent.application.control import CreateWorkspaceCommand, SubmitMessageCommand
from stata_research_agent.application.model_gateway import (
    ContextBuildDecisionCandidate,
    ContextItemCandidate,
    ProviderDispatchError,
    ProviderResponse,
    StartModelStepCommand,
)
from stata_research_agent.application.model_gateway_service import (
    ModelGatewayService,
    ProviderFallbackRoute,
    ProviderResiliencePolicy,
)
from stata_research_agent.application.provider_credentials import ResolvedProviderCredential
from stata_research_agent.application.runtime_usage import (
    ProviderPrice,
    ProviderPricingCatalog,
)
from stata_research_agent.application.workspace_service import WorkspaceControlService
from stata_research_agent.domain.identifiers import CommandId, WorkspaceId
from stata_research_agent.interfaces.api import WorkspaceHost, create_app
from stata_research_agent.persistence.control_store import SqliteControlStore
from stata_research_agent.persistence.model_gateway_store import SqliteModelGatewayRepository
from stata_research_agent.runtime.uuid_identity import UuidIdentityGenerator


def request(app: FastAPI, method: str, url: str, **kwargs: object) -> httpx.Response:
    async def send() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as session:
            return await session.request(method, url, **kwargs)

    return asyncio.run(send())


class Credential:
    def resolve_for_transport(
        self, credential_ref: str, *, provider_profile_id: str, endpoint: str
    ) -> ResolvedProviderCredential:
        del credential_ref
        return ResolvedProviderCredential(
            provider_profile_id, "credentialversion_test", endpoint, "secret"
        )


class FallbackTransport:
    async def send(
        self, *, endpoint: str, request_json: str, credential: str
    ) -> ProviderResponse:
        del request_json, credential
        if "primary" in endpoint:
            raise ProviderDispatchError("provider_http_429", retry_safe=True)
        return ProviderResponse(
            {"text": "completed", "tool_calls": []},
            usage_kind="exact",
            input_tokens=1000,
            output_tokens=100,
            cached_input_tokens=400,
            uncached_input_tokens=600,
        )


def test_turn_usage_explains_retry_fallback_cache_and_unknown_cost(tmp_path: Path) -> None:
    workspace_id = WorkspaceId("ws_usage")
    host = WorkspaceHost(tmp_path / "host")
    database = host.database(workspace_id)
    database.create()
    connection = database.open(writable=True)
    control = WorkspaceControlService(SqliteControlStore(connection), UuidIdentityGenerator())
    control.create_workspace(CreateWorkspaceCommand(CommandId("cmd_usage_create"), workspace_id))
    turn = control.submit_message(
        SubmitMessageCommand(CommandId("cmd_usage_message"), "Estimate the model")
    )
    command = StartModelStepCommand(
        command_id=CommandId("cmd_usage_step"),
        turn_id=turn.turn_id,
        expected_turn_revision=1,
        system_prompt_revision="system-v1",
        system_prompt="Trace every research result.",
        main_skill_name="default-research",
        main_skill_revision="skill-v1",
        main_skill_content="Discuss, execute, verify, and report.",
        tool_catalog_revision="tools-v1",
        tool_schemas=(),
        context_items=(
            ContextItemCandidate(
                "user_message",
                "message",
                "msg_usage",
                "1",
                "remote_allowed",
                "Estimate the model",
                "user_instruction",
            ),
        ),
        build_decisions=(
            ContextBuildDecisionCandidate(
                "included", "message", "msg_usage", "active_request", {}
            ),
        ),
        permission_policy_revision="permission-v1",
        permissions={"workspace_read": True},
        model_policy_revision="model-policy-v1",
        provider_profile="primary",
        provider_kind="openai-compatible",
        model_name="primary-model",
        endpoint="https://primary.example/v1/responses",
        credential_ref="credential://primary",
        provider_policy={},
    )

    async def no_delay(_seconds: float) -> None:
        return None

    gateway = ModelGatewayService(
        SqliteModelGatewayRepository(connection),
        UuidIdentityGenerator(),
        Credential(),
        FallbackTransport(),
        resilience_policy=ProviderResiliencePolicy(
            retry_base_seconds=0,
            retry_max_seconds=0,
            jitter_ratio=0,
            attempts_before_fallback=1,
        ),
        fallback_routes=(
            ProviderFallbackRoute(
                "fallback", "fallback-model", "https://fallback.example/v1/responses", "cred"
            ),
        ),
        sleeper=no_delay,
    )
    asyncio.run(gateway.execute_step(command))
    connection.close()

    pricing = ProviderPricingCatalog(
        "test-prices-v1",
        "USD",
        (ProviderPrice("fallback", Decimal("1"), Decimal("2"), Decimal("0.25")),),
    )
    app = create_app(host, provider_pricing=pricing)
    response = request(
        app,
        "GET",
        f"/api/v1/workspaces/ws_usage/turns/{turn.turn_id.value}/usage",
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["step_count"] == 1
    assert data["retry_count"] == 1
    assert data["fallback_count"] == 1
    assert data["cached_input_tokens_observed"] == 400
    assert data["provider_attempts"][1]["provider_profile"] == "fallback"
    assert data["provider_attempts"][1]["is_fallback"] is True
    assert data["count_budget"]["used_provider_attempts"] == 2
    assert data["monetary_estimate"]["quality"] == "unknown"
    assert data["monetary_estimate"]["amount"] is None
    assert data["monetary_estimate"]["known_subtotal"] == "0.0009"
    assert data["monetary_estimate"]["known_cached_savings"] == "0.0003"
    assert data["monetary_estimate"]["unpriced_attempt_count"] == 1


def test_turn_usage_for_unstarted_turn_is_zero_not_missing(tmp_path: Path) -> None:
    app = create_app(WorkspaceHost(tmp_path / "host"))
    created = request(
        app,
        "POST",
        "/api/v1/commands",
        json={
            "schema_version": "1",
            "command_id": "cmd_create_usage_empty",
            "workspace_id": "ws_usage_empty",
            "command_type": "workspace.create",
            "payload": {},
            "preconditions": {},
        },
    )
    assert created.status_code == 200
    submitted = request(
        app,
        "POST",
        "/api/v1/commands",
        json={
            "schema_version": "1",
            "command_id": "cmd_submit_usage_empty",
            "workspace_id": "ws_usage_empty",
            "command_type": "message.submit",
            "payload": {"content": "Wait before running"},
            "preconditions": {},
        },
    )
    turn_id = submitted.json()["turn_id"]

    response = request(
        app,
        "GET",
        f"/api/v1/workspaces/ws_usage_empty/turns/{turn_id}/usage",
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["step_count"] == 0
    assert data["provider_attempts"] == []
    assert data["monetary_estimate"]["amount"] == "0"
    assert data["monetary_estimate"]["quality"] == "exact"


def test_operational_evaluation_api_reports_not_applicable_without_inventing_scores(
    tmp_path: Path,
) -> None:
    app = create_app(WorkspaceHost(tmp_path / "host"))
    assert request(
        app,
        "POST",
        "/api/v1/commands",
        json={
            "schema_version": "1",
            "command_id": "cmd_create_eval_empty",
            "workspace_id": "ws_eval_empty",
            "command_type": "workspace.create",
            "payload": {},
            "preconditions": {},
        },
    ).status_code == 200
    submitted = request(
        app,
        "POST",
        "/api/v1/commands",
        json={
            "schema_version": "1",
            "command_id": "cmd_submit_eval_empty",
            "workspace_id": "ws_eval_empty",
            "command_type": "message.submit",
            "payload": {"content": "Do not start yet"},
            "preconditions": {},
        },
    )
    turn_id = submitted.json()["turn_id"]

    turn_response = request(
        app,
        "GET",
        f"/api/v1/workspaces/ws_eval_empty/turns/{turn_id}/evaluation",
    )
    workspace_response = request(
        app,
        "GET",
        "/api/v1/workspaces/ws_eval_empty/evaluation",
    )

    assert turn_response.status_code == workspace_response.status_code == 200
    turn_layers = turn_response.json()["data"]["layers"]
    l1_metrics = {
        metric["metric_id"]: metric for metric in turn_layers[0]["metrics"]
    }
    assert l1_metrics["l1.gateway.invocation_completion_rate"]["status"] == (
        "not_applicable"
    )
    assert workspace_response.json()["data"]["turns"][0]["turn_id"] == turn_id
