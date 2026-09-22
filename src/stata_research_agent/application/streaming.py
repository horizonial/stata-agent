"""Typed durable notification read models; notifications never own research facts."""

from __future__ import annotations

import asyncio
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


class EphemeralModelDeltaHub:
    """Best-effort live deltas; final Assistant Output remains the only authoritative text."""

    def __init__(self, queue_capacity: int = 256) -> None:
        if queue_capacity < 1:
            raise ValueError("ephemeral delta queue capacity must be positive")
        self._capacity = queue_capacity
        self._subscribers: dict[tuple[str, str], set[asyncio.Queue[EphemeralModelDelta]]] = {}

    async def publish(self, workspace_id: str, turn_id: str, delta: ProviderResponseDelta) -> None:
        event = EphemeralModelDelta(
            workspace_id,
            turn_id,
            delta.provider_attempt_id.value,
            delta.sequence,
            delta.channel,
            delta.content,
        )
        for queue in tuple(self._subscribers.get((workspace_id, turn_id), ())):
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
        queue: asyncio.Queue[EphemeralModelDelta] = asyncio.Queue(self._capacity)
        self._subscribers.setdefault(key, set()).add(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            subscribers = self._subscribers.get(key)
            if subscribers is not None:
                subscribers.discard(queue)
                if not subscribers:
                    self._subscribers.pop(key, None)
