from __future__ import annotations

from dataclasses import replace

import pytest

from stata_agent.application.outbox_retention import (
    MAX_SCAN_ROWS,
    OutboxRetentionConflictError,
    OutboxRetentionPreviewRequest,
    OutboxRetentionPruneRequest,
    OutboxRetentionService,
    OutboxRetentionValidationError,
)
from stata_agent.events.schema import EVENT_MEMORY_EXTRACTION_COMPLETED
from stata_agent.storage.store import OutboxRetentionExpectation


def _row(
    key: str,
    *,
    idea_id: str = "idea-1",
    workspace_id: str = "workspace-1",
    completed_at: int = 100,
    status: str = "completed",
    task_type: str = "memory.extraction",
    state_version: int = 2,
) -> dict[str, object]:
    return {
        "outbox_id": f"outbox-{key}",
        "idempotency_key": key,
        "task_type": task_type,
        "status": status,
        "completed_at": completed_at,
        "state_version": state_version,
        "payload": {
            "idea_id": idea_id,
            "workspace_id": workspace_id,
            "fingerprint": key,
            "kind": task_type,
            "raw_text": "PRIVATE_RAW_TEXT",
            "provider_response": "PRIVATE_PROVIDER_RESPONSE",
        },
    }


class FakeRetentionRepository:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows
        self.deleted: list[OutboxRetentionExpectation] = []
        self.list_calls: list[tuple[int, int | None, str | None]] = []

    def list_completed_outbox_before(
        self,
        *,
        task_type: str,
        completed_before: int,
        limit: int,
        after_completed_at: int | None = None,
        after_outbox_id: str | None = None,
    ) -> list[dict[str, object]]:
        self.list_calls.append((limit, after_completed_at, after_outbox_id))
        result = [
            row
            for row in self.rows
            if row["task_type"] == task_type
            and row["status"] == "completed"
            and isinstance(row["completed_at"], int)
            and row["completed_at"] <= completed_before
            and (
                after_completed_at is None
                or (row["completed_at"], row["outbox_id"])
                > (after_completed_at, after_outbox_id)
            )
        ]
        result.sort(key=lambda row: (row["completed_at"], row["outbox_id"]))
        return result[:limit]

    def delete_completed_outbox_batch(
        self,
        expectations: list[OutboxRetentionExpectation],
        *,
        cutoff: int,
    ) -> int:
        for expectation in expectations:
            row = next((item for item in self.rows if item["outbox_id"] == expectation.outbox_id), None)
            if row is None or row["state_version"] != expectation.expected_state_version:
                raise OutboxRetentionConflictError("stale")
            if row["completed_at"] != expectation.completed_at or row["completed_at"] > cutoff:
                raise OutboxRetentionConflictError("stale")
        ids = {item.outbox_id for item in expectations}
        self.rows[:] = [row for row in self.rows if row["outbox_id"] not in ids]
        self.deleted.extend(expectations)
        return len(expectations)


def _terminal(idea_id: str, workspace_id: str, fingerprint: str) -> dict[str, object]:
    return {
        "event_type": EVENT_MEMORY_EXTRACTION_COMPLETED,
        "idea_id": idea_id,
        "payload": {"workspace_id": workspace_id, "fingerprint": fingerprint},
    }


def _service(repo: FakeRetentionRepository, terminal_keys: set[str] | None = None) -> OutboxRetentionService:
    keys = terminal_keys if terminal_keys is not None else {str(row["idempotency_key"]) for row in repo.rows}
    return OutboxRetentionService(
        repo,
        lambda idea, workspace, key: _terminal(idea, workspace, key) if key in keys else None,
        clock=lambda: 10 * 86400,
    )


def test_preview_is_stable_bounded_and_blocks_unproven_rows() -> None:
    repo = FakeRetentionRepository([
        _row("old-a", completed_at=1),
        _row("old-b", completed_at=2),
        _row("blocked", completed_at=3),
        _row("recent", completed_at=10 * 86400),
        _row("failed", completed_at=1, status="failed"),
    ])
    service = _service(repo, {"old-a", "old-b"})
    request = OutboxRetentionPreviewRequest("idea-1", "workspace-1", retention_days=7, limit=100)
    first = service.preview(request)
    second = service.preview(request)

    assert [item.idempotency_key for item in first.items] == ["old-a", "old-b"]
    assert first.eligible_count == 2
    assert first.blocked_count == 1
    assert first.scanned_count == 3
    assert first.selection_token == second.selection_token
    assert first.to_dict()["items"][0]["idempotency_key"] == "old-a"
    encoded = str(first.to_dict())
    assert "PRIVATE_RAW_TEXT" not in encoded
    assert "PRIVATE_PROVIDER_RESPONSE" not in encoded
    assert repo.list_calls[0][0] == MAX_SCAN_ROWS


def test_cursor_can_cross_a_fully_blocked_page() -> None:
    rows = [_row(f"blocked-{index:04d}", completed_at=index) for index in range(MAX_SCAN_ROWS)]
    rows.append(_row("eligible-after-page", completed_at=MAX_SCAN_ROWS + 1))
    repo = FakeRetentionRepository(rows)
    service = _service(repo, {"eligible-after-page"})

    first = service.preview(OutboxRetentionPreviewRequest("idea-1", "workspace-1", retention_days=7))
    assert first.items == ()
    assert first.blocked_count == MAX_SCAN_ROWS
    assert first.scan_truncated is True
    assert first.next_cursor

    second = service.preview(
        OutboxRetentionPreviewRequest("idea-1", "workspace-1", retention_days=7, cursor=first.next_cursor)
    )
    assert [item.idempotency_key for item in second.items] == ["eligible-after-page"]
    assert second.scan_truncated is False


