from __future__ import annotations

from stata_agent.application.trace_projection import TraceActivityQuery, TraceProjectionService
from stata_agent.harness.agent_loop import run_loop
from stata_agent.events.schema import (
    ACTOR_AGENT,
    ACTOR_ORCH,
    EVENT_AGENT_STEP,
    EVENT_CARD_SIGNED,
    EVENT_CLAIM_SIGNED,
    EVENT_CONTEXT_ASSEMBLED,
    EVENT_PROVIDER_TURN_COMPLETED,
    EVENT_PROVIDER_TURN_STARTED,
    EVENT_RUN_REQ,
    EVENT_RUN_SUCCEEDED,
    EVENT_TOOL_CALL,
    EVENT_TOOL_DONE,
    EVENT_TOOL_INVOKED,
    EVENT_USER,
    Event,
)
from stata_agent.storage.sqlite_store import SQLiteStore
from stata_agent.toolkit import ToolContext, default_tools
from stata_agent.tools.fake_executor import FakeExecutor, default_test_contract


def _event(seq: int, kind: str, correlation: str | None, payload: dict | None = None, **kwargs) -> Event:
    return Event(
        idea_id="ui",
        event_type=kind,
        actor=kwargs.pop("actor", ACTOR_ORCH),
        source=kwargs.pop("source", ACTOR_ORCH),
        seq=seq,
        created_at=100 + seq,
        correlation_id=correlation,
        payload=payload or {},
        **kwargs,
    )


def test_activity_groups_request_and_hides_opaque_ids_from_collapsed_labels() -> None:
    events = [
        _event(1, EVENT_USER, "corr-123", {"text": "重新来"}, actor="user", source="user"),
        _event(2, EVENT_CONTEXT_ASSEMBLED, "corr-123", {"estimated_tokens": 3187, "budget_tokens": 12000}),
        _event(3, EVENT_PROVIDER_TURN_STARTED, "corr-123", {"turn_index": 1, "attempt_index": 1, "tools_exposed": 2}),
        _event(4, EVENT_PROVIDER_TURN_COMPLETED, "corr-123", {"turn_index": 1, "attempt_index": 1, "response_kind": "tool_calls", "tool_call_count": 1}),
        _event(5, EVENT_TOOL_INVOKED, "corr-123", {"tool": "run_stata", "call_id": "corr-123:tool:1"}),
        _event(6, EVENT_RUN_REQ, "corr-123", {"run_id": "run-opaque"}, operation_id="op-opaque"),
        _event(7, EVENT_TOOL_CALL, "corr-123", {"run_id": "run-opaque", "call_id": "op-opaque:call:1", "code_head": "sysuse auto, clear"}, operation_id="op-opaque"),
        _event(8, "tool.result", "corr-123", {"run_id": "run-opaque", "call_id": "op-opaque:call:1", "rc": 0}, operation_id="op-opaque"),
        _event(9, EVENT_RUN_SUCCEEDED, "corr-123", {"run_id": "run-opaque", "machine": {}}, operation_id="op-opaque"),
        _event(10, EVENT_TOOL_DONE, "corr-123", {"tool": "run_stata", "call_id": "corr-123:tool:1", "ok": True}),
        _event(11, EVENT_PROVIDER_TURN_STARTED, "corr-123", {"turn_index": 2, "attempt_index": 1, "tools_exposed": 0, "finalization_only": True}),
        _event(12, EVENT_PROVIDER_TURN_COMPLETED, "corr-123", {"turn_index": 2, "attempt_index": 1, "response_kind": "text", "tool_call_count": 0}),
        _event(13, EVENT_AGENT_STEP, "corr-123", {"reply": "运行完成", "terminal_reason": "model_stop"}, actor=ACTOR_AGENT, source=ACTOR_AGENT),
    ]

    page = TraceProjectionService().project(events, workspace="ui")
    group = page.items[0]
    labels = [step.label for step in group.steps]
    assert page.total_groups == 1
    assert group.title == "重新来"
    assert group.counts["provider_turns"] == 2
    assert group.counts["tools"] == 1
    assert group.counts["runs"] == 1
    assert group.evidence_status == "executed_only"
    assert "模型回合 1" in labels and "Stata 运行 1" in labels and "已完成" in labels
    collapsed = " ".join(labels + [step.summary for step in group.steps])
    assert "corr-123:tool:1" not in collapsed
    assert "run-opaque" not in collapsed
    run_step = next(step for step in group.steps if step.kind == "run")
    assert run_step.children[0]["label"] == "载入示例数据"
    assert run_step.technical.as_dict()["run_id"] == "run-opaque"


