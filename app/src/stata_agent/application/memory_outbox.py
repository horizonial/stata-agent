"""Application-layer dispatch for durable memory-extraction intents.

The event ledger remains the business source of truth.  A durable outbox is
only a dispatch index keyed by the extraction fingerprint.  This module keeps
the worker and queue policy independent from SQLite: the storage adapter owns
the atomic ``requested event + outbox row`` write and implements the small
repository protocol below.

The dispatcher deliberately acknowledges a claim only after a terminal
memory-extraction event can be observed.  A provider result or a successful
callback return by itself is not delivery evidence.  Queue admission failures
release the lease through ``retry`` so FULL/CLOSED queues leave the intent
pending for a later pump.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from ..memory.pipeline import MemoryExtractionRequest
from ..privacy.modes import LOCAL_STRICT, PrivacyViolation, normalize_mode
from .task_queue import TaskQueue, TaskSubmitResult, TaskSubmitStatus


MEMORY_EXTRACTION_KIND = "memory.extraction"
DEFAULT_OUTBOX_LEASE_SECONDS = 30.0
DEFAULT_OUTBOX_CLAIM_LIMIT = 4


class MemoryOutboxError(RuntimeError):
    """Base error for an unavailable or malformed durable outbox adapter."""


class MemoryOutboxLeaseError(MemoryOutboxError):
    """A completion/retry could not be applied to the claimed lease."""


@dataclass(frozen=True)
class MemoryOutboxIntent:
    """Immutable dispatch metadata for one requested extraction.

    Source text is intentionally absent.  ``source_ids`` and sequence bounds
    let the pipeline reconstruct the request from the immutable ledger while
    keeping the outbox safe to inspect as an operational index.
    """

    idea_id: str
    workspace_id: str
    fingerprint: str
    from_seq: int
    to_seq: int
    source_ids: tuple[str, ...]
    prompt_version: str
    privacy_mode: str
    provider_name: str
    kind: str = MEMORY_EXTRACTION_KIND

    def __post_init__(self) -> None:
        for name in ("idea_id", "workspace_id", "fingerprint", "prompt_version", "provider_name"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} must not be blank")
        if self.kind != MEMORY_EXTRACTION_KIND:
            raise ValueError(f"unsupported outbox kind: {self.kind!r}")
        try:
            from_seq = int(self.from_seq)
            to_seq = int(self.to_seq)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("sequence bounds must be integers") from exc
        if from_seq < 0 or to_seq < from_seq:
            raise ValueError("invalid extraction sequence range")
        object.__setattr__(self, "from_seq", from_seq)
        object.__setattr__(self, "to_seq", to_seq)
        object.__setattr__(self, "source_ids", tuple(str(item) for item in self.source_ids))
        try:
            object.__setattr__(self, "privacy_mode", normalize_mode(self.privacy_mode))
        except PrivacyViolation as exc:
            raise ValueError("privacy_mode must be a supported privacy mode") from exc

    @classmethod
    def from_request(
        cls,
        request: MemoryExtractionRequest,
        *,
        privacy_mode: str,
        provider_name: str,
        kind: str = MEMORY_EXTRACTION_KIND,
    ) -> "MemoryOutboxIntent":
        """Freeze dispatch identity from a prepared pipeline request."""

        return cls(
            idea_id=request.idea_id,
            workspace_id=request.workspace_id,
            fingerprint=request.fingerprint,
            from_seq=request.from_seq,
            to_seq=request.to_seq,
            source_ids=tuple(source.source_id for source in request.sources),
            prompt_version=request.prompt_version,
            privacy_mode=privacy_mode,
            provider_name=provider_name,
            kind=kind,
        )

    @property
    def key(self) -> str:
        """The only idempotency key used by both outbox and task queue."""

        return self.fingerprint

    def as_payload(self) -> dict[str, Any]:
        """Return storage-safe metadata without source text."""

        return {
            "idea_id": self.idea_id,
            "workspace_id": self.workspace_id,
            "fingerprint": self.fingerprint,
            "from_seq": self.from_seq,
            "to_seq": self.to_seq,
            "source_ids": list(self.source_ids),
            "prompt_version": self.prompt_version,
            "privacy_mode": self.privacy_mode,
            "provider_name": self.provider_name,
            "kind": self.kind,
        }


@dataclass(frozen=True)
class MemoryOutboxClaim:
    """A leased intent handed to one process-local callback."""

    intent: MemoryOutboxIntent
    lease_token: str
    owner: str
    outbox_id: str | None = None

    def __post_init__(self) -> None:
        if not str(self.lease_token).strip():
            raise ValueError("lease_token must not be blank")
        if not str(self.owner).strip():
            raise ValueError("owner must not be blank")

    @property
    def key(self) -> str:
        return self.intent.key


class MemoryOutboxRepository(Protocol):
    """Storage boundary expected from the future SQLite outbox adapter.

    ``ensure_outbox_intent`` is idempotent.  A SQLite implementation should
    call it from the same transaction that appends the requested event (or
    expose an equivalent atomic combined method at its write boundary).
    ``backfill_outbox_intents`` scans historical requested events and inserts
    only requests that have no matching terminal event.
    """

    def ensure_outbox_intent(self, intent: MemoryOutboxIntent) -> bool:
        """Ensure one fingerprint-keyed pending intent exists."""

        ...

    def backfill_outbox_intents(
        self,
        *,
        kind: str,
        idea_id: str | None = None,
    ) -> int:
        """Backfill requested-without-terminal rows and return new count."""

        ...

    def claim(
        self,
        kind: str,
        owner: str,
        lease_seconds: float,
        *,
        limit: int = DEFAULT_OUTBOX_CLAIM_LIMIT,
    ) -> Sequence[MemoryOutboxClaim | Mapping[str, Any]]:
        """Atomically claim pending rows and return lease tokens."""

        ...

    def complete(self, kind: str, key: str, lease_token: str) -> bool:
        """Ack only a terminally completed claim."""

        ...

    def retry(
        self,
        kind: str,
        key: str,
        lease_token: str,
        *,
        error_code: str,
        delay_seconds: float = 0.0,
    ) -> bool:
        """Release a lease while preserving the intent as pending."""

        ...


Worker = Callable[[MemoryOutboxClaim], object]
TerminalChecker = Callable[[MemoryOutboxClaim], bool]


@dataclass(frozen=True)
class OutboxDispatchReport:
    """One pump snapshot; callback completions happen asynchronously."""

    claimed: int = 0
    submitted: int = 0
    retained_pending: int = 0
    retried: int = 0
    errors: int = 0


def _claim_from_mapping(value: Mapping[str, Any]) -> MemoryOutboxClaim:
    """Normalize common SQLite row/dict shapes without exposing them upward."""

    raw_intent = value.get("intent")
    if isinstance(raw_intent, MemoryOutboxIntent):
        intent = raw_intent
    else:
        payload = raw_intent if isinstance(raw_intent, Mapping) else value.get("payload")
        metadata = dict(payload) if isinstance(payload, Mapping) else {}
        # A row may keep the key/type outside its JSON payload.  Explicit row
        # columns win because they are the storage uniqueness boundary.
        for field, aliases in {
            "idea_id": ("idea_id",),
            "workspace_id": ("workspace_id",),
            "fingerprint": ("fingerprint", "idempotency_key", "key"),
            "from_seq": ("from_seq",),
            "to_seq": ("to_seq",),
            "source_ids": ("source_ids",),
            "prompt_version": ("prompt_version",),
            "privacy_mode": ("privacy_mode",),
            "provider_name": ("provider_name", "provider"),
            "kind": ("kind", "task_type"),
        }.items():
            for alias in aliases:
                if alias in value and value[alias] is not None:
                    metadata[field] = value[alias]
                    break
        intent = MemoryOutboxIntent(
            idea_id=str(metadata.get("idea_id") or ""),
            workspace_id=str(metadata.get("workspace_id") or ""),
            fingerprint=str(metadata.get("fingerprint") or ""),
            from_seq=metadata.get("from_seq", 0),
            to_seq=metadata.get("to_seq", 0),
            source_ids=tuple(metadata.get("source_ids") or ()),
            prompt_version=str(metadata.get("prompt_version") or ""),
            privacy_mode=str(metadata.get("privacy_mode") or ""),
            provider_name=str(metadata.get("provider_name") or ""),
            kind=str(metadata.get("kind") or MEMORY_EXTRACTION_KIND),
        )
    lease_token = str(value.get("lease_token") or value.get("token") or "")
    owner = str(value.get("owner") or value.get("lease_owner") or "")
    outbox_id = value.get("outbox_id")
    return MemoryOutboxClaim(
        intent=intent,
        lease_token=lease_token,
        owner=owner,
        outbox_id=str(outbox_id) if outbox_id is not None else None,
    )


def _coerce_claim(value: MemoryOutboxClaim | Mapping[str, Any]) -> MemoryOutboxClaim:
    if isinstance(value, MemoryOutboxClaim):
        return value
    if isinstance(value, Mapping):
        return _claim_from_mapping(value)
    raise MemoryOutboxError("outbox claim has an unsupported shape")


def _submit_status(value: object) -> TaskSubmitStatus:
    if isinstance(value, TaskSubmitResult):
        return value.status
    if isinstance(value, TaskSubmitStatus):
        return value
    # A legacy queue only returned bool.  False is intentionally treated as a
    # full/closed admission miss and therefore released back to pending.
    return TaskSubmitStatus.ACCEPTED if bool(value) else TaskSubmitStatus.FULL


class MemoryOutboxDispatcher:
    """Pump leased outbox intents into a bounded application task queue."""

    def __init__(
        self,
        repository: MemoryOutboxRepository,
        queue: TaskQueue,
        *,
        worker: Worker,
        terminal_checker: TerminalChecker,
        owner: str | None = None,
        lease_seconds: float = DEFAULT_OUTBOX_LEASE_SECONDS,
        claim_limit: int = DEFAULT_OUTBOX_CLAIM_LIMIT,
        kind: str = MEMORY_EXTRACTION_KIND,
    ) -> None:
        if not callable(worker):
            raise TypeError("worker must be callable")
        if not callable(terminal_checker):
            raise TypeError("terminal_checker must be callable")
        if isinstance(lease_seconds, bool) or float(lease_seconds) <= 0:
            raise ValueError("lease_seconds must be positive")
        if isinstance(claim_limit, bool) or int(claim_limit) <= 0:
            raise ValueError("claim_limit must be positive")
        self.repository = repository
        self.queue = queue
        self.worker = worker
        self.terminal_checker = terminal_checker
        self.owner = str(owner or f"memory-outbox-{uuid.uuid4().hex}")
        self.lease_seconds = float(lease_seconds)
        self.claim_limit = int(claim_limit)
        self.kind = str(kind)
        if self.kind != MEMORY_EXTRACTION_KIND:
            raise ValueError(f"unsupported outbox kind: {self.kind!r}")

    def ensure_intent(self, intent: MemoryOutboxIntent) -> bool:
        """Persist an idempotent dispatch index row for a requested event."""

        if intent.kind != self.kind:
            raise ValueError("intent kind does not match dispatcher kind")
        result = self.repository.ensure_outbox_intent(intent)
        # Adapters may use a command-style ``None`` return while still
        # guaranteeing idempotent persistence.  Treat that as success.
        return True if result is None else bool(result)

    def reconcile(self, *, idea_id: str | None = None) -> int:
        """Backfill every requested fingerprint lacking a terminal event."""

        return int(
            self.repository.backfill_outbox_intents(
                kind=self.kind,
                idea_id=idea_id,
            )
        )

    def pump(self, *, limit: int | None = None) -> OutboxDispatchReport:
        """Claim and admit work without blocking on a full/closed queue."""

        claim_limit = self.claim_limit if limit is None else int(limit)
        if claim_limit <= 0:
            raise ValueError("limit must be positive")
        raw_claims = self.repository.claim(
            self.kind,
            self.owner,
            self.lease_seconds,
            limit=claim_limit,
        )
        submitted = retained = retried = errors = 0
        for raw_claim in raw_claims:
            claim: MemoryOutboxClaim | None = None
            try:
                claim = _coerce_claim(raw_claim)
                result = self.queue.submit(
                    claim.key,
                    self._callback(claim),
                )
                status = _submit_status(result)
                if status is TaskSubmitStatus.ACCEPTED:
                    submitted += 1
                    continue
                # FULL, CLOSED and DUPLICATE all mean this process did not own
                # callback execution.  Release the lease; the row stays
                # pending and can be claimed by a later pump/process.
                code = {
                    TaskSubmitStatus.FULL: "queue_full",
                    TaskSubmitStatus.CLOSED: "queue_closed",
                    TaskSubmitStatus.DUPLICATE: "queue_duplicate",
                }.get(status, "queue_not_admitted")
                if self._retry(claim, error_code=code):
                    retried += 1
                    retained += 1
                else:
                    errors += 1
            except Exception:
                errors += 1
                # A malformed claim cannot safely be acknowledged.  It is
                # already leased, so retry it only when normalization yielded
                # a valid claim with a usable token.
                if claim is not None and self._retry(claim, error_code="dispatch_error"):
                    retried += 1
                    retained += 1
        return OutboxDispatchReport(
            claimed=len(raw_claims),
            submitted=submitted,
            retained_pending=retained,
            retried=retried,
            errors=errors,
        )

    def start(self, *, idea_id: str | None = None, pump: bool = True) -> OutboxDispatchReport:
        """Resume the queue, reconcile durable intent, then optionally pump."""

        resume = getattr(self.queue, "resume", None)
        if callable(resume):
            resume()
        self.reconcile(idea_id=idea_id)
        return self.pump() if pump else OutboxDispatchReport()

    def shutdown(self, *, wait: bool = True, timeout: float | None = None) -> None:
        """Delegate bounded shutdown to the replaceable task queue."""

        self.queue.shutdown(wait=wait, timeout=timeout)

    close = shutdown

    def _execute_claim(self, claim: MemoryOutboxClaim) -> None:
        try:
            # The callback receives the immutable intent, including the
            # original privacy mode and provider identity.  It must not read
            # current process configuration to relax those frozen fields.
            self.worker(claim)
        except Exception as exc:  # noqa: BLE001 - retry is the durable outcome
            self._retry(claim, error_code=_error_code(exc, "worker_error"))
            return
        try:
            terminal = bool(self.terminal_checker(claim))
        except Exception:
            terminal = False
        if terminal:
            if not self.repository.complete(self.kind, claim.key, claim.lease_token):
                raise MemoryOutboxLeaseError("terminal event observed but outbox completion was fenced")
            return
        self._retry(claim, error_code="terminal_event_missing")

    def _callback(self, claim: MemoryOutboxClaim) -> Callable[[], object]:
        """Bind one immutable claim for a queue callback (and for mypy)."""

        def run() -> object:
            self._execute_claim(claim)
            return None

        return run

    def _retry(self, claim: MemoryOutboxClaim, *, error_code: str) -> bool:
        return bool(
            self.repository.retry(
                self.kind,
                claim.key,
                claim.lease_token,
                error_code=error_code,
                delay_seconds=0.0,
            )
        )


def _error_code(error: Exception, fallback: str) -> str:
    value = str(getattr(error, "code", "") or "").strip()
    if value:
        return value[:80]
    return fallback


class FakeMemoryOutboxRepository:
    """Small deterministic repository for application and contract tests.

    It models leases and pending preservation but does not pretend to be a
    SQLite transaction.  Production storage adapters must implement the same
    protocol with an atomic requested-event/outbox write.
    """

    def __init__(self) -> None:
        self.intents: dict[str, MemoryOutboxIntent] = {}
        self.status: dict[str, str] = {}
        self.claims: dict[str, MemoryOutboxClaim] = {}
        self.terminal: set[str] = set()
        self.retries: list[tuple[str, str]] = []
        self.completions: list[str] = []
        self.backfill_count = 0

    def ensure_outbox_intent(self, intent: MemoryOutboxIntent) -> bool:
        if intent.key in self.intents:
            return False
        self.intents[intent.key] = intent
        self.status[intent.key] = "pending"
        return True

    def backfill_outbox_intents(self, *, kind: str, idea_id: str | None = None) -> int:
        del kind, idea_id
        return self.backfill_count

    def claim(
        self,
        kind: str,
        owner: str,
        lease_seconds: float,
        *,
        limit: int = DEFAULT_OUTBOX_CLAIM_LIMIT,
    ) -> list[MemoryOutboxClaim]:
        del lease_seconds
        result: list[MemoryOutboxClaim] = []
        for key, intent in self.intents.items():
            if len(result) >= limit or self.status.get(key) != "pending" or intent.kind != kind:
                continue
            claim = MemoryOutboxClaim(
                intent=intent,
                lease_token=uuid.uuid4().hex,
                owner=owner,
                outbox_id=key,
            )
            self.status[key] = "processing"
            self.claims[key] = claim
            result.append(claim)
        return result

    def complete(self, kind: str, key: str, lease_token: str) -> bool:
        claim = self.claims.get(key)
        if claim is None or claim.intent.kind != kind or claim.lease_token != lease_token:
            return False
        self.status[key] = "completed"
        self.completions.append(key)
        return True

    def retry(
        self,
        kind: str,
        key: str,
        lease_token: str,
        *,
        error_code: str,
        delay_seconds: float = 0.0,
    ) -> bool:
        del delay_seconds
        claim = self.claims.get(key)
        if claim is None or claim.intent.kind != kind or claim.lease_token != lease_token:
            return False
        self.status[key] = "pending"
        self.retries.append((key, error_code))
        return True


class SQLiteMemoryOutboxRepository:
    """Adapt the generic application port to the SQLite task-outbox API.

    This adapter is intentionally kept beside the port so the UI composition
    root only wires factories.  Each operation opens a fresh store session;
    leased callbacks can therefore complete after the post-turn writer closes.
    """

    def __init__(
        self,
        store_factory: Callable[[], Any],
        *,
        idea_ids_factory: Callable[[], list[str]] | None = None,
    ) -> None:
        self._store_factory = store_factory
        self._idea_ids_factory = idea_ids_factory or (lambda: [])

    @staticmethod
    def to_storage_intent(intent: MemoryOutboxIntent) -> Any:
        from ..storage.store import OutboxIntent

        return OutboxIntent(
            idempotency_key=intent.key,
            task_type=intent.kind,
            payload=intent.as_payload(),
        )

    @staticmethod
    def _close(store: Any) -> None:
        close = getattr(store, "close", None)
        if callable(close):
            close()

    @staticmethod
    def _frozen_metadata(payload: Mapping[str, Any]) -> tuple[str, str]:
        mode = payload.get("privacy_mode")
        provider = payload.get("provider_name") or payload.get("provider")
        # A legacy request that lacks either half of the freeze must not be
        # upgraded to a more permissive mode during recovery.
        if not isinstance(mode, str) or not mode.strip() or not isinstance(provider, str) or not provider.strip():
            return LOCAL_STRICT, "local"
        try:
            return normalize_mode(mode), provider.strip().lower()[:80]
        except PrivacyViolation:
            return LOCAL_STRICT, "local"

    @classmethod
    def _intent_from_payload(
        cls,
        *,
        key: str,
        task_type: str,
        payload: Mapping[str, Any],
    ) -> MemoryOutboxIntent:
        mode, provider = cls._frozen_metadata(payload)
        source_ids = payload.get("source_ids")
        if not isinstance(source_ids, (list, tuple)):
            source_ids = ()
        return MemoryOutboxIntent(
            idea_id=str(payload.get("idea_id") or ""),
            workspace_id=str(payload.get("workspace_id") or ""),
            fingerprint=str(payload.get("fingerprint") or key),
            from_seq=payload.get("from_seq", 0),
            to_seq=payload.get("to_seq", 0),
            source_ids=tuple(str(item) for item in source_ids),
            prompt_version=str(payload.get("prompt_version") or "memory-extraction-v1"),
            privacy_mode=mode,
            provider_name=provider,
            kind=task_type,
        )

    def ensure_outbox_intent(self, intent: MemoryOutboxIntent) -> bool:
        store = self._store_factory()
        try:
            result = store.enqueue_outbox(self.to_storage_intent(intent))
            return bool(getattr(result, "accepted", result))
        finally:
            self._close(store)

    def backfill_outbox_intents(
        self,
        *,
        kind: str,
        idea_id: str | None = None,
    ) -> int:
        from ..events.schema import (
            EVENT_MEMORY_EXTRACTION_COMPLETED,
            EVENT_MEMORY_EXTRACTION_DENIED,
            EVENT_MEMORY_EXTRACTION_FAILED,
            EVENT_MEMORY_EXTRACTION_NOOP,
            EVENT_MEMORY_EXTRACTION_REQUESTED,
        )
        from ..memory.pipeline import MemoryExtractionPipeline

        if kind != MEMORY_EXTRACTION_KIND:
            return 0
        ideas = [idea_id] if idea_id is not None else list(dict.fromkeys(self._idea_ids_factory()))
        terminal_types = {
            EVENT_MEMORY_EXTRACTION_COMPLETED,
            EVENT_MEMORY_EXTRACTION_DENIED,
            EVENT_MEMORY_EXTRACTION_FAILED,
            EVENT_MEMORY_EXTRACTION_NOOP,
        }
        pipeline = MemoryExtractionPipeline()
        store = self._store_factory()
        try:
            intents: list[Any] = []
            for current in dict.fromkeys(str(item) for item in ideas if str(item).strip()):
                events = list(store.scan(current))
                terminal = {
                    str(event.fingerprint or (event.payload or {}).get("fingerprint") or "")
                    for event in events
                    if event.event_type in terminal_types
                }
                for event in events:
                    if event.event_type != EVENT_MEMORY_EXTRACTION_REQUESTED:
                        continue
                    payload = event.payload or {}
                    key = str(event.fingerprint or payload.get("fingerprint") or "")
                    if not key or key in terminal:
                        continue
                    workspace_id = str(payload.get("workspace_id") or "")
                    if not workspace_id:
                        continue
                    request = pipeline.request_for_fingerprint(
                        store=store,
                        idea_id=current,
                        workspace_id=workspace_id,
                        fingerprint=key,
                    )
                    if request is None:
                        continue
                    mode, provider = self._frozen_metadata(payload)
                    intents.append(
                        self.to_storage_intent(
                            MemoryOutboxIntent.from_request(
                                request,
                                privacy_mode=mode,
                                provider_name=provider,
                            )
                        )
                    )
            if not intents:
                return 0
            results = store.backfill_outbox(intents)
            return sum(1 for result in results if bool(getattr(result, "accepted", result)))
        finally:
            self._close(store)

    def claim(
        self,
        kind: str,
        owner: str,
        lease_seconds: float,
        *,
        limit: int = DEFAULT_OUTBOX_CLAIM_LIMIT,
    ) -> list[MemoryOutboxClaim]:
        store = self._store_factory()
        try:
            records = store.claim_outbox(
                str(owner),
                limit=int(limit),
                lease_seconds=max(1, int(lease_seconds)),
            )
            claims: list[MemoryOutboxClaim] = []
            for record in records:
                if record.task_type != kind or not record.lease_token:
                    if record.lease_token:
                        try:
                            store.retry_outbox(record, record.lease_token, error="unsupported_task_type")
                        except Exception:  # noqa: BLE001 - best-effort release
                            pass
                    continue
                payload = record.payload if isinstance(record.payload, Mapping) else {}
                try:
                    intent = self._intent_from_payload(
                        key=record.idempotency_key,
                        task_type=record.task_type,
                        payload=payload,
                    )
                    if not intent.idea_id or not intent.workspace_id:
                        raise ValueError("outbox metadata lacks workspace identity")
                    claims.append(
                        MemoryOutboxClaim(
                            intent=intent,
                            lease_token=record.lease_token,
                            owner=str(record.lease_owner or owner),
                            outbox_id=record.outbox_id,
                        )
                    )
                except (TypeError, ValueError):
                    try:
                        store.retry_outbox(record, record.lease_token, error="invalid_outbox_metadata")
                    except Exception:  # noqa: BLE001 - best-effort release
                        pass
            return claims
        finally:
            self._close(store)

    def complete(self, kind: str, key: str, lease_token: str) -> bool:
        store = self._store_factory()
        try:
            record = store.get_outbox(idempotency_key=key)
            if record is None or record.task_type != kind:
                return False
            store.complete_outbox(record, lease_token)
            return True
        except Exception:  # noqa: BLE001 - stale leases are not acknowledgements
            return False
        finally:
            self._close(store)

    def retry(
        self,
        kind: str,
        key: str,
        lease_token: str,
        *,
        error_code: str,
        delay_seconds: float = 0.0,
    ) -> bool:
        store = self._store_factory()
        try:
            record = store.get_outbox(idempotency_key=key)
            if record is None or record.task_type != kind:
                return False
            store.retry_outbox(
                record,
                lease_token,
                error=error_code,
                delay_seconds=max(0, int(delay_seconds)),
            )
            return True
        except Exception:  # noqa: BLE001 - stale leases are not acknowledgements
            return False
        finally:
            self._close(store)


__all__ = [
    "DEFAULT_OUTBOX_CLAIM_LIMIT",
    "DEFAULT_OUTBOX_LEASE_SECONDS",
    "FakeMemoryOutboxRepository",
    "MEMORY_EXTRACTION_KIND",
    "MemoryOutboxClaim",
    "MemoryOutboxDispatcher",
    "MemoryOutboxError",
    "MemoryOutboxIntent",
    "MemoryOutboxLeaseError",
    "MemoryOutboxRepository",
    "OutboxDispatchReport",
    "SQLiteMemoryOutboxRepository",
]
