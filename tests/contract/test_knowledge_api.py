"""Public read surface for the workspace literature index."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
from fastapi import FastAPI

from stata_research_agent.domain.identifiers import WorkspaceId
from stata_research_agent.interfaces.api import WorkspaceHost, create_app
from stata_research_agent.interfaces.knowledge_runtime import WorkspaceKnowledgeIndexService
from stata_research_agent.persistence.knowledge_store import SqliteKnowledgeRepository
from stata_research_agent.runtime.uuid_identity import UuidIdentityGenerator


def request(app: FastAPI, method: str, url: str, **kwargs: object) -> httpx.Response:
    async def send() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as session:
            return await session.request(method, url, **kwargs)

    return asyncio.run(send())


def test_knowledge_index_is_visible_without_exposing_payloads(tmp_path: Path) -> None:
    host = WorkspaceHost(tmp_path / "host")
    app = create_app(host)
    workspace_id = WorkspaceId("ws_knowledge_api")
    created = request(
        app,
        "POST",
        "/api/v1/commands",
        json={
            "schema_version": "1",
            "command_id": "cmd_knowledge_api_workspace",
            "workspace_id": workspace_id.value,
            "command_type": "workspace.create",
            "payload": {},
            "preconditions": {},
        },
    )
    assert created.status_code == 200

    database = host.database(workspace_id)
    literature = database.root / "literature"
    literature.mkdir()
    (literature / "design.md").write_text(
        "Instrument relevance and exclusion restrictions require separate justification.",
        encoding="utf-8",
    )
    connection = database.open(writable=True)
    try:
        WorkspaceKnowledgeIndexService(
            SqliteKnowledgeRepository(connection), database.root, UuidIdentityGenerator()
        ).synchronize()
    finally:
        connection.close()

    response = request(
        app, "GET", f"/api/v1/workspaces/{workspace_id.value}/knowledge-index"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["workspace_id"] == workspace_id.value
    assert body["documents"][0]["relative_path"] == "literature/design.md"
    assert body["documents"][0]["availability"] == "indexed"
    assert len(body["documents"][0]["content_sha256"]) == 64
    assert "Instrument relevance" not in response.text