def test_context_snapshots_are_deduplicated_and_machine_result_is_structured() -> None:
    events = [
        _event(1, EVENT_USER, "r1", {"text": "检查结果"}, actor="user", source="user"),
        _event(2, EVENT_CONTEXT_ASSEMBLED, "r1", {"estimated_tokens": 10, "budget_tokens": 100}),
        _event(3, EVENT_CONTEXT_ASSEMBLED, "r1", {"estimated_tokens": 10, "budget_tokens": 100}),
        _event(4, EVENT_RUN_REQ, "r1", {"run_id": "run-1"}, operation_id="op-1"),
        _event(5, EVENT_RUN_SUCCEEDED, "r1", {"run_id": "run-1", "machine": {"N": 74}}, operation_id="op-1"),
    ]
    group = TraceProjectionService().project(events).items[0]
    context_steps = [step for step in group.steps if step.kind == "context"]
    assert len(context_steps) == 1
    assert "2 次" in context_steps[0].summary
    assert group.evidence_status == "structured"
    assert group.context["peak_tokens"] == 10


def test_signed_card_is_verified_and_legacy_events_are_not_guessed() -> None:
    events = [
        _event(1, EVENT_USER, "r1", {"text": "a"}, actor="user", source="user"),
        _event(2, EVENT_CARD_SIGNED, "r1", {"card_id": "card-1"}),
        _event(3, EVENT_TOOL_INVOKED, None, {"tool": "ping", "call_id": "legacy-call"}),
    ]
    page = TraceProjectionService().project(events)
    assert page.legacy_uncorrelated_count == 1
    assert page.items[0].evidence_status == "verified"
    assert page.items[0].counts["evidence"] == 1


def test_signed_object_ids_and_orphan_ordinals_are_stable() -> None:
    events = [
        _event(1, EVENT_USER, "r1", {"text": "第一条"}, actor="user", source="user"),
        _event(2, EVENT_USER, "r2", {"text": "第二条"}, actor="user", source="user"),
        _event(3, EVENT_PROVIDER_TURN_STARTED, "orphan-a", {"turn_index": 1}),
        _event(4, EVENT_RUN_REQ, "orphan-b", {"run_id": "run-orphan"}),
        _event(5, EVENT_CARD_SIGNED, "r1", {"card": {"card_id": "card-r1"}}),
        _event(6, EVENT_CLAIM_SIGNED, "r1", {"claim": {"claim_id": "claim-r1"}}),
    ]
    page = TraceProjectionService().project(events)
    groups = {item.request_id: item for item in page.items}
    assert groups["r1"].display_ordinal == 1
    assert groups["r2"].display_ordinal == 2
    assert groups["orphan-a"].display_ordinal == 3
    assert groups["orphan-b"].display_ordinal == 4
    evidence = [step for step in groups["r1"].steps if step.kind == "evidence"]
    assert evidence[0].technical.as_dict()["card_id"] == "card-r1"
    assert evidence[1].technical.as_dict()["claim_id"] == "claim-r1"


def test_missing_internal_tool_call_id_is_not_paired_by_position() -> None:
    events = [
        _event(1, EVENT_USER, "r1", {"text": "检查"}, actor="user", source="user"),
        _event(2, EVENT_RUN_REQ, "r1", {"run_id": "run-1"}, operation_id="op-1"),
        _event(3, EVENT_TOOL_CALL, "r1", {"run_id": "run-1", "call_id": "c1", "code_head": "describe"}, operation_id="op-1"),
        _event(4, EVENT_TOOL_CALL, "r1", {"run_id": "run-1", "call_id": "c2", "code_head": "summarize"}, operation_id="op-1"),
        _event(5, "tool.result", "r1", {"run_id": "run-1", "rc": 0}, operation_id="op-1"),
    ]
    group = TraceProjectionService().project(events).items[0]
    run = next(step for step in group.steps if step.kind == "run")
    assert [child["status"] for child in run.children] == ["running", "running", "completed"]
    assert run.children[-1]["warning"] == "未找到可配对的内部调用。"


def test_zero_context_metrics_are_not_replaced_by_legacy_fallbacks() -> None:
    events = [
        _event(1, EVENT_USER, "r1", {"text": "检查"}, actor="user", source="user"),
        _event(2, EVENT_CONTEXT_ASSEMBLED, "r1", {
            "estimated_input_tokens": 0,
            "estimated_tokens": 99,
            "hard_limit_tokens": 0,
            "budget_tokens": 100,
        }),
    ]
    group = TraceProjectionService().project(events).items[0]
    assert group.context == {"peak_tokens": 0, "latest_tokens": 0, "budget_tokens": 0}
    assert group.steps[0].label == "上下文 0 / 0 tokens"


