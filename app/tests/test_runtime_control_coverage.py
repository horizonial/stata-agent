"""Deterministic high-value coverage for runtime cancellation boundaries.

No test in this module starts Stata or opens a network connection.  Transport
tests replace the MCP async context with in-memory fakes.
"""

from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

from stata_agent.events.schema import ACTOR_AGENT, EVENT_IDEA, Event
from stata_agent.harness.agent_loop import run_loop
from stata_agent.harness.cancellation import (
    CancellationRequested,
    CancellationToken,
    cancellation_reason,
    is_cancel_requested,
    raise_if_cancelled,
)
from stata_agent.harness.tool_enforcer import ToolEnforcer
from stata_agent.storage.sqlite_store import SQLiteStore
from stata_agent.toolkit import Tool, ToolContext, ok
from stata_agent.tools import stata_client
from stata_agent.tools.executor import StataCancelledError, StataExecutor
from stata_agent.tools.stata_client import CallResult


def _store(tmp_path, name="ledger.db"):
    store = SQLiteStore(str(tmp_path / name), writer_id="runtime-coverage")
    store.append(
        Event(
            idea_id="i1",
            event_type=EVENT_IDEA,
            actor=ACTOR_AGENT,
            source=ACTOR_AGENT,
            payload={"question": "coverage"},
        )
    )
    return store


def _safe_tool(name="ping", handler=None, *, permission="safe", timeout=1):
    return Tool(
        name=name,
        description=name,
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=handler or (lambda _args, _ctx: ok({"pong": True})),
        permission=permission,
        timeout_seconds=timeout,
    )


def test_cancellation_adapters_and_state_edges():
    token = CancellationToken()
    assert token.acknowledge() is False
    assert token.wait(0) is False
    assert is_cancel_requested(None) is False
    assert cancellation_reason(None) == "cancelled"
    assert token.request_cancel("first") is True
    assert token.cancel("second") is False
    assert token.cancel_count == 2
    assert token.cancelled and token.requested and token.cancel_requested
    assert token.snapshot() == {
        "state": CancellationToken.CANCEL_REQUESTED,
        "reason": "first",
        "cancel_count": 2,
        "requested": True,
    }
    with pytest.raises(CancellationRequested, match="first"):
        token.throw_if_requested()
    assert token.acknowledge() is True

    class LegacyEvent:
        def is_set(self):
            return True

        def reason(self):
            return "legacy-stop"

    legacy = LegacyEvent()
    assert is_cancel_requested(legacy) is True
    assert cancellation_reason(legacy) == "legacy-stop"
    with pytest.raises(CancellationRequested, match="legacy-stop"):
        raise_if_cancelled(legacy)

    class BrokenFlags:
        @property
        def is_set(self):
            raise RuntimeError("broken flag")

    assert is_cancel_requested(BrokenFlags()) is False

    class BrokenReason:
        is_set = True

        @property
        def reason(self):
            raise RuntimeError("broken reason")

    assert cancellation_reason(BrokenReason(), default="fallback") == "fallback"
    raise_if_cancelled(None)


def test_loop_stream_stop_and_abnormal_streams(tmp_path):
    store = _store(tmp_path)
    seen = []

    class StreamProvider:
        def chat(self, _messages, tools=None):
            raise AssertionError("stream path should not call chat")

        def stream_chat(self, _messages, tools=None):
            yield {"type": "text_delta", "text": "hello"}
            yield {"type": "done", "content": "done", "tool_calls": None}

    result = run_loop(
        store,
        StreamProvider(),
        {},
        ToolContext(idea="i1", store=store),
        user_text="hello",
        on_event=seen.append,
    )
    assert result.reply == "done" and seen == [{"type": "text_delta", "text": "hello"}]
    store.close()

    for index, (stream_events, expected) in enumerate((
        ([{"type": "text_delta", "text": "partial"}], "provider_error"),
        ([{"type": "error", "message": "upstream"}], "provider_error"),
        (["not-an-object"], "provider_error"),
    )):
        store = _store(tmp_path, f"{expected}-{index}.db")

        class BadStreamProvider:
            def chat(self, _messages, tools=None):
                raise AssertionError("stream path should not call chat")

            def stream_chat(self, _messages, tools=None):
                yield from stream_events

        result = run_loop(
            store,
            BadStreamProvider(),
            {},
            ToolContext(idea="i1", store=store),
            user_text="bad stream",
            on_event=lambda _event: None,
        )
        assert result.terminal_reason == expected
        store.close()


