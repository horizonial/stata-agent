from __future__ import annotations

from dataclasses import dataclass, replace
from types import SimpleNamespace

import pytest

from stata_agent.application.outbox_recovery import (
    MEMORY_EXTRACTION_KIND,
    OutboxRecoveryConflictError,
    OutboxRecoveryNotFoundError,
    OutboxRecoveryRequest,
    OutboxRecoveryService,
    OutboxRecoveryValidationError,
    TERMINAL_EXTRACTION_EVENTS,
)


@dataclass(frozen=True)
class FakeRow:
    outbox_id: str
    idempotency_key: str
    task_type: str
    payload: object
    status: str = "failed"
    attempt_count: int = 3
    max_attempts: int = 3
    available_at: int = 0
    lease_owner: str | None = None
    lease_token: str | None = None
    lease_until: int | None = None
    last_error: str | None = None
    event_seq: int | None = 1
    created_at: int = 1
    updated_at: int = 2
    completed_at: int | None = None
    state_version: int = 7


def _row(
    key: str = "sha256:one",
    *,
    idea_id: str = "idea-1",
    workspace_id: str = "workspace-1",
    created_at: int = 1,
    status: str = "failed",
    task_type: str = MEMORY_EXTRACTION_KIND,
    last_error: str | None = "worker_error",
    state_version: int = 7,
    **payload: object,
) -> FakeRow:
    metadata = {
        "idea_id": idea_id,
        "workspace_id": workspace_id,
        "fingerprint": key,
        "kind": task_type,
        "prompt_version": "memory-extraction-v1",
        "source_ids": ["event-1"],
        **payload,
    }
    return FakeRow(
        outbox_id=f"outbox-{key.rsplit(':', 1)[-1]}",
        idempotency_key=key,
        task_type=task_type,
        payload=metadata,
        status=status,
        attempt_count=3,
        max_attempts=3,
        last_error=last_error,
        created_at=created_at,
        updated_at=created_at + 1,
        state_version=state_version,
    )


class FakeRepository:
    def __init__(self, rows: list[FakeRow]) -> None:
        self.rows = {row.idempotency_key: row for row in rows}
        self.reconciled: list[tuple[str, str, int]] = []
        self.redriven: list[tuple[str, str, int]] = []

    def list_outbox(self, *, status: str | None = None, limit: int | None = None):
        del limit
        rows = list(self.rows.values())
        return [row for row in rows if status is None or row.status == status]

    def get_outbox(self, *, idempotency_key: str):
        return self.rows.get(idempotency_key)

    def _transition(self, key: str, task_type: str, expected: int, *, status: str, attempts: int) -> FakeRow:
        row = self.rows[key]
        if (
            row.task_type != task_type
            or row.status != "failed"
            or row.state_version != expected
        ):
            raise RuntimeError("CAS conflict")
        transitioned = replace(row, status=status, attempt_count=attempts, state_version=expected + 1)
        self.rows[key] = transitioned
        return transitioned

    def reconcile_failed_outbox(self, key: str, task_type: str, expected: int):
        self.reconciled.append((key, task_type, expected))
        return self._transition(key, task_type, expected, status="completed", attempts=3)

    def redrive_failed_outbox(self, key: str, task_type: str, expected: int):
        self.redriven.append((key, task_type, expected))
        return self._transition(key, task_type, expected, status="pending", attempts=0)


class FakeTerminalLookup:
    def __init__(self, event: object | None = None, *, error: Exception | None = None) -> None:
        self.event = event
        self.error = error
        self.calls: list[tuple[str, str, str]] = []

    def find_terminal_event(self, idea_id: str, workspace_id: str, fingerprint: str):
        self.calls.append((idea_id, workspace_id, fingerprint))
        if self.error is not None:
            raise self.error
        return self.event


def _service(
    rows: list[FakeRow],
    event: object | None = None,
    *,
    lookup_error: Exception | None = None,
) -> tuple[OutboxRecoveryService, FakeRepository, FakeTerminalLookup]:
    repository = FakeRepository(rows)
    lookup = FakeTerminalLookup(event, error=lookup_error)
    return OutboxRecoveryService(repository, lookup), repository, lookup


