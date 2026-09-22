"""Deterministic idle-exit policy; browser closure alone never stops research."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum


@dataclass(frozen=True, slots=True)
class RuntimeLivenessSnapshot:
    observed_at: datetime
    nonterminal_turns: int
    browser_sessions: int
    managed_executions: int
    unfinished_control_work: int
    unflushed_authority_or_outbox: int
    managed_child_processes: int

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None:
            raise ValueError("runtime liveness time must be timezone-aware")
        counts = (
            self.nonterminal_turns,
            self.browser_sessions,
            self.managed_executions,
            self.unfinished_control_work,
            self.unflushed_authority_or_outbox,
            self.managed_child_processes,
        )
        if any(value < 0 for value in counts):
            raise ValueError("runtime liveness counts cannot be negative")

    @property
    def safe_to_begin_idle_grace(self) -> bool:
        return not any(
            (
                self.nonterminal_turns,
                self.browser_sessions,
                self.managed_executions,
                self.unfinished_control_work,
                self.unflushed_authority_or_outbox,
                self.managed_child_processes,
            )
        )


class IdleExitDecision(StrEnum):
    KEEP_RUNNING = "KEEP_RUNNING"
    IDLE_GRACE = "IDLE_GRACE"
    BEGIN_GRACEFUL_EXIT = "BEGIN_GRACEFUL_EXIT"


class IdleExitPolicy:
    def __init__(self, grace_period: timedelta = timedelta(seconds=60)) -> None:
        if grace_period.total_seconds() <= 0:
            raise ValueError("idle exit grace period must be positive")
        self._grace_period = grace_period
        self._idle_since: datetime | None = None

    def evaluate(self, snapshot: RuntimeLivenessSnapshot) -> IdleExitDecision:
        if not snapshot.safe_to_begin_idle_grace:
            self._idle_since = None
            return IdleExitDecision.KEEP_RUNNING
        if self._idle_since is None or snapshot.observed_at < self._idle_since:
            self._idle_since = snapshot.observed_at
            return IdleExitDecision.IDLE_GRACE
        if snapshot.observed_at - self._idle_since < self._grace_period:
            return IdleExitDecision.IDLE_GRACE
        return IdleExitDecision.BEGIN_GRACEFUL_EXIT