def test_loop_stream_cancellation_after_partial_output(tmp_path):
    store = _store(tmp_path)
    token = CancellationToken()
    deltas = []

    class Provider:
        def chat(self, _messages, tools=None):
            raise AssertionError("stream path should be selected")

        def stream_chat(self, _messages, tools=None):
            yield {"type": "text_delta", "text": "partial"}
            token.cancel("disconnect")
            # The loop checks before consuming this event, so cancellation is
            # observed even though the provider has not emitted done.
            yield {"type": "done", "content": "late", "tool_calls": None}

    result = run_loop(
        store,
        Provider(),
        {},
        ToolContext(idea="i1", store=store, cancellation=token),
        user_text="cancel stream",
        on_event=lambda event: deltas.append(event),
    )
    assert result.cancelled and result.terminal_reason == "cancelled"
    assert deltas == [{"type": "text_delta", "text": "partial"}]
    store.close()


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"max_steps": "bad"}, "budget_invalid"),
        ({"max_steps": 0}, "max_steps"),
        ({"max_tool_calls": 0}, "tool_calls"),
    ],
)
def test_loop_budget_and_provider_contract_edges(tmp_path, kwargs, reason):
    store = _store(tmp_path, f"budget-{reason}.db")

    class Provider:
        def chat(self, _messages, tools=None):
            return {"content": None, "tool_calls": [{"name": "ping", "arguments": {}}]}

    tool = _safe_tool()
    result = run_loop(
        store,
        Provider(),
        {"ping": tool},
        ToolContext(idea="i1", store=store),
        user_text="budget",
        **kwargs,
    )
    assert result.terminal_reason == reason
    store.close()


def test_loop_invalid_provider_response_and_duplicate_run_guard(tmp_path):
    for index, response in enumerate(("not-object", {"content": None, "tool_calls": {"bad": True}})):
        store = _store(tmp_path, f"invalid-provider-{index}.db")

        class Provider:
            def chat(self, _messages, tools=None):
                return response

        result = run_loop(
            store,
            Provider(),
            {},
            ToolContext(idea="i1", store=store),
            user_text="bad provider",
        )
        assert result.terminal_reason in {"provider_error", "invalid_tool_calls"}
        store.close()

    store = _store(tmp_path, "duplicate-run.db")
    responses = [
        {"content": None, "tool_calls": [{"name": "run_stata", "arguments": {"code": "same"}}]},
        {"content": None, "tool_calls": [{"name": "run_stata", "arguments": {"code": "same"}}]},
        {"content": None, "tool_calls": [{"name": "run_stata", "arguments": {"code": "same"}}]},
    ]

    class Provider:
        def chat(self, _messages, tools=None):
            return responses.pop(0)

    run_tool = _safe_tool("run_stata", permission="execute")
    result = run_loop(
        store,
        Provider(),
        {"run_stata": run_tool},
        ToolContext(idea="i1", store=store),
        user_text="repeat",
        # Three duplicate attempts plus the reserved result-finalization turn.
        max_steps=4,
    )
    assert result.terminal_reason == "duplicate_run_blocked"
    assert result.tool_calls == 2
    store.close()