def _request(row: FakeRow, *, acknowledge: object = True, version: int | None = None) -> OutboxRecoveryRequest:
    return OutboxRecoveryRequest(
        idea_id="idea-1",
        workspace_id="workspace-1",
        idempotency_key=row.idempotency_key,
        expected_state_version=row.state_version if version is None else version,
        acknowledge_at_least_once=acknowledge,  # type: ignore[arg-type]
    )


def test_failed_listing_is_scoped_stable_and_safe() -> None:
    newest = _row("sha256:new", created_at=20, last_error="provider_error")
    oldest = _row(
        "sha256:old",
        created_at=10,
        last_error="API_KEY=do-not-return",
        prompt="do-not-return-prompt",
        provider_response="do-not-return-provider-response",
    )
    other_workspace = _row("sha256:other", created_at=1, workspace_id="workspace-2")
    wrong_kind = _row("sha256:wrong", created_at=0, task_type="memory.review")
    pending = _row("sha256:pending", created_at=0, status="pending")
    service, _repository, lookup = _service(
        [newest, oldest, other_workspace, wrong_kind, pending],
        event=None,
    )

    items = service.list_failed("idea-1", "workspace-1", limit=100)

    assert [item.idempotency_key for item in items] == ["sha256:old", "sha256:new"]
    assert items[0].error_code == "delivery_failed"
    assert items[1].error_code == "provider_error"
    assert items[0].terminal_present is False
    assert lookup.calls == [
        ("idea-1", "workspace-1", "sha256:old"),
        ("idea-1", "workspace-1", "sha256:new"),
    ]
    serialized = str(items[0].to_dict())
    assert set(items[0].to_dict()) == {
        "outbox_id",
        "idempotency_key",
        "fingerprint",
        "idea_id",
        "workspace_id",
        "attempt_count",
        "max_attempts",
        "created_at",
        "updated_at",
        "state_version",
        "error_code",
        "terminal_present",
    }
    assert "do-not-return" not in serialized
    assert "API_KEY" not in serialized


@pytest.mark.parametrize("event_type", sorted(TERMINAL_EXTRACTION_EVENTS))
def test_terminal_event_reconciles_without_redrive(event_type: str) -> None:
    row = _row("sha256:terminal")
    event = SimpleNamespace(
        event_type=event_type,
        idea_id="idea-1",
        fingerprint=row.idempotency_key,
        payload={"workspace_id": "workspace-1", "fingerprint": row.idempotency_key},
    )
    service, repository, lookup = _service([row], event=event)

    result = service.recover(_request(row))

    assert result.outcome == "reconciled"
    assert result.status == "completed"
    assert result.item.terminal_present is True
    assert result.item.state_version == row.state_version + 1
    assert repository.reconciled == [(row.idempotency_key, MEMORY_EXTRACTION_KIND, row.state_version)]
    assert repository.redriven == []
    assert lookup.calls == [("idea-1", "workspace-1", row.idempotency_key)]


def test_missing_terminal_redrives_to_normal_pending_path() -> None:
    row = _row("sha256:redrive", last_error="terminal_event_missing")
    service, repository, _lookup = _service([row], event=None)

    result = service.recover(_request(row))

    assert result.outcome == "redriven"
    assert result.status == "pending"
    assert result.item.attempt_count == 0
    assert result.item.max_attempts == row.max_attempts
    assert result.item.state_version == row.state_version + 1
    assert repository.reconciled == []
    assert repository.redriven == [(row.idempotency_key, MEMORY_EXTRACTION_KIND, row.state_version)]


@pytest.mark.parametrize(
    ("idea_id", "workspace_id", "expected_error"),
    [
        ("idea-2", "workspace-1", OutboxRecoveryConflictError),
        ("idea-1", "workspace-2", OutboxRecoveryConflictError),
    ],
)
def test_cross_scope_recovery_fails_closed(idea_id: str, workspace_id: str, expected_error: type[Exception]) -> None:
    row = _row("sha256:scope")
    service, repository, _lookup = _service([row])
    request = OutboxRecoveryRequest(
        idea_id=idea_id,
        workspace_id=workspace_id,
        idempotency_key=row.idempotency_key,
        expected_state_version=row.state_version,
        acknowledge_at_least_once=True,
    )

    with pytest.raises(expected_error):
        service.recover(request)
    assert repository.reconciled == []
    assert repository.redriven == []


