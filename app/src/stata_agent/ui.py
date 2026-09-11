"""FastAPI UI for the single-idea empirical-research workbench.

The server deliberately exposes read-only projections of the event ledger. UI
actions that change research state continue to go through the runner; the
browser never writes directly to the events table or to a projection.

The page itself lives in ``webui/index.html`` with local CSS and ES module
JavaScript. Keeping those files separate makes the interface easy to inspect
and keeps the FastAPI layer small enough to hand over to the next maintainer.
"""

from __future__ import annotations

import asyncio
import json
import hashlib
import os
import re
import sqlite3
import threading
import time
import uuid
import secrets
import urllib.parse
from collections.abc import Callable, Mapping
from contextlib import asynccontextmanager, contextmanager, nullcontext, suppress
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

from .application.agent_budget import (
    GOAL_MAX_STEPS_DEFAULT,
    INTERACTIVE_MAX_STEPS_DEFAULT,
    MAX_TOOL_CALLS_DEFAULT,
)
from .application.chat_service import ChatService, ChatTurnRequest, terminal_outcome_for
from .application.attachment_service import (
    AttachmentConflictError,
    AttachmentLimitError,
    AttachmentService,
    AttachmentPaths,
    AttachmentUnsupportedError,
    AttachmentValidationError,
    ERROR_INTERNAL,
    ERROR_FILE_TOO_LARGE,
    ERROR_PARSER_CRASHED,
    ERROR_PARSER_TIMEOUT,
    ERROR_WORKSPACE_QUOTA,
)
from .application.diagnostics import (
    DiagnosticBundleRequest,
    DiagnosticBundleService,
    DiagnosticReadError,
    DiagnosticValidationError,
)
from .application.local_task_queue import LocalTaskQueue
from .application.memory_outbox import (
    MemoryOutboxClaim,
    MemoryOutboxDeferred,
    MemoryOutboxDispatcher,
    MemoryOutboxIntent,
    SQLiteMemoryOutboxRepository,
)
from .application.outbox_recovery import (
    OutboxRecoveryConflictError,
    OutboxRecoveryNotFoundError,
    OutboxRecoveryRequest,
    OutboxRecoveryService,
    OutboxRecoveryValidationError,
)
from .application.outbox_retention import (
    DEFAULT_RETENTION_DAYS,
    OutboxRetentionConflictError,
    OutboxRetentionPreviewRequest,
    OutboxRetentionPruneRequest,
    OutboxRetentionService,
    OutboxRetentionValidationError,
)
from .application.request_control import RequestControlNotFound, RequestControlRegistry
from .application.task_queue import TaskQueue, TaskSubmitResult
from .application.workspace_service import (
    CreateWorkspaceRequest,
    WorkspaceConflictError,
    WorkspaceNotFoundError,
    WorkspaceService,
    WorkspaceValidationError,
)
from .application.settings import (
    EffectiveSettings,
    SettingsEnvironmentManagedError,
    SettingsPatch,
    SettingsReadOnlyError,
    SettingsRevisionConflict,
    SettingsService,
    SettingsValidationError,
)
from .application.trace_projection import TraceActivityQuery, TraceProjectionService
from .domain.action import Act, ActionProposal
from .events.schema import (
    ACTOR_ORCH,
    ACTOR_USER,
    EVENT_AGENT_STEP,
    EVENT_APPROVAL_GRANT,
    EVENT_APPROVAL_REJECT,
    EVENT_APPROVAL_REQ,
    EVENT_BUDGET,
    EVENT_CARD_SIGNED,
    EVENT_CLAIM_SIGNED,
    EVENT_CONTEXT_ASSEMBLED,
    EVENT_HEALTH,
    EVENT_IDEA,
    EVENT_MEMORY_EXTRACTION_COMPLETED,
    EVENT_MEMORY_EXTRACTION_DENIED,
    EVENT_MEMORY_EXTRACTION_FAILED,
    EVENT_MEMORY_EXTRACTION_NOOP,
    EVENT_MEMORY_REVIEW_COMPLETED,
    EVENT_MEMORY_REVIEW_FAILED,
    EVENT_MEMORY_REVIEW_REQUESTED,
    EVENT_PHASE,
    EVENT_PRIVACY,
    EVENT_PROVIDER_TURN_COMPLETED,
    EVENT_PROVIDER_TURN_FAILED,
    EVENT_PROVIDER_TURN_STARTED,
    EVENT_RUN_REQ,
    EVENT_RUN_FAILED,
    EVENT_RUN_SUCCEEDED,
    EVENT_RUN_UNCERTAIN,
    EVENT_TOOL_DONE,
    EVENT_TOOL_INVOKED,
    EVENT_TOOL_CALL,
    EVENT_TOOL_RESULT,
    EVENT_STEERING,
    EVENT_USER,
    Event,
)
from .harness.research_turn import bootstrap_idea
from .privacy.modes import (
    MIXED_SANITIZED,
    PrivacyViolation,
    is_remote,
    normalize_mode,
    sanitize_messages,
)
from .providers.deepseek import MissingApiKey
from .providers.mock import MockReplayProvider
from .providers.catalog import provider_definitions, provider_public_catalog
from .runner import approve as runner_approve
from .storage.sqlite_store import SQLiteStore
from .storage.store import DuplicateFingerprint, LeaseConflict, StaleWrite, StoreError
from .storage.store import ATTACHMENT_FAILED, ATTACHMENT_QUARANTINED, ATTACHMENT_READY, AttachmentRecord
from .tools.executor import StataExecutor  # noqa: E402  （真 Stata 可选）
from .tools.fake_executor import FakeExecutor
from .settings.secret_store import (
    SecretStoreError,
    SecureStoreUnavailable,
    WindowsCredentialSecretStore,
)
from .writer.docx_out import claims_to_docx
from .writer.ground import render_claim_sentence

DEFAULT_DB = Path(os.environ.get("STATA_AGENT_DB", "samples/ideas/ui/ledger.sqlite3"))
_IDEA = "ui"
_APP_ROOT = Path(__file__).resolve().parents[2]
_UI_DIR = Path(__file__).with_name("webui")
_INDEX_FILE = _UI_DIR / "index.html"
_PACKAGE_SKILLS_DIR = Path(__file__).with_name("skills")

_WORKSPACE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_WORKSPACE_REGISTRY_LOCK = threading.RLock()
_RAG_CACHE: dict[tuple[str, str], Any] = {}
_RAG_CACHE_LOCK = threading.RLock()
_SKILL_ERRORS: list[str] = []
_MEMORY_MIGRATION_LOCK = threading.RLock()
_MIGRATED_MEMORY_DATABASES: set[str] = set()
_MIGRATED_WORKSPACE_DATABASES: set[str] = set()
_SETTINGS_SERVICE_LOCK = threading.RLock()
_SETTINGS_SERVICE: SettingsService | None = None
_SETTINGS_SECRET_STORE: Any | None = None
_SETTINGS_CSRF_TOKEN = secrets.token_urlsafe(32)
_TRACE_PROJECTION = TraceProjectionService()


def _attachment_root(settings: EffectiveSettings | None = None) -> Path:
    configured = _setting_value(settings, "storage.attachments", None)
    if configured is None:
        configured = os.environ.get("STATA_AGENT_ATTACHMENTS")
    return Path(configured).resolve() if configured else (DEFAULT_DB.parent / ".attachments").resolve()


@contextmanager
def _open_attachment_service():
    store = _store()
    try:
        yield AttachmentService(store, _attachment_root())
    finally:
        store.close()


def _safe_attachment(record: AttachmentRecord) -> dict[str, Any]:
    """Project one attachment without exposing its hash or storage key."""

    return {
        "attachment_id": record.attachment_id,
        "display_name": record.display_name,
        "status": record.status,
        "source_role": record.source_role,
        "detected_format": record.detected_format,
        "byte_size": record.byte_size,
        "page_count": record.page_count,
        "chunk_count": record.chunk_count,
        "scanned_suspect": record.scanned_suspect,
        "error_code": record.error_code,
        # Only transient parser/internal failures can be safely retried.  A
        # deterministic rejection or quarantine (encrypted, active, scanned,
        # malformed) requires a new file or operator review instead.
        "retryable": bool(
            record.status == ATTACHMENT_FAILED
            or record.error_code in {ERROR_INTERNAL, ERROR_PARSER_TIMEOUT, ERROR_PARSER_CRASHED}
        ),
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }


def _resolve_chat_attachments(
    store,
    workspace_id: str,
    attachment_ids,
    *,
    settings: EffectiveSettings | None = None,
):
    """Resolve only ready records in the current workspace into safe metadata."""

    service = AttachmentService(store, _attachment_root(settings))
    records = service.resolve_ready(workspace_id, attachment_ids)
    for record in records:
        if not service.path_for_attachment(record).is_file():
            raise AttachmentConflictError("attachment reference is unavailable")
    return [
        {
            "attachment_id": record.attachment_id,
            "display_name": record.display_name,
            "source_role": record.source_role,
            "detected_format": record.detected_format,
            "page_count": record.page_count,
        }
        for record in records
    ]


def _validate_chat_attachment_refs(idea: str, attachment_ids: list[str]) -> None:
    if not attachment_ids:
        return
    try:
        if len(attachment_ids) != len(set(attachment_ids)):
            raise AttachmentValidationError("attachment_ids must be unique")
        if any(
            not attachment_id
            or len(attachment_id) > 128
            or attachment_id != attachment_id.strip()
            or any(ord(char) < 0x20 for char in attachment_id)
            for attachment_id in attachment_ids
        ):
            raise AttachmentValidationError("attachment_id is invalid")
        with _open_attachment_service() as service:
            records = service.resolve_ready(_workspace_id(idea), attachment_ids)
            for record in records:
                if not service.path_for_attachment(record).is_file():
                    raise AttachmentConflictError("attachment reference is unavailable")
    except Exception as error:  # noqa: BLE001
        _attachment_http_error(error)


def reconcile_workspace_attachments(*, limit_per_workspace: int = 100) -> int:
    """Run one bounded, local-only attachment reconciliation pass."""

    workspace_ids = [str(row["id"]) for row in _read_workspace_registry()]
    changed = 0
    with _open_attachment_service() as service:
        for idea in workspace_ids:
            changed += len(
                service.reconcile(
                    _workspace_id(idea),
                    limit=min(max(int(limit_per_workspace), 1), service.limits.reconcile_batch),
                )
            )
    return changed


# Active stream controls are deliberately process-local.  The event ledger is
# the durable source of research state; this small registry only lets a user
# address one in-flight HTTP request without accidentally stopping another
# workspace.  The application-layer registry owns locking, TTL pruning, and
# lifecycle transitions; this module keeps only the transport adapter names
# used by existing routes and tests.
_REQUEST_CONTROL_TTL = 3600
_REQUEST_CONTROL_REGISTRY = RequestControlRegistry(ttl_seconds=_REQUEST_CONTROL_TTL)
_MEMORY_EXTRACTION_SCHEDULER: TaskQueue = LocalTaskQueue(max_pending=4, shutdown_timeout=1.0)


@asynccontextmanager
async def _app_lifespan(_app: FastAPI):
    """Start local background work, recover durable intent, then stop boundedly."""

    _MEMORY_EXTRACTION_SCHEDULER.start()
    pump_task: asyncio.Task[None] | None = None
    try:
        try:
            resume_memory_extractions()
        except Exception:  # noqa: BLE001 - recovery must not make the UI unavailable
            pass
        try:
            reconcile_workspace_attachments()
        except Exception:  # noqa: BLE001 - bounded repair must not make the UI unavailable
            pass
        effective = _effective_settings()
        if _setting_value(effective, "memory.extraction_mode", "off") == "provider":
            pump_task = asyncio.create_task(_memory_outbox_pump_loop(_memory_outbox_dispatcher()))
        yield
    finally:
        if pump_task is not None:
            pump_task.cancel()
            with suppress(asyncio.CancelledError):
                await pump_task
        shutdown_memory_extractions(wait=True, timeout=1.0)


app = FastAPI(title="stata-agent · research UI", lifespan=_app_lifespan)
app.mount("/static", StaticFiles(directory=str(_UI_DIR)), name="ui-static")


def _settings_secret_store() -> Any:
    """Return the process credential adapter without a plaintext fallback."""

    global _SETTINGS_SECRET_STORE
    with _SETTINGS_SERVICE_LOCK:
        if _SETTINGS_SECRET_STORE is None:
            _SETTINGS_SECRET_STORE = WindowsCredentialSecretStore()
        return _SETTINGS_SECRET_STORE


def _settings_service() -> SettingsService:
    """Build/reuse the application settings service at the composition edge.

    Tests and embedded hosts may inject ``app.state.settings_service``.  The
    default path is the documented local-profile location; a dedicated test
    environment variable is accepted only for process composition and is not
    exposed as a browser-controlled path.
    """

    injected = getattr(app.state, "settings_service", None)
    # Keep the composition boundary replaceable for embedded hosts and tests.
    # A duck-typed service is sufficient: the UI only relies on the public
    # ``resolve``/patch/secret methods, and requiring the concrete class would
    # make dependency injection unnecessarily brittle.
    if injected is not None and callable(getattr(injected, "resolve", None)):
        return injected
    # Resolve on demand instead of caching environment-derived values.  This
    # matters for embedded hosts/tests that rotate an explicit environment
    # override between requests; each request still captures one immutable
    # EffectiveSettings object before composition.
    configured = os.environ.get("STATA_AGENT_SETTINGS_PATH", "").strip()
    return SettingsService(
        path=Path(configured) if configured else None,
        secret_store=_settings_secret_store(),
    )


def _effective_settings() -> EffectiveSettings:
    """Capture one immutable settings projection for a request/bootstrap."""

    return _settings_service().resolve()


def _setting_value(settings: EffectiveSettings | Mapping[str, Any] | None, key: str, default: Any = None) -> Any:
    if settings is None:
        return default
    values = getattr(settings, "values", settings)
    if not isinstance(values, Mapping):
        return default
    raw = values.get(key, default)
    return getattr(raw, "value", raw)


def _factory_with_snapshot(factory: Callable[..., Any], settings: EffectiveSettings, *args: Any) -> Any:
    """Call legacy zero-argument monkeypatches while preferring snapshots."""

    try:
        return factory(*args, settings=settings)
    except TypeError as error:
        # Existing CLI/tests commonly inject ``lambda: provider`` or
        # ``lambda store: executor``.  Preserve that contract without making
        # the settings snapshot optional in the production composition.
        try:
            return factory(*args)
        except TypeError:
            raise error


def _error_message(detail: Any, fallback: str) -> str:
    if isinstance(detail, str) and detail.strip():
        return detail.strip()
    if isinstance(detail, (dict, list)):
        try:
            return json.dumps(detail, ensure_ascii=False)[:1000]
        except (TypeError, ValueError):
            pass
    return fallback


_SAFE_HTTP_FALLBACKS = {
    400: "请求无效。",
    404: "请求的对象不存在或不可用。",
    409: "请求与当前状态冲突。",
    413: "请求超过允许大小。",
    415: "请求类型不受支持。",
    422: "请求参数无效。",
    500: "请求未完成，请查看 Trace 或导出诊断包。",
    503: "服务暂时不可用，请稍后重试。",
}
_SENSITIVE_HTTP_DETAIL = re.compile(
    r"(?i)(?:api[_-]?key|access[_-]?token|password|secret|bearer|traceback|exception|[a-z]:\\|\\\\|/(?:home|tmp|private|users)/)"
)


