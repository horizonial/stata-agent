"""P0-A runtime-control regressions: cooperative cancellation and closure."""

from __future__ import annotations

import threading
import time

import pytest

from stata_agent.events.schema import ACTOR_AGENT, EVENT_AGENT_STEP, EVENT_IDEA, Event
from stata_agent.harness.agent_loop import run_loop
from stata_agent.harness.cancellation import CancellationToken
from stata_agent.harness.tool_enforcer import ToolEnforcer
from stata_agent.storage.sqlite_store import SQLiteStore
from stata_agent.toolkit import Tool, ToolContext, ok
from stata_agent.tools.executor import StataCancelledError, StataExecutor


def _store(tmp_path):
    store = SQLiteStore(str(tmp_path / "ledger.db"), writer_id="runtime-test")
    store.append(
        Event(
            idea_id="i1",
            event_type=EVENT_IDEA,
            actor=ACTOR_AGENT,
            source=ACTOR_AGENT,
            payload={"question": "runtime"},
        )
    )
    return store


def test_cancellation_token_is_thread_safe_and_idempotent():
    token = CancellationToken()
    barrier = threading.Barrier(12)
    first = []

    def request() -> None:
        barrier.wait()
        first.append(token.cancel("user-stop"))

    threads = [threading.Thread(target=request) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert sum(first) == 1
    assert token.cancel_count == 12
    assert token.is_cancelled and token.is_set()
    assert token.reason == "user-stop"
    assert token.state == CancellationToken.CANCEL_REQUESTED
    assert token.acknowledge() is True
    assert token.state == CancellationToken.CANCELLED
    assert token.acknowledge() is True


def test_enforcer_wait_returns_early_on_cancel_without_killing_worker():
    token = CancellationToken()
    started = threading.Event()
    tool = Tool(
        name="cooperative",
        description="cooperative",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=lambda _args, ctx: _cooperative_handler(ctx, started),
        permission="safe",
        timeout_seconds=2,
    )

    def cancel() -> None:
        assert started.wait(1)
        token.cancel("timeout-by-user")

    thread = threading.Thread(target=cancel)
    thread.start()
    begin = time.monotonic()
    result = ToolEnforcer({tool.name: tool}).execute(
        tool.name, {}, ToolContext(cancellation=token)
    )
    elapsed = time.monotonic() - begin
    thread.join(timeout=2)

    assert elapsed < 1
    assert result["ok"] is False
    assert result["error"]["type"] == "cancelled"
    assert result["error"]["cancel_requested"] is True


def _cooperative_handler(ctx, started):
    started.set()
    while not ctx.cancellation_requested():
        time.sleep(0.005)
    return ok({"finished": True})


def test_agent_loop_checks_cancel_between_provider_steps(tmp_path):
    store = _store(tmp_path)
    token = CancellationToken()

    class Provider:
        def __init__(self):
            self.calls = 0

        def chat(self, _messages, tools=None):
            self.calls += 1
            token.cancel("stop-between-steps")
            return {"content": "should not continue", "tool_calls": None}

    result = run_loop(
        store,
        Provider(),
        {},
        ToolContext(idea="i1", store=store, cancellation=token),
        user_text="stop",
    )

    assert result.cancelled is True
    assert result.terminal_reason == "cancelled"
    terminal = [event for event in store.scan("i1") if event.event_type == EVENT_AGENT_STEP][-1]
    assert terminal.payload["terminal_reason"] == "cancelled"
    assert terminal.payload["cancel_requested"] is True
    store.close()


def test_agent_loop_checks_cancel_before_next_tool(tmp_path):
    store = _store(tmp_path)
    token = CancellationToken()
    seen = []
    ping = Tool(
        name="ping",
        description="ping",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=lambda _args, _ctx: seen.append(True) or ok({"pong": True}),
        permission="safe",
    )

    class Provider:
        def chat(self, _messages, tools=None):
            if not seen:
                return {
                    "content": None,
                    "tool_calls": [
                        {"id": "one", "name": "ping", "arguments": {}},
                        {"id": "two", "name": "ping", "arguments": {}},
                    ],
                }
            return {"content": "done", "tool_calls": None}

    def stop_after_first(_args, _ctx):
        seen.append(True)
        token.cancel("stop-before-second-tool")
        return ok({"pong": True})

    ping.handler = stop_after_first
    result = run_loop(
        store,
        Provider(),
        {"ping": ping},
        ToolContext(idea="i1", store=store, cancellation=token),
        user_text="two pings",
    )

    assert result.cancelled is True
    assert result.tool_calls == 1
    assert len(seen) == 1
    assert result.terminal_reason == "cancelled"
    store.close()


def test_executor_cancel_before_external_call_is_failed_and_audited(monkeypatch, tmp_path):
    store = _store(tmp_path)
    token = CancellationToken()

    class CancelAtInit:
        closed = False

        def __init__(self):
            token.cancel("cancel-before-submit")

        def call(self, _code, cancellation=None):
            raise AssertionError("external call must not start")

        def close(self):
            self.closed = True

    monkeypatch.setattr("stata_agent.tools.executor.StataSession", CancelAtInit)
    executor = StataExecutor(store, run_root=tmp_path / "runs")

    with pytest.raises(StataCancelledError) as caught:
        executor.execute("display 1", idea="i1", cancellation=token)
    assert caught.value.uncertain is False

    events = list(store.scan("i1"))
    assert [event.event_type for event in events] == [
        "idea.declared",
        "run.requested",
        "tool.call",
        "tool.result",
        "run.failed",
    ]
    terminal = events[-1]
    assert terminal.side_effect_state == "cancelled"
    assert terminal.payload["terminal_reason"] == "cancel_requested"
    assert terminal.payload["cancel_requested"] is True
    store.close()


def test_executor_cancel_in_flight_is_uncertain_and_closes_session(monkeypatch, tmp_path):
    store = _store(tmp_path)
    token = CancellationToken()
    started = threading.Event()
    holder = {}

    class BlockingSession:
        def __init__(self):
            self.closed = False
            holder["session"] = self

        def call(self, _code, cancellation=None):
            started.set()
            while not token.is_cancelled:
                time.sleep(0.005)
            raise StataCancelledError("user-stop", uncertain=True)

        def close(self):
            self.closed = True

    monkeypatch.setattr("stata_agent.tools.executor.StataSession", BlockingSession)
    executor = StataExecutor(store, run_root=tmp_path / "runs")

    def request():
        assert started.wait(1)
        token.cancel("user-stop")

    canceller = threading.Thread(target=request)
    canceller.start()
    with pytest.raises(StataCancelledError) as caught:
        executor.execute("display 1", idea="i1", cancellation=token)
    canceller.join(timeout=2)

    assert caught.value.uncertain is True
    assert holder["session"].closed is True
    terminal = list(store.scan("i1"))[-1]
    assert terminal.event_type == "run.uncertain"
    assert terminal.side_effect_state == "uncertain"
    assert terminal.payload["terminal_reason"] == "cancel_requested"
    assert terminal.payload["provenance"]["attested"] is False
    # Both executor and transport cleanup are safe to repeat.
    executor.close()
    executor.close()
    store.close()


def test_executor_close_swallows_cleanup_error_and_is_repeatable(tmp_path):
    store = _store(tmp_path)
    executor = StataExecutor(store, run_root=tmp_path / "runs")

    class FlakyClose:
        calls = 0

        def close(self):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("already closed")

    session = FlakyClose()
    executor._session = session
    executor.close()
    executor.close()
    assert session.calls == 1
    store.close()
