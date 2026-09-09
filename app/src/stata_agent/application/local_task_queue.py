"""Non-durable bounded single-worker ``TaskQueue`` implementation.

This adapter is intentionally equivalent in scope to the existing
``MemoryExtractionScheduler`` but returns typed admission results and stable
stats.  It owns only callbacks and idempotency keys.  The callback is
responsible for opening any provider, ledger, or memory resources it needs.

The queue is process-local and non-durable: accepted work is lost if the
process exits.  A future SQLite outbox or RQ/Redis adapter can replace this
class behind :class:`~stata_agent.application.task_queue.TaskQueue`.
"""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass

from .task_queue import (
    QueueStats,
    TaskCallback,
    TaskSubmitResult,
    TaskSubmitStatus,
)


@dataclass(frozen=True)
class _Job:
    idempotency_key: str
    callback: TaskCallback


class LocalTaskQueue:
    """Lazy, bounded, single-worker implementation of ``TaskQueue``."""

    def __init__(
        self,
        *,
        max_pending: int = 4,
        shutdown_timeout: float = 1.0,
        worker_name: str = "stata-agent-task-queue",
    ) -> None:
        if isinstance(max_pending, bool) or int(max_pending) <= 0:
            raise ValueError("max_pending must be a positive integer")
        if isinstance(shutdown_timeout, bool) or float(shutdown_timeout) < 0:
            raise ValueError("shutdown_timeout must be non-negative")
        if not str(worker_name).strip():
            raise ValueError("worker_name must not be blank")
        self._queue: queue.Queue[_Job] = queue.Queue(maxsize=int(max_pending))
        self._shutdown_timeout = float(shutdown_timeout)
        self._worker_name = str(worker_name).strip()
        self._lock = threading.RLock()
        self._worker: threading.Thread | None = None
        self._accepting = True
        self._stopping = False
        self._running = 0
        self._keys: set[str] = set()
        self._accepted = 0
        self._completed = 0
        self._failed = 0
        self._duplicates = 0
        self._full = 0
        self._closed = 0

    def submit(self, idempotency_key: str, callback: TaskCallback) -> TaskSubmitResult:
        """Admit one callback without blocking and report the exact outcome."""

        if not callable(callback):
            raise TypeError("callback must be callable")
        key = str(idempotency_key or "").strip()
        if not key:
            raise ValueError("idempotency_key must not be blank")
        with self._lock:
            if not self._accepting:
                self._closed += 1
                return TaskSubmitResult(TaskSubmitStatus.CLOSED, key)
            if key in self._keys:
                self._duplicates += 1
                return TaskSubmitResult(TaskSubmitStatus.DUPLICATE, key)
            try:
                self._queue.put_nowait(_Job(key, callback))
            except queue.Full:
                self._full += 1
                return TaskSubmitResult(TaskSubmitStatus.FULL, key)
            self._keys.add(key)
            self._accepted += 1
            self._ensure_worker_locked()
            return TaskSubmitResult(TaskSubmitStatus.ACCEPTED, key)

    def start(self) -> None:
        """Open admission and start a worker when queued work exists."""

        with self._lock:
            self._accepting = True
            self._stopping = False
            if not self._queue.empty():
                self._ensure_worker_locked()

    def resume(self) -> None:
        """Alias for ``start`` used by process-resume hooks."""

        self.start()

    def shutdown(self, *, wait: bool = True, timeout: float | None = None) -> None:
        """Close admission and join the worker for at most ``timeout`` seconds."""

        with self._lock:
            self._accepting = False
            self._stopping = True
            worker = self._worker
        if not wait or worker is None or worker is threading.current_thread():
            return
        interval = self._shutdown_timeout if timeout is None else max(0.0, float(timeout))
        worker.join(interval)

    @property
    def stats(self) -> QueueStats:
        with self._lock:
            return QueueStats(
                accepting=self._accepting,
                pending=self._queue.qsize(),
                running=self._running,
                accepted=self._accepted,
                completed=self._completed,
                failed=self._failed,
                duplicates=self._duplicates,
                full=self._full,
                closed=self._closed,
            )

    @property
    def accepting(self) -> bool:
        with self._lock:
            return self._accepting

    @property
    def pending(self) -> int:
        with self._lock:
            return self._queue.qsize()

    def _ensure_worker_locked(self) -> None:
        worker = self._worker
        if worker is not None and worker.is_alive():
            return
        self._stopping = False
        worker = threading.Thread(target=self._run, name=self._worker_name, daemon=True)
        self._worker = worker
        worker.start()

    def _run(self) -> None:
        current = threading.current_thread()
        try:
            while True:
                with self._lock:
                    if self._stopping and self._queue.empty():
                        return
                try:
                    job = self._queue.get(timeout=0.05)
                except queue.Empty:
                    continue
                with self._lock:
                    self._running = 1
                try:
                    job.callback()
                except Exception:
                    with self._lock:
                        self._failed += 1
                else:
                    with self._lock:
                        self._completed += 1
                finally:
                    with self._lock:
                        self._running = 0
                        self._keys.discard(job.idempotency_key)
                    self._queue.task_done()
        finally:
            with self._lock:
                if self._worker is current:
                    self._worker = None


__all__ = ["LocalTaskQueue"]
