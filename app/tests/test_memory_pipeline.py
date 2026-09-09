"""Focused contract tests for the conservative memory intake pipeline."""

from __future__ import annotations

import json

import pytest

from stata_agent.events.schema import (
    ACTOR_AGENT,
    ACTOR_USER,
    EVENT_AGENT_STEP,
    EVENT_APPROVAL_GRANT,
    EVENT_MEMORY_EXTRACTION_COMPLETED,
    EVENT_MEMORY_EXTRACTION_DENIED,
    EVENT_MEMORY_EXTRACTION_NOOP,
    EVENT_MEMORY_EXTRACTION_REQUESTED,
    EVENT_STEERING,
    EVENT_USER,
    Event,
)
from stata_agent.memory import (
    ChatMemoryExtractionProvider,
    MemoryExtractionRequest,
    MemoryExtractionPipeline,
    MemorySource,
    MemoryStore,
    validate_extraction_output,
)
from stata_agent.storage.sqlite_store import SQLiteStore


def _event(idea: str, kind: str, payload: dict, *, actor: str = ACTOR_USER) -> Event:
    return Event(idea_id=idea, event_type=kind, actor=actor, source=actor, payload=payload)


class CountingProvider:
    provider = "local"

    def __init__(self, output):
        self.output = output
        self.calls = 0
        self.requests = []

    def extract(self, request):
        self.calls += 1
        self.requests.append(request)
        return self.output(request) if callable(self.output) else self.output


class RemoteProvider(CountingProvider):
    provider = "deepseek"


def _seed(store: SQLiteStore) -> None:
    store.append(_event("i1", EVENT_USER, {"text": "以后默认使用中文回答"}))
    store.append(_event("i1", EVENT_AGENT_STEP, {"reply": "assistant claim: use English"}, actor=ACTOR_AGENT))
    store.append(_event("i1", EVENT_AGENT_STEP, {"tool_result": "p=0.01; n=1200"}, actor=ACTOR_AGENT))
    store.append(
        _event(
            "i1",
            EVENT_APPROVAL_GRANT,
            {"decision": "approve", "note": "主回归必须保留稳健标准误"},
        )
    )
    store.append(
        _event(
            "i1",
            EVENT_STEERING,
            {"kind": "approval_revision", "note": "不要自动删除中间结果"},
        )
    )


def test_pipeline_only_sends_bounded_user_and_approval_sources(tmp_path):
    ledger = SQLiteStore(str(tmp_path / "ledger.db"), writer_id="pipeline")
    _seed(ledger)
    memory = MemoryStore(tmp_path / "memory.json", workspace_id="ws-a")
    provider = CountingProvider(
        lambda request: [
            {
                "kind": "preference",
                "text": "以后默认使用中文回答",
                "source_ids": [request.sources[0].source_id],
            }
        ]
    )

    result = MemoryExtractionPipeline().run_once(
        store=ledger,
        memory=memory,
        idea_id="i1",
        workspace_id="ws-a",
        provider=provider,
        privacy_mode="local_strict",
    )

    assert result.status == "completed"
    assert provider.calls == 1
    request = provider.requests[0]
    assert len(request.sources) == 3
    assert all(source.role in {"user", "approval", "modification"} for source in request.sources)
    assert all("p=0.01" not in source.text for source in request.sources)
    assert all(source.source_id != "seq:2" for source in request.sources)
    assert all(entry["status"] == "candidate" for entry in memory.candidates(workspace_id="ws-a"))
    assert memory.search("中文", workspace_id="ws-a") == []
    event_types = [event.event_type for event in ledger.scan("i1")]
    assert event_types[-2:] == [EVENT_MEMORY_EXTRACTION_REQUESTED, EVENT_MEMORY_EXTRACTION_COMPLETED]
    ledger.close()


def test_fingerprint_is_idempotent_before_provider_io(tmp_path):
    ledger = SQLiteStore(str(tmp_path / "ledger.db"), writer_id="pipeline")
    ledger.append(_event("i1", EVENT_USER, {"text": "始终保留中文表头"}))
    memory = MemoryStore(tmp_path / "memory.json", workspace_id="ws-a")
    provider = CountingProvider([])
    pipeline = MemoryExtractionPipeline()

    first = pipeline.run_once(
        store=ledger, memory=memory, idea_id="i1", workspace_id="ws-a", provider=provider
    )
    second = pipeline.run_once(
        store=ledger, memory=memory, idea_id="i1", workspace_id="ws-a", provider=provider
    )

    assert first.status == second.status == "noop"
    assert first.fingerprint == second.fingerprint
    assert provider.calls == 1
    assert len(
        [
            event
            for event in ledger.scan("i1")
            if event.event_type in {EVENT_MEMORY_EXTRACTION_REQUESTED, EVENT_MEMORY_EXTRACTION_NOOP}
        ]
    ) == 2
    ledger.close()


