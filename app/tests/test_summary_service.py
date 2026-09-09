"""Focused tests for optional model-assisted compaction summaries."""

from __future__ import annotations

import pytest

from stata_agent.events.schema import ACTOR_AGENT, EVENT_IDEA, EVENT_USER, Event
from stata_agent.harness.compaction import compact
from stata_agent.harness.summary_service import (
    ChatCompactionSummaryProvider,
    CompactionSummaryRequest,
    SummarySource,
    SummaryValidationError,
    validate_model_summary,
)
from stata_agent.storage.sqlite_store import SQLiteStore


def _request(*sources: SummarySource) -> CompactionSummaryRequest:
    return CompactionSummaryRequest(
        idea_id="i1",
        from_seq=1,
        to_seq=2,
        previous_summary={},
        deterministic_summary={
            "objective": "deterministic objective",
            "constraints": [],
            "decisions": [],
            "open_items": [],
            "research_state": "phase=IDEA",
            "evidence_refs": ["claim-1"],
        },
        sources=tuple(sources),
    )


def _candidate(source_id: str = "seq:1") -> dict:
    item = {"text": "用户希望验证主假设", "source_ids": [source_id]}
    return {
        "objective": item,
        "constraints": [],
        "decisions": [],
        "open_items": [],
    }


def test_validate_model_summary_requires_eligible_existing_source():
    request = _request(SummarySource("seq:1", "user_message", "验证主假设"))
    validated = validate_model_summary(request, _candidate())
    assert validated["objective"] == {"text": "用户希望验证主假设", "source_ids": ["seq:1"]}

    with pytest.raises(SummaryValidationError):
        validate_model_summary(request, _candidate("seq:missing"))
    with pytest.raises(SummaryValidationError):
        validate_model_summary(
            _request(SummarySource("tool:1", "tool_result", "p-value=0.01")),
            _candidate("tool:1"),
        )


def test_chat_adapter_uses_json_mode_and_fixed_bounded_prompt():
    class Provider:
        provider = "test"

        def __init__(self):
            self.calls = []

        def chat(self, messages, *, json_mode=False):
            self.calls.append((messages, json_mode))
            return {"content": '{"objective":{"text":"目标","source_ids":["seq:1"]},'
                    '"constraints":[],"decisions":[],"open_items":[]}'}

    provider = Provider()
    adapter = ChatCompactionSummaryProvider(provider)
    request = _request(SummarySource("seq:1", "user_message", "验证主假设"))
    raw = adapter.summarize(request)
    assert raw["objective"]["source_ids"] == ["seq:1"]
    assert provider.calls[0][1] is True
    messages = provider.calls[0][0]
    assert messages[0]["role"] == "system"
    assert "untrusted" in messages[0]["content"]
    assert "验证主假设" in messages[1]["content"]
    assert "research_state" not in messages[1]["content"]
    assert "evidence_refs" not in messages[1]["content"]


def test_compaction_without_summarizer_keeps_legacy_result_shape(tmp_path):
    store = SQLiteStore(str(tmp_path / "ledger.db"), writer_id="summary-test")
    store.append(Event(
        idea_id="i1", event_type=EVENT_IDEA, actor=ACTOR_AGENT, source=ACTOR_AGENT,
        payload={"question": "验证主假设"},
    ))
    store.append(Event(
        idea_id="i1", event_type=EVENT_USER, actor=ACTOR_AGENT, source=ACTOR_AGENT,
        payload={"text": "继续"},
    ))
    result = compact(store, "i1")
    boundary = list(store.scan("i1"))[-1]
    assert set(result) == {
        "seq", "version", "from_seq", "to_seq", "previous_boundary_seq", "retained_from_seq",
        "summary", "summary_payload", "claim_ids",
    }
    assert "summary_mode" not in boundary.payload
    store.close()


def test_invalid_model_summary_falls_back_without_storing_response(tmp_path):
    store = SQLiteStore(str(tmp_path / "ledger.db"), writer_id="summary-test")
    store.append(Event(
        idea_id="i1", event_type=EVENT_IDEA, actor=ACTOR_AGENT, source=ACTOR_AGENT,
        payload={"question": "验证主假设"},
    ))
    store.append(Event(
        idea_id="i1", event_type=EVENT_USER, actor=ACTOR_AGENT, source=ACTOR_AGENT,
        payload={"text": "继续"},
    ))

    class Bad:
        provider_name = "bad"

        def __init__(self):
            self.calls = 0

        def summarize(self, request):
            self.calls += 1
            return {"objective": {"text": "evidence: fabricated", "source_ids": ["seq:1"]},
                    "constraints": [], "decisions": [], "open_items": [],
                    "raw_response": "do not persist"}

    bad = Bad()
    result = compact(store, "i1", summarizer=bad)
    event = list(store.scan("i1"))[-1]
    assert bad.calls == 1
    assert result["summary_payload"]["objective"] == "验证主假设"
    assert event.payload["summary_mode"] == "model_fallback"
    assert event.payload["summary_error_code"] == "validation_failed"
    assert "raw_response" not in event.payload
    assert "evidence: fabricated" not in str(event.payload)
    store.close()


def test_valid_model_summary_preserves_deterministic_state_and_provenance(tmp_path):
    store = SQLiteStore(str(tmp_path / "ledger.db"), writer_id="summary-test")
    store.append(Event(
        idea_id="i1", event_type=EVENT_IDEA, actor=ACTOR_AGENT, source=ACTOR_AGENT,
        payload={"question": "验证主假设"},
    ))
    store.append(Event(
        idea_id="i1", event_type=EVENT_USER, actor=ACTOR_AGENT, source=ACTOR_AGENT,
        payload={"text": "继续"},
    ))

    class Good:
        provider_name = "fake"
        prompt_version = "test-prompt"

        def summarize(self, request):
            return {
                "objective": {"text": "模型目标", "source_ids": ["seq:1"]},
                "constraints": [],
                "decisions": [],
                "open_items": [],
            }

    result = compact(store, "i1", summarizer=Good())
    event = list(store.scan("i1"))[-1]
    assert result["summary_payload"]["objective"] == "模型目标"
    assert "research_state" in result["summary_payload"]
    assert "evidence_refs" in result["summary_payload"]
    assert event.payload["summary_mode"] == "model_validated"
    assert event.payload["provider"] == "fake"
    assert event.payload["prompt_version"] == "test-prompt"
    assert event.payload["summary_provenance"] == {"objective": ["seq:1"]}
    assert "prompt" not in event.payload and "response" not in event.payload
    store.close()
