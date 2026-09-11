from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from stata_agent.application.diagnostics import (
    DiagnosticBundleRequest,
    DiagnosticBundleService,
    DiagnosticValidationError,
)
from stata_agent.events.schema import Event
from stata_agent.storage.sqlite_store import SQLiteStore
from stata_agent.storage.store import OutboxIntent


class _DiagnosticStore:
    def __init__(self, events, outbox):
        self.events = events
        self.outbox = outbox
        self.event_calls = []
        self.outbox_calls = []

    def scan_recent_events(self, idea_id, *, limit, correlation_id=None):
        self.event_calls.append((idea_id, limit, correlation_id))
        selected = [event for event in self.events if correlation_id is None or event.correlation_id == correlation_id]
        return selected[:limit], len(selected) > limit

    def scan_recent_outbox(self, *, task_type, limit):
        self.outbox_calls.append((task_type, limit))
        selected = [row for row in self.outbox if row.task_type == task_type]
        return selected[:limit], len(selected) > limit

    def outbox_stats(self):
        return {
            "pending": 1,
            "processing": 0,
            "completed": 2,
            "failed": 0,
            "ready": 1,
            "expired_leases": 0,
            "oldest_pending_age_seconds": 4,
            "secret": "must-not-leak",
        }


def _row(*, event_seq=1, status="completed"):
    return SimpleNamespace(
        outbox_id="outbox-1",
        task_type="memory.extraction",
        status=status,
        attempt_count=1,
        max_attempts=3,
        lease_owner="secret-owner",
        lease_token="secret-token",
        lease_until=None,
        event_seq=event_seq,
        created_at=1,
        updated_at=2,
        completed_at=2,
        state_version=3,
        last_error="raw provider secret",
        payload={"response": "raw secret"},
        idempotency_key="raw-key",
        fingerprint="raw-fingerprint",
    )


def test_bundle_is_fixed_shape_bounded_and_redacts_hostile_payloads() -> None:
    secret = "CANARY-SECRET-DO-NOT-EXPORT"
    events = [
        Event(
            idea_id="idea-1",
            event_type="user.message",
            actor="user",
            source="user",
            seq=1,
            created_at=1,
            correlation_id="req-1",
            payload={"text": secret, "system": secret},
        ),
        Event(
            idea_id="idea-1",
            event_type="tool.done",
            actor="agent",
            source="agent",
            seq=2,
            created_at=2,
            correlation_id="req-1",
            operation_id="op-1",
            payload={
                "tool": "run_stata",
                "ok": True,
                "result": {"data": {"run_id": "run-1", "reused": False, "response": secret}},
                "arguments": {"code": secret},
            },
        ),
        Event(
            idea_id="idea-1",
            event_type="unknown-secret-event",
            actor="agent",
            source="agent",
            seq=3,
            created_at=3,
            correlation_id=None,
            payload={"secret": secret, "message": secret},
        ),
    ]
    store = _DiagnosticStore(events, [_row(event_seq=1)])
    service = DiagnosticBundleService(
        store,
        queue_snapshot={"accepting": True, "pending": 2, "secret": secret},
        health_snapshot={
            "ok": True,
            "privacy_mode": "local_strict",
            "detail": secret,
            "skill_errors": [secret],
        },
        clock=lambda: 100,
    )

    bundle = service.build(
        DiagnosticBundleRequest(
            idea_id="idea-1",
            workspace_id="workspace-1",
            event_limit=3,
            outbox_limit=1,
            request_id="req-1",
        )
    )
    encoded = json.dumps(bundle, ensure_ascii=False, sort_keys=True)

    assert secret not in encoded
    assert set(bundle) == {"manifest", "health", "queue", "outbox", "events", "correlations", "coverage"}
    assert bundle["manifest"]["schema"] == "diagnostic.bundle.v1"
    assert bundle["manifest"]["request_filter_applied"] is True
    assert bundle["events"][0]["facts"] == {}
    assert bundle["events"][1]["facts"] == {"tool": "run_stata", "ok": True, "run_id": "run-1", "reused": False}
    assert bundle["correlations"] == [
        {
            "request_id": "req-1",
            "event_seqs": [1, 2],
            "operation_ids": ["op-1"],
            "run_ids": ["run-1"],
            "outbox_ids": ["outbox-1"],
        }
    ]
    assert bundle["coverage"] == {
        "events_included": 2,
        "correlated_events": 2,
        "uncorrelated_events": 0,
        "correlation_groups": 1,
        "linked_outbox_rows": 1,
    }
    assert store.event_calls == [("idea-1", 3, "req-1")]
    assert store.outbox_calls == [("memory.extraction", 1)]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"idea_id": "", "event_limit": 1, "outbox_limit": 1},
        {"idea_id": "i", "event_limit": 0, "outbox_limit": 1},
        {"idea_id": "i", "event_limit": 501, "outbox_limit": 1},
        {"idea_id": "i", "event_limit": 1, "outbox_limit": 201},
        {"idea_id": "i", "event_limit": 1, "outbox_limit": 1, "request_id": True},
    ],
)
def test_bundle_request_rejects_invalid_bounds_and_ids(kwargs) -> None:
    with pytest.raises(DiagnosticValidationError):
        DiagnosticBundleRequest(**kwargs)


def test_sqlite_diagnostic_reads_are_bounded_ordered_and_filtered(tmp_path) -> None:
    store = SQLiteStore(str(tmp_path / "diagnostics.sqlite3"), writer_id="diagnostics")
    try:
        for seq in range(1, 4):
            store.append(
                Event(
                    idea_id="idea-1",
                    event_type="idea.declared",
                    actor="agent",
                    source="agent",
                    correlation_id="req-1" if seq != 2 else "req-2",
                    payload={"question": f"q-{seq}"},
                )
            )
        events, truncated = store.scan_recent_events("idea-1", limit=2)
        assert [event.seq for event in events] == [2, 3]
        assert truncated is True
        filtered, filtered_truncated = store.scan_recent_events("idea-1", limit=2, correlation_id="req-2")
        assert [event.seq for event in filtered] == [2]
        assert filtered_truncated is False

        for key in ("outbox-1", "outbox-2"):
            store.enqueue_outbox(
                OutboxIntent(
                    idempotency_key=key,
                    task_type="memory.extraction",
                    payload={"from_seq": 1, "to_seq": 2},
                    available_at=0,
                )
            )
        rows, outbox_truncated = store.scan_recent_outbox(limit=1)
        assert len(rows) == 1
        assert outbox_truncated is True
    finally:
        store.close()
