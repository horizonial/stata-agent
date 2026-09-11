from __future__ import annotations

import copy

import pytest

from stata_agent.events.schema import ACTOR_AGENT, EVENT_AGENT_STEP, EVENT_IDEA, Event
from stata_agent.harness.agent_loop import run_loop
from stata_agent.privacy.modes import LOCAL_STRICT, MIXED_SANITIZED
from stata_agent.providers.deepseek import QwenProvider
from stata_agent.providers.protocol import ProviderError
from stata_agent.providers.provider_check import ProviderRouter
from stata_agent.providers.registry import LiveProviderDisabled, default_provider, live_available
from stata_agent.storage.sqlite_store import SQLiteStore
from stata_agent.toolkit import ToolContext


class _ChatProvider:
    def __init__(self, name: str, responses: list[dict] | None = None, errors: list[BaseException] | None = None):
        self.provider = name
        self.responses = list(responses or [])
        self.errors = list(errors or [])
        self.calls = 0
        self.captured: list[tuple[list[dict], list[dict] | None]] = []

    def chat(self, messages, *, tools=None):
        self.calls += 1
        self.captured.append((copy.deepcopy(messages), copy.deepcopy(tools)))
        if self.errors:
            raise self.errors.pop(0)
        return self.responses.pop(0)


class _StreamingProvider(_ChatProvider):
    def __init__(self, name: str, events: list[dict], error: BaseException | None = None):
        super().__init__(name)
        self.events = list(events)
        self.error = error

    def stream_chat(self, messages, *, tools=None):
        self.calls += 1
        self.captured.append((copy.deepcopy(messages), copy.deepcopy(tools)))
        yield from self.events
        if self.error is not None:
            raise self.error


def _store(tmp_path, name: str = "idea") -> SQLiteStore:
    store = SQLiteStore(str(tmp_path / "ledger.db"), writer_id="provider-test")
    store.append(
        Event(
            idea_id=name,
            event_type=EVENT_IDEA,
            actor=ACTOR_AGENT,
            source=ACTOR_AGENT,
            payload={"question": "provider test"},
        )
    )
    return store


def test_unknown_named_provider_is_blocked_under_local_strict(tmp_path):
    store = _store(tmp_path)
    provider = _ChatProvider("unregistered-cloud", responses=[{"content": "must not run", "tool_calls": None}])

    result = run_loop(
        store,
        provider,
        {},
        ToolContext(idea="idea", store=store),
        user_text="hello",
        privacy_mode=LOCAL_STRICT,
    )

    assert result.terminal_reason == "privacy_denied"
    assert provider.calls == 0
    assert "must not run" not in result.reply
    assert list(store.scan("idea"))[-1].event_type == EVENT_AGENT_STEP
    store.close()


def test_router_freezes_one_mixed_projection_and_records_safe_fallback():
    first = _ChatProvider("deepseek", errors=[ProviderError("unavailable", detail="Bearer SECRET")])
    second = _ChatProvider("qwen", responses=[{"content": "ok", "tool_calls": None}])
    audit: list[dict] = []
    router = ProviderRouter([first, second], privacy_mode=MIXED_SANITIZED, request_id="req-1")
    router.set_fallback_callback(lambda event: audit.append(event) or True)
    messages = [{"role": "user", "content": "firm_id=12 income=987654 secret=TOP_SECRET"}]
    tools = [{"type": "function", "name": "run", "description": "local wage table", "parameters": {}}]

    response = router.chat(messages, tools=tools)

    assert response["content"] == "ok"
    assert router.attempts_used == 2
    assert len(audit) == 1
    assert audit[0] == {
        "from_provider": "deepseek",
        "to_provider": "qwen",
        "reason_code": "unavailable",
        "attempt": 1,
        "privacy_mode": MIXED_SANITIZED,
        "request_id": "req-1",
    }
    assert first.captured[0] == second.captured[0]
    rendered = repr(first.captured[0])
    assert "firm_id=12" not in rendered
    assert "987654" not in rendered
    assert "TOP_SECRET" not in rendered
    assert "local wage table" not in rendered


def test_router_does_not_fallback_on_nontransient_error():
    first = _ChatProvider("deepseek", errors=[ProviderError("auth", detail="secret-token")])
    second = _ChatProvider("qwen", responses=[{"content": "must not run", "tool_calls": None}])
    router = ProviderRouter([first, second], privacy_mode="approved_remote")

    with pytest.raises(ProviderError) as raised:
        router.chat([{"role": "user", "content": "hello"}])

    assert raised.value.code == "auth"
    assert first.calls == 1
    assert second.calls == 0
    assert router.attempts_used == 1


def test_stream_delta_prevents_fallback():
    first = _StreamingProvider(
        "deepseek",
        [{"type": "text_delta", "text": "partial"}],
        error=ProviderError("timeout"),
    )
    second = _StreamingProvider("qwen", [{"type": "done", "content": "fallback", "tool_calls": None}])
    router = ProviderRouter([first, second], privacy_mode="approved_remote")

    with pytest.raises(ProviderError) as raised:
        list(router.stream_chat([{"role": "user", "content": "hello"}]))

    assert raised.value.code == "timeout"
    assert first.calls == 1
    assert second.calls == 0
    assert router.attempts_used == 1


def test_agent_loop_never_writes_raw_provider_exception(tmp_path, monkeypatch):
    monkeypatch.setenv("STATA_AGENT_LIVE", "1")
    store = _store(tmp_path)
    provider = _ChatProvider(
        "deepseek",
        errors=[RuntimeError("Bearer TOP_SECRET at C:\\Users\\alice\\private.dta")],
    )

    result = run_loop(
        store,
        provider,
        {},
        ToolContext(idea="idea", request_id="req-raw", store=store),
        user_text="hello",
        privacy_mode="approved_remote",
    )

    assert result.reply == "模型调用失败。"
    non_user_events = [event for event in store.scan("idea") if event.event_type != "user.message"]
    assert "TOP_SECRET" not in repr(non_user_events)
    assert "private.dta" not in repr(non_user_events)
    store.close()


def test_live_provider_requires_explicit_switch_and_qwen_base_is_independent(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-key")
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.setenv("STATA_AGENT_PRIVACY", "approved_remote")
    monkeypatch.delenv("STATA_AGENT_LIVE", raising=False)
    assert live_available() is False
    with pytest.raises(LiveProviderDisabled) as raised:
        default_provider()
    assert raised.value.code == "provider_disabled"

    monkeypatch.setenv("STATA_AGENT_LIVE", "1")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://wrong.example")
    monkeypatch.delenv("DASHSCOPE_BASE_URL", raising=False)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "qwen-key")
    qwen = QwenProvider()
    assert qwen._base == "https://dashscope.aliyuncs.com/compatible-mode/v1"
