"""Small, bounded scheduler for optional background memory intake.

The scheduler deliberately owns no provider, ledger, or memory objects.  A
submitted callback must open all per-job resources itself.  This keeps a
closed request SQLite connection from escaping into the worker and makes the
queue straightforward to test without model/network access.
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class _Job:
    key: str
    callback: Callable[[], object]


class MemoryExtractionScheduler:
    """A lazy, single-worker, bounded callback scheduler.

    ``shutdown`` stops accepting new work and waits only for the configured
    short interval.  ``resume`` reopens admission for a process-resume hook;
    queued jobs are retained, so callers can recover requested-but-unfinished
    ledger work without creating a second worker.
    """

    def __init__(self, *, max_pending: int = 4, shutdown_timeout: float = 1.0) -> None:
        if isinstance(max_pending, bool) or int(max_pending) <= 0:
            raise ValueError("max_pending must be a positive integer")
        self._queue: queue.Queue[_Job] = queue.Queue(maxsize=int(max_pending))
        self._shutdown_timeout = max(0.0, float(shutdown_timeout))
        self._lock = threading.RLock()
        self._worker: threading.Thread | None = None
        self._accepting = True
        self._stopping = False
        self._keys: set[str] = set()
        self._completed = 0
        self._failed = 0

    def _ensure_worker_locked(self) -> None:
        worker = self._worker
        if worker is not None and worker.is_alive():
            return
        self._stopping = False
        worker = threading.Thread(
            target=self._run,
            name="stata-agent-memory",
            daemon=True,
        )
        self._worker = worker
        worker.start()

    def submit(self, callback: Callable[[], object], *, key: str) -> bool:
        """Queue one callback; return ``False`` when closed/full/duplicate."""

        if not callable(callback):
            raise TypeError("callback must be callable")
        normalized_key = str(key).strip()
        if not normalized_key:
            raise ValueError("key must not be blank")
        with self._lock:
            if not self._accepting or normalized_key in self._keys:
                return False
            try:
                self._queue.put_nowait(_Job(normalized_key, callback))
            except queue.Full:
                return False
            self._keys.add(normalized_key)
            self._ensure_worker_locked()
            return True

    def _run(self) -> None:
        while True:
            with self._lock:
                stopping = self._stopping
            if stopping and self._queue.empty():
                return
            try:
                job = self._queue.get(timeout=0.05)
            except queue.Empty:
                continue
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
                    self._keys.discard(job.key)
                self._queue.task_done()

    def shutdown(self, *, wait: bool = True, timeout: float | None = None) -> None:
        """Stop admission and optionally wait a bounded interval."""

        with self._lock:
            self._accepting = False
            self._stopping = True
            worker = self._worker
        if wait and worker is not None:
            interval = self._shutdown_timeout if timeout is None else max(0.0, float(timeout))
            worker.join(interval)

    def resume(self) -> None:
        """Resume admission and start a worker if the previous one exited."""

        with self._lock:
            self._accepting = True
            self._stopping = False
            if not self._queue.empty():
                self._ensure_worker_locked()

    @property
    def accepting(self) -> bool:
        with self._lock:
            return self._accepting

    @property
    def pending(self) -> int:
        return self._queue.qsize()

    @property
    def stats(self) -> dict[str, int | bool]:
        with self._lock:
            return {
                "accepting": self._accepting,
                "pending": self._queue.qsize(),
                "completed": self._completed,
                "failed": self._failed,
            }


__all__ = ["MemoryExtractionScheduler"]