def test_enforcer_exception_shapes_and_cancelled_side_effect_wait():
    enforcer = ToolEnforcer(
        {
            "raises": _safe_tool("raises", handler=lambda _a, _c: 1 / 0),
            "nondict": _safe_tool("nondict", handler=lambda _a, _c: "not-object"),
            "bad-timeout": _safe_tool("bad-timeout", timeout="bad"),
            "negative-timeout": _safe_tool("negative-timeout", timeout=-1),
        }
    )
    assert enforcer.execute("raises", {}, ToolContext())["error"]["type"] == "tool_error"
    assert enforcer.execute("nondict", {}, ToolContext())["error"]["type"] == "tool_error"
    assert enforcer.execute("bad-timeout", {}, ToolContext())["error"]["type"] == "timeout"
    assert enforcer.execute("negative-timeout", {}, ToolContext())["error"]["type"] == "timeout"

    token = CancellationToken()
    started = threading.Event()

    def ignores_cancel(_args, _ctx):
        started.set()
        while not token.is_cancelled:
            time.sleep(0.005)
        return ok({"late": True})

    execute_tool = _safe_tool("external", handler=ignores_cancel, permission="execute", timeout=2)
    enforcer.tools[execute_tool.name] = execute_tool

    def stop():
        assert started.wait(1)
        token.cancel("user-stop")

    stopper = threading.Thread(target=stop)
    stopper.start()
    result = enforcer.execute("external", {}, ToolContext(cancellation=token))
    stopper.join(timeout=1)
    assert result["error"]["type"] == "uncertain"
    assert result["error"]["uncertain"] is True

    alias_ctx = SimpleNamespace(
        cancellation=None,
        cancel_token=CancellationToken(),
        cancellation_token=None,
        privacy_mode="local_strict",
        phase=None,
        store=None,
    )
    alias_ctx.cancel_token.cancel("alias")
    assert enforcer.execute("raises", {}, alias_ctx)["error"]["type"] == "cancelled"


def test_enforcer_store_thread_exception_path():
    from stata_agent.toolkit import default_tools

    tool = default_tools()["verify_result"]
    result = ToolEnforcer({tool.name: tool}).execute(
        tool.name,
        {"run_id": "missing"},
        ToolContext(store=object()),
    )
    assert result["ok"] is False and result["error"]["type"] == "tool_error"


_MARKERS = "MACHINE_N= 3\nMACHINE_R2= 0.25\nSTA_ENV version= 18\nSTA_ENV flavor= IC"


class _SuccessSession:
    instances = []

    def __init__(self):
        self.closed = False
        self.calls = []
        self.instances.append(self)

    def call(self, code, cancellation=None):
        self.calls.append(code)
        return CallResult(text=_MARKERS)

    def close(self):
        self.closed = True


def test_executor_success_failure_parse_and_pre_cancel(monkeypatch, tmp_path):
    store = _store(tmp_path, "executor-success.db")
    monkeypatch.setattr("stata_agent.tools.executor.StataSession", _SuccessSession)
    executor = StataExecutor(store, run_root=tmp_path / "runs")
    output = executor.execute("display 1", idea="i1")
    assert output["machine"] == {"N": 3, "r2": 0.25}
    assert store.project("i1").runs[output["run_id"]].status == "succeeded"
    assert _SuccessSession.instances[-1].closed is True
    executor.close()
    store.close()

    token = CancellationToken()
    store = _store(tmp_path, "executor-pre-cancel.db")
    executor = StataExecutor(store, run_root=tmp_path / "runs-pre")
    token.cancel("before-request")
    with pytest.raises(StataCancelledError) as caught:
        executor.execute("display 1", idea="i1", cancellation=token)
    assert caught.value.uncertain is False
    assert [event.event_type for event in store.scan("i1")] == [EVENT_IDEA]
    store.close()


def test_executor_cancel_after_result_is_uncertain(monkeypatch, tmp_path):
    store = _store(tmp_path, "executor-cancel-after.db")
    token = CancellationToken()

    class CancelAfterResult(_SuccessSession):
        def call(self, code, cancellation=None):
            result = super().call(code, cancellation=cancellation)
            token.cancel("after-submit")
            return result

    monkeypatch.setattr("stata_agent.tools.executor.StataSession", CancelAfterResult)
    executor = StataExecutor(store, run_root=tmp_path / "runs-after")
    with pytest.raises(StataCancelledError) as caught:
        executor.execute("display 1", idea="i1", cancellation=token)
    assert caught.value.uncertain is True
    events = list(store.scan("i1"))
    assert [event.event_type for event in events] == [
        EVENT_IDEA,
        "run.requested",
        "tool.call",
        "tool.result",
        "run.uncertain",
    ]
    assert events[-1].payload["terminal_reason"] == "cancel_requested"
    store.close()


