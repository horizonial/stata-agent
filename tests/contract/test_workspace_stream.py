"""M4-03 durable Outbox replay and resync contracts."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from stata_research_agent.domain.identifiers import CommandId, WorkspaceId
from stata_research_agent.interfaces.api import WorkspaceHost, create_app
from stata_research_agent.persistence.atomic_commit import AtomicCommitService, JournalDraft
from stata_research_agent.persistence.outbox_stream import (
    SqliteOutboxStreamQuery,
    StreamResyncRequiredError,
)


def request(app: FastAPI, method: str, url: str, **kwargs: object) -> httpx.Response:
    async def send() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as session:
            return await session.request(method, url, **kwargs)

    return asyncio.run(send())


def create_workspace(app: FastAPI) -> None:
    response = request(
        app,
        "POST",
        "/api/v1/commands",
        json={
            "schema_version": "1",
            "command_id": "cmd_stream_create",
            "workspace_id": "ws_stream",
            "command_type": "workspace.create",
            "payload": {},
            "preconditions": {},
        },
    )
    assert response.status_code == 200


def test_bootstrap_cursor_replays_commit_gap_at_least_once_and_keeps_orders_separate(
    tmp_path: Path,
) -> None:
    host = WorkspaceHost(tmp_path / "host")
    app = create_app(host)
    create_workspace(app)
    bootstrap = request(app, "GET", "/api/v1/workspaces/ws_stream/bootstrap").json()
    bootstrap_cursor = bootstrap["durable_stream_cursor"]

    submitted = request(
        app,
        "POST",
        "/api/v1/commands",
        json={
            "schema_version": "1",
            "command_id": "cmd_after_bootstrap",
            "workspace_id": "ws_stream",
            "command_type": "message.submit",
            "payload": {"content": "Committed before SSE connect"},
            "preconditions": {},
        },
    )
    assert submitted.status_code == 200

    connection = host.database(WorkspaceId("ws_stream")).open(writable=False)
    try:
        replay = SqliteOutboxStreamQuery(connection).replay_after(bootstrap_cursor)
        duplicate = SqliteOutboxStreamQuery(connection).replay_after(bootstrap_cursor)
    finally:
        connection.close()
    assert replay.notifications == duplicate.notifications
    assert len(replay.notifications) == 1
    notification = replay.notifications[0]
    assert notification.workspace_revision.value == 2
    assert notification.event_type == "workspace.changed"
    assert any(ref.resource_type == "turn" for ref in notification.resource_refs)

    # A later authoritative revision may intentionally emit zero notifications.
    writer = host.database(WorkspaceId("ws_stream")).open(writable=True)
    try:
        AtomicCommitService(writer).commit(
            command_id=CommandId("cmd_no_notification"),
            command_type="test.no_notification",
            request={},
            response={"committed": True},
            journal=(JournalDraft("test.committed", "test", "test_1", {}),),
            outbox=(),
        )
    finally:
        writer.close()
    reader = host.database(WorkspaceId("ws_stream")).open(writable=False)
    try:
        after_notification = SqliteOutboxStreamQuery(reader).replay_after(
            notification.stream_cursor
        )
    finally:
        reader.close()
    assert after_notification.authoritative_revision.value == 3
    assert after_notification.notifications == ()
    assert after_notification.current_cursor == notification.stream_cursor


def test_stream_head_requires_a_retained_exact_cursor_and_signals_resync(
    tmp_path: Path,
) -> None:
    host = WorkspaceHost(tmp_path / "host")
    app = create_app(host)
    create_workspace(app)
    cursor = request(app, "GET", "/api/v1/workspaces/ws_stream/bootstrap").json()[
        "durable_stream_cursor"
    ]
    head = request(
        app,
        "GET",
        f"/api/v1/workspaces/ws_stream/stream-head?after={cursor}",
    )
    assert head.status_code == 200
    assert head.json()["pending_notifications"] is False

    unknown_anchor = cursor.rsplit("_", 1)[0] + "_not-a-real-anchor"
    invalid = request(
        app,
        "GET",
        f"/api/v1/workspaces/ws_stream/stream-head?after={unknown_anchor}",
    )
    assert invalid.status_code == 409
    assert invalid.json()["error"]["code"] == "RESYNC_REQUIRED"

    reader = host.database(WorkspaceId("ws_stream")).open(writable=False)
    try:
        with pytest.raises(StreamResyncRequiredError):
            SqliteOutboxStreamQuery(reader).replay_after("jpc1_wrong_namespace")
    finally:
        reader.close()
