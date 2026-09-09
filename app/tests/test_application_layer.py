from __future__ import annotations

import threading
from dataclasses import dataclass

import pytest

from stata_agent.application import (
    ChatService,
    ChatTurnRequest,
    RequestControlNotFound,
    RequestControlRegistry,
)
from stata_agent.harness.agent_loop import LoopResult
from stata_agent.toolkit import ToolContext


@dataclass
class _Store:
    events: list = None
    closed: bool = False

    def __post_init__(self) -> None:
        self.events = [] if self.events is None else self.events

    def append(self, event):
        event.seq = len(self.events) + 1
        self.events.append(event)
        return event.seq

    def scan(self, idea_id, *args, **kwargs):
        return (event for event in self.events if event.idea_id == idea_id)

    def project(self, idea_id):
        return None

    def close(self) -> None:
        self.closed = True


class _Executor:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_request_control_registry_is_scoped_idempotent_and_terminal_wins() -> None:
    now = [100.0]
    registry = RequestControlRegistry(ttl_seconds=10, clock=lambda: now[0])
    cancel_event = threading.Event()

    registered = registry.register("req-1", "workspace-a", cancel_event)
    assert registered["status"] == "running"
    assert "cancel_event" not in registered

    with pytest.raises(RequestControlNotFound):
        registry.cancel("req-1", "workspace-b")
    assert not cancel_event.is_set()

    first = registry.cancel("req-1", "workspace-a", reason="user")
    second = registry.cancel("req-1", "workspace-a", reason="late-retry")
    assert first == second
    assert first["status"] == "cancelling"
    assert first["cancel_reason"] == "user"
    assert cancel_event.is_set()

    terminal = registry.finish("req-1", status="cancelled")
    assert terminal and terminal["status"] == "cancelled"
    assert registry.disconnect("req-1") == terminal
    assert registry.finish("req-1", status="failed") == terminal
    assert registry.latest_active("workspace-a") is None

    now[0] = 111.0
    assert registry.prune() == 1
    assert registry.snapshot("req-1") is None


def test_request_control_registry_disconnect_and_latest_active_are_thread_safe() -> None:
    registry = RequestControlRegistry()
    events = [threading.Event() for _ in range(8)]
    for index, event in enumerate(events):
        registry.register(f"req-{index}", "workspace", event)

    errors: list[BaseException] = []

    def cancel(index: int) -> None:
        try:
            if index % 2:
                registry.disconnect(f"req-{index}")
            else:
                registry.cancel(f"req-{index}", "workspace")
        except BaseException as error:  # pragma: no cover - diagnostic guard
            errors.append(error)

    threads = [threading.Thread(target=cancel, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert registry.latest_active("workspace")["status"] == "cancelling"
    assert all(event.is_set() for event in events)


def test_chat_service_runs_one_turn_and_closes_resources() -> None:
    store = _Store()
    executor = _Executor()
    calls: dict[str, object] = {}

    def run_loop(store_arg, provider, tools, context, **kwargs):
        calls.update({"store": store_arg, "provider": provider, "tools": tools, "context": context})
        calls.update(kwargs)
        return LoopResult(reply="answer", tool_calls=2, terminal_reason="model_stop")

    service = ChatService(
        store_factory=lambda: store,
        provider_factory=lambda: "provider",
        executor_factory=lambda _store: executor,
        context_factory=lambda **kwargs: ToolContext(
            idea=kwargs["request"].idea,
            store=kwargs["store"],
            executor=kwargs["executor"],
            cancellation=kwargs["cancellation"],
        ),
        tools_factory=lambda: {},
        loop_runner=run_loop,
        bootstrapper=lambda _store, _idea, _text: None,
    )

    result = service.run(ChatTurnRequest(idea="w1", text="hello", request_id="req-1"))

    assert result.request_id == "req-1"
    assert result.reply == "answer"
    assert result.ask is None
    assert result.tool_calls == 2
    assert result.terminal_reason == "model_stop"
    assert store.events and store.events[0].event_type == "user.message"
    assert store.events[0].payload == {"text": "hello", "request_id": "req-1"}
    assert calls["max_steps"] == 1
    assert calls["max_tool_calls"] == 32
    assert calls["store"] is not store
    assert list(calls["store"].scan("w1")) == []
    assert calls["context"].store is store
    assert executor.closed
    assert store.closed


def test_chat_service_goal_mode_and_exception_still_close_resources() -> None:
    store = _Store()
    executor = _Executor()
    captured: dict[str, object] = {}

    def failing_loop(*_args, **kwargs):
        captured.update(kwargs)
        raise RuntimeError("provider down")

    service = ChatService(
        store_factory=lambda: store,
        provider_factory=lambda: object(),
        executor_factory=lambda _store: executor,
        context_factory=lambda **kwargs: ToolContext(store=kwargs["store"], executor=kwargs["executor"]),
        tools_factory=lambda: {},
        loop_runner=failing_loop,
        bootstrapper=lambda _store, _idea, _text: None,
        goal_max_steps=7,
        max_tool_calls=9,
    )

    with pytest.raises(RuntimeError, match="provider down"):
        service.run(ChatTurnRequest(idea="w1", text="run", mode="goal"))

    assert captured["max_steps"] == 7
    assert captured["max_tool_calls"] == 9
    assert executor.closed
    assert store.closed


def test_chat_service_pre_cancel_closes_store_without_constructing_runtime() -> None:
    store = _Store()
    cancellation = threading.Event()
    cancellation.set()
    provider_called = False
    executor_called = False

    def provider_factory():
        nonlocal provider_called
        provider_called = True
        return object()

    def executor_factory(_store):
        nonlocal executor_called
        executor_called = True
        return _Executor()

    service = ChatService(
        store_factory=lambda: store,
        provider_factory=provider_factory,
        executor_factory=executor_factory,
        bootstrapper=lambda _store, _idea, _text: None,
    )

    result = service.run(ChatTurnRequest(idea="w1", text="cancel", cancellation=cancellation))

    assert result.cancelled
    assert result.reply == ""
    assert not provider_called
    assert not executor_called
    assert store.events == []
    assert store.closed


def test_chat_service_rejects_invalid_request_before_opening_store() -> None:
    opened = False

    def store_factory():
        nonlocal opened
        opened = True
        return _Store()

    service = ChatService(store_factory=store_factory, provider_factory=lambda: object())
    with pytest.raises(ValueError, match="mode"):
        service.run(ChatTurnRequest(idea="w1", text="hello", mode="invalid"))
    assert not opened
