"""M4-02 typed research views and snapshot-bound Journal pagination."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
from fastapi import FastAPI

from stata_research_agent.interfaces.api import WorkspaceHost, create_app
from stata_research_agent.interfaces.api.models import ResourceRef


def request(app: FastAPI, method: str, url: str, **kwargs: object) -> httpx.Response:
    async def send() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as session:
            return await session.request(method, url, **kwargs)

    return asyncio.run(send())


def create_workspace(app: FastAPI, workspace_id: str = "ws_views") -> None:
    response = request(
        app,
        "POST",
        "/api/v1/commands",
        json={
            "schema_version": "1",
            "command_id": f"cmd_create_{workspace_id}",
            "workspace_id": workspace_id,
            "command_type": "workspace.create",
            "payload": {},
            "preconditions": {},
        },
    )
    assert response.status_code == 200


def test_plan_revision_is_a_public_resource_reference() -> None:
    reference = ResourceRef(resource_type="plan_revision", resource_id="planrev_test")

    assert reference.model_dump() == {
        "resource_type": "plan_revision",
        "resource_id": "planrev_test",
    }


def submit_message(app: FastAPI, command_id: str, content: str) -> dict[str, object]:
    response = request(
        app,
        "POST",
        "/api/v1/commands",
        json={
            "schema_version": "1",
            "command_id": command_id,
            "workspace_id": "ws_views",
            "command_type": "message.submit",
            "payload": {"content": content, "execution_mode": "write"},
            "preconditions": {},
        },
    )
    assert response.status_code == 200
    return response.json()


def test_default_views_read_stable_resources_without_page_owned_facts(tmp_path: Path) -> None:
    app = create_app(WorkspaceHost(tmp_path / "host"))
    create_workspace(app)
    submitted = submit_message(app, "cmd_view_message", "Estimate the baseline specification")
    conversation_id = next(
        ref["resource_id"]
        for ref in submitted["created_resource_refs"]
        if ref["resource_type"] == "conversation"
    )

    conversation = request(
        app,
        "GET",
        f"/api/v1/workspaces/ws_views/conversations/{conversation_id}",
    )
    assert conversation.status_code == 200
    assert conversation.json()["data"]["timeline"][0]["content"] == (
        "Estimate the baseline specification"
    )
    assert conversation.json()["data"]["turns"][0]["turn_id"] == submitted["turn_id"]

    bootstrap = request(app, "GET", "/api/v1/workspaces/ws_views/bootstrap").json()
    path_id = bootstrap["data"]["research_paths"][0]["research_path_id"]
    results = request(
        app,
        "GET",
        f"/api/v1/workspaces/ws_views/research-paths/{path_id}/results",
    )
    plan = request(
        app,
        "GET",
        f"/api/v1/workspaces/ws_views/research-paths/{path_id}/plan",
    )
    documents = request(
        app,
        "GET",
        f"/api/v1/workspaces/ws_views/research-paths/{path_id}/documents",
    )
    analysis_outputs = request(
        app,
        "GET",
        "/api/v1/workspaces/ws_views/analysis-outputs",
    )
    assert results.status_code == 200
    assert results.json()["data"] == {"research_path_id": path_id, "items": []}
    assert documents.status_code == 200
    assert documents.json()["data"]["research_path_id"] == path_id
    assert analysis_outputs.status_code == 200
    assert analysis_outputs.json()["data"] == {"items": []}
    assert plan.status_code == 200
    assert plan.json()["data"]["research_path_id"] == path_id
    assert plan.json()["data"]["plan_revision_id"] is None
    assert plan.json()["data"]["nodes"] == []


def test_journal_cursor_freezes_snapshot_and_rejects_tampering_or_mismatch(
    tmp_path: Path,
) -> None:
    app = create_app(WorkspaceHost(tmp_path / "host"))
    create_workspace(app)
    submit_message(app, "cmd_trace_one", "First trace request")

    first = request(
        app,
        "GET",
        "/api/v1/workspaces/ws_views/journal-entries?page_size=2&sort=desc",
    )
    assert first.status_code == 200
    first_page = first.json()
    assert len(first_page["items"]) == 2
    assert first_page["next_cursor"].startswith("jpc1_")
    frozen_revision = first_page["as_of_workspace_revision"]

    # Cursor integrity survives a main-service restart because the local host key
    # is persisted outside any Workspace research ledger.
    app = create_app(WorkspaceHost(tmp_path / "host"))
    submit_message(app, "cmd_trace_two", "Second trace request")
    cursor = first_page["next_cursor"]
    second = request(
        app,
        "GET",
        f"/api/v1/workspaces/ws_views/journal-entries?page_size=2&sort=desc&cursor={cursor}",
    )
    assert second.status_code == 200
    second_page = second.json()
    assert second_page["as_of_workspace_revision"] == frozen_revision
    assert second_page["workspace_advanced"] is True
    assert second_page["newer_matching_entries_available"] is True
    returned = first_page["items"] + second_page["items"]
    assert len({item["journal_entry_id"] for item in returned}) == len(returned)
    assert all(item["workspace_revision"] <= frozen_revision for item in returned)

    mismatch = request(
        app,
        "GET",
        f"/api/v1/workspaces/ws_views/journal-entries?page_size=3&sort=desc&cursor={cursor}",
    )
    assert mismatch.status_code == 409
    assert mismatch.json()["error"]["code"] == "CURSOR_QUERY_MISMATCH"

    tampered = cursor[:-1] + ("A" if cursor[-1] != "A" else "B")
    invalid = request(
        app,
        "GET",
        f"/api/v1/workspaces/ws_views/journal-entries?page_size=2&sort=desc&cursor={tampered}",
    )
    assert invalid.status_code == 400
    assert invalid.json()["error"]["code"] == "CURSOR_INVALID"