def test_remote_provider_is_denied_and_accounted_in_local_strict(tmp_path):
    ledger = SQLiteStore(str(tmp_path / "ledger.db"), writer_id="pipeline")
    ledger.append(_event("i1", EVENT_USER, {"text": "默认使用短格式表格"}))
    memory = MemoryStore(tmp_path / "memory.json", workspace_id="ws-a")
    provider = RemoteProvider([])

    result = MemoryExtractionPipeline().run_once(
        store=ledger,
        memory=memory,
        idea_id="i1",
        workspace_id="ws-a",
        provider=provider,
        privacy_mode="local_strict",
    )

    assert result.status == "denied"
    assert result.error_code == "privacy_denied"
    assert provider.calls == 0
    terminal = list(ledger.scan("i1"))[-1]
    assert terminal.event_type == EVENT_MEMORY_EXTRACTION_DENIED
    assert terminal.payload["error_code"] == "privacy_denied"
    ledger.close()


def test_mixed_mode_sanitizes_request_without_changing_fingerprint(tmp_path):
    ledger = SQLiteStore(str(tmp_path / "ledger.db"), writer_id="pipeline")
    ledger.append(_event("i1", EVENT_USER, {"text": "以后默认保留本地路径 C:/private/data.csv"}))
    memory = MemoryStore(tmp_path / "memory.json", workspace_id="ws-a")
    provider = RemoteProvider([])

    result = MemoryExtractionPipeline().run_once(
        store=ledger,
        memory=memory,
        idea_id="i1",
        workspace_id="ws-a",
        provider=provider,
        privacy_mode="mixed_sanitized",
    )

    assert result.status == "noop"
    assert provider.calls == 1
    assert "C:/private/data.csv" not in provider.requests[0].sources[0].text
    assert "sanitized" in provider.requests[0].sources[0].text
    assert provider.requests[0].fingerprint == result.fingerprint
    ledger.close()


@pytest.mark.parametrize(
    "output, code",
    [
        ([{"kind": "preference", "text": "x", "source_ids": ["other"]}], "invalid_source_ids"),
        ([{"kind": "fact", "text": "x", "source_ids": ["s1"]}], "invalid_kind"),
        ([{"kind": "preference", "text": "p=0.01", "source_ids": ["s1"]}], "unsafe_candidate"),
        ([{"kind": "preference", "text": "x", "source_ids": ["s1"], "confidence": "explicit"}], "invalid_candidate"),
    ],
)
def test_provider_output_is_strictly_validated(output, code):
    request = MemoryExtractionRequest(
        idea_id="i1",
        workspace_id="ws-a",
        from_seq=1,
        to_seq=1,
        sources=(MemorySource("s1", "user", "以后使用中文"),),
        fingerprint="sha256:test",
    )
    with pytest.raises(Exception) as exc_info:
        validate_extraction_output(request, output)
    assert getattr(exc_info.value, "code", None) == code


def test_candidate_reads_are_workspace_isolated(tmp_path):
    memory = MemoryStore(tmp_path / "memory.json", workspace_id="ws-a")
    candidate = memory.add_candidate(
        "以后使用中文",
        workspace_id="ws-a",
        source_ids=["seq:1"],
    )
    other = memory.add_candidate(
        "另一个项目使用英文",
        workspace_id="ws-b",
        source_ids=["seq:2"],
    )

    assert [item["id"] for item in memory.candidates(workspace_id="ws-a")] == [candidate["id"]]
    assert memory.candidate(other["id"], workspace_id="ws-a") is None
    assert [item["id"] for item in memory.all(workspace_id="ws-a", include_candidates=True)] == [candidate["id"]]
    assert memory.accept_candidate(other["id"], workspace_id="ws-a") is None


def test_chat_adapter_accepts_structured_content():
    class ChatDouble:
        provider = "local"

        def __init__(self):
            self.messages = []

        def chat(self, messages, *, json_mode=False):
            self.messages.append((messages, json_mode))
            return {"content": json.dumps({"candidates": []})}

    double = ChatDouble()
    adapter = ChatMemoryExtractionProvider(double)
    request = MemoryExtractionRequest(
        idea_id="i1",
        workspace_id="ws-a",
        from_seq=1,
        to_seq=1,
        sources=(MemorySource("s1", "user", "以后使用中文"),),
        fingerprint="sha256:test",
    )
    assert adapter.extract(request) == ()
    assert double.messages[0][1] is True
