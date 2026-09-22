"""Public API/OpenAPI contract and real M0 command/query vertical."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
from fastapi import FastAPI

from stata_research_agent.application.control import SubmitMessageCommand
from stata_research_agent.application.workspace_service import WorkspaceControlService
from stata_research_agent.domain.identifiers import CommandId, WorkspaceId
from stata_research_agent.interfaces.api import WorkspaceHost, create_app
from stata_research_agent.persistence.control_query import (
    SqliteWorkspaceQuery,
    decode_workspace_stream_cursor,
)
from stata_research_agent.persistence.control_store import SqliteControlStore
from stata_research_agent.runtime.uuid_identity import UuidIdentityGenerator
from tools.generate_contracts import build_ipc_schema, build_tool_schema, generate_typescript

PROJECT_ROOT = Path(__file__).parents[2]


def client(tmp_path: Path) -> FastAPI:
    return create_app(WorkspaceHost(tmp_path / "host"))


def request(app: FastAPI, method: str, url: str, **kwargs) -> httpx.Response:
    async def send() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as session:
            return await session.request(method, url, **kwargs)

    return asyncio.run(send())


def workspace_create_payload() -> dict:
    return {
        "schema_version": "1",
        "command_id": "cmd_create_api",
        "workspace_id": "ws_api",
        "command_type": "workspace.create",
        "payload": {},
        "preconditions": {},
    }


def test_public_command_query_vertical_and_idempotent_replay(tmp_path: Path) -> None:
    api = client(tmp_path)
    created = request(api, "POST", "/api/v1/commands", json=workspace_create_payload())
    assert created.status_code == 200
    assert created.json()["commit_revision"] == 1

    replay = request(api, "POST", "/api/v1/commands", json=workspace_create_payload())
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert replay.json()["commit_revision"] == 1

    message = {
        "schema_version": "1",
        "command_id": "cmd_message_api",
        "workspace_id": "ws_api",
        "command_type": "message.submit",
        "payload": {"content": "Estimate the baseline model", "execution_mode": "write"},
        "preconditions": {},
    }
    submitted = request(api, "POST", "/api/v1/commands", json=message)
    assert submitted.status_code == 200
    turn_id = submitted.json()["turn_id"]
    assert turn_id.startswith("turn_")

    query = request(api, "GET", "/api/v1/workspaces/ws_api/execution")
    assert query.status_code == 200
    body = query.json()
    assert body["authoritative_revision"] == 2
    assert body["data"]["active_write_turn_id"] == turn_id
    assert body["data"]["turns"][0]["status"] == "running"

    bootstrap = request(api, "GET", "/api/v1/workspaces/ws_api/bootstrap")
    assert bootstrap.status_code == 200
    snapshot = bootstrap.json()
    assert snapshot["authoritative_revision"] == 2
    assert snapshot["durable_stream_cursor"].startswith("wsc1_")
    assert snapshot["data"]["active_conversation_id"].startswith("conv_")
    assert snapshot["data"]["recent_messages"][0]["content"] == ("Estimate the baseline model")
    assert snapshot["data"]["execution"]["active_write_turn_id"] == turn_id
    assert snapshot["data"]["research_paths"][0]["canonical_key"] == "main"
    watermark = snapshot["data"]["projection_watermarks"][0]
    assert watermark["projection_name"] == "evidence_current_state"
    assert watermark["projection_revision"] == 0
    assert watermark["projection_lag"] == 2


def test_workspace_data_catalog_exposes_only_safe_relative_dta_observations(
    tmp_path: Path,
) -> None:
    host = WorkspaceHost(tmp_path / "host")
    api = create_app(host)
    created = request(api, "POST", "/api/v1/commands", json=workspace_create_payload())
    assert created.status_code == 200
    root = host.database(WorkspaceId("ws_api")).root
    (root / "inputs").mkdir()
    (root / "inputs" / "study.dta").write_bytes(b"dataset")
    (root / ".stata-agent" / "hidden").mkdir(parents=True, exist_ok=True)
    (root / ".stata-agent" / "hidden" / "private.dta").write_bytes(b"private")

    response = request(api, "GET", "/api/v1/workspaces/ws_api/data-files")

    assert response.status_code == 200
    body = response.json()
    assert body["authoritative_revision"] == 1
    assert body["items"] == [
        {
            "relative_path": "inputs/study.dta",
            "display_name": "study.dta",
            "size_bytes": 7,
            "modified_ns": body["items"][0]["modified_ns"],
        }
    ]
    assert Path(body["items"][0]["relative_path"]).is_absolute() is False


def test_bootstrap_data_and_cursor_share_one_consistent_read_view(tmp_path: Path) -> None:
    host = WorkspaceHost(tmp_path / "host")
    api = create_app(host)
    created = request(api, "POST", "/api/v1/commands", json=workspace_create_payload())
    assert created.status_code == 200
    database = host.database(WorkspaceId("ws_api"))
    reader = database.open(writable=False)
    writer = database.open(writable=True)
    try:
        invoked = False

        def commit_between_snapshot_reads() -> None:
            nonlocal invoked
            assert not invoked
            invoked = True
            WorkspaceControlService(
                SqliteControlStore(writer), UuidIdentityGenerator()
            ).submit_message(
                SubmitMessageCommand(
                    CommandId("cmd_bootstrap_interleaved"),
                    "Committed after the Bootstrap read view was established",
                )
            )

        old_view = SqliteWorkspaceQuery(
            reader, during_bootstrap_snapshot_hook=commit_between_snapshot_reads
        ).bootstrap_snapshot()
        assert invoked
        assert old_view.authoritative_revision.value == 1
        assert old_view.recent_messages == ()
        old_cursor = decode_workspace_stream_cursor(old_view.durable_stream_cursor)
        assert old_cursor[:2] == (1, 1)

        new_view = SqliteWorkspaceQuery(reader).bootstrap_snapshot()
        assert new_view.authoritative_revision.value == 2
        assert len(new_view.recent_messages) == 1
        new_cursor = decode_workspace_stream_cursor(new_view.durable_stream_cursor)
        assert new_cursor[:2] == (2, 1)
    finally:
        writer.close()
        reader.close()


def test_unknown_command_version_type_and_extra_fields_fail_closed(tmp_path: Path) -> None:
    api = client(tmp_path)
    invalid_cases = []
    wrong_version = workspace_create_payload()
    wrong_version["schema_version"] = "2"
    invalid_cases.append(wrong_version)
    wrong_type = workspace_create_payload()
    wrong_type["command_type"] = "database.patch"
    invalid_cases.append(wrong_type)
    extra_internal = workspace_create_payload()
    extra_internal["sqlite_table"] = "turns"
    invalid_cases.append(extra_internal)

    for payload in invalid_cases:
        response = request(api, "POST", "/api/v1/commands", json=payload)
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "INVALID_REQUEST"


def test_fastapi_serves_the_built_shell_same_origin_without_shadowing_api(
    tmp_path: Path,
) -> None:
    static = tmp_path / "dist"
    static.mkdir()
    (static / "index.html").write_text("<main>typed browser shell</main>", encoding="utf-8")
    api = create_app(WorkspaceHost(tmp_path / "host"), static_directory=static)
    root = request(api, "GET", "/")
    assert root.status_code == 200
    assert "typed browser shell" in root.text
    meta = request(api, "GET", "/api/v1/meta")
    assert meta.status_code == 200
    assert meta.json()["api_version"] == "1"


def test_idempotency_collision_has_stable_typed_error(tmp_path: Path) -> None:
    api = client(tmp_path)
    created = request(api, "POST", "/api/v1/commands", json=workspace_create_payload())
    assert created.status_code == 200
    first = {
        "schema_version": "1",
        "command_id": "cmd_collision_api",
        "workspace_id": "ws_api",
        "command_type": "message.submit",
        "payload": {"content": "First"},
        "preconditions": {},
    }
    assert request(api, "POST", "/api/v1/commands", json=first).status_code == 200
    second = {**first, "payload": {"content": "Changed"}}
    response = request(api, "POST", "/api/v1/commands", json=second)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_published_openapi_is_specific_and_does_not_expose_internal_rows() -> None:
    contract = json.loads(
        (PROJECT_ROOT / "contracts" / "api" / "openapi-v1.json").read_text(encoding="utf-8")
    )
    assert "/api/v1/commands" in contract["paths"]
    assert "/api/v1/workspaces/{workspace_id}/execution" in contract["paths"]
    assert "/api/v1/workspaces/{workspace_id}/bootstrap" in contract["paths"]
    assert "/api/v1/workspaces/{workspace_id}/data-files" in contract["paths"]
    assert "/api/v1/workspaces/{workspace_id}/lineage" in contract["paths"]
    assert "/api/v1/workspaces/{workspace_id}/analysis-outputs" in contract["paths"]
    assert "/api/v1/workspaces/{workspace_id}/journal-entries" in contract["paths"]
    assert "/api/v1/workspaces/{workspace_id}/stream-head" in contract["paths"]
    assert "/api/v1/workspaces/{workspace_id}/events" in contract["paths"]
    assert "/api/v1/workspace-attention" in contract["paths"]
    assert "/api/v1/workspaces/{workspace_id}/conversations/{conversation_id}" in contract["paths"]
    assert "/api/v1/workspaces/{workspace_id}/turns/{turn_id}/usage" in contract["paths"]
    assert (
        "/api/v1/workspaces/{workspace_id}/research-paths/{research_path_id}/results"
        in contract["paths"]
    )
    assert (
        "/api/v1/workspaces/{workspace_id}/research-paths/{research_path_id}/documents"
        in contract["paths"]
    )
    serialized = json.dumps(contract).lower()
    for forbidden in (
        "database_path",
        "sqlite_table",
        "provider_api_key",
        "managed_store_absolute_path",
        "relationship_registry",
        "view_registry",
    ):
        assert forbidden not in serialized

    generated_types = (PROJECT_ROOT / "web" / "src" / "generated" / "api-v1.ts").read_text(
        encoding="utf-8"
    )
    assert "Generated by tools/generate_contracts.py" in generated_types
    assert "CommandReceiptResponse" in generated_types
    assert '"/api/v1/commands"' in generated_types
    assert "export async function submitCommand" in generated_types
    assert "export async function getWorkspaceExecution" in generated_types
    assert "export async function getWorkspaceBootstrap" in generated_types
    assert "export async function getEvidenceLineage" in generated_types
    assert "export async function getConversationDetail" in generated_types
    assert "export async function getAnalysisOutputIndex" in generated_types
    assert "export async function getResultIndex" in generated_types
    assert "export async function getJournalEntries" in generated_types
    assert "export async function getDocumentIndex" in generated_types
    assert "export async function getWorkspaceStreamHead" in generated_types
    assert "export function workspaceEventStreamUrl" in generated_types
    assert "export async function getWorkspaceAttention" in generated_types
    assert "WaitingAnswerEnvelope" in generated_types
    assert "TurnPauseRequestEnvelope" in generated_types


def test_generated_contract_files_have_no_drift(tmp_path: Path) -> None:
    current_openapi = create_app(WorkspaceHost(tmp_path / "schema-host")).openapi()
    published_openapi = json.loads(
        (PROJECT_ROOT / "contracts" / "api" / "openapi-v1.json").read_text(encoding="utf-8")
    )
    assert published_openapi == current_openapi
    assert (
        json.loads(
            (PROJECT_ROOT / "contracts" / "ipc" / "agent-ipc-v2.schema.json").read_text(
                encoding="utf-8"
            )
        )
        == build_ipc_schema()
    )
    assert (
        json.loads(
            (PROJECT_ROOT / "contracts" / "tools" / "tool-contract-v1.schema.json").read_text(
                encoding="utf-8"
            )
        )
        == build_tool_schema()
    )
    assert (PROJECT_ROOT / "web" / "src" / "generated" / "api-v1.ts").read_text(
        encoding="utf-8"
    ) == generate_typescript(current_openapi)
