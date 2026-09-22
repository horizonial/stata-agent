"""M4-04 browser intent commands and minimal Attention read model."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
from fastapi import FastAPI

from stata_research_agent.application.turn_interaction import OpenWaitingCommand
from stata_research_agent.application.turn_interaction_service import TurnInteractionService
from stata_research_agent.domain.identifiers import CommandId, TurnId, WorkspaceId
from stata_research_agent.domain.status import WaitReason
from stata_research_agent.interfaces.api import WorkspaceHost, create_app
from stata_research_agent.persistence.turn_interaction_store import (
    SqliteTurnInteractionRepository,
)
from stata_research_agent.runtime.uuid_identity import UuidIdentityGenerator


def request(app: FastAPI, method: str, url: str, **kwargs: object) -> httpx.Response:
    async def send() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as session:
            return await session.request(method, url, **kwargs)

    return asyncio.run(send())


def command(app: FastAPI, body: dict[str, object]) -> httpx.Response:
    return request(app, "POST", "/api/v1/commands", json=body)


def test_waiting_answer_queue_pause_and_attention_keep_distinct_semantics(
    tmp_path: Path,
) -> None:
    host = WorkspaceHost(tmp_path / "host")
    app = create_app(host)
    assert (
        command(
            app,
            {
                "schema_version": "1",
                "command_id": "cmd_controls_create",
                "workspace_id": "ws_controls",
                "command_type": "workspace.create",
                "payload": {},
                "preconditions": {},
            },
        ).status_code
        == 200
    )
    submitted = command(
        app,
        {
            "schema_version": "1",
            "command_id": "cmd_controls_message",
            "workspace_id": "ws_controls",
            "command_type": "message.submit",
            "payload": {"content": "Start a controlled study"},
            "preconditions": {},
        },
    ).json()
    turn_id = str(submitted["turn_id"])

    connection = host.database(WorkspaceId("ws_controls")).open(writable=True)
    try:
        waiting = TurnInteractionService(
            SqliteTurnInteractionRepository(connection), UuidIdentityGenerator()
        ).open_waiting(
            OpenWaitingCommand(
                CommandId("cmd_controls_waiting"),
                TurnId(turn_id),
                1,
                WaitReason.USER_INPUT,
                "Choose the missing-value treatment",
            )
        )
    finally:
        connection.close()
    assert waiting.turn_revision == 2

    attention = request(app, "GET", "/api/v1/workspace-attention")
    assert attention.status_code == 200
    workspace_attention = attention.json()["workspaces"][0]
    assert workspace_attention["requires_action"] is True
    assert workspace_attention["highest_severity"] == "ACTION_REQUIRED"
    assert workspace_attention["attention_refs"][0]["attention_kind"] == "waiting_for_user"
    assert "Choose the missing-value treatment" not in json.dumps(attention.json())

    stale = command(
        app,
        {
            "schema_version": "1",
            "command_id": "cmd_controls_stale_answer",
            "workspace_id": "ws_controls",
            "command_type": "waiting.answer",
            "payload": {
                "waiting_request_id": waiting.waiting_request_id.value,
                "turn_id": turn_id,
                "waiting_revision": 1,
                "answer": "Use complete cases",
            },
            "preconditions": {},
        },
    )
    assert stale.status_code == 409
    still_waiting = request(app, "GET", "/api/v1/workspaces/ws_controls/bootstrap")
    assert still_waiting.json()["data"]["open_waiting_request"] is not None

    answer_body = {
        "schema_version": "1",
        "command_id": "cmd_controls_answer",
        "workspace_id": "ws_controls",
        "command_type": "waiting.answer",
        "payload": {
            "waiting_request_id": waiting.waiting_request_id.value,
            "turn_id": turn_id,
            "waiting_revision": 2,
            "answer": "Use complete cases and report the attrition",
        },
        "preconditions": {},
    }
    answered = command(app, answer_body)
    assert answered.status_code == 200
    assert answered.json()["effect"] == "waiting_answered"
    assert answered.json()["turn_revision"] == 3

    queued = command(
        app,
        {
            "schema_version": "1",
            "command_id": "cmd_controls_queued",
            "workspace_id": "ws_controls",
            "command_type": "message.submit",
            "payload": {"content": "Run an additional robustness check"},
            "preconditions": {},
        },
    )
    assert queued.status_code == 200
    execution = request(app, "GET", "/api/v1/workspaces/ws_controls/execution").json()
    assert [turn["status"] for turn in execution["data"]["turns"]] == ["running", "queued"]

    pause_body = {
        "schema_version": "1",
        "command_id": "cmd_controls_pause",
        "workspace_id": "ws_controls",
        "command_type": "turn.pause.request",
        "payload": {
            "turn_id": turn_id,
            "expected_turn_revision": 3,
            "reason": "inspect current work",
        },
        "preconditions": {},
    }
    paused = command(app, pause_body)
    assert paused.status_code == 200
    assert paused.json()["effect"] == "pause_requested"
    assert paused.json()["turn_revision"] == 4
    replay = command(app, pause_body)
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True

    bootstrap = request(app, "GET", "/api/v1/workspaces/ws_controls/bootstrap").json()
    assert bootstrap["data"]["execution"]["active_write_turn_id"] == turn_id
    assert bootstrap["data"]["active_pause_intent"]["status"] == "requested"
    assert bootstrap["data"]["execution"]["turns"][0]["status"] == "running"

    stale_pause = command(
        app,
        {
            **pause_body,
            "command_id": "cmd_controls_pause_stale",
        },
    )
    assert stale_pause.status_code == 409

    final_attention = request(app, "GET", "/api/v1/workspace-attention").json()["workspaces"][0]
    assert final_attention["queued_write_count"] == 1
    assert any(
        ref["attention_kind"] == "pause_converging" for ref in final_attention["attention_refs"]
    )


def test_research_path_branch_is_a_replay_safe_product_command(tmp_path: Path) -> None:
    host = WorkspaceHost(tmp_path / "branch-host")
    app = create_app(host)
    created = command(
        app,
        {
            "schema_version": "1",
            "command_id": "cmd_branch_workspace",
            "workspace_id": "ws_branch_product",
            "command_type": "workspace.create",
            "payload": {},
            "preconditions": {},
        },
    )
    assert created.status_code == 200
    bootstrap = request(app, "GET", "/api/v1/workspaces/ws_branch_product/bootstrap").json()
    main_path = bootstrap["data"]["research_paths"][0]["research_path_id"]
    body = {
        "schema_version": "1",
        "command_id": "cmd_branch_create",
        "workspace_id": "ws_branch_product",
        "command_type": "research_path.branch",
        "payload": {
            "source_research_path_id": main_path,
            "conversation_id": None,
            "canonical_key": "branch.alternative",
            "display_name": "Alternative specification",
            "branch_reason": "Preserve the main direction while testing another specification",
            "expected_workspace_revision": bootstrap["authoritative_revision"],
        },
        "preconditions": {},
    }
    branched = command(app, body)
    assert branched.status_code == 200
    branch_ref = next(
        item
        for item in branched.json()["created_resource_refs"]
        if item["resource_type"] == "research_path"
    )
    assert branch_ref["resource_id"] != main_path
    replay = command(app, body)
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    after = request(app, "GET", "/api/v1/workspaces/ws_branch_product/bootstrap").json()
    assert len(after["data"]["research_paths"]) == 2
    assert after["data"]["execution"]["active_write_turn_id"] is None


def test_terminal_turn_accepts_optional_outcome_feedback_for_operational_evaluation(
    tmp_path: Path,
) -> None:
    host = WorkspaceHost(tmp_path / "feedback-host")
    app = create_app(host)
    assert command(
        app,
        {
            "schema_version": "1",
            "command_id": "cmd_feedback_workspace",
            "workspace_id": "ws_feedback_product",
            "command_type": "workspace.create",
            "payload": {},
            "preconditions": {},
        },
    ).status_code == 200
    bootstrap = request(app, "GET", "/api/v1/workspaces/ws_feedback_product/bootstrap").json()
    main_path = bootstrap["data"]["research_paths"][0]["research_path_id"]
    branched = command(
        app,
        {
            "schema_version": "1",
            "command_id": "cmd_feedback_branch",
            "workspace_id": "ws_feedback_product",
            "command_type": "research_path.branch",
            "payload": {
                "source_research_path_id": main_path,
                "conversation_id": None,
                "canonical_key": "branch.feedback",
                "display_name": "Feedback target",
                "branch_reason": "Create a terminal product Turn",
                "expected_workspace_revision": bootstrap["authoritative_revision"],
            },
            "preconditions": {},
        },
    )
    turn_id = branched.json()["turn_id"]

    feedback = request(
        app,
        "POST",
        f"/api/v1/workspaces/ws_feedback_product/turns/{turn_id}/outcome-feedback",
        json={
            "command_id": "cmd_feedback_record",
            "disposition": "accepted",
            "ratings": [{"dimension": "research_fit", "score": 4}],
            "issue_codes": [],
            "comment": "Useful result",
        },
    )

    assert feedback.status_code == 200
    assert feedback.json()["disposition"] == "accepted"
    evaluation = request(
        app,
        "GET",
        f"/api/v1/workspaces/ws_feedback_product/turns/{turn_id}/evaluation",
    ).json()["data"]
    metrics = {
        metric["metric_id"]: metric
        for layer in evaluation["layers"]
        for metric in layer["metrics"]
    }
    assert metrics["l3.product.explicit_user_acceptance_rate"]["value"] == 1.0
    assert evaluation["configuration"]["model_names"] == []