class _SettingsHTTPException(HTTPException):
    """Transport exception carrying a stable, allow-listed settings code."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        self.error_code = code
        super().__init__(status_code=status_code, detail=message)


def _safe_http_message(status_code: int, detail: Any) -> str:
    """Keep HTTP errors actionable without echoing exception/path payloads."""

    fallback = _SAFE_HTTP_FALLBACKS.get(status_code, "请求未完成，请稍后重试。")
    if status_code >= 500:
        return fallback
    candidate = _error_message(detail, fallback)
    if len(candidate) > 500 or _SENSITIVE_HTTP_DETAIL.search(candidate):
        return fallback
    return candidate


def _error_body(
    status_code: int,
    message: str,
    *,
    code: str | None = None,
    details: Any = None,
    retryable: bool | None = None,
    support_action: str | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": False,
        "error": {"code": code or f"http_{status_code}", "message": message},
        # ``detail`` is kept for clients written against the original v0 API.
        "detail": message,
        "status_code": status_code,
    }
    if details:
        payload["error"]["details"] = details
    if retryable is not None:
        payload["error"]["retryable"] = bool(retryable)
    if support_action in {"retry", "download_diagnostics"}:
        payload["error"]["support_action"] = support_action
    safe_request_id = _safe_projection_id(request_id)
    if safe_request_id is not None:
        payload["error"]["request_id"] = safe_request_id
    return payload


@app.exception_handler(HTTPException)
async def _http_error_handler(_request: Request, exc: HTTPException) -> JSONResponse:
    message = _safe_http_message(exc.status_code, exc.detail)
    return JSONResponse(
        status_code=exc.status_code,
        content=_error_body(exc.status_code, message, code=getattr(exc, "error_code", None)),
        headers={"Cache-Control": "no-store"} if getattr(exc, "error_code", None) else None,
    )


@app.exception_handler(RequestValidationError)
async def _validation_error_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
    # Do not echo submitted values (which may contain Stata code or private
    # data); locations/messages are enough for a client-side form error.
    details = [
        {
            "loc": list(item.get("loc") or []),
            "msg": str(item.get("msg") or "参数无效"),
            "type": str(item.get("type") or "value_error"),
        }
        for item in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content=_error_body(422, "请求参数无效。", code="validation_error", details=details),
    )


@app.exception_handler(StoreError)
async def _store_error_handler(request: Request, exc: StoreError) -> JSONResponse:
    """Expose writer conflicts as bounded, actionable transport errors."""

    if isinstance(exc, LeaseConflict):
        status_code, code, message, retryable = (
            409,
            "ledger_writer_conflict",
            "账本当前由另一运行实例占用，请关闭重复实例后刷新重试。",
            True,
        )
    elif isinstance(exc, StaleWrite):
        status_code, code, message, retryable = (
            409,
            "ledger_writer_stale",
            "本次账本写入已失效，请刷新状态后再重试。",
            True,
        )
    else:
        status_code, code, message, retryable = (
            503,
            "ledger_unavailable",
            "账本暂时不可用，请稍后重试。",
            True,
        )
    return JSONResponse(
        status_code=status_code,
        content=_error_body(
            status_code,
            message,
            code=code,
            retryable=retryable,
            support_action="retry",
            request_id=request.headers.get("x-request-id"),
        ),
    )


# SQLiteStore owns a fenced single-writer lease even for projection reads.
# FastAPI runs synchronous routes in a thread pool, while the browser refreshes
# state/events/approvals concurrently. Serialising UI store sessions prevents a
# read request from taking over the lease halfway through a chat write.
_UI_STORE_LOCK = threading.RLock()
# Approval decisions need one process-local critical section spanning the
# pending snapshot and its terminal write.  The store lease remains the durable
# fencing boundary; this lock closes the check-then-append race between UI
# request threads (including direct callers of ``_decision`` in tests/tools).
_APPROVAL_DECISION_LOCK = threading.RLock()


def _prune_request_controls(now: float | None = None) -> None:
    _REQUEST_CONTROL_REGISTRY.prune(now)


def _register_request_control(request_id: str, idea: str, cancel_event: threading.Event) -> None:
    _REQUEST_CONTROL_REGISTRY.register(request_id, idea, cancel_event)


def _request_control_public(control: dict[str, Any] | None) -> dict[str, Any] | None:
    if control is None:
        return None
    return {
        "request_id": control.get("request_id"),
        "workspace": control.get("workspace") or control.get("idea"),
        "status": control.get("status"),
        "cancel_requested": bool(control.get("cancel_requested")),
        "cancel_reason": control.get("cancel_reason"),
        "created_at": control.get("created_at"),
        "finished_at": control.get("finished_at"),
        "terminal_reason": control.get("terminal_reason"),
        "error_code": control.get("error_code"),
        "retryable": control.get("retryable"),
    }


def _finish_request_control(
    request_id: str,
    *,
    status: str,
    terminal_reason: str | None = None,
    error_code: str | None = None,
    retryable: bool | None = None,
) -> None:
    # The registry preserves the first terminal result, so a late disconnect
    # callback cannot overwrite a completed/failed/cancelled request.
    _REQUEST_CONTROL_REGISTRY.finish(
        request_id,
        status,
        terminal_reason=terminal_reason,
        error_code=error_code,
        retryable=retryable,
    )


def _request_cancel(request_id: str, idea: str, *, reason: str = "user") -> dict[str, Any]:
    try:
        control = _REQUEST_CONTROL_REGISTRY.cancel(request_id, idea, reason=reason)
    except (RequestControlNotFound, ValueError) as error:
        raise HTTPException(status_code=404, detail="运行请求不存在或已过期。") from error
    return _request_control_public(control) or {}


def _mark_request_disconnected(request_id: str) -> None:
    _REQUEST_CONTROL_REGISTRY.disconnect(request_id)


def _request_control_snapshot(request_id: str) -> dict[str, Any] | None:
    return _request_control_public(_REQUEST_CONTROL_REGISTRY.snapshot(request_id))


def _latest_active_request(idea: str) -> dict[str, Any] | None:
    return _REQUEST_CONTROL_REGISTRY.latest_active(idea)


class _LockedStore:
    """Delegate to SQLiteStore while holding the process-local UI lease lock."""

    def __init__(self, store: SQLiteStore):
        self._store = store
        self._closed = False

    def __getattr__(self, name: str):
        return getattr(self._store, name)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._store.close()
        finally:
            _UI_STORE_LOCK.release()


def _store() -> _LockedStore:
    DEFAULT_DB.parent.mkdir(parents=True, exist_ok=True)
    _UI_STORE_LOCK.acquire()
    try:
        return _LockedStore(SQLiteStore(str(DEFAULT_DB), writer_id="ui", takeover=True))
    except Exception:
        _UI_STORE_LOCK.release()
        raise


class _ScopedOutboxRecoveryRepository:
    """Add transport-scope hiding around the framework-neutral repository port.

    ``OutboxRecoveryService`` owns payload/task validation and transition policy.
    This adapter only prevents a key from another resolved workspace from being
    observable through the single-item lookup, so the HTTP layer can preserve
    the required not-found semantics without duplicating recovery policy.
    """

    def __init__(self, store: Any, *, idea_id: str, workspace_id: str) -> None:
        self._store = store
        self._idea_id = idea_id
        self._workspace_id = workspace_id

    @staticmethod
    def _belongs_to_other_scope(row: object, *, idea_id: str, workspace_id: str) -> bool:
        payload = getattr(row, "payload", None)
        if not isinstance(payload, Mapping):
            return False
        row_idea = payload.get("idea_id")
        row_workspace = payload.get("workspace_id")
        # Leave malformed metadata visible to the application service so it can
        # return a conflict.  Only a fully specified, valid identity is hidden
        # as a cross-workspace item.
        if not (
            isinstance(row_idea, str)
            and bool(row_idea.strip())
            and isinstance(row_workspace, str)
            and bool(row_workspace.strip())
        ):
            return False
        return row_idea != idea_id or row_workspace != workspace_id

    def list_outbox(self, *, status: str | None = None, limit: int | None = None):
        # Listing scope and payload validation remain in OutboxRecoveryService.
        return self._store.list_outbox(status=status, limit=limit)

    def get_outbox(self, *, idempotency_key: str):
        row = self._store.get_outbox(idempotency_key=idempotency_key)
        if row is None:
            return None
        if self._belongs_to_other_scope(
            row,
            idea_id=self._idea_id,
            workspace_id=self._workspace_id,
        ):
            return None
        return row

    def reconcile_failed_outbox(
        self,
        idempotency_key: str,
        task_type: str,
        expected_state_version: int,
    ):
        return self._store.reconcile_failed_outbox(
            idempotency_key,
            task_type,
            expected_state_version,
        )

    def redrive_failed_outbox(
        self,
        idempotency_key: str,
        task_type: str,
        expected_state_version: int,
    ):
        return self._store.redrive_failed_outbox(
            idempotency_key,
            task_type,
            expected_state_version,
        )


class _ScopedOutboxRetentionRepository:
    """Compose the narrow retention storage port for one resolved workspace."""

    def __init__(self, store: Any) -> None:
        self._store = store

    def list_completed_outbox_before(self, **kwargs: Any):
        return self._store.list_completed_outbox_before(**kwargs)

    def delete_completed_outbox_batch(self, expectations, *, cutoff: int) -> int:
        return self._store.delete_completed_outbox_batch(expectations, cutoff=cutoff)


_MEMORY_OUTBOX_TERMINAL_EVENTS = frozenset(
    {
        EVENT_MEMORY_EXTRACTION_COMPLETED,
        EVENT_MEMORY_EXTRACTION_NOOP,
        EVENT_MEMORY_EXTRACTION_FAILED,
        EVENT_MEMORY_EXTRACTION_DENIED,
    }
)


def _memory_outbox_terminal_for_scope(
    store: Any,
    idea_id: str,
    workspace_id: str,
    fingerprint: str,
) -> Event | None:
    """Find one exact terminal extraction event in the selected ledger scope."""

    for event in store.scan(idea_id):
        if event.event_type not in _MEMORY_OUTBOX_TERMINAL_EVENTS:
            continue
        payload = event.payload if isinstance(event.payload, Mapping) else {}
        event_workspace = payload.get("workspace_id")
        event_fingerprint = event.fingerprint or payload.get("fingerprint")
        if event_workspace != workspace_id or event_fingerprint != fingerprint:
            continue
        return event
    return None


@contextmanager
def _open_memory_outbox_recovery(idea_id: str, workspace_id: str):
    """Compose a short-lived SQLite-backed recovery service for one workspace."""

    store = _store()
    repository = _ScopedOutboxRecoveryRepository(
        store,
        idea_id=idea_id,
        workspace_id=workspace_id,
    )
    try:
        yield OutboxRecoveryService(
            repository,
            lambda current_idea, current_workspace, fingerprint: _memory_outbox_terminal_for_scope(
                store,
                current_idea,
                current_workspace,
                fingerprint,
            ),
        )
    finally:
        store.close()


@contextmanager
def _open_memory_outbox_retention(idea_id: str, workspace_id: str):
    """Compose a short-lived SQLite-backed retention service for one workspace."""

    store = _store()
    repository = _ScopedOutboxRetentionRepository(store)
    try:
        yield OutboxRetentionService(
            repository,
            lambda current_idea, current_workspace, fingerprint: _memory_outbox_terminal_for_scope(
                store,
                current_idea,
                current_workspace,
                fingerprint,
            ),
        )
    finally:
        store.close()


def _parse_memory_outbox_limit(value: object) -> int:
    """Parse a bounded query value without accepting float/bool coercions."""

    if isinstance(value, bool):
        raise OutboxRecoveryValidationError("limit must be an integer between 1 and 100")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and re.fullmatch(r"[0-9]+", value.strip()):
        parsed = int(value.strip())
    else:
        raise OutboxRecoveryValidationError("limit must be an integer between 1 and 100")
    if not 1 <= parsed <= 100:
        raise OutboxRecoveryValidationError("limit must be an integer between 1 and 100")
    return parsed


def _parse_memory_retention_days(value: object) -> int:
    """Parse the bounded retention period without float/bool coercions."""

    if isinstance(value, bool):
        raise OutboxRetentionValidationError("retention_days must be between 7 and 3650")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and re.fullmatch(r"[0-9]+", value.strip()):
        parsed = int(value.strip())
    else:
        raise OutboxRetentionValidationError("retention_days must be between 7 and 3650")
    if not 7 <= parsed <= 3650:
        raise OutboxRetentionValidationError("retention_days must be between 7 and 3650")
    return parsed


def _parse_diagnostic_limit(value: object, *, maximum: int, name: str) -> int:
    """Parse diagnostic bounds without FastAPI's permissive coercions."""

    if isinstance(value, bool):
        raise DiagnosticValidationError(f"{name} must be an integer between 1 and {maximum}")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and re.fullmatch(r"[0-9]+", value.strip()):
        parsed = int(value.strip())
    else:
        raise DiagnosticValidationError(f"{name} must be an integer between 1 and {maximum}")
    if not 1 <= parsed <= maximum:
        raise DiagnosticValidationError(f"{name} must be an integer between 1 and {maximum}")
    return parsed


def _raise_memory_outbox_http_error(error: Exception) -> None:
    """Map expected application errors to bounded, non-sensitive HTTP errors."""

    if isinstance(error, OutboxRecoveryValidationError):
        raise HTTPException(status_code=422, detail="请求参数无效。") from error
    if isinstance(error, OutboxRecoveryNotFoundError):
        raise HTTPException(status_code=404, detail="任务不存在。") from error
    if isinstance(error, OutboxRecoveryConflictError):
        raise HTTPException(status_code=409, detail="任务状态已变化，无法恢复。") from error
    raise HTTPException(status_code=500, detail="恢复操作未完成，请稍后重试。") from error


def _raise_memory_outbox_retention_http_error(error: Exception) -> None:
    """Map retention policy failures without exposing stored row data."""

    if isinstance(error, (OutboxRetentionValidationError, OutboxRecoveryValidationError)):
        raise HTTPException(status_code=422, detail="请求参数无效。") from error
    if isinstance(error, OutboxRetentionConflictError):
        raise HTTPException(status_code=409, detail="清理选择已变化，请重新预览。") from error
    raise HTTPException(status_code=500, detail="清理操作未完成，请稍后重试。") from error


def _raise_diagnostic_http_error(error: Exception) -> None:
    """Map diagnostic validation/read failures without exposing source data."""

    if isinstance(error, DiagnosticValidationError):
        raise HTTPException(status_code=422, detail="诊断参数无效。") from error
    if isinstance(error, DiagnosticReadError):
        raise HTTPException(status_code=500, detail="诊断数据暂不可用。") from error
    raise HTTPException(status_code=500, detail="诊断导出未完成，请稍后重试。") from error


def _legacy_memory_path() -> Path:
    """Return the read-only source used for an explicit V1 memory import."""

    return DEFAULT_DB.parent / "memory.json"


def _migrate_legacy_memory_once() -> dict[str, Any] | None:
    """Import a legacy JSON store into SQLite once per database process.

    The repository import is transactional and fingerprint-idempotent.  The
    source file is never renamed or modified, so operators retain a recoverable
    rollback artifact while all new writes go exclusively to SQLite.
    """

    source = _legacy_memory_path()
    if not source.is_file():
        return None
    database_key = str(DEFAULT_DB.expanduser().resolve(strict=False)).casefold()
    with _MEMORY_MIGRATION_LOCK:
        if database_key in _MIGRATED_MEMORY_DATABASES:
            return None
        from .memory.sqlite_repository import SQLiteMemoryRepository

        DEFAULT_DB.parent.mkdir(parents=True, exist_ok=True)
        with SQLiteMemoryRepository(DEFAULT_DB) as repository:
            report = repository.import_json(source)
        _MIGRATED_MEMORY_DATABASES.add(database_key)
        return report


