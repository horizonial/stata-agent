from __future__ import annotations

import asyncio
import json

import stata_agent.ui as ui
from fastapi import HTTPException
from stata_agent.events.schema import (
    ACTOR_EVIDENCE,
    ACTOR_ORCH,
    ACTOR_VALIDATOR,
    EVENT_CARD_SIGNED,
    EVENT_CLAIM_SIGNED,
    EVENT_RUN_SUCCEEDED,
    EVENT_TOOL_DONE,
    EVENT_TOOL_INVOKED,
    EVENT_USER,
    Event,
)
from stata_agent.harness.agent_loop import run_loop
from stata_agent.storage.sqlite_store import SQLiteStore
from stata_agent.toolkit import Tool, ToolContext, ok


class _Provider:
    def __init__(self, responses):
        self.responses = list(responses)
        self.messages = []

    def chat(self, messages, tools=None):
        self.messages.append(messages)
        return self.responses.pop(0)


def _rows(response) -> list[dict]:
    async def collect():
        return [item.decode() if isinstance(item, bytes) else item async for item in response.body_iterator]

    payload = "".join(asyncio.run(collect()))
    return [json.loads(part.split("data: ", 1)[1]) for part in payload.strip().split("\n\n")]


def test_loop_assigns_request_scoped_ids_to_legacy_same_name_calls(tmp_path):
    store = SQLiteStore(str(tmp_path / "ledger.sqlite3"), writer_id="trace-test")
    seen = []
    ping = Tool(
        name="ping",
        description="ping",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=lambda _args, _ctx: seen.append(True) or ok({"pong": True}),
        permission="safe",
    )
    provider = _Provider(
        [
            {
                "content": None,
                "tool_calls": [
                    {"name": "ping", "arguments": {}},
                    {"name": "ping", "arguments": {}},
                ],
            },
            {"content": "done", "tool_calls": None},
        ]
    )
    events = []
    result = run_loop(
        store,
        provider,
        {"ping": ping},
        ToolContext(idea="ui", request_id="req-trace", store=store),
        user_text="ping",
        on_event=events.append,
    )

    assert result.reply == "done"
    started = [event for event in events if event["type"] == "tool_started"]
    completed = [event for event in events if event["type"] == "tool_completed"]
    assert [event["call_id"] for event in started] == ["req-trace:tool:1", "req-trace:tool:2"]
    assert [event["call_id"] for event in completed] == ["req-trace:tool:1", "req-trace:tool:2"]
    ledger = [event for event in store.scan("ui") if event.event_type in {EVENT_TOOL_INVOKED, EVENT_TOOL_DONE}]
    assert [event.payload["call_id"] for event in ledger] == [
        "req-trace:tool:1",
        "req-trace:tool:1",
        "req-trace:tool:2",
        "req-trace:tool:2",
    ]
    assistant = next(message for message in provider.messages[1] if message.get("role") == "assistant")
    assistant_calls = assistant["tool_calls"]
    assert [call["id"] for call in assistant_calls] == ["req-trace:tool:1", "req-trace:tool:2"]
    assert len(seen) == 2
    store.close()


def test_conversation_pairs_by_call_id_and_does_not_guess_legacy_interleaving():
    events = [
        Event(
            idea_id="ui",
            event_type=EVENT_TOOL_INVOKED,
            actor=ACTOR_ORCH,
            seq=1,
            correlation_id="req-1",
            payload={"tool": "ping", "call_id": "c1", "args": {"first": 1}},
        ),
        Event(
            idea_id="ui",
            event_type=EVENT_TOOL_INVOKED,
            actor=ACTOR_ORCH,
            seq=2,
            correlation_id="req-1",
            payload={"tool": "ping", "call_id": "c2", "args": {"second": 2}},
        ),
        Event(
            idea_id="ui",
            event_type=EVENT_TOOL_DONE,
            actor=ACTOR_ORCH,
            seq=3,
            correlation_id="req-1",
            payload={"tool": "ping", "call_id": "c2", "ok": False, "result": {"error": "拒绝：超出运行目录"}},
        ),
        Event(
            idea_id="ui",
            event_type=EVENT_TOOL_DONE,
            actor=ACTOR_ORCH,
            seq=4,
            correlation_id="req-1",
            payload={"tool": "ping", "call_id": "c1", "ok": True, "result": {}},
        ),
    ]
    messages = ui._conversation(events)
    cards = {item["call_id"]: item for item in messages if item["kind"] == "tool"}
    assert cards["c1"]["status"] == "succeeded"
    assert cards["c2"]["status"] == "failed"
    assert cards["c2"]["error"] == "拒绝：超出运行目录"
    assert cards["c1"]["args_keys"] == ["first"]
    assert cards["c2"]["args_keys"] == ["second"]

    legacy = ui._conversation(
        [
            Event(idea_id="ui", event_type=EVENT_TOOL_INVOKED, actor=ACTOR_ORCH, seq=10, payload={"tool": "ping"}),
            Event(idea_id="ui", event_type=EVENT_TOOL_INVOKED, actor=ACTOR_ORCH, seq=11, payload={"tool": "ping"}),
            Event(idea_id="ui", event_type=EVENT_TOOL_DONE, actor=ACTOR_ORCH, seq=12, payload={"tool": "ping", "ok": True, "result": {}}),
        ]
    )
    legacy_cards = [item for item in legacy if item["kind"] == "tool"]
    assert len(legacy_cards) == 3
    assert sum(item["status"] == "succeeded" for item in legacy_cards) == 1
    assert sum(item["status"] == "running" for item in legacy_cards) == 2