def test_output_limit_exposes_cursor_for_remaining_rows() -> None:
    repo = FakeRetentionRepository([
        _row("first", completed_at=1),
        _row("second", completed_at=2),
        _row("third", completed_at=3),
    ])
    service = _service(repo)

    first = service.preview(
        OutboxRetentionPreviewRequest("idea-1", "workspace-1", retention_days=7, limit=2)
    )
    assert [item.idempotency_key for item in first.items] == ["first", "second"]
    assert first.scanned_count == 2
    assert first.scan_truncated is True
    assert first.next_cursor

    second = service.preview(
        OutboxRetentionPreviewRequest(
            "idea-1", "workspace-1", retention_days=7, limit=2, cursor=first.next_cursor
        )
    )
    assert [item.idempotency_key for item in second.items] == ["third"]
    assert second.scan_truncated is False


def test_cursor_page_does_not_call_clock_again() -> None:
    repo = FakeRetentionRepository([_row("first", completed_at=1), _row("second", completed_at=2)])
    calls = [0]

    def clock() -> int:
        calls[0] += 1
        return 10 * 86400

    service = OutboxRetentionService(
        repo,
        lambda idea, workspace, key: _terminal(idea, workspace, key),
        clock=clock,
    )
    first = service.preview(
        OutboxRetentionPreviewRequest("idea-1", "workspace-1", retention_days=7, limit=1)
    )
    assert first.next_cursor
    service.preview(
        OutboxRetentionPreviewRequest(
            "idea-1", "workspace-1", retention_days=7, limit=1, cursor=first.next_cursor
        )
    )
    assert calls == [1]


def test_cursor_rejects_tampering_and_scope_mismatch() -> None:
    repo = FakeRetentionRepository([_row("first", completed_at=1), _row("second", completed_at=2)])
    service = _service(repo)
    first = service.preview(
        OutboxRetentionPreviewRequest("idea-1", "workspace-1", retention_days=7, limit=1)
    )
    assert first.next_cursor

    for cursor in ("not-a-cursor", f"{first.next_cursor}!"):
        with pytest.raises(OutboxRetentionValidationError):
            service.preview(
                OutboxRetentionPreviewRequest(
                    "idea-1", "workspace-1", retention_days=7, limit=1, cursor=cursor
                )
            )
    with pytest.raises(OutboxRetentionValidationError, match="scope"):
        service.preview(
            OutboxRetentionPreviewRequest(
                "other-idea", "workspace-1", retention_days=7, limit=1, cursor=first.next_cursor
            )
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [("retention_days", 6), ("retention_days", 3651), ("limit", 0), ("limit", 101), ("limit", True)],
)
def test_preview_rejects_out_of_bounds_values(field: str, value: object) -> None:
    values: dict[str, object] = {"idea_id": "idea-1", "workspace_id": "workspace-1", field: value}
    with pytest.raises(OutboxRetentionValidationError):
        OutboxRetentionPreviewRequest(**values)


def test_prune_requires_fresh_token_and_is_atomic() -> None:
    repo = FakeRetentionRepository([_row("old-a", completed_at=1), _row("old-b", completed_at=2)])
    service = _service(repo)
    preview = service.preview(OutboxRetentionPreviewRequest("idea-1", "workspace-1", retention_days=7))
    stale = replace(
        OutboxRetentionPruneRequest(
            "idea-1", "workspace-1", preview.cutoff, 100, preview.selection_token, True
        ),
        selection_token="v1:stale",
    )
    with pytest.raises(OutboxRetentionConflictError):
        service.prune(stale)
    assert len(repo.rows) == 2

    result = service.prune(
        OutboxRetentionPruneRequest(
            "idea-1", "workspace-1", preview.cutoff, 100, preview.selection_token, True
        )
    )
    assert result.outcome == "pruned"
    assert result.deleted_count == 2
    assert repo.rows == []

    empty_preview = service.preview(OutboxRetentionPreviewRequest("idea-1", "workspace-1", retention_days=7))
    noop = service.prune(
        OutboxRetentionPruneRequest(
            "idea-1", "workspace-1", empty_preview.cutoff, 100, empty_preview.selection_token, True
        )
    )
    assert noop.outcome == "noop"
    assert noop.deleted_count == 0


def test_prune_acknowledgement_and_lookup_errors_fail_closed() -> None:
    repo = FakeRetentionRepository([_row("old", completed_at=1)])
    service = _service(repo)
    preview = service.preview(OutboxRetentionPreviewRequest("idea-1", "workspace-1", retention_days=7))
    with pytest.raises(OutboxRetentionValidationError):
        OutboxRetentionPruneRequest("idea-1", "workspace-1", preview.cutoff, 100, preview.selection_token, False)

    def broken_lookup(*_args: object) -> object:
        raise RuntimeError("provider secret must not be swallowed")

    broken = OutboxRetentionService(repo, broken_lookup, clock=lambda: 10 * 86400)
    with pytest.raises(RuntimeError):
        broken.preview(OutboxRetentionPreviewRequest("idea-1", "workspace-1", retention_days=7))
    assert len(repo.rows) == 1


def test_wrong_scope_and_unterminated_rows_are_blocked() -> None:
    repo = FakeRetentionRepository([
        _row("foreign", idea_id="other", completed_at=1),
        _row("unproven", completed_at=2),
    ])
    service = _service(repo, set())
    preview = service.preview(OutboxRetentionPreviewRequest("idea-1", "workspace-1", retention_days=7))
    assert preview.items == ()
    assert preview.blocked_count == 2
    assert len(repo.rows) == 2