def _close_memory(memory: Any) -> None:
    """Best-effort close for SQLite memory handles and lightweight test doubles."""

    close = getattr(memory, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # noqa: BLE001 - cleanup must not hide the primary result
            pass


def _memory_transaction():
    """Compatibility scope; SQLite repositories provide real transactions."""

    return nullcontext()


DEMO = os.environ.get("STATA_AGENT_DEMO") == "1"


def _demo_enabled() -> bool:
    """Read demo mode dynamically so tests/config reloads are deterministic."""

    return DEMO or os.environ.get("STATA_AGENT_DEMO") == "1"


def _workspace_registry_path() -> Path:
    """Return the read-only legacy workspace registry source.

    SQLite is the runtime registry. ``STATA_AGENT_WORKSPACES`` remains an
    import escape hatch so existing installations can migrate without moving
    their old JSON file first.
    """

    configured = os.environ.get("STATA_AGENT_WORKSPACES", "").strip()
    return Path(configured) if configured else DEFAULT_DB.parent / "workspaces.json"


def _validate_workspace_id(value: str | None) -> str:
    try:
        return WorkspaceService.validate_id(value, default=_IDEA)
    except WorkspaceValidationError as error:
        raise HTTPException(
            status_code=422,
            detail="工作区标识只允许字母、数字、下划线和连字符。",
        ) from error


def _workspace_root_base() -> Path:
    configured = os.environ.get("STATA_AGENT_PROJECT_ROOT", "").strip()
    return Path(configured).expanduser() if configured else Path(DEFAULT_DB).resolve().parent


def _canonical_workspace_root(idea: str = _IDEA) -> Path:
    """Return the stable local project root used for memory partitioning.

    The UI keeps several logical ideas in one SQLite ledger.  Consequently a
    workspace identity must include the validated idea key as well as the
    canonical ledger directory; using ``memory.json`` here would make a
    process-wide storage detail accidentally define project identity.
    """

    validated = _validate_workspace_id(idea)
    return (_workspace_root_base() / validated).resolve()


def _workspace_id(idea: str = _IDEA) -> str:
    """Derive the opaque, deterministic V2 workspace id from its root."""

    root = _canonical_workspace_root(idea)
    digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


# Descriptive alias for integrations that need to distinguish the opaque V2
# identity from the legacy ``idea``/registry slug.
_memory_workspace_id = _workspace_id


@contextmanager
def _open_workspace_service():
    """Own one short-lived SQLite repository behind the application service."""

    from .memory.sqlite_repository import SQLiteMemoryRepository

    DEFAULT_DB.parent.mkdir(parents=True, exist_ok=True)
    repository = SQLiteMemoryRepository(DEFAULT_DB)
    try:
        yield WorkspaceService(repository, root_base=_workspace_root_base())
    finally:
        repository.close()


def _migrate_legacy_workspaces_once(service: WorkspaceService) -> None:
    """Import valid legacy JSON rows without modifying the source file."""

    source = _workspace_registry_path()
    database_key = str(DEFAULT_DB.expanduser().resolve(strict=False)).casefold()
    with _WORKSPACE_REGISTRY_LOCK:
        if database_key in _MIGRATED_WORKSPACE_DATABASES:
            return
        try:
            data = json.loads(source.read_text(encoding="utf-8")) if source.is_file() else []
        except (OSError, ValueError, TypeError):
            data = []
        if isinstance(data, list):
            for item in data:
                if not isinstance(item, dict):
                    continue
                ident = str(item.get("id") or "").strip()
                if not _WORKSPACE_ID_RE.fullmatch(ident):
                    continue
                try:
                    service.create(
                        CreateWorkspaceRequest(
                            id=ident,
                            name=str(item.get("name") or ident)[:120],
                            root=service.canonical_root(workspace_id=ident),
                            now=int(item.get("created_at") or 0),
                        )
                    )
                except (WorkspaceConflictError, WorkspaceValidationError, TypeError, ValueError):
                    continue
        _MIGRATED_WORKSPACE_DATABASES.add(database_key)


def _read_workspace_registry() -> list[dict[str, Any]]:
    """Read the SQLite registry, lazily importing legacy JSON once."""

    # Repository construction sets SQLite pragmas and applies migrations.  A
    # short adapter lock prevents concurrent first-open calls from racing on
    # those database-level setup statements; row mutations remain transactional.
    with _WORKSPACE_REGISTRY_LOCK:
        with _open_workspace_service() as service:
            _migrate_legacy_workspaces_once(service)
            return [record.as_dict() for record in service.list()]


def _workspace_known(idea: str) -> bool:
    return any(row["id"] == idea for row in _read_workspace_registry())


def _resolve_workspace(ws: str | None) -> str:
    idea = _validate_workspace_id(ws)
    if not _workspace_known(idea):
        raise HTTPException(status_code=404, detail="工作区不存在，请先新建或从列表选择。")
    return idea


def _workspace_entries(store: SQLiteStore) -> list[dict[str, Any]]:
    """Combine registry metadata with current read-only ledger counters."""

    rows = _read_workspace_registry()
    result: list[dict[str, Any]] = []
    for row in rows:
        idea = row["id"]
        events = list(store.scan(idea))
        first_idea = next((event for event in events if event.event_type == EVENT_IDEA), None)
        question = str((first_idea.payload if first_idea else {}).get("question") or "").strip()
        approvals = _approval_records(events)
        status, status_detail = _run_state(events, approvals)
        result.append(
            {
                "id": idea,
                "workspace_id": _workspace_id(idea),
                "name": (question or row.get("name") or idea)[:120],
                "created_at": row.get("created_at") or (events[0].created_at if events else None),
                "updated_at": events[-1].created_at if events else row.get("created_at"),
                "events": len(events),
                "last_seq": events[-1].seq if events else None,
                "run_status": status,
                "status_detail": status_detail,
                "pending_approvals": len([item for item in approvals if item["status"] == "pending"]),
            }
        )
    return result


def _touch_workspace_name(idea: str, text: str) -> None:
    """Persist a useful display name after the first idea is declared."""

    with _WORKSPACE_REGISTRY_LOCK:
        with _open_workspace_service() as service:
            _migrate_legacy_workspaces_once(service)
            try:
                service.touch_name(idea, text)
            except (WorkspaceNotFoundError, WorkspaceValidationError):
                return


def _demo_seed(store: SQLiteStore, idea: str = _IDEA) -> None:
    """DEMO 模式：首次进入把 idea 推进到 ESTIMATION（供演示 spec→run→证据→Word）。

    仅当开了 STATA_AGENT_DEMO=1 且还没有 phase 事件时生效；正常模式不做任何越权推进。
    """
    if not _demo_enabled():
        return
    if any(ev.event_type == EVENT_PHASE for ev in store.scan(idea)):
        return
    store.append(
        Event(
            idea_id=idea,
            event_type=EVENT_PHASE,
            actor=ACTOR_ORCH,
            source=ACTOR_ORCH,
            payload={"from": "IDEA", "to": "ESTIMATION"},
        )
    )


def _provider(*, settings: EffectiveSettings | None = None):
    from .providers.registry import default_provider

    effective = settings or _effective_settings()
    secret_store = _settings_service().secret_store
    try:
        return default_provider(effective, secret_store=secret_store)
    except MissingApiKey:
        if not _demo_enabled():
            raise
        # 无 LLM key 的离线演示：以确定性响应验证聊天/UI 链路。
        # Legacy proposal acts 不冒充现代 tool call；完整工具编排仍需真模型。
        return MockReplayProvider(
            [
                ActionProposal(
                    decision_summary="先定主 spec",
                    acts=[Act(act_type="propose_spec", target={"spec_id": "s1"}, reason="主 spec")],
                ),
                ActionProposal(
                    decision_summary="跑主回归",
                    acts=[Act(act_type="request_run", target={"spec_id": "s1"}, reason="跑主回归")],
                ),
                ActionProposal(decision_summary="出结果", ask_user="是否生成 Word 初稿？"),
            ]
        )


def _provider_name(provider: Any) -> str:
    value = getattr(provider, "provider_name", None) or getattr(provider, "provider", None)
    return str(value or provider.__class__.__name__).strip().lower()


class _PrivacyChatAdapter:
    """Reuse one selected provider while applying feature-specific privacy gates."""

    def __init__(self, provider: Any, *, privacy_mode: str) -> None:
        chat = getattr(provider, "chat", None)
        if not callable(chat):
            raise TypeError("provider does not support chat")
        self._provider = provider
        self._privacy_mode = privacy_mode
        self.provider_name = _provider_name(provider)
        explicit = getattr(provider, "is_remote", None)
        self.is_remote = (
            explicit
            if isinstance(explicit, bool)
            else is_remote(self.provider_name) or self.provider_name in {"remote", "cloud", "http", "https"}
        )

    def chat(self, messages: list[dict[str, str]], **kwargs: Any) -> Any:
        mode = normalize_mode(self._privacy_mode)
        if self.is_remote and mode == "local_strict":
            raise PrivacyViolation("local_strict 禁止 provider-assisted context/memory calls")
        request_messages = messages
        if self.is_remote and mode == MIXED_SANITIZED:
            request_messages = sanitize_messages(
                messages,
                mode=mode,
                provider=self.provider_name,
            )
        return self._provider.chat(request_messages, **kwargs)


def _feature_adapters(
    provider: Any,
    privacy_mode: str,
    settings: EffectiveSettings | None = None,
) -> tuple[Any, Any]:
    """Build optional summary/extraction adapters around the current provider.

    Construction is local-only.  Neither adapter calls the provider until the
    corresponding overflow or background job is explicitly reached.
    """

    summary_adapter = None
    extraction_adapter = None
    if not callable(getattr(provider, "chat", None)):
        return summary_adapter, extraction_adapter
    configured_summary = _setting_value(settings, "compaction.summary_mode", None)
    configured_memory = _setting_value(settings, "memory.extraction_mode", None)
    if configured_summary is None:
        from .config import compaction_summary_mode

        configured_summary = compaction_summary_mode()
    if configured_memory is None:
        from .config import memory_extraction_mode

        configured_memory = memory_extraction_mode()
    if configured_summary == "provider":
        from .harness.summary_service import ChatCompactionSummaryProvider

        summary_adapter = ChatCompactionSummaryProvider(
            _PrivacyChatAdapter(provider, privacy_mode=privacy_mode),
        )
    if configured_memory == "provider":
        from .memory.pipeline import ChatMemoryExtractionProvider

        extraction_adapter = ChatMemoryExtractionProvider(
            _PrivacyChatAdapter(provider, privacy_mode=privacy_mode),
        )
    return summary_adapter, extraction_adapter


def _executor(store: SQLiteStore, *, settings: EffectiveSettings | None = None):
    """Return an explicitly selected executor; never invent fake results live."""

    configured = str(_setting_value(settings, "executor.kind", "") or "").strip().lower()
    if not configured:
        configured = os.environ.get("STATA_AGENT_EXECUTOR", "").strip().lower()
    if configured == "stata":
        # 绝对化 run_root：ToolEnforcer 的路径 containment 要求绝对路径，
        # 且 Stata 的 cwd 在 stata-mcp，不能让相对路径导致越界/误判。
        run_root = Path(DEFAULT_DB).resolve().parent / "runs"
        run_root.mkdir(parents=True, exist_ok=True)
        mcp_dir = _setting_value(settings, "stata.mcp_dir", None)
        return StataExecutor(store, run_root=run_root, share_session=True, stata_mcp_dir=mcp_dir)
    if configured in {"fake", "demo"} or (_demo_enabled() and not configured):
        return FakeExecutor(store)
    # A missing/unknown executor is an unavailable capability.  In particular,
    # a live LLM must not be handed FakeExecutor merely because Stata is absent.
    return None


def _privacy_mode(settings: EffectiveSettings | None = None) -> str:
    from .providers.registry import privacy_mode

    return privacy_mode(settings)


def _config_info() -> dict:
    """给设置页展示的真实运行时配置（只读）。"""
    from .providers.registry import live_available
    effective = _effective_settings()
    secret_store = _settings_service().secret_store
    executor_kind = str(_setting_value(effective, "executor.kind", "disabled") or "disabled").strip().lower()
    if executor_kind == "stata":
        executor = "真 Stata"
    elif executor_kind in {"fake", "demo"} or (_demo_enabled() and not executor_kind):
        executor = "演示(Fake)"
    else:
        executor = "未配置"
    provider = "未配置"
    if live_available(effective, secret_store=secret_store):
        provider = str(_setting_value(effective, "provider.primary", "auto") or "auto")
        if provider == "auto":
            provider = next(
                (
                    item.provider_id
                    for item in provider_definitions()
                    if _settings_service().secret_status(item.provider_id).configured
                ),
                "auto",
            )
    lib = str(_setting_value(effective, "library.root", "") or "")
    try:
        skills = list(_skills(effective).keys())
    except TypeError as error:
        try:
            skills = list(_skills().keys())
        except TypeError:
            raise error
    summary_mode = str(_setting_value(effective, "compaction.summary_mode", "deterministic") or "deterministic")
    memory_mode = str(_setting_value(effective, "memory.extraction_mode", "off") or "off")
    try:
        config_privacy = _privacy_mode(effective)
    except TypeError as error:
        try:
            config_privacy = _privacy_mode()
        except TypeError:
            raise error
    return {
        "provider": provider,
        "executor": executor,
        "privacy_mode": config_privacy,
        "library": lib or "(未配置文献库)",
        "skills": skills,
        "skill_errors": list(_SKILL_ERRORS),
        "workspace_db": str(DEFAULT_DB),
        "compaction_summary": summary_mode,
        "memory_extraction": memory_mode,
        "settings_revision": effective.revision,
        "settings_restart_required": effective.restart_required,
        "settings_pending_keys": list(effective.pending_keys),
    }


def _memory():
    """项目记忆（约束，非证据）；没有就 None，工具会优雅降级。"""
    try:
        from .memory.sqlite_store import SQLiteMemoryStore

        _migrate_legacy_memory_once()
        return SQLiteMemoryStore(DEFAULT_DB)
    except Exception:  # noqa: BLE001
        return None


def _memory_outbox_repository() -> SQLiteMemoryOutboxRepository:
    return SQLiteMemoryOutboxRepository(
        _store,
        idea_ids_factory=lambda: [str(row["id"]) for row in _read_workspace_registry()],
    )


def _run_memory_extraction_job(
    *,
    idea: str,
    workspace_id: str,
    provider: Any,
    privacy_mode: str,
    prepared_request: Any | None = None,
) -> None:
    """Open fresh per-job resources; a failed intake never changes chat output."""

    store = None
    memory = None
    try:
        # _store serializes takeover of SQLiteStore's single-writer lease with
        # UI requests.  The worker never captures the request's store/memory.
        store = _store()
        memory = _memory()
        if memory is None:
            return
        from .memory.pipeline import MemoryExtractionPipeline

        MemoryExtractionPipeline().run_once(
            store=store,
            memory=memory,
            idea_id=idea,
            workspace_id=workspace_id,
            provider=provider,
            privacy_mode=privacy_mode,
            prepared_request=prepared_request,
        )
    except Exception:
        # The pipeline records stable failure/denial events where possible;
        # scheduler failures remain isolated from the already delivered turn.
        return
    finally:
        _close_memory(memory)
        if store is not None:
            try:
                store.close()
            except Exception:
                pass


def _run_memory_outbox_claim(claim: MemoryOutboxClaim) -> None:
    """Run one claimed request using its frozen provider/privacy identity."""

    provider = _provider()
    if _provider_name(provider) != claim.intent.provider_name:
        # No terminal event is written.  The dispatcher will release the
        # lease, allowing a later process with the matching provider to retry.
        raise MemoryOutboxDeferred("provider_identity_mismatch")
    _, extraction_provider = _feature_adapters(provider, claim.intent.privacy_mode)
    if extraction_provider is None:
        raise MemoryOutboxDeferred("provider_unavailable_for_frozen_privacy")
    from .memory.pipeline import MemoryExtractionPipeline

    store = _store()
    try:
        prepared_request = MemoryExtractionPipeline().request_for_fingerprint(
            store=store,
            idea_id=claim.intent.idea_id,
            workspace_id=claim.intent.workspace_id,
            fingerprint=claim.intent.fingerprint,
        )
    finally:
        store.close()
    if prepared_request is None:
        return
    _run_memory_extraction_job(
        idea=claim.intent.idea_id,
        workspace_id=claim.intent.workspace_id,
        provider=extraction_provider,
        privacy_mode=claim.intent.privacy_mode,
        prepared_request=prepared_request,
    )


def _memory_outbox_terminal(claim: MemoryOutboxClaim) -> bool:
    """Check the ledger terminal event that authoritatively completes a job."""

    from .memory.pipeline import TERMINAL_EXTRACTION_EVENTS

    store = _store()
    try:
        return any(
            event.event_type in TERMINAL_EXTRACTION_EVENTS
            and str(event.fingerprint or (event.payload or {}).get("fingerprint") or "")
            == claim.intent.fingerprint
            for event in store.scan(claim.intent.idea_id)
        )
    except Exception:
        return False
    finally:
        store.close()


def _memory_outbox_dispatcher() -> MemoryOutboxDispatcher:
    return MemoryOutboxDispatcher(
        _memory_outbox_repository(),
        _MEMORY_EXTRACTION_SCHEDULER,
        worker=_run_memory_outbox_claim,
        terminal_checker=_memory_outbox_terminal,
        owner=f"ui:{os.getpid()}",
    )


async def _memory_outbox_pump_loop(
    dispatcher: MemoryOutboxDispatcher,
    *,
    interval_seconds: float = 1.0,
) -> None:
    """Continuously redeliver durable pending work while the app is alive."""

    while True:
        await asyncio.sleep(max(0.1, float(interval_seconds)))
        try:
            await asyncio.to_thread(dispatcher.pump)
        except Exception:  # noqa: BLE001 - the next tick remains a recovery path
            continue


def _submit_memory_extraction_task(
    *,
    key: str,
    callback: Callable[[], object],
) -> bool:
    """Submit one memory callback through the application queue port.

    ``TaskQueue`` returns a typed ``TaskSubmitResult``.  The UI keeps its
    historical boolean contract because queue admission is an internal detail.
    Adapters must implement the application port's ``submit(key, callback)``
    signature; retrying on ``TypeError`` could accidentally submit twice when a
    queue implementation raises from inside its own method.
    """

    result = _MEMORY_EXTRACTION_SCHEDULER.submit(key, callback)
    if isinstance(result, TaskSubmitResult):
        return result.accepted
    return bool(result)


def _schedule_memory_extraction(
    *,
    idea: str,
    workspace_id: str,
    provider: Any,
    privacy_mode: str,
) -> bool:
    from .config import memory_extraction_mode

    if memory_extraction_mode() != "provider":
        return False
    _, extraction_provider = _feature_adapters(provider, privacy_mode)
    if extraction_provider is None:
        return False
    key = f"{idea}:{workspace_id}"
    return _submit_memory_extraction_task(
        key=key,
        callback=lambda: _run_memory_extraction_job(
            idea=idea,
            workspace_id=workspace_id,
            provider=extraction_provider,
            privacy_mode=privacy_mode,
        ),
    )


def resume_memory_extractions(ws: str | None = None) -> int:
    """Queue requested jobs that lack a matching terminal event."""

    from .config import memory_extraction_mode

    if memory_extraction_mode() != "provider":
        return 0
    idea = _resolve_workspace(ws) if ws is not None else None
    try:
        dispatcher = _memory_outbox_dispatcher()
        dispatcher.start(idea_id=idea, pump=False)
        return dispatcher.pump().submitted
    except AttributeError:
        # Keep the legacy scan as a compatibility path for an older store
        # without the outbox capability.  Current SQLiteStore takes the path
        # above, so new requests never use prepare→enqueue.
        pass

    from .events.schema import (
        EVENT_MEMORY_EXTRACTION_COMPLETED,
        EVENT_MEMORY_EXTRACTION_DENIED,
        EVENT_MEMORY_EXTRACTION_FAILED,
        EVENT_MEMORY_EXTRACTION_NOOP,
        EVENT_MEMORY_EXTRACTION_REQUESTED,
    )

    store = _store()
    try:
        ideas = [idea] if idea is not None else [row["id"] for row in _read_workspace_registry()]
        pending: list[tuple[str, str]] = []
        terminal_types = {
            EVENT_MEMORY_EXTRACTION_COMPLETED,
            EVENT_MEMORY_EXTRACTION_DENIED,
            EVENT_MEMORY_EXTRACTION_FAILED,
            EVENT_MEMORY_EXTRACTION_NOOP,
        }
        for current in dict.fromkeys(ideas):
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
                fingerprint = str(event.fingerprint or payload.get("fingerprint") or "")
                if fingerprint and fingerprint not in terminal:
                    pending.append((current, str(payload.get("workspace_id") or _workspace_id(current))))
    finally:
        store.close()
    if not pending:
        return 0
    provider = _provider()
    privacy_mode = _privacy_mode()
    queued = 0
    for current, workspace_id in pending:
        if _schedule_memory_extraction(
            idea=current,
            workspace_id=workspace_id,
            provider=provider,
            privacy_mode=privacy_mode,
        ):
            queued += 1
    return queued


def shutdown_memory_extractions(*, wait: bool = True, timeout: float | None = None) -> None:
    """Bounded shutdown hook for application teardown/tests."""

    _MEMORY_EXTRACTION_SCHEDULER.shutdown(wait=wait, timeout=timeout)


def _context_budget(settings: EffectiveSettings | None = None):
    """Build an optional ContextBudget from explicit environment overrides.

    No override keeps the assembler's documented defaults.  Invalid operator
    input is ignored here so a stale deployment variable cannot make the UI
    fail before the loop has a chance to emit its normal budget error.
    """

    values: dict[str, int] = {}
    env_map = {
        "max_input_tokens": "STATA_AGENT_CONTEXT_MAX_INPUT_TOKENS",
        "reserve_output_tokens": "STATA_AGENT_CONTEXT_RESERVE_OUTPUT_TOKENS",
        "recent_tail_tokens": "STATA_AGENT_CONTEXT_RECENT_TAIL_TOKENS",
        "memory_tokens": "STATA_AGENT_CONTEXT_MEMORY_TOKENS",
    }
    for field, env_name in env_map.items():
        configured = _setting_value(settings, f"context.{field.removesuffix('_tokens')}_tokens", None)
        # The catalog keys already include the ``_tokens`` suffix; keep an
        # explicit map for readability and fall back to legacy environment
        # variables when no snapshot was supplied.
        if settings is not None:
            configured = _setting_value(settings, {
                "max_input_tokens": "context.max_input_tokens",
                "reserve_output_tokens": "context.reserve_output_tokens",
                "recent_tail_tokens": "context.recent_tail_tokens",
                "memory_tokens": "context.memory_tokens",
            }[field], None)
            raw = "" if configured is None else str(configured)
        else:
            raw = os.environ.get(env_name, "").strip()
        if raw:
            try:
                values[field] = int(raw)
            except ValueError:
                return None
    if not values:
        return None
    try:
        from .harness.context_assembler import ContextBudget

        return ContextBudget(**values)
    except (ImportError, AttributeError, TypeError, ValueError):
        return None


class _CompositeRag:
    """Small read-only facade combining global and workspace-local indexes."""

    def __init__(self, indexes: list[Any]):
        self._indexes = tuple(indexes)

    def search(self, query: str, *, top_k: int = 5, roles=None, attachment_ids=None):
        found: dict[str, Any] = {}
        for index in self._indexes:
            for chunk in index.search(query, top_k=top_k, roles=roles, attachment_ids=attachment_ids):
                found.setdefault(chunk.chunk_id, chunk)
        return list(found.values())[:top_k]


def _rag(
    workspace_id: str | None = None,
    *,
    store: Any | None = None,
    settings: EffectiveSettings | None = None,
):
    """Build bounded global plus ready, same-workspace attachment retrieval."""
    lib = str(_setting_value(settings, "library.root", "") or "").strip()
    if settings is None and not lib:
        lib = os.environ.get("STATA_AGENT_LIBRARY", "").strip()
    library = Path(lib).expanduser() if lib else None
    indexes: list[Any] = []
    try:
        from .rag.index import build_hybrid

        if library is not None and library.exists():
            cache_path = DEFAULT_DB.parent / "rag_cache.json"
            index = build_hybrid(library, cache_path=cache_path, source_role="citable_evidence")
            with _RAG_CACHE_LOCK:
                _RAG_CACHE[(str(library.resolve()), str(cache_path.resolve()))] = index
            indexes.append(index)
        if workspace_id:
            service_scope = (
                nullcontext(AttachmentService(store, _attachment_root(settings)))
                if store is not None
                else _open_attachment_service()
            )
            with service_scope as service:
                records = service.ready(workspace_id)
                for role in ("citable_evidence", "style_only"):
                    selected = [record for record in records if record.source_role == role]
                    if not selected:
                        continue
                    paths = {record.attachment_id: service.path_for_attachment(record) for record in selected}
                    attachment_root = _attachment_root(settings)
                    indexes.append(
                        build_hybrid(
                            attachment_root / workspace_id,
                            cache_path=(attachment_root / workspace_id / "indexes" / f"{role}.json"),
                            source_role=role,
                            ready_attachment_ids=set(paths),
                            attachment_paths=paths,
                        )
                    )
    except Exception:  # noqa: BLE001
        pass
    if not indexes:
        return None
    return indexes[0] if len(indexes) == 1 else _CompositeRag(indexes)


def _rag_for_workspace(
    workspace_id: str | None,
    *,
    store: Any | None = None,
    settings: EffectiveSettings | None = None,
):
    """Preserve the historical zero-argument test/integration seam."""

    try:
        return _rag(workspace_id, store=store, settings=settings)
    except TypeError as error:
        try:
            return _rag(workspace_id, store=store)
        except TypeError:
            try:
                return _rag()
            except TypeError:
                raise error


def _skills(settings: EffectiveSettings | None = None) -> dict:
    """Load the configured skill root, including packaged defaults."""
    global _SKILL_ERRORS
    from .skills.loader import load_skill_dir

    configured = str(_setting_value(settings, "skills.root", "") or "").strip()
    if settings is None and not configured:
        configured = os.environ.get("STATA_AGENT_SKILLS", "").strip()
    if configured:
        skills_dir = Path(configured).expanduser()
        if not skills_dir.is_absolute():
            skills_dir = _APP_ROOT / skills_dir
    else:
        skills_dir = _APP_ROOT / "skills"
        if not skills_dir.exists():
            skills_dir = _PACKAGE_SKILLS_DIR
    if not skills_dir.exists():
        _SKILL_ERRORS = [f"{skills_dir}: skill directory does not exist"]
        return {}
    try:
        loaded = load_skill_dir(skills_dir)
        _SKILL_ERRORS = []
        return loaded
    except Exception as error:  # noqa: BLE001
        _SKILL_ERRORS = [str(error)[:500]]
        return {}


def _matched_skills(text: str, settings: EffectiveSettings | None = None) -> list:
    from .skills.loader import match_skills

    try:
        loaded = _skills(settings)
    except TypeError as error:
        try:
            loaded = _skills()
        except TypeError:
            raise error
    return match_skills(loaded, text)


_PHASE_LABELS = {
    "IDEA": "研究问题",
    "LITERATURE": "可行性",
    "DESIGN": "模型设定",
    "DATA": "数据准备",
    "ESTIMATION": "估计",
    "ROBUSTNESS": "稳健性",
    "WRITING": "写作",
    "VALIDATION": "验证",
    "DONE": "完成",
}
_PERMISSION_ACTS = frozenset({"delete_file", "web_search", "web_fetch", "download"})


def _event_list(store: SQLiteStore, idea: str = _IDEA) -> list[Event]:
    return list(store.scan(idea))


_SAFE_FAILURE_MESSAGES: dict[str, str] = {
    "permission_denied": "工具调用未通过当前策略。",
    "invalid_arguments": "工具参数无效。",
    "not_found": "请求的研究对象不存在。",
    "timeout": "工具执行超时，请稍后重试。",
    "unavailable": "工具当前不可用，请稍后重试。",
    "network_error": "网络工具调用失败，请检查连接后重试。",
    "uncertain": "外部副作用状态暂不可确认，请先核对运行记录。",
    "cancel_requested": "本轮已收到停止请求。",
    "cancelled": "工具调用已取消。",
    "tool_error": "工具执行失败，请查看 Trace 或导出诊断包。",
    "provider_error": "模型调用失败，请查看 Trace 或导出诊断包。",
    "credentials_missing": "尚未配置模型凭据，请在应用设置中填写 API Key。",
    "provider_disabled": "已检测到模型凭据，请先在应用设置中开启远端模型调用。",
    "provider_unavailable": "模型服务暂时不可用，请稍后重试。",
    "provider_unsupported": "当前模型不支持本轮请求，请调整后重试。",
    "context_error": "上下文组装失败，本轮未调用模型。",
    "context_budget": "上下文超过本轮预算，请缩短输入或从停点继续。",
    "privacy_denied": "当前隐私策略不允许这次模型调用。",
    "invalid_tool_calls": "模型返回的工具调用格式无效。",
    "budget_invalid": "本轮预算参数无效，已安全停止。",
    "budget_limit": "已达到单次请求预算上限，可继续处理或重试本轮。",
    "max_steps": "已达到单条消息的内部模型调用上限，可继续处理或重试本轮。",
    "tool_calls": "已达到单次请求的工具调用上限，可继续处理或重试本轮。",
    "storage_error": "研究账本写入失败，请刷新状态后重试。",
    "ledger_error": "研究账本操作失败，请刷新状态后重试。",
    "ledger_writer_conflict": "账本当前由另一运行实例占用，请关闭重复实例后刷新重试。",
    "ledger_writer_stale": "本次账本写入已失效，请刷新状态后再重试。",
    "ledger_unavailable": "账本暂时不可用，请稍后重试。",
    "run_cancelled": "本轮已停止，研究状态已保存。",
    "request_failed": "本轮未完成，请查看 Trace 或导出诊断包。",
    "operation_failed": "操作未完成，请查看 Trace 或导出诊断包。",
    # This is a stable policy message already used by the existing path
    # guard.  Preserve it for old clients without copying arbitrary payload
    # text into a browser projection.
    "path_outside_workspace": "拒绝：超出运行目录",
}
_SAFE_FAILURE_CODES = frozenset(_SAFE_FAILURE_MESSAGES)
_SAFE_FAILURE_TEXT_CODES = {
    "拒绝：超出运行目录": "path_outside_workspace",
}
_RETRYABLE_FAILURE_CODES = frozenset({
    "timeout",
    "unavailable",
    "network_error",
    "not_found",
    "credentials_missing",
    "provider_disabled",
    "provider_unavailable",
    "context_budget",
    "budget_invalid",
    "budget_limit",
    "max_steps",
    "tool_calls",
    "ledger_writer_conflict",
    "ledger_writer_stale",
    "ledger_unavailable",
})


def _safe_failure_projection(
    payload: Mapping[str, Any] | None,
    *,
    default_code: str = "operation_failed",
    default_message: str | None = None,
) -> dict[str, Any]:
    """Project an operation failure without copying untrusted detail.

    Tool/runner payloads may contain provider text, paths, Stata output, or
    exception messages.  The UI only needs a stable code and a fixed message;
    the diagnostic bundle remains the local-only place for deeper inspection.
    """

    source: Any = payload if isinstance(payload, Mapping) else {}
    candidate: Any = None
    for key in ("error", "detail", "reason", "issues"):
        if isinstance(source, Mapping) and source.get(key) not in (None, "", {}):
            candidate = source.get(key)
            break
    code_value: Any = None
    if isinstance(candidate, Mapping):
        for key in ("code", "type", "error_code", "failure_code"):
            value = candidate.get(key)
            if isinstance(value, str) and value.strip():
                code_value = value.strip().lower()
                break
    elif isinstance(candidate, str):
        mapped = _SAFE_FAILURE_TEXT_CODES.get(candidate.strip())
        if mapped:
            code_value = mapped
        elif candidate.strip().lower() in _SAFE_FAILURE_CODES:
            code_value = candidate.strip().lower()

    code = code_value if isinstance(code_value, str) and code_value in _SAFE_FAILURE_CODES else default_code
    if code not in _SAFE_FAILURE_CODES:
        code = "operation_failed"
    message = _SAFE_FAILURE_MESSAGES[code]
    if default_message is not None and code == default_code:
        message = default_message
    retryable = code in _RETRYABLE_FAILURE_CODES
    return {
        "code": code,
        "message": message,
        "retryable": retryable,
        "support_action": "retry" if retryable else "download_diagnostics",
    }


def _terminal_outcome_projection(events: list[Event]) -> dict[str, Any] | None:
    """Find the newest durable terminal reason and expose only safe fields."""

    for event in reversed(events):
        payload = event.payload or {}
        if event.event_type == EVENT_AGENT_STEP:
            reason = payload.get("terminal_reason") or payload.get("stop_reason")
            if reason not in (None, ""):
                outcome = terminal_outcome_for(
                    reason,
                    cancelled=bool(payload.get("cancel_requested")) and str(reason) == "cancelled",
                )
                result: dict[str, Any] = outcome.as_dict()
                if outcome.code:
                    result["failure"] = _safe_failure_projection({"error": {"code": outcome.code}})
                return result
        elif event.event_type == EVENT_RUN_UNCERTAIN:
            outcome = terminal_outcome_for("uncertain")
            return {**outcome.as_dict(), "failure": _safe_failure_projection({"error": {"code": "uncertain"}})}
        elif event.event_type == EVENT_RUN_FAILED:
            outcome = terminal_outcome_for("operation_failed")
            return {**outcome.as_dict(), "failure": _safe_failure_projection({"error": {"code": "operation_failed"}})}
        elif event.event_type == EVENT_BUDGET:
            reason = str(payload.get("kind") or "budget_limit")
            outcome = terminal_outcome_for(reason)
            return {**outcome.as_dict(), "failure": _safe_failure_projection({"error": {"code": outcome.code or "budget_limit"}})}
        elif event.event_type == EVENT_RUN_SUCCEEDED:
            return terminal_outcome_for("model_stop").as_dict()
    return None


def _safe_projection_id(value: Any) -> str | None:
    """Keep only bounded, printable correlation ids in a public projection."""

    if not isinstance(value, str):
        return None
    value = value.strip()
    if not 1 <= len(value) <= 128:
        return None
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        return None
    return value


def _payload_call_id(event: Event) -> str | None:
    payload = event.payload or {}
    return _safe_projection_id(payload.get("call_id") or payload.get("tool_id"))


def _legacy_tool_id(event: Event, *, terminal: bool) -> str:
    """Derive a deterministic id for pre-call_id ledger events."""

    base = _safe_projection_id(event.correlation_id) or _safe_projection_id(event.operation_id)
    base = base or _safe_projection_id(event.idea_id) or "legacy"
    seq = event.seq if isinstance(event.seq, int) and event.seq >= 0 else 0
    phase = "done" if terminal else "invoked"
    return f"{base}:legacy:{phase}:{seq}"


def _safe_attachment_manifest(value: Any) -> list[dict[str, Any]]:
    """Allow-list durable attachment metadata for conversation replay."""

    if not isinstance(value, list):
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in value[:8]:
        if not isinstance(row, Mapping):
            continue
        attachment_id = _safe_projection_id(row.get("attachment_id"))
        if attachment_id is None or attachment_id in seen:
            continue
        display_name = row.get("display_name")
        source_role = row.get("source_role")
        detected_format = row.get("detected_format")
        page_count = row.get("page_count")
        if not all(isinstance(item, str) and 0 < len(item) <= 180 for item in (display_name, source_role, detected_format)):
            continue
        if isinstance(page_count, bool) or not isinstance(page_count, int) or page_count < 0:
            continue
        seen.add(attachment_id)
        out.append(
            {
                "attachment_id": attachment_id,
                "display_name": display_name,
                "source_role": source_role,
                "detected_format": detected_format,
                "page_count": page_count,
            }
        )
    return out


def _event_payload_preview(event: Event) -> dict[str, Any]:
    """Return enough payload for a trace/replay view without dumping raw JSON.

    Text is still treated as untrusted by the browser, which renders it with
    ``textContent``. Long research material is capped before it leaves the
    read-only API, while evidence and run endpoints provide their own detail
    views when the researcher explicitly opens them.
    """

    payload = event.payload or {}
    kind = event.event_type
    if kind == EVENT_USER:
        out = {"text": str(payload.get("text", ""))[:2000]}
        attachments = _safe_attachment_manifest(payload.get("attachments"))
        if attachments:
            out["attachments"] = attachments
        return out
    if kind == EVENT_AGENT_STEP:
        acts = payload.get("acts") or []
        out = {
            "decision_summary": str(payload.get("decision_summary") or "")[:500],
            "ask": str(payload.get("ask") or "")[:1000],
            "stop_reason": payload.get("stop_reason"),
            "terminal_reason": payload.get("terminal_reason") or payload.get("stop_reason"),
            "acts": [str(a.get("act_type", "")) for a in acts if isinstance(a, dict)][:12],
        }
        reason = payload.get("terminal_reason") or payload.get("stop_reason")
        if reason not in (None, ""):
            outcome = terminal_outcome_for(reason, cancelled=bool(payload.get("cancel_requested")))
            if outcome.code:
                out["failure"] = _safe_failure_projection({"error": {"code": outcome.code}})
        return out
    keys = {
        "request_id",
        "act",
        "tool",
        "run_id",
        "result_id",
        "spec_id",
        "operation_id",
        "status",
        "ok",
        "call_id",
        "tool_id",
    }
    out: dict[str, Any] = {}
    for key in keys:
        if key not in payload:
            continue
        if key in {"call_id", "tool_id", "request_id", "run_id", "result_id", "spec_id", "operation_id"}:
            value = _safe_projection_id(payload.get(key))
            if value is not None:
                out[key] = value
        elif key == "tool":
            value = _safe_projection_id(payload.get(key))
            if value is not None:
                out[key] = value
        elif key in {"status"}:
            value = payload.get(key)
            if isinstance(value, str) and len(value) <= 64:
                out[key] = value
        elif key == "ok" and isinstance(payload.get(key), bool):
            out[key] = payload[key]
    if kind in {EVENT_RUN_SUCCEEDED, EVENT_RUN_FAILED, EVENT_RUN_UNCERTAIN}:
        machine = payload.get("machine")
        if isinstance(machine, dict):
            out["machine"] = {str(k): machine[k] for k in list(machine)[:12]}
    if kind == EVENT_TOOL_DONE and payload.get("ok") is False:
        out["failure"] = _safe_failure_projection(
            payload.get("result") if isinstance(payload.get("result"), Mapping) else payload
        )
    elif kind in {EVENT_RUN_FAILED, EVENT_RUN_UNCERTAIN}:
        out["failure"] = _safe_failure_projection(payload, default_code="operation_failed")
    elif kind == EVENT_HEALTH and payload.get("ok") is False:
        out["failure"] = _safe_failure_projection(
            payload,
            default_code="operation_failed",
            default_message="健康检查未通过，自动推进已暂停。",
        )
    if kind == EVENT_APPROVAL_REQ and isinstance(payload.get("target"), dict):
        out["target_keys"] = sorted(str(k) for k in payload["target"].keys())[:12]
    return out


def _public_event(event: Event) -> dict[str, Any]:
    """Stable, compact event shape retained for old clients and trace UI."""

    payload = event.payload or {}
    call_id = _payload_call_id(event)
    if call_id is None and event.event_type == EVENT_TOOL_INVOKED:
        call_id = _legacy_tool_id(event, terminal=False)
    elif call_id is None and event.event_type == EVENT_TOOL_DONE:
        call_id = _legacy_tool_id(event, terminal=True)
    object_value = next(
        (
            _safe_projection_id(payload.get(key))
            for key in ("object", "claim_id", "card_id", "run_id", "result_id", "spec_id", "request_id")
            if _safe_projection_id(payload.get(key)) is not None
        ),
        None,
    )
    if object_value is None and isinstance(payload.get("target"), dict):
        # Target values may contain paths, code, or private arguments.  Keep
        # only its shape for trace search/display.
        object_value = {"keys": sorted(str(key) for key in payload["target"].keys())[:12]}
    if event.event_type == EVENT_TOOL_DONE and payload.get("ok") is False:
        summary = _safe_failure_projection(
            payload.get("result") if isinstance(payload.get("result"), Mapping) else payload
        )["message"]
    elif event.event_type in {EVENT_RUN_FAILED, EVENT_RUN_UNCERTAIN}:
        summary = _safe_failure_projection(payload)["message"]
    elif event.event_type == EVENT_HEALTH and payload.get("ok") is False:
        summary = "健康检查未通过，自动推进已暂停。"
    elif event.event_type == EVENT_APPROVAL_REQ:
        summary = "审批请求"
    elif event.event_type == EVENT_TOOL_DONE and payload.get("ok") is True:
        summary = "已完成"
    else:
        summary = next(
            (
                str(payload.get(key))
                for key in ("summary", "reply", "ask", "decision_summary", "text")
                if isinstance(payload.get(key), str) and payload.get(key)
            ),
            "",
        )[:240]

    event_labels = {
        EVENT_USER: "用户消息",
        EVENT_AGENT_STEP: "Agent 终态",
        EVENT_TOOL_INVOKED: "工具开始",
        EVENT_TOOL_DONE: "工具完成",
        EVENT_RUN_REQ: "Stata 运行请求",
        EVENT_RUN_SUCCEEDED: "Stata 运行完成",
        EVENT_RUN_FAILED: "Stata 运行失败",
        EVENT_RUN_UNCERTAIN: "Stata 状态待确认",
        EVENT_CONTEXT_ASSEMBLED: "上下文准备",
        EVENT_BUDGET: "预算限制",
        EVENT_CARD_SIGNED: "证据卡签发",
        EVENT_CLAIM_SIGNED: "结论签发",
    }

    return {
        "seq": event.seq,
        "type": event.event_type,
        "event_type": event.event_type,
        "actor": event.actor,
        "phase": event.phase,
        "op": event.operation_id,
        "operation_id": event.operation_id,
        "correlation_id": _safe_projection_id(event.correlation_id),
        "created_at": event.created_at,
        "object": object_value,
        "call_id": call_id,
        "tool_id": call_id if event.event_type in {EVENT_TOOL_INVOKED, EVENT_TOOL_DONE} else None,
        "summary": summary,
        "label": event_labels.get(event.event_type, event.event_type),
        "payload_keys": sorted((event.payload or {}).keys())[:12],
        "payload_preview": _event_payload_preview(event),
    }


def _approval_records(events: list[Event]) -> list[dict[str, Any]]:
    """Fold approval request/decision events into a read-only UI list."""

    records: dict[str, dict[str, Any]] = {}
    for event in events:
        payload = event.payload or {}
        request_id = str(payload.get("request_id") or "")
        if not request_id:
            continue
        if event.event_type == EVENT_APPROVAL_REQ:
            act = str(payload.get("act") or "")
            records[request_id] = {
                "request_id": request_id,
                "act": act,
                "kind": "permission_gate" if act in _PERMISSION_ACTS else "research_gate",
                "reason": str(payload.get("reason") or ""),
                "note": str(payload.get("note") or ""),
                "target": payload.get("target") if isinstance(payload.get("target"), dict) else {},
                "status": "pending",
                "decision": None,
                "requested_seq": event.seq,
                "requested_at": event.created_at,
                "decided_note": "",
                "decision_seq": None,
                "decided_at": None,
            }
        elif event.event_type in {EVENT_APPROVAL_GRANT, EVENT_APPROVAL_REJECT} and request_id in records:
            decision = str(payload.get("decision") or "")
            records[request_id]["status"] = (
                "modified"
                if decision == "modify"
                else "approved"
                if event.event_type == EVENT_APPROVAL_GRANT
                else "rejected"
            )
            records[request_id]["decision"] = decision or (
                "approve" if event.event_type == EVENT_APPROVAL_GRANT else "reject"
            )
            records[request_id]["decided_note"] = str(payload.get("note") or "")
            records[request_id]["decision_seq"] = event.seq
            records[request_id]["decided_at"] = event.created_at
    return list(records.values())


def _phase_history(events: list[Event], current: str | None) -> list[str]:
    history: list[str] = []
    for event in events:
        if event.event_type != EVENT_PHASE:
            continue
        phase = (event.payload or {}).get("to") or (event.payload or {}).get("phase") or event.phase
        if phase and str(phase) not in history:
            history.append(str(phase))
    if current and current not in history:
        history.append(current)
    return history


def _run_state(events: list[Event], approvals: list[dict[str, Any]]) -> tuple[str, str]:
    pending = [item for item in approvals if item["status"] == "pending"]
    if pending:
        return "awaiting_user", f"等待你决定 {pending[-1]['act'] or '一个研究闸门'}"
    for event in reversed(events):
        payload = event.payload or {}
        if event.event_type == EVENT_BUDGET:
            return "paused", "已达到单次请求预算上限，可继续处理或重试本轮"
        if event.event_type == EVENT_HEALTH and payload.get("ok") is False:
            return "paused", "健康检查未通过，已停在最近一致状态"
        if event.event_type == EVENT_RUN_FAILED:
            return "failed", "最近一次运行未完成，请查看运行记录"
        if event.event_type == EVENT_RUN_UNCERTAIN:
            return "uncertain", "最近一次运行状态不确定，请先核对运行记录"
        if event.event_type == EVENT_AGENT_STEP:
            if payload.get("ask"):
                return "awaiting_user", "等待你补充研究信息"
            reason = payload.get("terminal_reason") or payload.get("stop_reason")
            if reason not in (None, ""):
                outcome = terminal_outcome_for(
                    reason,
                    cancelled=bool(payload.get("cancel_requested")) and str(reason) == "cancelled",
                )
                if outcome.status == "uncertain":
                    return "uncertain", "最近一次运行状态不确定，请先核对运行记录"
                if outcome.status == "cancelled":
                    return "cancelled", "本轮已停止，研究状态已保存"
                if outcome.status == "failed":
                    failure = _safe_failure_projection({"error": {"code": outcome.code or "operation_failed"}})
                    return "failed", failure["message"]
                if outcome.status == "paused":
                    return "paused", "本轮已安全暂停，可继续处理或重试"
            break
    if any(event.event_type == EVENT_RUN_SUCCEEDED for event in events):
        return "idle", "最近一次运行已完成"
    if events:
        return "idle", "等待你的下一条研究指示"
    return "idle", "等待你的第一条研究问题"


def _signed_evidence_by_run(events: list[Event]) -> tuple[dict[str, int], dict[str, int]]:
    """Index signed evidence by run without trusting assistant prose.

    Evidence events store their immutable card/claim payloads, while a run
    event carries the run id.  The conversation projection uses this small
    read-only index to choose a truthful status sentence; it never infers a
    signature from ``run.succeeded`` or model-authored text.
    """

    cards_by_id: dict[str, str] = {}
    cards_by_run: dict[str, int] = {}
    claim_card_sets: list[list[str]] = []
    for event in events:
        payload = event.payload if isinstance(event.payload, Mapping) else {}
        if event.event_type == EVENT_CARD_SIGNED:
            card = payload.get("card") if isinstance(payload.get("card"), Mapping) else payload
            card_id = card.get("card_id") if isinstance(card, Mapping) else None
            locator = card.get("locator") if isinstance(card, Mapping) else None
            run_id = locator.get("run_id") if isinstance(locator, Mapping) else None
            if isinstance(card_id, str) and isinstance(run_id, str) and card_id and run_id:
                cards_by_id[card_id] = run_id
                cards_by_run[run_id] = cards_by_run.get(run_id, 0) + 1
        elif event.event_type == EVENT_CLAIM_SIGNED:
            claim = payload.get("claim") if isinstance(payload.get("claim"), Mapping) else payload
            card_ids = claim.get("cards") if isinstance(claim, Mapping) else None
            if not isinstance(card_ids, list):
                continue
            claim_card_sets.append([card_id for card_id in card_ids if isinstance(card_id, str)])
    claims_by_run: dict[str, int] = {}
    # Resolve claims after the card pass so replayed or migrated ledgers do
    # not depend on card/claim append order.
    for card_ids in claim_card_sets:
        run_ids = {cards_by_id.get(card_id) for card_id in card_ids}
        for run_id in sorted(item for item in run_ids if item):
            claims_by_run[run_id] = claims_by_run.get(run_id, 0) + 1
    return cards_by_run, claims_by_run


def _conversation(events: list[Event]) -> list[dict[str, Any]]:
    """Project durable conversation/replay blocks from append-only events."""

    messages: list[dict[str, Any]] = []
    tools_by_id: dict[str, dict[str, Any]] = {}
    pending_tools: dict[str, list[dict[str, Any]]] = {}
    signed_cards_by_run, signed_claims_by_run = _signed_evidence_by_run(events)

    def remove_pending(item: dict[str, Any], tool_name: str) -> None:
        queue = pending_tools.get(tool_name)
        if not queue:
            return
        pending_tools[tool_name] = [candidate for candidate in queue if candidate is not item]

    def new_tool_item(event: Event, tool_name: str, tool_id: str, args: Mapping[str, Any] | None) -> dict[str, Any]:
        raw_args = args if isinstance(args, Mapping) else {}
        item = {
            "seq": event.seq,
            "phase": event.phase,
            "created_at": event.created_at,
            "role": "system",
            "kind": "tool",
            "tool_id": tool_id,
            "call_id": tool_id,
            "tool_name": tool_name,
            "status": "running",
            "ok": None,
            "args_keys": sorted(str(key) for key in raw_args)[:16],
        }
        messages.append(item)
        tools_by_id[tool_id] = item
        pending_tools.setdefault(tool_name, []).append(item)
        return item

    for event in events:
        payload = event.payload or {}
        base = {"seq": event.seq, "phase": event.phase, "created_at": event.created_at}
        if event.event_type == EVENT_USER:
            item = {**base, "role": "user", "kind": "text", "text": str(payload.get("text") or "")}
            attachments = _safe_attachment_manifest(payload.get("attachments"))
            if attachments:
                item["attachments"] = attachments
            messages.append(item)
        elif event.event_type == EVENT_AGENT_STEP:
            ask = str(payload.get("ask") or "")
            reply = str(payload.get("reply") or "")
            summary = str(payload.get("decision_summary") or "")
            acts = payload.get("acts") if isinstance(payload.get("acts"), list) else []
            agent_item = {
                **base,
                "role": "assistant",
                "kind": "agent",
                "text": reply or ask or summary or "Agent 已完成一轮判断。",
                "reply": reply,
                "summary": summary,
                "ask": ask,
                "stop_reason": payload.get("stop_reason"),
                "terminal_reason": payload.get("terminal_reason") or payload.get("stop_reason"),
                "acts": [
                    {"act_type": str(a.get("act_type") or ""), "reason": str(a.get("reason") or "")}
                    for a in acts
                    if isinstance(a, dict)
                ][:12],
            }
            reason = agent_item["terminal_reason"]
            if reason not in (None, ""):
                outcome = terminal_outcome_for(
                    reason,
                    cancelled=bool(payload.get("cancel_requested")) and str(reason) == "cancelled",
                )
                agent_item["terminal_status"] = outcome.status
                if outcome.code:
                    failure = _safe_failure_projection({"error": {"code": outcome.code}})
                    agent_item.update(
                        {
                            "error_code": failure["code"],
                            "retryable": failure["retryable"],
                            "support_action": failure["support_action"],
                        }
                    )
            messages.append(agent_item)
        elif event.event_type == EVENT_APPROVAL_REQ:
            act = str(payload.get("act") or "")
            messages.append(
                {
                    **base,
                    "role": "assistant",
                    "kind": "approval",
                    "request_id": str(payload.get("request_id") or ""),
                    "act": act,
                    "gate_kind": "permission_gate" if act in _PERMISSION_ACTS else "research_gate",
                    "reason": str(payload.get("reason") or ""),
                    "note": str(payload.get("note") or ""),
                    "target": payload.get("target") if isinstance(payload.get("target"), dict) else {},
                }
            )
        elif event.event_type in {EVENT_APPROVAL_GRANT, EVENT_APPROVAL_REJECT}:
            decision = str(payload.get("decision") or "")
            messages.append(
                {
                    **base,
                    "role": "system",
                    "kind": "approval_decision",
                    "request_id": str(payload.get("request_id") or ""),
                    "decision": (
                        decision
                        if decision in {"approve", "reject", "modify"}
                        else "approved"
                        if event.event_type == EVENT_APPROVAL_GRANT
                        else "rejected"
                    ),
                    "note": str(payload.get("note") or ""),
                }
            )
        elif event.event_type == EVENT_TOOL_INVOKED:
            tool_name = str(payload.get("tool") or "未知工具")
            args = payload.get("args") if isinstance(payload.get("args"), dict) else {}
            tool_id = _payload_call_id(event) or _legacy_tool_id(event, terminal=False)
            item = tools_by_id.get(tool_id)
            if item is None:
                item = new_tool_item(event, tool_name, tool_id, args)
            else:
                # Completion may legally arrive before its start in a replay
                # stream.  Fill in the start metadata without reopening a
                # terminal card or adding a duplicate.
                item.setdefault("seq", event.seq)
                item.setdefault("phase", event.phase)
                item.setdefault("created_at", event.created_at)
                item["tool_name"] = tool_name
                item["args_keys"] = sorted(str(key) for key in args)[:16]
                if item.get("status") == "running" and item not in pending_tools.setdefault(tool_name, []):
                    pending_tools[tool_name].append(item)
        elif event.event_type == EVENT_TOOL_DONE:
            tool_name = str(payload.get("tool") or "未知工具")
            result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
            ok = bool(payload.get("ok"))
            explicit_id = _payload_call_id(event)
            item = tools_by_id.get(explicit_id) if explicit_id else None
            if item is None and explicit_id is None:
                pending = [candidate for candidate in (pending_tools.get(tool_name) or []) if candidate.get("status") == "running"]
                # A legacy event without an id may be paired only when the
                # name identifies exactly one outstanding call.  If multiple
                # same-name calls are open, creating a separate card is safer
                # than guessing and attaching the result to the wrong call.
                if len(pending) == 1:
                    item = pending[0]
                    remove_pending(item, tool_name)
            if item is None:
                tool_id = explicit_id or _legacy_tool_id(event, terminal=True)
                item = tools_by_id.get(tool_id)
                if item is None:
                    item = new_tool_item(event, tool_name, tool_id, {})
                remove_pending(item, tool_name)
            else:
                remove_pending(item, tool_name)
            failure = _safe_failure_projection(result if isinstance(result, Mapping) else payload) if not ok else None
            item.update(
                {
                    "status": "succeeded" if ok else "failed",
                    "ok": ok,
                    "completed_seq": event.seq,
                }
            )
            if failure is not None:
                item.update(
                    {
                        "error": failure["message"],
                        "error_code": failure["code"],
                        "retryable": failure["retryable"],
                        "support_action": failure["support_action"],
                    }
                )
        elif event.event_type == EVENT_RUN_SUCCEEDED:
            provenance = payload.get("provenance") if isinstance(payload.get("provenance"), dict) else {}
            machine = payload.get("machine") if isinstance(payload.get("machine"), dict) else {}
            run_id = str(payload.get("run_id") or payload.get("result_id") or "")
            signed_cards = signed_cards_by_run.get(run_id, 0)
            signed_claims = signed_claims_by_run.get(run_id, 0)
            if signed_claims:
                evidence_status = "claim_signed"
                text = "运行完成，已形成已验证研究结论。"
            elif signed_cards:
                evidence_status = "evidence_card_signed"
                text = f"运行完成，已签入证据卡（{signed_cards} 项）。"
            elif machine:
                evidence_status = "structured_result_available"
                text = "运行完成，已获得结构化结果，尚未签入证据。"
            else:
                evidence_status = "execution_succeeded"
                text = "运行完成，仅确认执行成功，未生成结构化可验证结果。"
            messages.append(
                {
                    **base,
                    "role": "assistant",
                    "kind": "run",
                    "status": "succeeded",
                    "run_id": run_id,
                    "machine": machine,
                    "provenance": provenance,
                    "text": text,
                    "evidence_status": evidence_status,
                    "signed_cards": signed_cards,
                    "signed_claims": signed_claims,
                }
            )
        elif event.event_type in {EVENT_RUN_FAILED, EVENT_RUN_UNCERTAIN}:
            failure = _safe_failure_projection(payload)
            messages.append(
                {
                    **base,
                    "role": "assistant",
                    "kind": "error",
                    "status": "uncertain" if event.event_type == EVENT_RUN_UNCERTAIN else "failed",
                    "run_id": str(payload.get("run_id") or payload.get("result_id") or ""),
                    "text": "主回归未完成；请查看最近运行记录，再决定从停点续跑还是调整口径。",
                    "detail": failure["message"],
                    "error_code": failure["code"],
                    "retryable": failure["retryable"],
                    "support_action": failure["support_action"],
                }
            )
        elif event.event_type == EVENT_BUDGET:
            messages.append(
                {
                    **base,
                    "role": "assistant",
                    "kind": "error",
                    "status": "paused",
                    "text": "本轮已达到预算上限，研究状态停在最近一致状态。",
                    "detail": "可继续处理、重试本轮，或发送新的研究口径。",
                }
            )
        elif event.event_type == EVENT_HEALTH and payload.get("ok") is False:
            failure = _safe_failure_projection(
                payload,
                default_code="operation_failed",
                default_message="健康检查未通过，自动推进已暂停。",
            )
            messages.append(
                {
                    **base,
                    "role": "assistant",
                    "kind": "error",
                    "status": "paused",
                    "text": "健康检查未通过，自动推进已暂停。",
                    "detail": failure["message"],
                    "error_code": failure["code"],
                    "retryable": failure["retryable"],
                    "support_action": failure["support_action"],
                }
            )
    return messages


def _summary(store: SQLiteStore, idea: str = _IDEA) -> dict[str, Any]:
    events = _event_list(store, idea)
    proj = store.project(idea)
    rs = proj.research_state
    approvals = _approval_records(events)
    run_status, status_detail = _run_state(events, approvals)
    terminal = _terminal_outcome_projection(events)
    first_idea = next((e for e in events if e.event_type == EVENT_IDEA), None)
    idea_title = str((first_idea.payload if first_idea else {}).get("question") or "未命名研究")
    current_phase = proj.phase
    if not current_phase:
        phase_events = _phase_history(events, None)
        current_phase = phase_events[-1] if phase_events else ("IDEA" if events else None)
    phase_history = _phase_history(events, proj.phase)
    claims = sorted((c.claim_id, c.status, c.statement[:80]) for c in proj.claims.values())
    claim_records = [
        {**claim.model_dump(), "citation_count": len(claim.cards), "card_ids": list(claim.cards)}
        for claim in sorted(proj.claims.values(), key=lambda item: item.claim_id)
    ]
    card_records = [card.model_dump() for card in sorted(proj.cards.values(), key=lambda item: item.card_id)]
    run_records = [{"run_id": run_id, **record.model_dump()} for run_id, record in sorted(proj.runs.items())]
    checkpoint_event = next((e for e in reversed(events) if e.event_type == "checkpoint.snapshot"), None)
    workspace = next((item for item in _workspace_entries(store) if item["id"] == idea), None)
    workspace = workspace or {
        "id": idea,
        "name": idea,
        "events": len(events),
        "run_status": run_status,
        "pending_approvals": len([item for item in approvals if item["status"] == "pending"]),
    }
    active_request = _request_control_public(_latest_active_request(idea))
    return {
        # Existing v0 fields; keep their shape for older clients.
        "phase": proj.phase,
        "spec": rs.current_spec_id if rs else None,
        "claims": claims,
        "cards": sorted(proj.cards),
        "runs": {k: v.status for k, v in sorted(proj.runs.items())},
        "events": len(events),
        "workspace_id": idea,
        # ``workspace_id`` above is the legacy registry/ledger key.  Expose
        # the opaque V2 identity separately so old clients remain compatible
        # while memory/context consumers can audit their partition.
        "memory_workspace_id": _workspace_id(idea),
        "workspace_name": workspace["name"],
        "workspace": workspace,
        # Read-only UI projection fields.
        "idea_title": idea_title,
        "privacy_mode": _privacy_mode(),
        "local_strict": _privacy_mode() == "local_strict",
        "config": _config_info(),
        "display_phase": current_phase,
        "phase_history": phase_history,
        "phase_labels": _PHASE_LABELS,
        "run_status": run_status,
        "status_detail": status_detail,
        "terminal_status": terminal.get("status") if terminal else None,
        "terminal_reason": terminal.get("terminal_reason") if terminal else None,
        "terminal_failure": terminal.get("failure") if terminal else None,
        "research_state": {
            "sample_sig": rs.sample_sig if rs else None,
            "dependent_variable": None,
            "core_explanatory_variable": None,
            "controls": [],
            "fixed_effects": None,
            "cluster_level": None,
            "identification_strategy": None,
            "current_spec_id": rs.current_spec_id if rs else None,
            "current_family_id": rs.current_family_id if rs else None,
        },
        "claim_records": claim_records,
        "card_records": card_records,
        "run_records": run_records,
        "pending_approvals": [item for item in approvals if item["status"] == "pending"],
        "approvals": approvals,
        "messages": _conversation(events),
        "checkpoint": {
            "available": checkpoint_event is not None,
            "seq": checkpoint_event.seq if checkpoint_event else None,
        },
        "active_request": active_request,
        "active_request_id": active_request.get("request_id") if active_request else None,
        "draft_ready": bool(any(c.status == "supported" for c in proj.claims.values())),
        "capabilities": {
            "approvals": True,
            "approval_revision": True,
            "resume": True,
            "goal_mode": True,
            "stop": True,
            "draft": True,
            "cursor_events": True,
        },
        "health": {"ok": run_status not in {"failed", "uncertain", "paused"}, "detail": status_detail},
        "updated_at": events[-1].created_at if events else None,
    }


def _draft_response(
    store: SQLiteStore,
    *,
    require_ready: bool = False,
    idea: str = _IDEA,
) -> StreamingResponse:
    from .writer.draft_multi import draft_from_ledger

    proj = store.project(idea)
    if require_ready and not any(c.status == "supported" for c in proj.claims.values()):
        raise HTTPException(status_code=409, detail="尚未有已确认的证据结论，暂不能生成 Word 初稿。")
    method = (
        "本研究用双重差分（DID）识别：结果变量对 处理组×期后 交互回归，"
        "标准误聚类到处理相关层级；样本为双期店面（长表）。"
    )
    limits = (
        "数据仅两期，无法做事件研究/动态效应；平行趋势改用基线特征平衡与多个"
        "稳健性口径替代，并在解读时保留因果表述的审慎。"
    )
    if proj.runs:
        data = draft_from_ledger(proj, method=method, limits=limits)
    else:
        paragraphs: list[tuple[str, object]] = []
        for claim in sorted(proj.claims.values(), key=lambda c: c.claim_id):
            paragraphs.append((render_claim_sentence(claim, proj.cards), claim))
        data = claims_to_docx("实证研究初稿（自动渲染）", paragraphs).getvalue()
    return StreamingResponse(
        iter([data]),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": "attachment; filename=draft.docx"},
    )


def _last_seq(store: SQLiteStore, idea: str = _IDEA) -> int:
    return max((event.seq or 0 for event in store.scan(idea)), default=0)


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    """Serve the local, dependency-free UI document."""

    return _INDEX_FILE.read_text(encoding="utf-8")


class WorkspaceIn(BaseModel):
    """Payload for creating a local workspace; no ledger event is written."""

    name: str = "新研究工作区"
    id: str | None = None


class MemoryOutboxRecoveryIn(BaseModel):
    """Strict body for one acknowledged failed-outbox recovery."""

    model_config = ConfigDict(extra="forbid")

    idempotency_key: StrictStr
    expected_state_version: StrictInt
    acknowledge_at_least_once: StrictBool


class MemoryOutboxRetentionPruneIn(BaseModel):
    """Strict body for one preview-bound completed-row deletion."""

    model_config = ConfigDict(extra="forbid")

    cutoff: StrictInt
    limit: StrictInt
    cursor: StrictStr | None = None
    selection_token: StrictStr
    acknowledge_irreversible_delete: StrictBool


@app.get("/api/workspaces")
def workspaces():
    """List the local workspace registry plus read-only ledger counters."""

    s = _store()
    try:
        return {"items": _workspace_entries(s), "active": _IDEA}
    finally:
        s.close()


@app.post("/api/workspaces")
def create_workspace(body: WorkspaceIn):
    """Create a workspace through the SQLite-backed application service.

    The registry is metadata only. Research state still enters the event
    ledger through ``/api/chat`` and runner/approval routes.
    """

    try:
        with _WORKSPACE_REGISTRY_LOCK:
            with _open_workspace_service() as service:
                _migrate_legacy_workspaces_once(service)
                record = service.create(CreateWorkspaceRequest(name=body.name, id=body.id))
                row = record.as_dict()
                rows = [item.as_dict() for item in service.list()]
    except WorkspaceValidationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except WorkspaceConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    idea = row["id"]
    return {
        "workspace": {**row, "events": 0, "last_seq": None, "run_status": "idle", "pending_approvals": 0},
        "items": rows,
        "active": idea,
    }


@app.get("/api/state")
def state(ws: str = _IDEA):
    idea = _resolve_workspace(ws)
    s = _store()
    try:
        return _summary(s, idea)
    finally:
        s.close()


# ---------------------------------------------------------------------------
# Application settings boundary
# ---------------------------------------------------------------------------

_SETTINGS_MAX_BODY_BYTES = 64 * 1024
_SETTINGS_MAX_BUNDLE_BYTES = 64 * 1024 * 1024
_SETTINGS_ALLOWED_PROVIDERS = frozenset(item.provider_id for item in provider_definitions())
_SETTINGS_UNSAFE_WHILE_ACTIVE = frozenset({
    "stata.mcp_dir",
    "library.root",
    "skills.root",
    "ui.port",
    "storage.database",
    "storage.workspaces",
    "storage.attachments",
})


def _settings_projection(service: SettingsService) -> dict[str, Any]:
    effective = service.resolve()
    values = {key: item.as_dict() for key, item in effective.values.items()}
    statuses: dict[str, Any] = {}
    for provider in sorted(_SETTINGS_ALLOWED_PROVIDERS):
        try:
            statuses[provider] = service.secret_status(provider).as_dict()
        except SecureStoreUnavailable:
            statuses[provider] = {
                "provider": provider,
                "configured": False,
                "source": "none",
                "editable": False,
            }
        except Exception:  # noqa: BLE001 - safe health projection only
            statuses[provider] = {
                "provider": provider,
                "configured": False,
                "source": "none",
                "editable": False,
            }
    catalog_statuses = {
        provider: statuses.get(provider, {})
        for provider in _SETTINGS_ALLOWED_PROVIDERS
    }
    return {
        "revision": effective.revision,
        "values": values,
        "settings": values,
        "secrets": statuses,
        # The catalog is the only source used by the browser for provider and
        # model choices.  It contains capability metadata and masked secret
        # status, never API keys or credential-manager internals.
        "provider_catalog": provider_public_catalog(catalog_statuses),
        "restart_required": effective.restart_required,
        "pending_restart": effective.restart_required,
        "pending_keys": list(effective.pending_keys),
        "health": [item.as_dict() for item in effective.health],
    }


def _settings_json_response(payload: Mapping[str, Any], *, status_code: int = 200) -> JSONResponse:
    response = JSONResponse(status_code=status_code, content=dict(payload))
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return response


def _host_without_port(value: str) -> str:
    raw = str(value or "").strip().lower()
    if raw.startswith("["):
        return raw.split("]", 1)[0] + "]"
    return raw.rsplit(":", 1)[0] if raw.count(":") == 1 else raw


def _settings_allowed_hosts(request: Request) -> set[str]:
    configured = getattr(request.app.state, "settings_allowed_hosts", None)
    if configured is None:
        return {"localhost", "127.0.0.1", "[::1]"}
    return {str(item).strip().lower() for item in configured if str(item).strip()}


def _settings_security_guard(
    request: Request,
    *,
    require_json: bool = True,
    max_body_bytes: int = _SETTINGS_MAX_BODY_BYTES,
) -> None:
    """Enforce local-origin, CSRF and bounded-body settings mutations."""

    allowed_hosts = _settings_allowed_hosts(request)
    host = _host_without_port(request.headers.get("host", ""))
    if not host or host not in allowed_hosts:
        raise _SettingsHTTPException(403, "settings_host_forbidden", "设置操作只允许来自本机。")

    origin = request.headers.get("origin") or request.headers.get("referer")
    if not origin:
        if not bool(getattr(request.app.state, "settings_allow_missing_origin", False)):
            raise _SettingsHTTPException(403, "settings_origin_required", "设置操作缺少同源来源。")
    else:
        try:
            parsed = urllib.parse.urlsplit(origin)
            origin_host = _host_without_port(parsed.netloc)
            if (
                parsed.scheme.lower() not in {"http", "https"}
                or origin_host not in allowed_hosts
                or origin_host != host
            ):
                raise ValueError
        except (TypeError, ValueError):
            raise _SettingsHTTPException(403, "settings_origin_forbidden", "设置操作来源不是本机同源页面。") from None

    token = request.headers.get("x-settings-csrf") or request.headers.get("x-csrf-token")
    if not token or token != _SETTINGS_CSRF_TOKEN:
        raise _SettingsHTTPException(403, "settings_csrf_failed", "设置操作校验失败，请刷新页面后重试。")

    if require_json:
        media_type = (request.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
        if media_type != "application/json":
            raise _SettingsHTTPException(415, "settings_content_type", "设置请求必须使用 JSON。")
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            length = int(declared)
        except ValueError:
            raise _SettingsHTTPException(400, "settings_body_invalid", "请求大小无效。") from None
        if length < 0 or length > max_body_bytes:
            raise _SettingsHTTPException(413, "settings_body_too_large", "设置请求超过允许大小。")


async def _settings_json_body(request: Request, *, allow_empty: bool = False) -> dict[str, Any]:
    raw = await request.body()
    if len(raw) > _SETTINGS_MAX_BODY_BYTES:
        raise _SettingsHTTPException(413, "settings_body_too_large", "设置请求超过允许大小。")
    if allow_empty and not raw:
        return {}
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise _SettingsHTTPException(400, "settings_body_invalid", "设置请求不是有效 JSON。") from None
    if not isinstance(parsed, dict):
        raise _SettingsHTTPException(422, "settings_body_invalid", "设置请求必须是 JSON 对象。")
    return parsed


def _settings_service_error(error: Exception) -> _SettingsHTTPException:
    if isinstance(error, SettingsRevisionConflict):
        return _SettingsHTTPException(409, "settings_revision_conflict", "设置已被其他实例更新，请刷新后重试。")
    if isinstance(error, SettingsEnvironmentManagedError):
        return _SettingsHTTPException(409, "settings_environment_managed", "该设置由进程环境管理，不能在此覆盖。")
    if isinstance(error, SettingsReadOnlyError):
        return _SettingsHTTPException(409, "settings_read_only", "该设置为只读投影。")
    if isinstance(error, SettingsValidationError):
        return _SettingsHTTPException(422, getattr(error, "code", "settings_invalid_value"), "设置值无效。")
    if isinstance(error, SecureStoreUnavailable) or isinstance(error, SecretStoreError):
        return _SettingsHTTPException(503, "secure_store_unavailable", "安全凭据存储不可用，未保存密钥。")
    return _SettingsHTTPException(503, "settings_unavailable", "设置暂时不可用，请稍后重试。")


@app.get("/api/settings")
def get_settings() -> JSONResponse:
    service = _settings_service()
    payload = _settings_projection(service)
    payload["ok"] = True
    # The token is process-local and is never persisted or logged.  A browser
    # must obtain it from this same-origin response before mutations.
    payload["csrf_token"] = _SETTINGS_CSRF_TOKEN
    return _settings_json_response(payload)


@app.patch("/api/settings")
async def patch_settings(request: Request) -> JSONResponse:
    _settings_security_guard(request)
    body = await _settings_json_body(request, allow_empty=True)
    expected = body.get("expected_revision")
    changes = body.get("changes")
    if isinstance(expected, bool) or not isinstance(expected, int) or not isinstance(changes, dict):
        raise _SettingsHTTPException(422, "settings_invalid_value", "设置 patch 格式无效。")
    service = _settings_service()
    current = service.resolve()
    if "privacy.mode" in changes:
        try:
            old_mode = normalize_mode(str(_setting_value(current, "privacy.mode", "local_strict")))
            new_mode = normalize_mode(str(changes["privacy.mode"]))
        except Exception:
            old_mode = "local_strict"
            new_mode = "local_strict"
        relaxes = (
            (old_mode == "local_strict" and new_mode != "local_strict")
            or (old_mode == "mixed_sanitized" and new_mode == "approved_remote")
        )
        if relaxes and body.get("privacy_acknowledgement") is not True:
            raise _SettingsHTTPException(
                422,
                "settings_privacy_acknowledgement_required",
                "放宽隐私模式前必须明确确认数据出境影响。",
            )
    active = _latest_active_request(_IDEA)
    if active and _SETTINGS_UNSAFE_WHILE_ACTIVE.intersection(changes):
        raise _SettingsHTTPException(409, "settings_busy", "当前有运行中的请求，暂不能修改该资源设置。")
    try:
        result = service.apply_patch(SettingsPatch(expected_revision=expected, changes=changes))
    except Exception as error:  # noqa: BLE001 - stable safe mapping
        raise _settings_service_error(error) from error
    payload = result.as_dict()
    payload["ok"] = True
    return _settings_json_response(payload)


def _validate_secret_provider(provider: str) -> str:
    normalized = str(provider or "").strip().lower()
    if normalized not in _SETTINGS_ALLOWED_PROVIDERS:
        raise _SettingsHTTPException(404, "settings_invalid_value", "不支持的 provider。")
    return normalized


@app.put("/api/settings/secrets/{provider}")
async def put_settings_secret(provider: str, request: Request) -> JSONResponse:
    _settings_security_guard(request)
    normalized = _validate_secret_provider(provider)
    body = await _settings_json_body(request)
    value = body.get("value")
    if set(body) != {"value"} or not isinstance(value, str) or not value.strip():
        raise _SettingsHTTPException(422, "settings_invalid_value", "凭据值无效。")
    service = _settings_service()
    try:
        status = service.secret_status(normalized)
        if status.source == "environment":
            raise SettingsEnvironmentManagedError()
        result = service.replace_secret(normalized, value) if status.configured else service.set_secret(normalized, value)
    except Exception as error:  # noqa: BLE001 - safe mapping never includes value
        raise _settings_service_error(error) from error
    return _settings_json_response({"ok": True, "secret": result.as_dict()})


@app.delete("/api/settings/secrets/{provider}")
async def delete_settings_secret(provider: str, request: Request) -> JSONResponse:
    _settings_security_guard(request)
    normalized = _validate_secret_provider(provider)
    body = await _settings_json_body(request, allow_empty=True)
    if body:
        raise _SettingsHTTPException(422, "settings_body_invalid", "删除凭据不接受请求体。")
    service = _settings_service()
    try:
        status = service.secret_status(normalized)
        if status.source == "environment":
            raise SettingsEnvironmentManagedError()
        result = service.delete_secret(normalized)
    except Exception as error:  # noqa: BLE001
        raise _settings_service_error(error) from error
    return _settings_json_response({"ok": True, "secret": result.as_dict()})


def _safe_health(status: str, code: str, *, component: str, message: str = "", metrics: Mapping[str, Any] | None = None) -> dict[str, Any]:
    allowed_status = status if status in {"ready", "passed", "error", "unavailable", "skipped"} else "error"
    return {
        "component": component,
        "status": allowed_status,
        "code": str(code)[:80],
        "message": str(message)[:240],
        "checked_at": int(time.time() * 1000),
        "metrics": dict(metrics or {}),
    }


def _run_provider_health_check(settings: EffectiveSettings) -> dict[str, Any]:
    from .privacy.modes import provider_is_remote

    mode = _privacy_mode(settings)
    if mode == "local_strict" or not bool(_setting_value(settings, "provider.live_enabled", False)):
        return _safe_health("skipped", "provider_disabled", component="provider", message="远端 provider 未启用。")
    started = time.monotonic()
    provider = None
    try:
        provider = _provider(settings=settings)
        if not provider_is_remote(provider):
            return _safe_health("skipped", "provider_disabled", component="provider", message="当前 provider 不是远端。")
        response = provider.chat([
            {"role": "system", "content": "Return exactly OK."},
            {"role": "user", "content": "STATA_AGENT_PROVIDER_CANARY_V1"},
        ])
        if not isinstance(response, dict):
            return _safe_health("error", "invalid_response", component="provider")
        return _safe_health(
            "passed",
            "ok",
            component="provider",
            metrics={"provider": _provider_name(provider), "latency_ms": min(60000, int((time.monotonic() - started) * 1000))},
        )
    except Exception as error:  # noqa: BLE001 - never expose provider response/detail
        del error
        return _safe_health("error", "provider_unavailable", component="provider")
    finally:
        close = getattr(provider, "close", None)
        if callable(close):
            with suppress(Exception):
                close()


def _run_stata_health_check() -> dict[str, Any]:
    try:
        from .stata_doctor import run_acceptance

        report = run_acceptance(iterations=1)
        raw = report.as_dict()
        checks = []
        for item in raw.get("checks", []) if isinstance(raw, dict) else []:
            if not isinstance(item, Mapping):
                continue
            checks.append({
                "name": str(item.get("name") or "check")[:80],
                "status": str(item.get("status") or "error")[:40],
                "code": str(item.get("code") or "")[:80],
                "metrics": dict(item.get("metrics") or {}) if isinstance(item.get("metrics"), Mapping) else {},
            })
        return _safe_health("passed" if bool(raw.get("ok")) else "error", "ok" if raw.get("ok") else "stata_unavailable", component="stata", metrics={"checks": checks})
    except Exception as error:  # noqa: BLE001
        del error
        return _safe_health("error", "stata_unavailable", component="stata")


def _run_library_health_check(settings: EffectiveSettings) -> dict[str, Any]:
    root_value = _setting_value(settings, "library.root", None)
    if not root_value:
        return _safe_health("skipped", "library_unconfigured", component="library")
    try:
        root = Path(str(root_value)).expanduser()
        if root.is_symlink() or not root.is_dir() or not os.access(root, os.R_OK | os.X_OK):
            return _safe_health("error", "library_unavailable", component="library")
        count = 0
        total = 0
        for item in root.iterdir():
            if item.is_file() and not item.is_symlink():
                count += 1
                if count > 10000:
                    break
                with suppress(OSError):
                    total += item.stat().st_size
        return _safe_health("passed", "ok", component="library", metrics={"file_count_bucket": min(10000, count), "bytes_bucket": min(1_000_000_000, total)})
    except (OSError, ValueError):
        return _safe_health("error", "library_unavailable", component="library")


@app.post("/api/settings/check/provider")
def check_settings_provider(request: Request) -> JSONResponse:
    # Health checks are non-mutating but still protected against cross-origin
    # use because they can trigger a fixed external canary.
    _settings_security_guard(request, require_json=False)
    return _settings_json_response({"ok": True, "health": _run_provider_health_check(_effective_settings())})


@app.post("/api/settings/check/stata")
def check_settings_stata(request: Request) -> JSONResponse:
    _settings_security_guard(request, require_json=False)
    return _settings_json_response({"ok": True, "health": _run_stata_health_check()})


@app.post("/api/settings/check/library")
def check_settings_library(request: Request) -> JSONResponse:
    _settings_security_guard(request, require_json=False)
    return _settings_json_response({"ok": True, "health": _run_library_health_check(_effective_settings())})


@app.post("/api/settings/backup")
def settings_backup(request: Request) -> Response:
    # This endpoint does not accept a browser-controlled destination.  The
    # bundle is created in a bounded temporary file and streamed immediately.
    _settings_security_guard(request, require_json=False)
    from tempfile import NamedTemporaryFile

    temporary = NamedTemporaryFile(prefix="stata-agent-settings-", suffix=".zip", delete=False)
    temporary_path = Path(temporary.name)
    temporary.close()
    try:
        from .application.release_ops import ReleaseOperations

        attachment_root = _attachment_root()
        asset_root = attachment_root if attachment_root.is_dir() else None
        ReleaseOperations(max_bytes=_SETTINGS_MAX_BUNDLE_BYTES).create_backup(
            DEFAULT_DB,
            temporary_path,
            asset_root=asset_root,
        )
        payload = temporary_path.read_bytes()
    except Exception as error:  # noqa: BLE001
        temporary_path.unlink(missing_ok=True)
        del error
        raise _SettingsHTTPException(503, "backup_unavailable", "备份未完成。") from None
    temporary_path.unlink(missing_ok=True)
    return Response(
        content=payload,
        media_type="application/zip",
        headers={"Cache-Control": "no-store", "Content-Disposition": "attachment; filename=stata-agent-backup.zip"},
    )


@app.post("/api/settings/backup/verify")
async def settings_backup_verify(request: Request, bundle: UploadFile = File(...)) -> JSONResponse:
    _settings_security_guard(request, require_json=False, max_body_bytes=_SETTINGS_MAX_BUNDLE_BYTES)
    from tempfile import NamedTemporaryFile

    temporary = NamedTemporaryFile(prefix="stata-agent-verify-", suffix=".zip", delete=False)
    temporary_path = Path(temporary.name)
    total = 0
    try:
        while True:
            block = await bundle.read(64 * 1024)
            if not block:
                break
            total += len(block)
            if total > _SETTINGS_MAX_BUNDLE_BYTES:
                raise _SettingsHTTPException(413, "settings_body_too_large", "备份超过允许大小。")
            temporary.write(block)
        temporary.flush()
        os.fsync(temporary.fileno())
        temporary.close()
        from .application.release_ops import ReleaseOperations

        manifest = ReleaseOperations().verify_backup(temporary_path)
        safe_manifest = {"schema": manifest.get("schema"), "database_schema_version": manifest.get("database_schema_version"), "file_count": len(manifest.get("files") or [])}
        return _settings_json_response({"ok": True, "verified": True, "manifest": safe_manifest})
    except _SettingsHTTPException:
        raise
    except Exception as error:  # noqa: BLE001
        del error
        raise _SettingsHTTPException(422, "backup_invalid", "备份校验失败。") from None
    finally:
        with suppress(Exception):
            temporary.close()
        temporary_path.unlink(missing_ok=True)


@app.get("/api/events")
def events(limit: int = 300, before_seq: int | None = None, ws: str = _IDEA):
    """Return compact trace rows, with optional backward cursor pagination.

    The original v0 response is a list. Supplying ``before_seq`` opts into
    the richer ``{items, next_before_seq}`` shape so old clients remain valid.
    """
    idea = _resolve_workspace(ws)
    s = _store()
    try:
        rows = [_public_event(event) for event in _event_list(s, idea)]
        limit = max(1, min(int(limit), 500))
        if before_seq is None:
            return rows[-limit:]
        older = [row for row in rows if row.get("seq") is not None and int(row["seq"]) < before_seq]
        page = older[-limit:]
        next_before = page[0]["seq"] if len(page) == limit else None
        return {"items": page, "next_before_seq": next_before}
    finally:
        s.close()


@app.get("/api/trace")
def trace(
    limit: int = 50,
    before_seq: int | None = None,
    search: str | None = None,
    type: str | None = None,
    event_type: str | None = None,
    actor: str | None = None,
    phase: str | None = None,
    ws: str = _IDEA,
):
    """Return a single-page trace projection with search/filter/cursor support."""

    idea = _resolve_workspace(ws)
    s = _store()
    try:
        rows = [_public_event(event) for event in _event_list(s, idea)]
        requested_type = (event_type or type or "").strip().lower()
        requested_actor = (actor or "").strip().lower()
        requested_phase = (phase or "").strip().lower()
        requested_search = (search or "").strip().lower()

        def matches(row: dict[str, Any]) -> bool:
            if requested_type and str(row.get("type") or "").lower() != requested_type:
                return False
            if requested_actor and str(row.get("actor") or "").lower() != requested_actor:
                return False
            if requested_phase and str(row.get("phase") or "").lower() != requested_phase:
                return False
            if requested_search:
                haystack = " ".join(
                    str(row.get(key) or "")
                    for key in ("seq", "type", "actor", "phase", "object", "summary", "op", "payload_preview")
                ).lower()
                if requested_search not in haystack:
                    return False
            return True

        filtered = [row for row in rows if matches(row)]
        ordered = list(reversed(filtered))
        if before_seq is not None:
            ordered = [
                row for row in ordered if row.get("seq") is not None and int(row["seq"]) < int(before_seq)
            ]
        safe_limit = max(1, min(int(limit), 500))
        page = ordered[:safe_limit]
        next_before = page[-1]["seq"] if len(page) == safe_limit and page else None
        return JSONResponse(
            content={
                "items": page,
                "next_before_seq": next_before,
                "total": len(filtered),
                "workspace": idea,
            },
            headers={"Cache-Control": "no-store"},
        )
    finally:
        s.close()


@app.get("/api/trace/activity")
def trace_activity(
    limit: str = "20",
    before_seq: str | None = None,
    category: str = "all",
    status: str | None = None,
    search: str | None = None,
    ws: str = _IDEA,
):
    """Return bounded request-level Activity Timeline groups.

    The raw ``/api/trace`` route above remains the compatibility/technical
    audit surface.  This route delegates grouping and filtering to the
    application projector so totals and cursors operate on request groups,
    not on whichever raw rows happened to be loaded in the browser.
    """

    idea = _resolve_workspace(ws)
    try:
        try:
            parsed_limit = int(limit)
            parsed_before = int(before_seq) if before_seq is not None and before_seq != "" else None
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError("limit/before_seq must be integers") from error
        query = TraceActivityQuery(
            limit=parsed_limit,
            before_seq=parsed_before,
            category=(category or "all").strip().lower(),
            status=(status.strip().lower() if isinstance(status, str) and status.strip() else None),
            search=(search.strip() if isinstance(search, str) and search.strip() else None),
        )
    except (TypeError, ValueError) as error:
        raise _SettingsHTTPException(422, "trace_query_invalid", "Trace 查询参数无效。") from error
    s = None
    try:
        s = _store()
        # Activity is a read model, but it must not materialize an unbounded
        # ledger on every refresh.  Keep the storage window bounded and use
        # the same cursor to fetch the next chronological window.
        events, storage_truncated = s.scan_recent_events(
            idea,
            limit=500,
            before_seq=parsed_before,
        )
        body = _TRACE_PROJECTION.project(events, workspace=idea, query=query).as_dict()
        # A SQL aggregate supplies the complete group total for filters that
        # are expressible by event vocabulary.  Status/search remain owned by
        # the bounded semantic projector and therefore use its exact window
        # total when those filters are active.
        if query.status is None and query.search is None:
            category_event_types = {
                "model": {EVENT_PROVIDER_TURN_STARTED, EVENT_PROVIDER_TURN_COMPLETED, EVENT_PROVIDER_TURN_FAILED},
                "tool": {EVENT_TOOL_INVOKED, EVENT_TOOL_DONE},
                "run": {EVENT_RUN_REQ, EVENT_TOOL_CALL, EVENT_TOOL_RESULT, EVENT_RUN_SUCCEEDED, EVENT_RUN_FAILED, EVENT_RUN_UNCERTAIN},
                "evidence": {EVENT_CARD_SIGNED, EVENT_CLAIM_SIGNED},
                "approval": {EVENT_APPROVAL_REQ, EVENT_APPROVAL_GRANT, EVENT_APPROVAL_REJECT},
            }
            if query.category == "all" or query.category in category_event_types:
                total_hint = s.count_event_correlations(
                    idea,
                    event_types=category_event_types.get(query.category),
                )
                body["total_groups"] = total_hint
                body["total"] = total_hint
        body["truncated"] = bool(body.get("truncated") or storage_truncated)
        if storage_truncated and body.get("next_before_seq") is None and events:
            oldest = min((event.seq for event in events if isinstance(event.seq, int)), default=None)
            if oldest is not None:
                body["next_before_seq"] = oldest
        return JSONResponse(content=body, headers={"Cache-Control": "no-store"})
    except StoreError:
        raise
    except sqlite3.DatabaseError as error:
        # Keep SQLite busy/corrupt details behind the existing ledger error
        # envelope; the activity endpoint must never echo driver text.
        raise StoreError("trace read unavailable") from error
    except Exception as error:  # noqa: BLE001 - projection must not leak payload details
        raise _SettingsHTTPException(503, "trace_projection_unavailable", "Trace 暂时不可用，请稍后重试。") from error
    finally:
        if s is not None:
            s.close()


def _attachment_http_error(error: Exception) -> None:
    if isinstance(error, AttachmentLimitError):
        raise HTTPException(status_code=413, detail="附件超过文件或工作区限额。") from error
    if isinstance(error, AttachmentUnsupportedError):
        raise HTTPException(status_code=415, detail="仅支持未加密、含文本的 PDF。") from error
    if isinstance(error, AttachmentValidationError):
        raise HTTPException(status_code=422, detail="附件名称、类型或请求字段无效。") from error
    if isinstance(error, AttachmentConflictError):
        raise HTTPException(status_code=409, detail="附件状态或幂等键发生冲突。") from error
    raise HTTPException(status_code=500, detail="附件处理未完成，请稍后重试。") from error


def _decoded_attachment_filename(value: str | None) -> str:
    if not value:
        raise AttachmentValidationError("filename is required")
    try:
        return urllib.parse.unquote_to_bytes(value).decode("utf-8", errors="strict")
    except (UnicodeDecodeError, ValueError) as error:
        raise AttachmentValidationError("filename encoding is invalid") from error


async def _attachment_intake_from_request(
    request: Request,
    *,
    workspace_id: str,
    idea_id: str,
    filename: str,
    media_type: str | None,
    source_role: str,
    declared_size: int | None,
    idempotency_key: str | None,
) -> tuple[AttachmentRecord, bool]:
    """Stage bounded ASGI bytes, then parse/store off the event-loop thread."""

    paths = AttachmentPaths(_attachment_root(), workspace_id)
    paths.prepare()
    transport_path = paths.staging_root / f"http-{uuid.uuid4().hex}.part"
    total = 0
    try:
        with transport_path.open("xb") as output:
            async for received in request.stream():
                chunk = bytes(received)
                for offset in range(0, len(chunk), 64 * 1024):
                    block = chunk[offset : offset + 64 * 1024]
                    total += len(block)
                    if total > 25 * 1024 * 1024:
                        raise AttachmentLimitError(ERROR_FILE_TOO_LARGE)
                    output.write(block)
            output.flush()
            os.fsync(output.fileno())

        def intake() -> tuple[AttachmentRecord, bool]:
            with _open_attachment_service() as service:
                existing = (
                    service.store.get_attachment_by_idempotency(idempotency_key, workspace_id=workspace_id)
                    if idempotency_key
                    else None
                )
                if existing is not None:
                    return existing, True
                with transport_path.open("rb") as source:
                    record = service.intake(
                        workspace_id,
                        filename,
                        source,
                        idea_id=idea_id,
                        declared_media_type=media_type,
                        source_role=source_role,
                        declared_size=declared_size,
                        idempotency_key=idempotency_key,
                    )
                return record, False

        return await asyncio.to_thread(intake)
    finally:
        transport_path.unlink(missing_ok=True)


@app.get("/api/attachments")
def list_attachments(ws: str = _IDEA, limit: int = 100):
    idea = _resolve_workspace(ws)
    if isinstance(limit, bool) or not 1 <= int(limit) <= 100:
        raise HTTPException(status_code=422, detail="limit 必须在 1..100。")
    try:
        with _open_attachment_service() as service:
            rows = service.list(_workspace_id(idea), limit=int(limit))
    except Exception as error:  # noqa: BLE001
        _attachment_http_error(error)
    return {"workspace": idea, "items": [_safe_attachment(row) for row in rows]}


@app.post("/api/attachments")
async def upload_attachment(request: Request, ws: str = _IDEA):
    idea = _resolve_workspace(ws)
    workspace_id = _workspace_id(idea)
    try:
        filename = _decoded_attachment_filename(request.headers.get("x-attachment-filename"))
        source_role = request.headers.get("x-attachment-role", "style_only").strip()
        idempotency_key = request.headers.get("x-idempotency-key")
        raw_length = request.headers.get("content-length")
        if raw_length is None:
            declared_size = None
        elif not raw_length.isdecimal():
            raise AttachmentValidationError("content length is invalid")
        else:
            declared_size = int(raw_length)
        record, duplicate = await _attachment_intake_from_request(
            request,
            workspace_id=workspace_id,
            idea_id=idea,
            filename=filename,
            media_type=request.headers.get("content-type"),
            source_role=source_role,
            declared_size=declared_size,
            idempotency_key=idempotency_key,
        )
    except Exception as error:  # noqa: BLE001
        _attachment_http_error(error)
    if record.status == ATTACHMENT_READY:
        status_code = 200 if duplicate else 201
    elif record.status == ATTACHMENT_QUARANTINED:
        status_code = 202
    elif record.error_code in {ERROR_FILE_TOO_LARGE, ERROR_WORKSPACE_QUOTA}:
        status_code = 413
    elif record.error_code in {"extension_mismatch", "mime_not_allowed", "magic_mismatch"}:
        status_code = 415
    else:
        status_code = 422 if record.status == "rejected" else 500
    return JSONResponse(
        status_code=status_code, content={"attachment": _safe_attachment(record), "duplicate": duplicate}
    )


class ChatIn(BaseModel):
    text: str
    attachment_ids: list[StrictStr] = Field(default_factory=list, max_length=8)
    mode: str = "interactive"  # interactive=提问即停；goal=自动跑到需你决定/硬停才停


class ApproveIn(BaseModel):
    request_id: str
    decision: str  # approve | reject
    note: str = ""


class ApprovalDecisionIn(BaseModel):
    decision: str
    note: str = ""


class MemoryCandidateDecisionIn(BaseModel):
    note: str = ""


class StopIn(BaseModel):
    """Optional request id for the idempotent in-flight cancellation endpoint."""

    request_id: str | None = None


def _decision(
    store: SQLiteStore,
    request_id: str,
    decision: str,
    note: str,
    idea: str = _IDEA,
) -> dict[str, Any]:
    with _APPROVAL_DECISION_LOCK:
        return _decision_unlocked(store, request_id, decision, note, idea)


def _decision_unlocked(
    store: SQLiteStore,
    request_id: str,
    decision: str,
    note: str,
    idea: str = _IDEA,
) -> dict[str, Any]:
    records = {item["request_id"]: item for item in _approval_records(_event_list(store, idea))}
    record = records.get(request_id)
    if record is None:
        raise HTTPException(status_code=404, detail="审批请求不存在或已被清理。")
    if record["status"] != "pending":
        raise HTTPException(status_code=409, detail="该审批请求已经有决定，不能重复提交。")
    decision = str(decision or "").strip().lower()
    note = str(note or "").strip()
    if len(note) > 4000:
        raise HTTPException(status_code=413, detail="审批说明不能超过 4000 个字符。")
    if decision == "modify":
        if not note:
            raise HTTPException(status_code=422, detail="提出修改时必须填写具体修改要求。")
        # A revision is a first-class user instruction and an approval audit
        # entry.  The grant event carries the explicit ``decision=modify``
        # marker so the reducer can close the old pending gate without
        # pretending that the user approved the original proposal.
        event_seq = store.append_many(
            [
                Event(
                    idea_id=idea,
                    event_type=EVENT_USER,
                    actor=ACTOR_USER,
                    source=ACTOR_USER,
                    payload={
                        "text": note,
                        "request_id": request_id,
                        "approval_request_id": request_id,
                        "kind": "approval_revision",
                        "workspace_id": _workspace_id(idea),
                    },
                ),
                Event(
                    idea_id=idea,
                    event_type=EVENT_STEERING,
                    actor=ACTOR_USER,
                    source=ACTOR_USER,
                    payload={
                        "request_id": request_id,
                        "decision": "modify",
                        "note": note,
                        "kind": "approval_revision",
                        "workspace_id": _workspace_id(idea),
                    },
                ),
                Event(
                    idea_id=idea,
                    event_type=EVENT_APPROVAL_GRANT,
                    actor=ACTOR_USER,
                    source=ACTOR_USER,
                    payload={
                        "request_id": request_id,
                        "decision": "modify",
                        "note": note,
                        "kind": "approval_revision",
                        "workspace_id": _workspace_id(idea),
                    },
                ),
            ]
        )
        return {
            "decision": "modified",
            "event": EVENT_APPROVAL_GRANT,
            "event_seq": event_seq,
            "state": _summary(store, idea),
        }
    if decision not in {"approve", "reject"}:
        raise HTTPException(status_code=422, detail="decision 必须是 approve|reject|modify。")
    if decision == "approve" and note:
        # SQLite owns concurrency and transaction boundaries for the optional
        # decision memory; the ledger remains the approval audit source.
        memory = _memory()
        try:
            event_kind = runner_approve(
                store,
                request_id,
                decision=decision,
                note=note,
                idea=idea,
                memory=memory,
                workspace_id=_workspace_id(idea),
            )
        finally:
            _close_memory(memory)
    else:
        event_kind = runner_approve(
            store,
            request_id,
            decision=decision,
            note=note,
            idea=idea,
            memory=None,
            workspace_id=_workspace_id(idea),
        )
    return {
        "decision": "approved" if decision == "approve" else "rejected",
        "event": event_kind,
        "event_seq": _last_seq(store, idea),
        "state": _summary(store, idea),
    }


@app.get("/api/approvals")
def approvals(status: str = "pending", ws: str = _IDEA):
    """List durable approval requests; ``status=all`` restores the audit trail."""

    idea = _resolve_workspace(ws)
    s = _store()
    try:
        records = _approval_records(_event_list(s, idea))
        if status != "all":
            records = [item for item in records if item["status"] == "pending"]
        return (
            {"pending": records}
            if status != "all"
            else {"items": records, "pending": [i for i in records if i["status"] == "pending"]}
        )
    finally:
        s.close()


def _memory_review(
    candidate_id: str,
    *,
    idea: str,
    decision: str,
    note: str = "",
) -> dict[str, Any]:
    """Review one candidate through a durable two-phase audit protocol.

    The requested event is the recovery boundary.  Memory is mutated only
    after it is present; a completed event is emitted only after the mutation
    succeeds.  Any later ledger failure is surfaced as an error and, when
    possible, represented by a failed event instead of returning a false
    success.
    """

    from .memory.memstore import MemoryStore
    from .storage.store import DuplicateFingerprint

    normalized_note = str(note or "").strip()
    if len(normalized_note) > 4000:
        raise HTTPException(status_code=413, detail="记忆审核说明不能超过 4000 个字符。")
    decision = str(decision or "").strip().lower()
    if decision not in {"accept", "reject"}:
        raise HTTPException(status_code=422, detail="记忆审核决定必须是 accept|reject。")

    workspace_id = _workspace_id(idea)

    def review_fingerprint(candidate: dict[str, Any]) -> str:
        material = {
            "workspace_id": workspace_id,
            "candidate_id": str(candidate_id),
            "candidate_fingerprint": str(candidate.get("fingerprint") or ""),
            "decision": decision,
        }
        encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def matching_event(events: list[Event], event_type: str, fingerprint: str) -> Event | None:
        for event in reversed(events):
            event_fp = str(event.fingerprint or (event.payload or {}).get("fingerprint") or "")
            if event.event_type == event_type and event_fp == fingerprint:
                return event
        return None

    def append_failed(
        store: Any,
        *,
        fingerprint: str,
        source_ids: list[str],
        error_code: str,
        audit_note: str,
        mutation_applied: bool = False,
    ) -> int | None:
        try:
            return store.append(
                Event(
                    idea_id=idea,
                    event_type=EVENT_MEMORY_REVIEW_FAILED,
                    actor=ACTOR_USER,
                    source=ACTOR_USER,
                    payload={
                        "status": "failed",
                        "candidate_id": candidate_id,
                        "workspace_id": workspace_id,
                        "decision": decision,
                        "source_ids": source_ids,
                        "note": audit_note,
                        "error_code": error_code,
                        "mutation_applied": mutation_applied,
                        "fingerprint": fingerprint,
                    },
                    fingerprint=fingerprint,
                )
            )
        except DuplicateFingerprint:
            # A previous recovery attempt already accounted for the failure.
            return None
        except Exception:
            return None

    s = _store()
    memory = None
    try:
        # SQLite transactions serialize memory mutations.  The compatibility
        # scope avoids perturbing the review recovery protocol's structure.
        with _memory_transaction():
            memory = _memory()
            if not isinstance(memory, MemoryStore):
                raise HTTPException(status_code=503, detail="项目记忆当前不可用。")

            events = list(_event_list(s, idea))
            candidate = memory.candidate(candidate_id, workspace_id=workspace_id)
            if candidate is None:
                # A completed event makes a retried HTTP request idempotently
                # successful.  A pending request without a candidate means a
                # prior mutation/ledger write needs recovery; never claim it
                # completed.
                candidate_events = [
                    event
                    for event in events
                    if event.event_type
                    in {
                        EVENT_MEMORY_REVIEW_REQUESTED,
                        EVENT_MEMORY_REVIEW_COMPLETED,
                        EVENT_MEMORY_REVIEW_FAILED,
                    }
                    and str((event.payload or {}).get("candidate_id") or "") == str(candidate_id)
                    and str((event.payload or {}).get("workspace_id") or "") == workspace_id
                ]
                completed = next(
                    (
                        event
                        for event in reversed(candidate_events)
                        if event.event_type == EVENT_MEMORY_REVIEW_COMPLETED
                    ),
                    None,
                )
                if completed is not None:
                    payload = completed.payload or {}
                    return {
                        "ok": True,
                        "candidate_id": candidate_id,
                        "status": str(payload.get("status") or "accepted"),
                        "workspace": idea,
                        "workspace_id": workspace_id,
                        "event": EVENT_MEMORY_REVIEW_COMPLETED,
                        "event_seq": completed.seq,
                        "memory": ({"id": str(payload["memory_id"])} if payload.get("memory_id") else None),
                    }
                requested = next(
                    (
                        event
                        for event in reversed(candidate_events)
                        if event.event_type == EVENT_MEMORY_REVIEW_REQUESTED
                    ),
                    None,
                )
                if requested is not None:
                    payload = requested.payload or {}
                    fingerprint = str(requested.fingerprint or payload.get("fingerprint") or "")
                    append_failed(
                        s,
                        fingerprint=fingerprint,
                        source_ids=[str(item) for item in payload.get("source_ids") or []],
                        error_code="candidate_unavailable",
                        audit_note=str(payload.get("note") or ""),
                    )
                    raise HTTPException(status_code=503, detail="记忆审核待恢复，请稍后重试。")
                # Do not reveal whether the id belongs to another workspace.
                raise HTTPException(status_code=404, detail="记忆候选不存在或不属于当前工作区。")

            source_ids = [str(item) for item in candidate.get("source_ids") or []]
            fingerprint = review_fingerprint(candidate)
            requested = matching_event(events, EVENT_MEMORY_REVIEW_REQUESTED, fingerprint)
            audit_note = normalized_note
            if requested is None:
                try:
                    s.append(
                        Event(
                            idea_id=idea,
                            event_type=EVENT_MEMORY_REVIEW_REQUESTED,
                            actor=ACTOR_USER,
                            source=ACTOR_USER,
                            payload={
                                "status": "requested",
                                "candidate_id": candidate_id,
                                "workspace_id": workspace_id,
                                "decision": decision,
                                "source_ids": source_ids,
                                "note": normalized_note,
                                "fingerprint": fingerprint,
                            },
                            fingerprint=fingerprint,
                        )
                    )
                except DuplicateFingerprint:
                    # Re-read the immutable row to preserve the original note
                    # and make concurrent/retried calls deterministic.
                    requested = matching_event(
                        list(_event_list(s, idea)),
                        EVENT_MEMORY_REVIEW_REQUESTED,
                        fingerprint,
                    )
                except Exception as error:
                    raise HTTPException(
                        status_code=503, detail="记忆审核记录暂不可写，请稍后重试。"
                    ) from error
            if requested is not None:
                audit_note = str((requested.payload or {}).get("note") or normalized_note)

            status: str
            result: dict[str, Any] | None
            try:
                if decision == "accept":
                    result = memory.accept_candidate(
                        candidate_id,
                        workspace_id=workspace_id,
                        source_ids=source_ids,
                    )
                    if result is None:
                        raise LookupError("candidate_unavailable")
                    status = "accepted"
                else:
                    if not memory.reject_candidate(candidate_id, workspace_id=workspace_id):
                        raise LookupError("candidate_unavailable")
                    result = None
                    status = "rejected"
            except Exception as error:  # noqa: BLE001 - stable audit code only
                append_failed(
                    s,
                    fingerprint=fingerprint,
                    source_ids=source_ids,
                    error_code="memory_write_error",
                    audit_note=audit_note,
                )
                raise HTTPException(status_code=503, detail="记忆审核未完成，请稍后重试。") from error

            payload = {
                "status": status,
                "candidate_id": candidate_id,
                "workspace_id": workspace_id,
                "decision": decision,
                "source_ids": source_ids,
                "note": audit_note,
                "fingerprint": fingerprint,
            }
            if result is not None and result.get("id"):
                payload["memory_id"] = str(result["id"])
            try:
                event_seq = s.append(
                    Event(
                        idea_id=idea,
                        event_type=EVENT_MEMORY_REVIEW_COMPLETED,
                        actor=ACTOR_USER,
                        source=ACTOR_USER,
                        payload=payload,
                        fingerprint=fingerprint,
                    )
                )
            except DuplicateFingerprint:
                completed = matching_event(
                    list(_event_list(s, idea)),
                    EVENT_MEMORY_REVIEW_COMPLETED,
                    fingerprint,
                )
                if completed is not None:
                    event_seq = completed.seq or 0
                else:
                    append_failed(
                        s,
                        fingerprint=fingerprint,
                        source_ids=source_ids,
                        error_code="ledger_error",
                        audit_note=audit_note,
                        mutation_applied=True,
                    )
                    raise HTTPException(status_code=503, detail="记忆审核记录暂不可写，请稍后重试。")
            except Exception as error:
                append_failed(
                    s,
                    fingerprint=fingerprint,
                    source_ids=source_ids,
                    error_code="ledger_error",
                    audit_note=audit_note,
                    mutation_applied=True,
                )
                raise HTTPException(status_code=503, detail="记忆审核记录暂不可写，请稍后重试。") from error
    finally:
        _close_memory(memory)
        s.close()
    return {
        "ok": True,
        "candidate_id": candidate_id,
        "status": status,
        "workspace": idea,
        "workspace_id": workspace_id,
        "event": EVENT_MEMORY_REVIEW_COMPLETED,
        "event_seq": event_seq,
        "memory": result,
    }


@app.get("/api/memory/candidates")
def memory_candidates(ws: str = _IDEA):
    """List pending model-extracted candidates for exactly one workspace."""

    idea = _resolve_workspace(ws)
    s = _store()
    memory = None
    try:
        memory = _memory()
        workspace_id = _workspace_id(idea)
        items = memory.candidates(workspace_id=workspace_id) if memory is not None else []
        return {
            "items": items,
            "candidates": items,
            "workspace": idea,
            "workspace_id": workspace_id,
        }
    finally:
        _close_memory(memory)
        s.close()


@app.post("/api/memory/candidates/{candidate_id}/accept")
def accept_memory_candidate(
    candidate_id: str,
    body: MemoryCandidateDecisionIn | None = None,
    ws: str = _IDEA,
):
    idea = _resolve_workspace(ws)
    return _memory_review(
        candidate_id,
        idea=idea,
        decision="accept",
        note=body.note if body else "",
    )


@app.post("/api/memory/candidates/{candidate_id}/reject")
def reject_memory_candidate(
    candidate_id: str,
    body: MemoryCandidateDecisionIn | None = None,
    ws: str = _IDEA,
):
    idea = _resolve_workspace(ws)
    return _memory_review(
        candidate_id,
        idea=idea,
        decision="reject",
        note=body.note if body else "",
    )


@app.post("/api/approve")
def approve(body: ApproveIn, ws: str = _IDEA):
    idea = _resolve_workspace(ws)
    s = _store()
    try:
        result = _decision(s, body.request_id, body.decision, body.note, idea)
        # Keep the original small response fields while adding the richer state.
        return {
            "ok": True,
            "event": result["event"],
            "event_seq": result["event_seq"],
            "state": result["state"],
        }
    finally:
        s.close()


@app.post("/api/approvals/{request_id}/decision")
def approval_decision(request_id: str, body: ApprovalDecisionIn, ws: str = _IDEA):
    idea = _resolve_workspace(ws)
    s = _store()
    try:
        return _decision(s, request_id, body.decision, body.note, idea)
    finally:
        s.close()


def _resume_result(idea: str = _IDEA) -> dict[str, Any]:
    """Resume through the same agent-loop path used by ``/api/chat``.

    The legacy ``runner.run_until_gate`` path uses the old proposal provider
    contract and is intentionally not reachable from the UI resume endpoint.
    """

    try:
        reply, ask, snapshot = _run_chat_sync(
            idea,
            "请从当前停点继续；如果需要我决定或补充信息，请明确提问。",
            "goal",
        )
        return {
            "reply": reply,
            "ask": ask,
            "status": snapshot["run_status"],
            "state": snapshot,
        }
    except Exception as error:  # noqa: BLE001
        raise HTTPException(status_code=500, detail="续跑未完成：请查看 Trace 中的最近错误。") from error


@app.post("/api/resume")
def resume(ws: str = _IDEA):
    """Continue from the current durable projection without new input."""

    idea = _resolve_workspace(ws)
    resumed_memory_jobs = resume_memory_extractions(idea)
    result = _resume_result(idea)
    result["memory_extraction_jobs"] = resumed_memory_jobs
    return result


@app.post("/api/control/resume")
def control_resume(ws: str = _IDEA):
    idea = _resolve_workspace(ws)
    resumed_memory_jobs = resume_memory_extractions(idea)
    result = _resume_result(idea)
    result["memory_extraction_jobs"] = resumed_memory_jobs
    return result


@app.post("/api/control/stop")
def control_stop(body: StopIn | None = None, request_id: str | None = None, ws: str = _IDEA):
    """Request cancellation for exactly one in-flight stream.

    The endpoint is idempotent: once a request reaches a terminal state,
    repeating stop returns that same state and never mutates a later request.
    Omitting ``request_id`` is retained as a convenience for old clients and
    targets only the latest active request in the selected workspace.
    """

    idea = _resolve_workspace(ws)
    requested_id = body.request_id if body and body.request_id else request_id
    if not requested_id:
        control = _latest_active_request(idea)
        if control is None:
            return {
                "ok": True,
                "request_id": None,
                "workspace": idea,
                "status": "idle",
                "cancel_requested": False,
            }
        requested_id = str(control["request_id"])
    result = _request_cancel(str(requested_id), idea)
    return {"ok": True, **result}


@app.get("/api/draft")
def draft(ws: str = _IDEA):
    """Legacy download endpoint; retains the v0 empty-draft behavior."""

    idea = _resolve_workspace(ws)
    s = _store()
    try:
        return _draft_response(s, require_ready=False, idea=idea)
    finally:
        s.close()


@app.get("/api/draft.docx")
def draft_docx(ws: str = _IDEA):
    """Strict download endpoint used by the new UI."""

    idea = _resolve_workspace(ws)
    s = _store()
    try:
        return _draft_response(s, require_ready=True, idea=idea)
    finally:
        s.close()


@app.get("/api/claims")
def claims(status: str | None = None, ws: str = _IDEA):
    idea = _resolve_workspace(ws)
    s = _store()
    try:
        records = _summary(s, idea)["claim_records"]
        if status:
            records = [record for record in records if record.get("status") == status]
        return {"items": records}
    finally:
        s.close()


@app.get("/api/cards")
def cards(ws: str = _IDEA):
    idea = _resolve_workspace(ws)
    s = _store()
    try:
        return {"items": _summary(s, idea)["card_records"]}
    finally:
        s.close()


@app.get("/api/cards/{card_id}")
def card(card_id: str, ws: str = _IDEA):
    idea = _resolve_workspace(ws)
    s = _store()
    try:
        proj = s.project(idea)
        item = proj.cards.get(card_id)
        if item is None:
            raise HTTPException(status_code=404, detail="证据卡不存在。")
        record = item.model_dump()
        run_id = item.locator.get("run_id") if isinstance(item.locator, dict) else None
        run = proj.runs.get(run_id) if run_id else None
        provenance = (run.provenance if run else {}) or {}
        record.update(
            {
                "run_id": run_id,
                "run": run.model_dump() if run else None,
                "provenance": provenance,
                "traceability_complete": bool(run_id and run),
            }
        )
        return record
    finally:
        s.close()


@app.get("/api/runs")
def runs(ws: str = _IDEA):
    idea = _resolve_workspace(ws)
    s = _store()
    try:
        return {"items": _summary(s, idea)["run_records"]}
    finally:
        s.close()


@app.get("/api/runs/{run_id}")
def run(run_id: str, ws: str = _IDEA):
    idea = _resolve_workspace(ws)
    s = _store()
    try:
        record = s.project(idea).runs.get(run_id)
        if record is None:
            raise HTTPException(status_code=404, detail="运行记录不存在。")
        return {"run_id": run_id, **record.model_dump()}
    finally:
        s.close()


@app.get("/api/operations/memory-outbox/failed")
def failed_memory_outbox(limit: str = "100", ws: str = _IDEA):
    """List safe failed memory-extraction outbox items for one workspace."""

    idea = _resolve_workspace(ws)
    workspace_id = _workspace_id(idea)
    try:
        bounded_limit = _parse_memory_outbox_limit(limit)
        with _open_memory_outbox_recovery(idea, workspace_id) as recovery:
            items = recovery.list_failed(
                idea,
                workspace_id,
                limit=bounded_limit,
            )
    except Exception as error:  # noqa: BLE001 - map at the transport boundary
        _raise_memory_outbox_http_error(error)
    return {
        "items": [item.to_dict() for item in items],
        "workspace": idea,
        "workspace_id": workspace_id,
    }


@app.post("/api/operations/memory-outbox/recover")
def recover_memory_outbox(body: MemoryOutboxRecoveryIn, ws: str = _IDEA):
    """Reconcile or redrive one observed failed generation.

    A redrive only commits ``failed -> pending``.  The existing dispatcher is
    responsible for any later claim and provider interaction.
    """

    idea = _resolve_workspace(ws)
    workspace_id = _workspace_id(idea)
    try:
        request = OutboxRecoveryRequest(
            idea_id=idea,
            workspace_id=workspace_id,
            idempotency_key=body.idempotency_key,
            expected_state_version=body.expected_state_version,
            acknowledge_at_least_once=body.acknowledge_at_least_once,
        )
        with _open_memory_outbox_recovery(idea, workspace_id) as recovery:
            result = recovery.recover(request)
    except Exception as error:  # noqa: BLE001 - map at the transport boundary
        _raise_memory_outbox_http_error(error)
    return result.to_dict()


@app.get("/api/operations/memory-outbox/retention/preview")
def preview_memory_outbox_retention(
    retention_days: str = str(DEFAULT_RETENTION_DAYS),
    limit: str = "100",
    cursor: str | None = None,
    ws: str = _IDEA,
):
    """Preview old, terminally-proven completed memory outbox rows."""

    idea = _resolve_workspace(ws)
    workspace_id = _workspace_id(idea)
    try:
        request = OutboxRetentionPreviewRequest(
            idea_id=idea,
            workspace_id=workspace_id,
            retention_days=_parse_memory_retention_days(retention_days),
            limit=_parse_memory_outbox_limit(limit),
            cursor=cursor,
        )
        with _open_memory_outbox_retention(idea, workspace_id) as retention:
            result = retention.preview(request)
    except Exception as error:  # noqa: BLE001 - map at the transport boundary
        _raise_memory_outbox_retention_http_error(error)
    return {
        **result.to_dict(),
        "workspace": idea,
        "workspace_id": workspace_id,
    }


@app.post("/api/operations/memory-outbox/retention/prune")
def prune_memory_outbox_retention(body: MemoryOutboxRetentionPruneIn, ws: str = _IDEA):
    """Apply exactly one fresh, explicitly acknowledged retention preview."""

    idea = _resolve_workspace(ws)
    workspace_id = _workspace_id(idea)
    try:
        request = OutboxRetentionPruneRequest(
            idea_id=idea,
            workspace_id=workspace_id,
            cutoff=body.cutoff,
            limit=body.limit,
            cursor=body.cursor,
            selection_token=body.selection_token,
            acknowledge_irreversible_delete=body.acknowledge_irreversible_delete,
        )
        with _open_memory_outbox_retention(idea, workspace_id) as retention:
            result = retention.prune(request)
    except Exception as error:  # noqa: BLE001 - map at the transport boundary
        _raise_memory_outbox_retention_http_error(error)
    return {
        **result.to_dict(),
        "workspace": idea,
        "workspace_id": workspace_id,
    }


@app.get("/api/operations/diagnostics/bundle")
def diagnostic_bundle(
    event_limit: str = "200",
    outbox_limit: str = "100",
    request_id: str | None = None,
    ws: str = _IDEA,
):
    """Export one bounded, metadata-only support snapshot for a workspace."""

    idea = _resolve_workspace(ws)
    workspace_id = _workspace_id(idea)
    try:
        bounded_events = _parse_diagnostic_limit(event_limit, maximum=500, name="event_limit")
        bounded_outbox = _parse_diagnostic_limit(outbox_limit, maximum=200, name="outbox_limit")
        store = _store()
        try:
            privacy = _privacy_mode()
            health_snapshot = {
                "ok": not _SKILL_ERRORS,
                "local_strict": privacy == "local_strict",
                "privacy_mode": privacy,
                "network_available": privacy != "local_strict",
                "skill_error_count": len(_SKILL_ERRORS),
            }
            queue_snapshot = getattr(_MEMORY_EXTRACTION_SCHEDULER, "stats", None)
            bundle = DiagnosticBundleService(
                store,
                queue_snapshot=queue_snapshot,
                health_snapshot=health_snapshot,
            ).build(
                DiagnosticBundleRequest(
                    idea_id=idea,
                    workspace_id=workspace_id,
                    event_limit=bounded_events,
                    outbox_limit=bounded_outbox,
                    request_id=request_id,
                )
            )
        finally:
            store.close()
    except Exception as error:  # noqa: BLE001 - stable sanitized transport mapping
        _raise_diagnostic_http_error(error)
    return JSONResponse(
        content=bundle,
        headers={"Content-Disposition": "attachment; filename=diagnostic-bundle-v1.json"},
    )


@app.get("/api/health")
def health(ws: str = _IDEA):
    idea = _resolve_workspace(ws)
    s = _store()
    try:
        summary = _summary(s, idea)
        privacy = _privacy_mode()
        config = summary.get("config") or {}
        skill_errors = config.get("skill_errors") or []
        outbox_stats = s.outbox_stats()
        return {
            "ok": bool(summary["health"]["ok"]) and not skill_errors,
            "local_strict": privacy == "local_strict",
            "privacy_mode": privacy,
            "network_available": privacy != "local_strict",
            "executor": config.get("executor", "未配置"),
            "skill_errors": skill_errors,
            "status": summary["run_status"],
            "memory_outbox": outbox_stats,
            "detail": skill_errors[0] if skill_errors else summary["status_detail"],
        }
    finally:
        s.close()


def _privacy_audit_before_provider(
    store: Any,
    idea: str,
    *,
    privacy_mode: str,
    settings_revision: int,
    request_id: str | None = None,
) -> None:
    """Record a privacy-mode transition before a request can reach a provider."""

    mode = normalize_mode(privacy_mode)
    previous = "local_strict"
    try:
        events = list(store.scan(idea))
    except Exception:
        # A ledger read failure is itself a hard boundary: do not make a
        # provider call without proving the prior audited mode.
        raise
    for event in reversed(events):
        if event.event_type != EVENT_PRIVACY:
            continue
        payload = event.payload or {}
        candidate = payload.get("new_mode") or payload.get("privacy_mode")
        if isinstance(candidate, str):
            try:
                previous = normalize_mode(candidate)
            except Exception:
                previous = "local_strict"
        break
    if previous == mode:
        return
    store.append(
        Event(
            idea_id=idea,
            event_type=EVENT_PRIVACY,
            actor=ACTOR_ORCH,
            source=ACTOR_ORCH,
            correlation_id=request_id,
            payload={
                "scope": "application",
                "old_mode": previous,
                "new_mode": mode,
                "settings_revision": int(settings_revision),
                "workspace_id": _workspace_id(idea),
            },
        )
    )


def _chat_bootstrap(
    store: Any,
    idea: str,
    text: str,
    *,
    settings: EffectiveSettings | None = None,
    request_id: str | None = None,
) -> None:
    bootstrap_idea(store, idea, text)
    if settings is not None:
        _privacy_audit_before_provider(
            store,
            idea,
            privacy_mode=str(_setting_value(settings, "privacy.mode", "local_strict") or "local_strict"),
            settings_revision=int(getattr(settings, "revision", 0)),
            request_id=request_id,
        )
    _demo_seed(store, idea)
    _touch_workspace_name(idea, text)


def _chat_context_factory(*, request, store, executor, cancellation, provider):
    from .toolkit import ToolContext

    privacy = request.privacy_mode or "local_strict"
    settings = request.effective_settings
    summary_provider, _ = _feature_adapters(provider, privacy, settings)
    if settings is None:
        # Legacy callers that do not carry a request snapshot must continue
        # through the process-level fail-closed resolver; never default a
        # missing snapshot to an enabled network gate.
        from .config import live_provider_enabled

        live_gate: bool | None = live_provider_enabled()
        network_gate = privacy != "local_strict" and bool(live_gate)
    else:
        live_gate = bool(_setting_value(settings, "provider.live_enabled", False))
        network_gate = privacy != "local_strict" and live_gate
    return ToolContext(
        idea=request.idea,
        request_id=request.request_id,
        store=store,
        executor=executor,
        workspace_id=request.workspace_id,
        context_budget=request.context_budget,
        compaction_summarizer=summary_provider,
        # Reuse the turn-owned ledger handle. Opening a nested UI store would
        # take over SQLiteStore's fenced writer lease and make the terminal
        # agent event stale before it can be committed.
        rag=_rag_for_workspace(request.workspace_id, store=store, settings=settings),
        memory=_memory(),
        run_root=(DEFAULT_DB.parent / "runs"),
        privacy_mode=privacy,
        network_available=network_gate,
        live_provider_enabled=live_gate,
        phase=store.project(request.idea).phase,
        cancellation=cancellation,
    )


def _chat_post_turn(*, request, store, provider, context, result) -> None:
    """Atomically append a requested event and its durable outbox intent."""

    del context
    privacy = request.privacy_mode or "local_strict"
    _, extraction_provider = _feature_adapters(provider, privacy, request.effective_settings)
    if (
        extraction_provider is None
        or result.cancelled
        or result.terminal_reason
        in {
            "cancelled",
            "cancel_requested",
            "privacy_denied",
            "provider_error",
            "provider_unsupported",
            "context_error",
            "context_budget",
            "budget_invalid",
        }
    ):
        return

    from .memory.pipeline import MemoryExtractionPipeline

    try:
        pipeline = MemoryExtractionPipeline()
        prepared_request = pipeline.build_request(
            store=store,
            idea_id=request.idea,
            workspace_id=request.workspace_id,
        )
        if prepared_request is None:
            return
        intent = MemoryOutboxIntent.from_request(
            prepared_request,
            privacy_mode=privacy,
            provider_name=_provider_name(provider),
        )
        requested_event = pipeline.requested_event(
            prepared_request,
            privacy_mode=intent.privacy_mode,
            provider_name=intent.provider_name,
        )
        requested_event = requested_event.model_copy(update={"correlation_id": request.request_id})
        append_with_outbox = getattr(store, "append_with_outbox", None)
        if not callable(append_with_outbox):
            # Do not reintroduce the crash window with prepare→enqueue.  A
            # storage adapter without the atomic primitive can recover this
            # request only after its capability is upgraded.
            return
        append_with_outbox(
            requested_event,
            SQLiteMemoryOutboxRepository.to_storage_intent(intent),
        )
    except DuplicateFingerprint:
        # A previous atomic attempt already wrote the requested event and
        # outbox row.  Pumping below is still safe and fingerprint-idempotent.
        pass
    except Exception:
        return
    try:
        _memory_outbox_dispatcher().pump(limit=1)
    except Exception:
        # The outbox row is already durable; a full/closed/unavailable queue
        # is intentionally left for lifespan/startup recovery.
        return


def _agent_loop_limits(settings: EffectiveSettings) -> tuple[int, int, int]:
    """Freeze the per-request agent budgets from one effective settings snapshot."""

    return (
        int(_setting_value(settings, "agent.interactive_max_steps", INTERACTIVE_MAX_STEPS_DEFAULT)),
        int(_setting_value(settings, "agent.goal_max_steps", GOAL_MAX_STEPS_DEFAULT)),
        int(_setting_value(settings, "agent.max_tool_calls", MAX_TOOL_CALLS_DEFAULT)),
    )


def _run_chat_sync(
    idea: str,
    text: str,
    mode: str,
    on_event=None,
    *,
    request_id: str | None = None,
    cancel_event: threading.Event | None = None,
    attachment_ids: tuple[str, ...] = (),
):
    """Adapt the UI runtime to the framework-neutral chat service."""

    effective = _effective_settings()
    try:
        privacy = _privacy_mode(effective)
    except TypeError as error:
        try:
            privacy = _privacy_mode()
        except TypeError:
            raise error
    frozen_request_id = request_id or uuid.uuid4().hex

    def provider_factory():
        return _factory_with_snapshot(_provider, effective)

    def executor_factory(store):
        return _factory_with_snapshot(_executor, effective, store)

    def bootstrapper(store, current_idea, current_text):
        return _chat_bootstrap(
            store,
            current_idea,
            current_text,
            settings=effective,
            request_id=frozen_request_id,
        )

    def attachment_resolver(store, workspace_id, refs):
        return _resolve_chat_attachments(store, workspace_id, refs, settings=effective)

    interactive_max_steps, goal_max_steps, max_tool_calls = _agent_loop_limits(effective)
    service = ChatService(
        store_factory=_store,
        provider_factory=provider_factory,
        executor_factory=executor_factory,
        context_factory=_chat_context_factory,
        bootstrapper=bootstrapper,
        post_turn_hook=_chat_post_turn,
        state_factory=lambda store, workspace: _summary(store, workspace),
        attachment_resolver=attachment_resolver,
        interactive_max_steps=interactive_max_steps,
        goal_max_steps=goal_max_steps,
        max_tool_calls=max_tool_calls,
    )
    result = service.run(
        ChatTurnRequest(
            idea=idea,
            text=text,
            mode=mode,
            request_id=request_id,
            cancellation=cancel_event,
            on_event=on_event,
            skills=_matched_skills(text, effective),
            privacy_mode=privacy,
            workspace_id=_workspace_id(idea),
            context_budget=_context_budget(effective),
            attachment_ids=attachment_ids,
            effective_settings=effective,
        )
    )
    # Keep the historical three-value adapter contract while carrying the
    # application-level terminal outcome through the transport boundary.
    state = dict(result.state) if isinstance(result.state, Mapping) else {}
    outcome = result.outcome
    state["terminal_status"] = outcome.status
    state["terminal_reason"] = outcome.reason
    state["terminal_failure"] = (
        _safe_failure_projection({"error": {"code": outcome.code}})
        if outcome.code
        else None
    )
    if outcome.status in {"failed", "uncertain", "paused"}:
        state["run_status"] = outcome.status
        if outcome.code:
            state["status_detail"] = state["terminal_failure"]["message"]
    return result.reply, result.ask, state


def _chat_terminal_metadata(
    state: Any,
    cancel_event: threading.Event | None = None,
) -> tuple[str, str | None, dict[str, Any] | None]:
    """Normalize a chat adapter result without breaking legacy tuple callers."""

    if isinstance(state, Mapping):
        status = state.get("terminal_status")
        reason = state.get("terminal_reason")
        failure = state.get("terminal_failure")
        if isinstance(status, str) and status in {"completed", "failed", "cancelled", "uncertain", "paused"}:
            safe_failure = failure if isinstance(failure, Mapping) else None
            if safe_failure is None and status != "completed":
                outcome = terminal_outcome_for(reason or status, cancelled=status == "cancelled")
                if outcome.code:
                    safe_failure = _safe_failure_projection({"error": {"code": outcome.code}})
            return status, str(reason) if reason else None, dict(safe_failure) if safe_failure else None
    if cancel_event is not None and cancel_event.is_set():
        return "cancelled", "cancelled", _safe_failure_projection({"error": {"code": "run_cancelled"}})
    # Legacy monkeypatched adapters predate terminal metadata.  Preserve their
    # successful three-tuple behavior while treating malformed state as a
    # failed request in the explicit error path below.
    return "completed", None, None


@app.post("/api/chat")
def chat(body: ChatIn, ws: str = _IDEA):
    idea = _resolve_workspace(ws)
    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=422, detail="消息不能为空。")
    if len(text) > 20000:
        raise HTTPException(status_code=413, detail="消息过长，请拆成几条研究指示。")
    if body.mode not in {"interactive", "goal"}:
        raise HTTPException(status_code=422, detail="mode 必须是 interactive|goal。")
    _validate_chat_attachment_refs(idea, body.attachment_ids)
    request_id = uuid.uuid4().hex
    try:
        reply, ask, state = _run_chat_sync(
            idea,
            text,
            body.mode,
            request_id=request_id,
            attachment_ids=tuple(body.attachment_ids),
        )
        return {"request_id": request_id, "reply": reply, "mode": body.mode, "ask": ask, "state": state}
    except Exception as error:  # noqa: BLE001
        raise HTTPException(status_code=500, detail="本轮未完成：请查看 Trace 中的最近事件。") from error


@app.post("/api/chat/stream")
async def chat_stream(body: ChatIn, ws: str = _IDEA):
    """Stream one turn as SSE, preserving tail events before ``done``."""
    import asyncio
    import json as _json
    import queue as _queue

    idea = _resolve_workspace(ws)
    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=422, detail="消息不能为空。")
    if len(text) > 20000:
        raise HTTPException(status_code=413, detail="消息过长，请拆成几条研究指示。")
    if body.mode not in {"interactive", "goal"}:
        raise HTTPException(status_code=422, detail="mode 必须是 interactive|goal。")
    _validate_chat_attachment_refs(idea, body.attachment_ids)
    request_id = uuid.uuid4().hex

    def sse(obj: dict) -> str:
        return f"data: {_json.dumps({**obj, 'request_id': request_id}, ensure_ascii=False)}\n\n"

    async def gen():
        evq: _queue.Queue = _queue.Queue()
        cancel_event = threading.Event()
        _register_request_control(request_id, idea, cancel_event)
        yield sse({"type": "start", "status": "running", "schema": "sse.v1"})

        def on_event(ev):
            # Keep callbacks after stop/disconnect too.  A cooperative tool
            # may emit its final result while cancellation is being observed;
            # dropping that tail would leave the browser with a permanently
            # running card and no recoverable terminal evidence.
            if isinstance(ev, dict):
                evq.put(ev)

        def run():
            try:
                result = _run_chat_sync(
                    idea,
                    text,
                    body.mode,
                    on_event=on_event,
                    request_id=request_id,
                    cancel_event=cancel_event,
                    attachment_ids=tuple(body.attachment_ids),
                )
                state = result[2] if isinstance(result, tuple) and len(result) >= 3 else None
                status, reason, failure = _chat_terminal_metadata(state, cancel_event)
                _finish_request_control(
                    request_id,
                    status=status,
                    terminal_reason=reason,
                    error_code=failure.get("code") if failure else None,
                    retryable=failure.get("retryable") if failure else None,
                )
                return result
            except StoreError:
                failure = _safe_failure_projection({"error": {"code": "ledger_unavailable"}})
                _finish_request_control(
                    request_id,
                    status="failed",
                    terminal_reason="storage_error",
                    error_code=failure["code"],
                    retryable=failure["retryable"],
                )
                return None, None, {
                    "__error__": "request_failed",
                    "terminal_status": "failed",
                    "terminal_reason": "storage_error",
                    "terminal_failure": failure,
                }
            except Exception:  # noqa: BLE001 - raw provider/tool errors never cross SSE
                _finish_request_control(
                    request_id,
                    status="cancelled" if cancel_event.is_set() else "failed",
                    terminal_reason="cancelled" if cancel_event.is_set() else "request_failed",
                    error_code="run_cancelled" if cancel_event.is_set() else "request_failed",
                    retryable=not cancel_event.is_set(),
                )
                return None, None, {
                    "__error__": "request_failed",
                    "terminal_status": "cancelled" if cancel_event.is_set() else "failed",
                    "terminal_reason": "cancelled" if cancel_event.is_set() else "request_failed",
                    "terminal_failure": (
                        _safe_failure_projection(
                            {"error": {"code": "run_cancelled" if cancel_event.is_set() else "request_failed"}}
                        )
                        if cancel_event.is_set()
                        else {
                            **_safe_failure_projection({"error": {"code": "request_failed"}}),
                            "retryable": True,
                        }
                    ),
                }

        task = asyncio.create_task(asyncio.to_thread(run))
        last_heartbeat = time.monotonic()

        def drain():
            events = []
            while True:
                try:
                    events.append(evq.get_nowait())
                except _queue.Empty:
                    return events

        tool_counter = 0
        pending_tool_ids: dict[str, list[str]] = {}
        active_tool_ids: set[str] = set()
        seen_tool_ids: set[str] = set()

        def render_event(ev: dict) -> str | None:
            nonlocal tool_counter
            kind = ev.get("type")
            if kind == "text_delta":
                return sse({"type": "token", "text": ev.get("text", "")})
            if kind == "tool_started":
                name = str(ev.get("name") or ev.get("tool") or "未知工具")
                explicit_id = _safe_projection_id(ev.get("call_id") or ev.get("tool_id"))
                if explicit_id is not None:
                    tool_id = explicit_id
                else:
                    tool_counter += 1
                    tool_id = f"{request_id}:tool:{tool_counter}"
                    while tool_id in seen_tool_ids:
                        tool_counter += 1
                        tool_id = f"{request_id}:tool:{tool_counter}"
                seen_tool_ids.add(tool_id)
                active_tool_ids.add(tool_id)
                queue = pending_tool_ids.setdefault(name, [])
                if tool_id not in queue:
                    queue.append(tool_id)
                return sse(
                    {
                        "type": "tool_started",
                        "tool_id": tool_id,
                        "call_id": tool_id,
                        "tool": name,
                        "name": name,
                        "status": "running",
                    }
                )
            if kind in {"tool_completed", "tool_failed"}:
                name = str(ev.get("name") or ev.get("tool") or "未知工具")
                explicit_id = _safe_projection_id(ev.get("call_id") or ev.get("tool_id"))
                if explicit_id is not None:
                    tool_id = explicit_id
                else:
                    ids = [candidate for candidate in (pending_tool_ids.get(name) or []) if candidate in active_tool_ids]
                    if len(ids) == 1:
                        tool_id = ids[0]
                    else:
                        tool_counter += 1
                        tool_id = f"{request_id}:tool:{tool_counter}:done"
                        while tool_id in seen_tool_ids:
                            tool_counter += 1
                            tool_id = f"{request_id}:tool:{tool_counter}:done"
                seen_tool_ids.add(tool_id)
                active_tool_ids.discard(tool_id)
                for queue in pending_tool_ids.values():
                    while tool_id in queue:
                        queue.remove(tool_id)
                ok = bool(ev.get("ok"))
                result = {
                    "type": "tool_completed",
                    "tool_id": tool_id,
                    "call_id": tool_id,
                    "tool": name,
                    "name": name,
                    "ok": ok,
                    "status": "succeeded" if ok else "failed",
                }
                if not ok:
                    result["error"] = {
                        "code": "tool_failed",
                        "message": "工具执行失败，请查看 Trace 或导出诊断包。",
                        "retryable": False,
                        "support_action": "download_diagnostics",
                    }
                return sse(result)
            return None

        try:
            while True:
                pending = drain()
                for event in pending:
                    rendered = render_event(event)
                    if rendered is not None:
                        yield rendered
                if task.done():
                    # The worker enqueues callbacks before its future becomes
                    # done.  Drain once more at the terminal boundary so the
                    # last token/tool event can never be placed after done.
                    for event in drain():
                        rendered = render_event(event)
                        if rendered is not None:
                            yield rendered
                    reply, ask, state = task.result()
                    state = state if isinstance(state, Mapping) else {}
                    control = _request_control_snapshot(request_id) or {}
                    status, reason, failure = _chat_terminal_metadata(state, cancel_event)
                    if state.get("__error__"):
                        error_payload = failure or _safe_failure_projection(
                            {"error": {"code": "request_failed"}}
                        )
                        yield sse({"type": "error", "error": error_payload})
                        break
                    # The registry is the last-mile arbitration point for a
                    # concurrent stop.  A terminal worker outcome wins unless
                    # the request was still legacy-compatible and only the
                    # cancel signal is available.
                    status = control.get("status") if control.get("status") in {
                        "completed", "failed", "cancelled", "uncertain", "paused"
                    } else status
                    if status in {"failed", "uncertain"} and failure is None:
                        failure = _safe_failure_projection(
                            {"error": {"code": "uncertain" if status == "uncertain" else "request_failed"}}
                        )
                    done_payload: dict[str, Any] = {
                        "type": "done",
                        "ask": ask,
                        "state": state,
                        "status": status,
                        "terminal_reason": reason or control.get("terminal_reason"),
                        "cancel_requested": bool(control.get("cancel_requested")),
                    }
                    if failure is not None:
                        done_payload["error"] = failure
                    yield sse(done_payload)
                    break
                now = time.monotonic()
                if now - last_heartbeat >= 15:
                    yield sse({"type": "heartbeat", "ts": int(now * 1000)})
                    last_heartbeat = now
                await asyncio.sleep(0.02)
        except asyncio.CancelledError:
            # Starlette cancels an async generator when the client disconnects.
            # The worker may already be in a provider call, but this flag stops
            # forwarding and prevents not-yet-started UI work from proceeding.
            _mark_request_disconnected(request_id)
            raise
        finally:
            _mark_request_disconnected(request_id)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "X-Request-ID": request_id,
        },
    )


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("STATA_AGENT_UI_PORT", "8001")))


_HTML = _INDEX_FILE.read_text(encoding="utf-8") if _INDEX_FILE.exists() else ""


if __name__ == "__main__":
    main()
