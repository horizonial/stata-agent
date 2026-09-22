"""Open browser/API control surface for Project Memory."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
from fastapi import FastAPI

from stata_research_agent.interfaces.api import WorkspaceHost, create_app


def request(app: FastAPI, method: str, url: str, **kwargs: object) -> httpx.Response:
    async def send() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as session:
            return await session.request(method, url, **kwargs)

    return asyncio.run(send())


def test_memory_index_mutations_and_conversation_policy_are_public(tmp_path: Path) -> None:
    app = create_app(WorkspaceHost(tmp_path / "host"))
    workspace_id = "ws_memory_api"
    assert (
        request(
            app,
            "POST",
            "/api/v1/commands",
            json={
                "schema_version": "1",
                "command_id": "cmd_memory_api_workspace",
                "workspace_id": workspace_id,
                "command_type": "workspace.create",
                "payload": {},
                "preconditions": {},
            },
        ).status_code
        == 200
    )
    submitted = request(
        app,
        "POST",
        "/api/v1/commands",
        json={
            "schema_version": "1",
            "command_id": "cmd_memory_api_message",
            "workspace_id": workspace_id,
            "command_type": "message.submit",
            "payload": {"content": "Use price as the primary outcome."},
            "preconditions": {},
        },
    ).json()
    conversation_id = next(
        item["resource_id"]
        for item in submitted["created_resource_refs"]
        if item["resource_type"] == "conversation"
    )
    message_id = next(
        item["resource_id"]
        for item in submitted["created_resource_refs"]
        if item["resource_type"] == "message"
    )

    created = request(
        app,
        "POST",
        f"/api/v1/workspaces/{workspace_id}/memories",
        json={
            "command_id": "cmd_memory_api_create",
            "kind": "research_decision",
            "title": "Primary outcome",
            "content": "Use price as the primary outcome.",
            "source_message_id": message_id,
            "source_message_revision": submitted["commit_revision"],
        },
    )
    assert created.status_code == 200
    memory = created.json()
    assert memory["lifecycle"] == "active"

    index = request(app, "GET", f"/api/v1/workspaces/{workspace_id}/memories")
    assert index.status_code == 200
    assert index.json()["items"][0]["sources"][0]["object_id"] == message_id
    assert index.json()["items"][0]["recall_count"] == 0
    assert "never_recalled" in index.json()["items"][0]["quality_flags"]
    assert "Primary outcome" in index.json()["summaries"][0]["summary_text"]

    revised = request(
        app,
        "POST",
        f"/api/v1/workspaces/{workspace_id}/memories/{memory['memory_item_id']}/revise",
        json={
            "command_id": "cmd_memory_api_revise",
            "expected_pointer_revision": memory["pointer_revision"],
            "title": "Primary price outcome",
            "content": "Use price as the main outcome in the baseline specification.",
            "lifecycle": "active",
        },
    )
    assert revised.status_code == 200
    assert revised.json()["pointer_revision"] == 2
    memory = revised.json()

    policy_url = f"/api/v1/workspaces/{workspace_id}/conversations/{conversation_id}/memory-policy"
    initial_policy = request(app, "GET", policy_url)
    assert initial_policy.json()["policy_revision"] == 0
    changed_policy = request(
        app,
        "PUT",
        policy_url,
        json={
            "command_id": "cmd_memory_api_policy",
            "use_memory": False,
            "contribute_memory": True,
            "expected_policy_revision": 0,
        },
    )
    assert changed_policy.status_code == 200
    assert changed_policy.json()["use_memory"] is False

    retracted = request(
        app,
        "POST",
        (f"/api/v1/workspaces/{workspace_id}/memories/{memory['memory_item_id']}/retract"),
        json={
            "command_id": "cmd_memory_api_retract",
            "expected_pointer_revision": memory["pointer_revision"],
            "reason": "Changed by the researcher",
        },
    )
    assert retracted.status_code == 200
    assert retracted.json()["lifecycle"] == "retracted"
