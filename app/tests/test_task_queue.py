from __future__ import annotations

import threading
import time

from stata_agent.application import (
    LocalTaskQueue,
    QueueStats,
    TaskSubmitStatus,
)


def test_local_queue_distinguishes_admission_outcomes_and_keeps_stable_stats() -> None:
    queue = LocalTaskQueue(max_pending=1, shutdown_timeout=0.2)
    started = threading.Event()
    release = threading.Event()
    pending_done = threading.Event()

    def blocking_task() -> None:
        started.set()
        release.wait(2)

    def pending_task() -> None:
        pending_done.set()

    assert queue.submit("running", blocking_task).status is TaskSubmitStatus.ACCEPTED
    assert started.wait(1)
    assert queue.submit("running", lambda: None).status is TaskSubmitStatus.DUPLICATE
    assert queue.submit("pending", pending_task).status is TaskSubmitStatus.ACCEPTED
    assert queue.submit("full", lambda: None).status is TaskSubmitStatus.FULL

    release.set()
    queue.shutdown(wait=True)
    assert pending_done.is_set()

    stats = queue.stats
    assert isinstance(stats, QueueStats)
    assert stats.accepting is False
    assert stats.pending == 0
    assert stats.running == 0
    assert stats.accepted == 2
    assert stats.completed == 2
    assert stats.failed == 0
    assert stats.duplicates == 1
    assert stats.full == 1
    assert stats.closed == 0
    assert stats.as_dict() == stats.to_dict()
    assert stats["completed"] == 2
    assert queue.stats() == stats

    queue.resume()
    assert queue.submit("running", lambda: None).accepted
    queue.shutdown(wait=True)
    assert queue.stats.completed == 3


def test_local_queue_isolates_callback_errors_and_allows_key_retry() -> None:
    queue = LocalTaskQueue(shutdown_timeout=0.2)
    failed_done = threading.Event()
    succeeded_done = threading.Event()

    def failing_task() -> None:
        failed_done.set()
        raise RuntimeError("expected task failure")

    def succeeding_task() -> None:
        succeeded_done.set()

    assert queue.submit("retryable", failing_task).accepted
    queue.shutdown(wait=True)
    assert failed_done.is_set()
    assert queue.stats.failed == 1

    queue.start()
    retried = queue.submit("retryable", succeeding_task)
    assert retried.status is TaskSubmitStatus.ACCEPTED
    queue.shutdown(wait=True)
    assert succeeded_done.is_set()
    assert queue.stats.failed == 1
    assert queue.stats.completed == 1


def test_local_queue_closed_admission_reopens_with_start() -> None:
    queue = LocalTaskQueue()
    queue.shutdown(wait=True)

    closed = queue.submit("closed", lambda: None)
    assert closed.status is TaskSubmitStatus.CLOSED
    assert queue.stats.closed == 1

    done = threading.Event()
    queue.start()
    assert queue.submit("reopened", done.set).accepted
    queue.shutdown(wait=True)
    assert done.is_set()
    assert queue.stats.completed == 1


def test_local_queue_shutdown_wait_is_bounded_and_worker_can_finish_later() -> None:
    queue = LocalTaskQueue(shutdown_timeout=0.2)
    started = threading.Event()
    release = threading.Event()

    def blocking_task() -> None:
        started.set()
        release.wait(2)

    assert queue.submit("slow", blocking_task).accepted
    assert started.wait(1)

    begin = time.monotonic()
    queue.shutdown(wait=True, timeout=0.01)
    elapsed = time.monotonic() - begin
    assert elapsed < 0.5
    assert queue.stats.accepting is False
    assert queue.stats.running == 1

    release.set()
    queue.shutdown(wait=True)
    assert queue.stats.running == 0
    assert queue.stats.completed == 1
