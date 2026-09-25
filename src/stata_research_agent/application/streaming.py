"""Typed durable notification read models; notifications never own research facts."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass

from stata_research_agent.application.model_gateway import ProviderResponseDelta
from stata_research_agent.domain.revisions import WorkspaceRevision


@dataclass(frozen=True, slots=True)
class StreamResourceRef:
    resource_type: str
    resource_id: str


@dataclass(frozen=True, slots=True)
class DurableNotification:
    event_id: str
    stream_cursor: str
    workspace_id: str
    workspace_revision: WorkspaceRevision
    event_type: str
    resource_refs: tuple[StreamResourceRef, ...]
    summary_payload_json: str


@dataclass(frozen=True, slots=True)
class DurableReplayBatch:
    workspace_id: str
    after_cursor: str
    current_cursor: str
    authoritative_revision: WorkspaceRevision
    notifications: tuple[DurableNotification, ...]


@dataclass(frozen=True, slots=True)
class EphemeralModelDelta:
    workspace_id: str
    turn_id: str
    provider_attempt_id: str
    sequence: int
    channel: str
    content: str


@dataclass(slots=True)
class _TurnDeltaHistory:
    events: deque[EphemeralModelDelta]
    updated_at: float


class EphemeralModelDeltaHub:
    """Best-effort live deltas with bounded replay for late browser subscribers.

    The replay buffer only improves live UX.  It is intentionally memory-only and
    never becomes a source of research truth; the committed Assistant Output remains
    the sole authoritative response and replaces the live projection after recovery.
    """

    def __init__(
        self,
        queue_capacity: int = 256,
        *,
        history_capacity: int = 1024,
        history_ttl_seconds: float = 900.0,
        max_turn_histories: int = 256,
    ) -> None:
        if min(queue_capacity, history_capacity, max_turn_histories) < 1:
            raise ValueError("ephemeral delta capacities must be positive")
        if history_ttl_seconds <= 0:
            raise ValueError("ephemeral delta history TTL must be positive")
        self._capacity = queue_capacity
        self._history_capacity = history_capacity
        self._history_ttl_seconds = history_ttl_seconds
        self._max_turn_histories = max_turn_histories
        self._subscribers: dict[tuple[str, str], set[asyncio.Queue[EphemeralModelDelta]]] = {}
        self._history: dict[tuple[str, str], _TurnDeltaHistory] = {}

    async def publish(self, workspace_id: str, turn_id: str, delta: ProviderResponseDelta) -> None:
        now = time.monotonic()
        self._prune_history(now)
        key = (workspace_id, turn_id)
        event = EphemeralModelDelta(
            workspace_id,
            turn_id,
            delta.provider_attempt_id.value,
            delta.sequence,
            delta.channel,
            delta.content,
        )
        history = self._history.get(key)
        if history is None:
            if len(self._history) >= self._max_turn_histories:
                oldest = min(self._history, key=lambda item: self._history[item].updated_at)
                self._history.pop(oldest, None)
            history = _TurnDeltaHistory(deque(maxlen=self._history_capacity), now)
            self._history[key] = history
        history.events.append(event)
        history.updated_at = now
        for queue in tuple(self._subscribers.get(key, ())):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(event)

    async def subscribe(
        self, workspace_id: str, turn_id: str
    ) -> AsyncIterator[EphemeralModelDelta]:
        key = (workspace_id, turn_id)
        self._prune_history(time.monotonic())
        queue: asyncio.Queue[EphemeralModelDelta] = asyncio.Queue(self._capacity)
        self._subscribers.setdefault(key, set()).add(queue)
        history = self._history.get(key)
        backlog = () if history is None else tuple(history.events)
        try:
            for event in backlog:
                yield event
            while True:
                yield await queue.get()
        finally:
            subscribers = self._subscribers.get(key)
            if subscribers is not None:
                subscribers.discard(queue)
                if not subscribers:
                    self._subscribers.pop(key, None)

    def _prune_history(self, now: float) -> None:
        expired = [
            key
            for key, history in self._history.items()
            if now - history.updated_at > self._history_ttl_seconds
        ]
        for key in expired:
            self._history.pop(key, None)