def test_sse_uses_explicit_call_id_for_interleaved_same_name_and_done_before_start(monkeypatch):
    monkeypatch.setattr(ui, "_resolve_workspace", lambda _ws: "ui")

    def fake_run(*_args, on_event=None, **_kwargs):
        on_event({"type": "tool_started", "tool": "ping", "call_id": "c1"})
        on_event({"type": "tool_started", "tool": "ping", "call_id": "c2"})
        on_event({"type": "tool_completed", "tool": "ping", "call_id": "c2", "ok": False})
        on_event({"type": "tool_completed", "tool": "ping", "call_id": "c1", "ok": True})
        return "done", None, {}

    monkeypatch.setattr(ui, "_run_chat_sync", fake_run)
    rows = _rows(asyncio.run(ui.chat_stream(ui.ChatIn(text="hello"))))
    started = [row for row in rows if row["type"] == "tool_started"]
    completed = [row for row in rows if row["type"] == "tool_completed"]
    assert [row["tool_id"] for row in started] == ["c1", "c2"]
    assert [row["tool_id"] for row in completed] == ["c2", "c1"]
    assert [row["call_id"] for row in completed] == ["c2", "c1"]
    assert completed[0]["error"]["code"] == "tool_failed"


def test_public_failure_and_attachment_projection_are_allowlisted():
    events = [
        Event(
            idea_id="ui",
            event_type=EVENT_USER,
            actor="user",
            seq=1,
            payload={
                "text": "请使用附件",
                "attachments": [
                    {
                        "attachment_id": "a1",
                        "display_name": "paper.pdf",
                        "source_role": "citable_evidence",
                        "detected_format": "pdf",
                        "page_count": 2,
                        "sha256": "SECRET_HASH",
                        "storage_key": "C:\\private\\paper.pdf",
                        "content": "SECRET_PDF_TEXT",
                    }
                ],
            },
        ),
        Event(
            idea_id="ui",
            event_type=EVENT_TOOL_DONE,
            actor=ACTOR_ORCH,
            seq=2,
            payload={
                "tool": "ping",
                "call_id": "c1",
                "ok": False,
                "result": {"error": {"type": "tool_error", "message": "SECRET_EXCEPTION"}},
            },
        ),
    ]
    conversation = ui._conversation(events)
    encoded = json.dumps(conversation, ensure_ascii=False)
    assert "SECRET_HASH" not in encoded
    assert "SECRET_PDF_TEXT" not in encoded
    assert "SECRET_EXCEPTION" not in encoded
    assert conversation[0]["attachments"] == [
        {
            "attachment_id": "a1",
            "display_name": "paper.pdf",
            "source_role": "citable_evidence",
            "detected_format": "pdf",
            "page_count": 2,
        }
    ]

    public = ui._public_event(events[-1])
    assert "SECRET_EXCEPTION" not in json.dumps(public, ensure_ascii=False)
    assert public["call_id"] == "c1"
    assert public["tool_id"] == "c1"


def test_run_conversation_distinguishes_execution_from_verified_evidence():
    base = {
        "idea_id": "ui",
        "event_type": EVENT_RUN_SUCCEEDED,
        "actor": ACTOR_ORCH,
        "source": ACTOR_ORCH,
    }
    empty = ui._conversation([Event(seq=1, payload={"run_id": "run-empty", "machine": {}}, **base)])
    assert empty[0]["text"] == "运行完成，仅确认执行成功，未生成结构化可验证结果。"
    structured = ui._conversation([Event(seq=2, payload={"run_id": "run-structured", "machine": {"N": 10}}, **base)])
    assert structured[0]["text"] == "运行完成，已获得结构化结果，尚未签入证据。"

    verified = ui._conversation([
        Event(seq=3, payload={"run_id": "run-verified", "machine": {"N": 10}}, **base),
        Event(
            idea_id="ui", event_type=EVENT_CARD_SIGNED, actor=ACTOR_VALIDATOR, source=ACTOR_VALIDATOR, seq=4,
            payload={"card": {"card_id": "card-run-verified-N", "locator": {"run_id": "run-verified"}}},
        ),
        Event(
            idea_id="ui", event_type=EVENT_CLAIM_SIGNED, actor=ACTOR_EVIDENCE, source=ACTOR_EVIDENCE, seq=5,
            payload={"claim": {"claim_id": "claim-run-verified", "cards": ["card-run-verified-N"]}},
        ),
    ])
    assert verified[0]["evidence_status"] == "claim_signed"
    assert verified[0]["text"] == "运行完成，已形成已验证研究结论。"


def test_http_failure_projection_does_not_echo_server_detail():
    response = asyncio.run(
        ui._http_error_handler(
            None,
            HTTPException(status_code=500, detail="SECRET_EXCEPTION C:\\private\\token.txt"),
        )
    )
    assert response.status_code == 500
    body = json.loads(response.body)
    assert body["error"]["code"] == "http_500"
    assert "SECRET_EXCEPTION" not in json.dumps(body, ensure_ascii=False)
    assert "token.txt" not in json.dumps(body, ensure_ascii=False)