@pytest.mark.parametrize("acknowledge", [False, 1, "true", None])
def test_recovery_requires_literal_acknowledgement(acknowledge: object) -> None:
    with pytest.raises(OutboxRecoveryValidationError):
        _request(_row("sha256:ack"), acknowledge=acknowledge)


@pytest.mark.parametrize("limit", [0, 101, True, 1.0, "1"])
def test_listing_limit_is_bounded_and_strict(limit: object) -> None:
    with pytest.raises(OutboxRecoveryValidationError):
        # The constructor validates the scope and bound before a repository is touched.
        from stata_agent.application.outbox_recovery import FailedOutboxListRequest

        FailedOutboxListRequest("idea-1", "workspace-1", limit)  # type: ignore[arg-type]


def test_missing_and_non_failed_rows_are_conflicts_without_mutation() -> None:
    pending = _row("sha256:pending", status="pending")
    service, repository, _lookup = _service([pending])
    missing = OutboxRecoveryRequest(
        "idea-1", "workspace-1", "sha256:missing", 0, True
    )
    with pytest.raises(OutboxRecoveryNotFoundError):
        service.recover(missing)
    with pytest.raises(OutboxRecoveryConflictError):
        service.recover(_request(pending))
    assert repository.reconciled == []
    assert repository.redriven == []


def test_payload_identity_mismatch_is_rejected_for_list_and_recover() -> None:
    malformed = _row("sha256:malformed", fingerprint="wrong-fingerprint")
    service, repository, _lookup = _service([malformed])

    with pytest.raises(OutboxRecoveryConflictError):
        service.list_failed("idea-1", "workspace-1")
    with pytest.raises(OutboxRecoveryConflictError):
        service.recover(_request(malformed))
    assert repository.reconciled == []
    assert repository.redriven == []


def test_terminal_lookup_errors_are_not_converted_to_redrive() -> None:
    row = _row("sha256:lookup-error")
    error = RuntimeError("ledger unavailable")
    service, repository, _lookup = _service([row], lookup_error=error)

    with pytest.raises(RuntimeError, match="ledger unavailable"):
        service.recover(_request(row))
    assert repository.reconciled == []
    assert repository.redriven == []


def test_terminal_lookup_only_accepts_terminal_extraction_events() -> None:
    row = _row("sha256:non-terminal")
    event = SimpleNamespace(
        event_type="memory.candidate.review.completed",
        idea_id="idea-1",
        fingerprint=row.idempotency_key,
        payload={"workspace_id": "workspace-1"},
    )
    service, repository, _lookup = _service([row], event=event)

    result = service.recover(_request(row))

    assert result.outcome == "redriven"
    assert repository.redriven == [(row.idempotency_key, MEMORY_EXTRACTION_KIND, row.state_version)]


def test_callable_terminal_lookup_port_is_supported() -> None:
    row = _row("sha256:callable")
    calls: list[tuple[str, str, str]] = []

    def lookup(idea_id: str, workspace_id: str, fingerprint: str) -> bool:
        calls.append((idea_id, workspace_id, fingerprint))
        return True

    repository = FakeRepository([row])
    service = OutboxRecoveryService(repository, lookup)
    result = service.recover(_request(row))

    assert result.outcome == "reconciled"
    assert calls == [("idea-1", "workspace-1", row.idempotency_key)]


def test_stale_generation_is_rejected_before_terminal_lookup() -> None:
    row = _row("sha256:stale", state_version=9)
    service, repository, lookup = _service([row], event=True)

    with pytest.raises(OutboxRecoveryConflictError):
        service.recover(_request(row, version=8))
    assert lookup.calls == []
    assert repository.reconciled == []
    assert repository.redriven == []

