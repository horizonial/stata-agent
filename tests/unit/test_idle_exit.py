"""D-230 idle-exit state is stricter than browser-session lifetime."""

from datetime import UTC, datetime, timedelta

from stata_research_agent.runtime.idle_exit import (
    IdleExitDecision,
    IdleExitPolicy,
    RuntimeLivenessSnapshot,
)


def snapshot(now: datetime, **overrides: int) -> RuntimeLivenessSnapshot:
    values = {
        "nonterminal_turns": 0,
        "browser_sessions": 0,
        "managed_executions": 0,
        "unfinished_control_work": 0,
        "unflushed_authority_or_outbox": 0,
        "managed_child_processes": 0,
    }
    values.update(overrides)
    return RuntimeLivenessSnapshot(now, **values)


def test_browser_close_does_not_exit_while_research_or_children_remain() -> None:
    start = datetime(2026, 9, 19, tzinfo=UTC)
    policy = IdleExitPolicy(timedelta(seconds=60))
    assert policy.evaluate(snapshot(start, nonterminal_turns=1)) == IdleExitDecision.KEEP_RUNNING
    assert (
        policy.evaluate(snapshot(start + timedelta(minutes=10), managed_child_processes=1))
        == IdleExitDecision.KEEP_RUNNING
    )


def test_idle_exit_requires_an_uninterrupted_full_grace_period() -> None:
    start = datetime(2026, 9, 19, tzinfo=UTC)
    policy = IdleExitPolicy(timedelta(seconds=60))
    assert policy.evaluate(snapshot(start)) == IdleExitDecision.IDLE_GRACE
    assert policy.evaluate(snapshot(start + timedelta(seconds=59))) == IdleExitDecision.IDLE_GRACE
    assert (
        policy.evaluate(snapshot(start + timedelta(seconds=60)))
        == IdleExitDecision.BEGIN_GRACEFUL_EXIT
    )
    assert (
        policy.evaluate(snapshot(start + timedelta(seconds=61), browser_sessions=1))
        == IdleExitDecision.KEEP_RUNNING
    )
    assert policy.evaluate(snapshot(start + timedelta(seconds=62))) == IdleExitDecision.IDLE_GRACE