def test_context_deduplication_is_only_for_adjacent_snapshots() -> None:
    events = [
        _event(1, EVENT_USER, "r1", {"text": "检查"}, actor="user", source="user"),
        _event(2, EVENT_CONTEXT_ASSEMBLED, "r1", {"estimated_tokens": 10, "budget_tokens": 100}),
        _event(3, EVENT_CONTEXT_ASSEMBLED, "r1", {"estimated_tokens": 10, "budget_tokens": 100}),
        _event(4, EVENT_RUN_REQ, "r1", {"run_id": "run-1"}),
        _event(5, EVENT_CONTEXT_ASSEMBLED, "r1", {"estimated_tokens": 10, "budget_tokens": 100}),
    ]
    group = TraceProjectionService().project(events).items[0]
    context_steps = [step for step in group.steps if step.kind == "context"]
    assert len(context_steps) == 2
    assert "2 次" in context_steps[0].summary


def test_activity_filter_and_group_cursor_are_server_projection_semantics() -> None:
    events = [
        _event(1, EVENT_USER, "old", {"text": "旧请求"}, actor="user", source="user"),
        _event(2, EVENT_AGENT_STEP, "old", {"terminal_reason": "model_stop"}, actor=ACTOR_AGENT, source=ACTOR_AGENT),
        _event(3, EVENT_USER, "new", {"text": "Stata 回归"}, actor="user", source="user"),
        _event(4, EVENT_RUN_REQ, "new", {"run_id": "run-2"}, operation_id="op-2"),
    ]
    service = TraceProjectionService()
    newest = service.project(events, query=TraceActivityQuery(limit=1))
    assert newest.items[0].title == "Stata 回归"
    assert newest.next_before_seq == 3
    older = service.project(events, query=TraceActivityQuery(limit=1, before_seq=newest.next_before_seq))
    assert older.items[0].title == "旧请求"
    assert service.project(events, query=TraceActivityQuery(category="run")).total_groups == 1
    assert service.project(events, query=TraceActivityQuery(search="回归")).total_groups == 1


def test_activity_titles_redact_secret_values_and_local_paths() -> None:
    events = [
        _event(
            1,
            EVENT_USER,
            "r1",
            {"text": "检查 api_key=TOP_SECRET_VALUE C:\\private\\dataset.dta"},
            actor="user",
            source="user",
        ),
    ]
    encoded = str(TraceProjectionService().project(events).as_dict())
    assert "TOP_SECRET_VALUE" not in encoded
    assert "dataset.dta" not in encoded


def test_assistant_evidence_claim_does_not_upgrade_activity_truth() -> None:
    events = [
        _event(1, EVENT_USER, "r1", {"text": "检查结果"}, actor="user", source="user"),
        _event(2, EVENT_RUN_REQ, "r1", {"run_id": "run-1"}, operation_id="op-1"),
        _event(3, EVENT_RUN_SUCCEEDED, "r1", {"run_id": "run-1", "machine": {}}, operation_id="op-1"),
        _event(4, EVENT_AGENT_STEP, "r1", {"reply": "结果已签入证据链。", "terminal_reason": "model_stop"}, actor=ACTOR_AGENT, source=ACTOR_AGENT),
    ]
    group = TraceProjectionService().project(events).items[0]
    terminal = next(step for step in group.steps if step.kind == "system")
    assert group.evidence_status == "executed_only"
    assert "已签入证据链" not in terminal.summary
    assert "实际签名事件" in terminal.summary


def test_activity_projection_tracks_real_tool_roundtrip_and_verified_cards(tmp_path) -> None:
    class Provider:
        provider = "fixture"
        is_remote = False

        def __init__(self) -> None:
            self.calls = 0

        def chat(self, _messages, tools=None):
            self.calls += 1
            if self.calls == 1:
                return {
                    "content": None,
                    "tool_calls": [{
                        "id": "fixture-call",
                        "name": "run_stata",
                        "arguments": {
                            "code": "sysuse auto, clear",
                            "result_contract": default_test_contract().model_dump(),
                        },
                    }],
                }
            return {"content": "运行结果已整理。", "tool_calls": None}

    store = SQLiteStore(str(tmp_path / "trace-e2e.sqlite3"), writer_id="trace-e2e")
    try:
        result = run_loop(
            store,
            Provider(),
            default_tools(),
            ToolContext(idea="ui", request_id="trace-e2e-request", store=store, executor=FakeExecutor(store)),
            user_text="运行一次 Stata 主回归",
        )
        assert result.reply == "运行结果已整理。"
        page = TraceProjectionService().project(store.scan("ui"), workspace="ui")
        group = page.items[0]
        assert page.legacy_uncorrelated_count == 0
        assert group.counts == {"provider_turns": 2, "tools": 1, "runs": 1, "evidence": 4, "warnings": 0}
        assert group.evidence_status == "verified"
        labels = [step.label for step in group.steps]
        assert "模型回合 1" in labels and "模型回合 2" in labels
        assert labels.index("模型回合 1") < labels.index("Stata 运行 1") < labels.index("模型回合 2")
        assert labels[-1] == "已完成"
        run_step = next(step for step in group.steps if step.kind == "run")
        assert run_step.children and run_step.children[0]["label"] == "执行 Stata 命令"
    finally:
        store.close()