def test_executor_error_and_unexpected_error_get_terminal(monkeypatch, tmp_path):
    class ErrorSession(_SuccessSession):
        def call(self, code, cancellation=None):
            return CallResult(text="rc 9", is_error=True)

    store = _store(tmp_path, "executor-error.db")
    monkeypatch.setattr("stata_agent.tools.executor.StataSession", ErrorSession)
    executor = StataExecutor(store, run_root=tmp_path / "runs-error")
    with pytest.raises(Exception):
        executor.execute("display 1", idea="i1")
    assert list(store.scan("i1"))[-1].event_type == "run.failed"
    store.close()

    store = _store(tmp_path, "executor-unexpected.db")
    monkeypatch.setattr("stata_agent.tools.executor.StataSession", _SuccessSession)
    monkeypatch.setattr(
        StataExecutor,
        "parse_machine",
        staticmethod(lambda _text: (_ for _ in ()).throw(ValueError("parser bug"))),
    )
    executor = StataExecutor(store, run_root=tmp_path / "runs-unexpected")
    with pytest.raises(ValueError, match="parser bug"):
        executor.execute("display 1", idea="i1")
    terminal = list(store.scan("i1"))[-1]
    assert terminal.event_type == "run.uncertain"
    assert terminal.payload["terminal_reason"] == "executor_error"
    store.close()


class _AsyncContext:
    def __init__(self, value):
        self.value = value
        self.entered = 0
        self.exited = 0

    async def __aenter__(self):
        self.entered += 1
        return self.value

    async def __aexit__(self, *_args):
        self.exited += 1
        return False


class _FakeMcpSession:
    response = None
    delay = 0
    calls = 0

    def __init__(self, _read, _write):
        self.ctx = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def initialize(self):
        return None

    async def call_tool(self, _tool, _args):
        type(self).calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


class _Response:
    is_error = False

    def __init__(self, text):
        self.content = [SimpleNamespace(type="text", text=text)]


def _patch_mcp(monkeypatch, response, *, delay=0):
    _FakeMcpSession.response = response
    _FakeMcpSession.delay = delay
    monkeypatch.setattr(stata_client, "stdio_client", lambda _params: _AsyncContext(("r", "w")))
    monkeypatch.setattr(stata_client, "ClientSession", _FakeMcpSession)


def test_stata_client_call_success_cancel_timeout_and_close(monkeypatch):
    _patch_mcp(monkeypatch, _Response('{"rc": 0}'))
    result = asyncio.run(stata_client._call("display 1", timeout=1))
    assert result.rc == 0 and result.text == '{"rc": 0}'

    pre = CancellationToken()
    pre.cancel("before-connect")
    with pytest.raises(StataCancelledError) as caught:
        asyncio.run(stata_client._call("display 1", cancellation=pre))
    assert caught.value.uncertain is False

    inflight = CancellationToken()
    _patch_mcp(monkeypatch, _Response('{"rc": 0}'), delay=2)

    async def cancel_inflight():
        task = asyncio.create_task(stata_client._call("display 1", timeout=5, cancellation=inflight))
        await asyncio.sleep(0.02)
        inflight.cancel("disconnect")
        with pytest.raises(StataCancelledError) as caught:
            await task
        assert caught.value.uncertain is True

    asyncio.run(cancel_inflight())

    _patch_mcp(monkeypatch, _Response('{"rc": 0}'), delay=2)
    with pytest.raises(TimeoutError):
        asyncio.run(stata_client._call("display 1", timeout=0.02))

    # Exercise the no-loop/no-child close path; production close is tested by
    # executor tests without starting a real stdio process.
    session = object.__new__(stata_client.StataSession)
    session._state_lock = threading.RLock()
    session._loop = None
    session._closed_fut = None
    session._active = set()
    session._closing = False
    session._closed = False
    session._thread = threading.current_thread()
    session.close()
    session.close()
    assert session._closing is True


def test_stata_session_cancelled_ready_and_batch_edges():
    token = CancellationToken()
    token.cancel("before-call")
    session = object.__new__(stata_client.StataSession)
    with pytest.raises(StataCancelledError) as caught:
        session.call("display 1", cancellation=token)
    assert caught.value.uncertain is False

    token = CancellationToken()
    session = object.__new__(stata_client.StataSession)
    session.call = lambda _code, cancellation=None: token.cancel("after-one") or CallResult()
    with pytest.raises(StataCancelledError) as caught:
        session.run_batch(["one", "two"], cancellation=token)
    assert caught.value.uncertain is False
