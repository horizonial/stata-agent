"""Contract tests for durable memory-extraction dispatch."""

from __future__ import annotations

import threading

from stata_agent.application import (
    FakeMemoryOutboxRepository,
    MemoryOutboxDispatcher,
    MemoryOutboxIntent,
    MEMORY_EXTRACTION_KIND,
)
from stata_agent.application.local_task_queue import LocalTaskQueue
from stata_agent.memory.pipeline import MemoryExtractionRequest, MemorySource


def _intent(*, fingerprint: str = "sha256:memory-1") -> MemoryOutboxIntent:
    request = MemoryExtractionRequest(
        idea_id="idea-1",
        workspace_id="workspace-1",
        from_seq=2,
        to_seq=4,
        sources=(MemorySource("seq:2", "user", "以后默认使用中文回答"),),
        fingerprint=fingerprint,
    )
    return MemoryOutboxIntent.from_request(
        request,
        privacy_mode="local_strict",
        provider_name="local",
    )


def _dispatcher(repository, queue, *, worker, terminal_checker):
    return MemoryOutboxDispatcher(
        repository,
        queue,
        worker=worker,
        terminal_checker=terminal_checker,
        owner="test-worker",
        lease_seconds=5,
        claim_limit=4,
    )


def test_intent_uses_fingerprint_as_queue_key_and_does_not_store_source_text():
    intent = _intent()

    assert intent.kind == MEMORY_EXTRACTION_KIND
    assert intent.key == intent.fingerprint
    assert intent.as_payload()["source_ids"] == ["seq:2"]
    assert "以后默认使用中文回答" not in str(intent.as_payload())


def test_ensure_is_idempotent_and_reconcile_delegates_to_store():
    repository = FakeMemoryOutboxRepository()
    repository.backfill_count = 3
    queue = LocalTaskQueue(max_pending=1)
    dispatcher = _dispatcher(
        repository,
        queue,
        worker=lambda _claim: None,
        terminal_checker=lambda _claim: False,
    )

    assert dispatcher.ensure_intent(_intent()) is True
    assert dispatcher.ensure_intent(_intent()) is False
    assert dispatcher.reconcile(idea_id="idea-1") == 3
    queue.shutdown(wait=True)


def test_terminal_event_is_required_before_ack():
    repository = FakeMemoryOutboxRepository()
    queue = LocalTaskQueue(max_pending=1)
    finished = threading.Event()

    def worker(claim):
        repository.terminal.add(claim.key)
        finished.set()

    dispatcher = _dispatcher(
        repository,
        queue,
        worker=worker,
        terminal_checker=lambda claim: claim.key in repository.terminal,
    )
    dispatcher.ensure_intent(_intent())

    report = dispatcher.pump()
    assert report.claimed == report.submitted == 1
    assert finished.wait(1)
    queue.shutdown(wait=True)
    assert repository.status[_intent().key] == "completed"
    assert repository.completions == [_intent().key]


def test_missing_terminal_event_retries_and_preserves_pending():
    repository = FakeMemoryOutboxRepository()
    queue = LocalTaskQueue(max_pending=1)
    finished = threading.Event()
    dispatcher = _dispatcher(
        repository,
        queue,
        worker=lambda _claim: finished.set(),
        terminal_checker=lambda _claim: False,
    )
    dispatcher.ensure_intent(_intent())

    assert dispatcher.pump().submitted == 1
    assert finished.wait(1)
    queue.shutdown(wait=True)
    assert repository.status[_intent().key] == "pending"
    assert repository.retries == [(_intent().key, "terminal_event_missing")]


def test_full_queue_releases_claim_back_to_pending():
    repository = FakeMemoryOutboxRepository()
    queue = LocalTaskQueue(max_pending=1, shutdown_timeout=0.2)
    started = threading.Event()
    release = threading.Event()

    def blocking():
        started.set()
        release.wait(1)

    assert queue.submit("running", blocking).accepted
    assert started.wait(1)
    assert queue.submit("occupied", lambda: None).accepted
    dispatcher = _dispatcher(
        repository,
        queue,
        worker=lambda _claim: None,
        terminal_checker=lambda _claim: False,
    )
    dispatcher.ensure_intent(_intent())

    report = dispatcher.pump()
    assert report.claimed == 1
    assert report.submitted == 0
    assert report.retained_pending == 1
    assert repository.status[_intent().key] == "pending"
    assert repository.retries == [(_intent().key, "queue_full")]

    release.set()
    queue.shutdown(wait=True)


def test_closed_queue_releases_claim_back_to_pending():
    repository = FakeMemoryOutboxRepository()
    queue = LocalTaskQueue(max_pending=1)
    queue.shutdown(wait=True)
    dispatcher = _dispatcher(
        repository,
        queue,
        worker=lambda _claim: None,
        terminal_checker=lambda _claim: False,
    )
    dispatcher.ensure_intent(_intent())

    report = dispatcher.pump()
    assert report.claimed == 1
    assert report.submitted == 0
    assert report.retained_pending == 1
    assert repository.status[_intent().key] == "pending"
    assert repository.retries == [(_intent().key, "queue_closed")]


def test_claim_freezes_privacy_and_provider_identity_for_worker():
    repository = FakeMemoryOutboxRepository()
    queue = LocalTaskQueue(max_pending=1)
    seen = []
    finished = threading.Event()
    intent = MemoryOutboxIntent(
        idea_id="idea-1",
        workspace_id="workspace-1",
        fingerprint="sha256:memory-2",
        from_seq=1,
        to_seq=1,
        source_ids=("seq:1",),
        prompt_version="memory-v1",
        privacy_mode="mixed_sanitized",
        provider_name="deepseek",
    )

    def worker(claim):
        seen.append((claim.intent.privacy_mode, claim.intent.provider_name))
        repository.terminal.add(claim.key)
        finished.set()

    dispatcher = _dispatcher(
        repository,
        queue,
        worker=worker,
        terminal_checker=lambda claim: claim.key in repository.terminal,
    )
    dispatcher.ensure_intent(intent)
    assert dispatcher.pump().submitted == 1
    assert finished.wait(1)
    queue.shutdown(wait=True)

    assert seen == [("mixed_sanitized", "deepseek")]
    assert repository.status[intent.key] == "completed"

