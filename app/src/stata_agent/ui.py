"""FastAPI UI for the single-idea empirical-research workbench.

The server deliberately exposes read-only projections of the event ledger. UI
actions that change research state continue to go through the runner; the
browser never writes directly to the events table or to a projection.

The page itself lives in ``ui/index.html`` with local CSS and ES module
JavaScript. Keeping those files separate makes the interface easy to inspect
and keeps the FastAPI layer small enough to hand over to the next maintainer.
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .application.local_task_queue import LocalTaskQueue
from .application.request_control import RequestControlNotFound, RequestControlRegistry
from .application.task_queue import TaskQueue, TaskSubmitResult
from .application.workspace_service import (
    CreateWorkspaceRequest,
    WorkspaceConflictError,
    WorkspaceNotFoundError,
    WorkspaceService,
    WorkspaceValidationError,
)
from .domain.action import Act, ActionProposal
from .events.schema import (
    ACTOR_ORCH,
    ACTOR_USER,
    EVENT_AGENT_STEP,
    EVENT_APPROVAL_GRANT,
    EVENT_APPROVAL_REJECT,
    EVENT_APPROVAL_REQ,
    EVENT_BUDGET,
    EVENT_HEALTH,
    EVENT_IDEA,
    EVENT_MEMORY_REVIEW_COMPLETED,
    EVENT_MEMORY_REVIEW_FAILED,
    EVENT_MEMORY_REVIEW_REQUESTED,
    EVENT_PHASE,
    EVENT_RUN_FAILED,
    EVENT_RUN_SUCCEEDED,
    EVENT_RUN_UNCERTAIN,
    EVENT_TOOL_DONE,
    EVENT_TOOL_INVOKED,
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
from .providers.mock import MockFixedProvider, MockReplayProvider
from .runner import approve as runner_approve
from .storage.sqlite_store import SQLiteStore
from .tools.executor import StataExecutor  # noqa: E402  （真 Stata 可选）
from .tools.fake_executor import FakeExecutor
from .writer.docx_out import claims_to_docx
from .writer.ground import render_claim_sentence

DEFAULT_DB = Path(os.environ.get("STATA_AGENT_DB", "samples/ideas/ui/ledger.sqlite3"))
_IDEA = "ui"
_APP_ROOT = Path(__file__).resolve().parents[2]
_UI_DIR = Path(__file__).with_name("ui")
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

# Active stream controls are deliberately process-local.  The event ledger is
# the durable source of research state; this small registry only lets a user
# address one in-flight HTTP request without accidentally stopping another
# workspace.  The application-layer registry owns locking, TTL pruning, and
# lifecycle transitions; this module keeps only the transport adapter names
# used by existing routes and tests.
_REQUEST_CONTROL_TTL = 3600
_REQUEST_CONTROL_REGISTRY = RequestControlRegistry(ttl_seconds=_REQUEST_CONTROL_TTL)
_MEMORY_EXTRACTION_SCHEDULER: TaskQueue = LocalTaskQueue(max_pending=4, shutdown_timeout=1.0)

app = FastAPI(title="stata-agent · research UI")
app.mount("/static", StaticFiles(directory=str(_UI_DIR)), name="ui-static")


def _error_message(detail: Any, fallback: str) -> str:
    if isinstance(detail, str) and detail.strip():
        return detail.strip()
    if isinstance(detail, (dict, list)):
        try:
            return json.dumps(detail, ensure_ascii=False)[:1000]
        except (TypeError, ValueError):
            pass
    return fallback


def _error_body(status_code: int, message: str, *, code: str | None = None, details: Any = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": False,
        "error": {"code": code or f"http_{status_code}", "message": message},
        # ``detail`` is kept for clients written against the original v0 API.
        "detail": message,
        "status_code": status_code,
    }
    if details:
        payload["error"]["details"] = details
    return payload


@app.exception_handler(HTTPException)
async def _http_error_handler(_request: Request, exc: HTTPException) -> JSONResponse:
    message = _error_message(exc.detail, "请求未完成。")
    return JSONResponse(status_code=exc.status_code, content=_error_body(exc.status_code, message))


@app.exception_handler(RequestValidationError)
async def _validation_error_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
    # Do not echo submitted values (which may contain Stata code or private
    # data); locations/messages are enough for a client-side form error.
    details = [
        {"loc": list(item.get("loc") or []), "msg": str(item.get("msg") or "参数无效"),
         "type": str(item.get("type") or "value_error")}
        for item in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content=_error_body(422, "请求参数无效。", code="validation_error", details=details),
    )

# SQLiteStore owns a fenced single-writer lease even for projection reads.
# FastAPI runs synchronous routes in a thread pool, while the browser refreshes
# state/events/approvals concurrently. Serialising UI store sessions prevents a
# read request from taking over the lease halfway through a chat write.
_UI_STORE_LOCK = threading.RLock()


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
    }


def _finish_request_control(request_id: str, *, status: str) -> None:
    # The registry preserves the first terminal result, so a late disconnect
    # callback cannot overwrite a completed/failed/cancelled request.
    _REQUEST_CONTROL_REGISTRY.finish(request_id, status)


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


class _HistoryStoreView:
    """Store facade that hides the just-appended user event from loop history.

    ``agent_loop`` appends the current user event itself to the message list
    after reading durable history.  The UI persists that event before entering
    the loop so the conversation remains ordered; this narrow facade prevents
    that one event from being sent a second time without changing the ledger
    or the shared loop implementation.
    """

    def __init__(self, store: SQLiteStore, *, hidden_seq: int):
        self._store = store
        self._hidden_seq = hidden_seq

    def scan(self, idea_id: str, *args, **kwargs):
        return (
            event
            for event in self._store.scan(idea_id, *args, **kwargs)
            if event.seq != self._hidden_seq
        )

    def __getattr__(self, name: str):
        return getattr(self._store, name)


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
                    service.create(CreateWorkspaceRequest(
                        id=ident,
                        name=str(item.get("name") or ident)[:120],
                        root=service.canonical_root(workspace_id=ident),
                        now=int(item.get("created_at") or 0),
                    ))
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
        result.append({
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
        })
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
    store.append(Event(idea_id=idea, event_type=EVENT_PHASE, actor=ACTOR_ORCH, source=ACTOR_ORCH,
                       payload={"from": "IDEA", "to": "ESTIMATION"}))


def _provider():
    from .providers.registry import live_available

    if live_available():
        from .providers.registry import default_provider

        return default_provider()
    if _demo_enabled():
        # 无 LLM key 的离线演示：剧本走完 定 spec → 跑(fake) → 问是否出稿
        return MockReplayProvider([
            ActionProposal(decision_summary="先定主 spec",
                           acts=[Act(act_type="propose_spec", target={"spec_id": "s1"}, reason="主 spec")]),
            ActionProposal(decision_summary="跑主回归",
                           acts=[Act(act_type="request_run", target={"spec_id": "s1"}, reason="跑主回归")]),
            ActionProposal(decision_summary="出结果", ask_user="是否生成 Word 初稿？"),
        ])
    return MockFixedProvider(
        ActionProposal(
            decision_summary="记录消息",
            ask_user="（未配置 LLM key）已记下。设 DEEPSEEK_API_KEY 或 DASHSCOPE_API_KEY 后启用真模型。",
        )
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
            else is_remote(self.provider_name)
            or self.provider_name in {"remote", "cloud", "http", "https"}
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


def _feature_adapters(provider: Any, privacy_mode: str) -> tuple[Any, Any]:
    """Build optional summary/extraction adapters around the current provider.

    Construction is local-only.  Neither adapter calls the provider until the
    corresponding overflow or background job is explicitly reached.
    """

    from .config import compaction_summary_mode, memory_extraction_mode

    summary_adapter = None
    extraction_adapter = None
    if not callable(getattr(provider, "chat", None)):
        return summary_adapter, extraction_adapter
    if compaction_summary_mode() == "provider":
        from .harness.summary_service import ChatCompactionSummaryProvider

        summary_adapter = ChatCompactionSummaryProvider(
            _PrivacyChatAdapter(provider, privacy_mode=privacy_mode),
        )
    if memory_extraction_mode() == "provider":
        from .memory.pipeline import ChatMemoryExtractionProvider

        extraction_adapter = ChatMemoryExtractionProvider(
            _PrivacyChatAdapter(provider, privacy_mode=privacy_mode),
        )
    return summary_adapter, extraction_adapter


def _executor(store: SQLiteStore):
    """Return an explicitly selected executor; never invent fake results live."""

    configured = os.environ.get("STATA_AGENT_EXECUTOR", "").strip().lower()
    if configured == "stata":
        # 绝对化 run_root：ToolEnforcer 的路径 containment 要求绝对路径，
        # 且 Stata 的 cwd 在 stata-mcp，不能让相对路径导致越界/误判。
        run_root = Path(DEFAULT_DB).resolve().parent / "runs"
        run_root.mkdir(parents=True, exist_ok=True)
        return StataExecutor(store, run_root=run_root, share_session=True)
    if configured in {"fake", "demo"} or (_demo_enabled() and not configured):
        return FakeExecutor(store)
    # A missing/unknown executor is an unavailable capability.  In particular,
    # a live LLM must not be handed FakeExecutor merely because Stata is absent.
    return None


def _privacy_mode() -> str:
    from .providers.registry import privacy_mode

    return privacy_mode()


def _config_info() -> dict:
    """给设置页展示的真实运行时配置（只读）。"""
    from .providers.registry import live_available
    from .config import compaction_summary_mode, memory_extraction_mode

    executor_kind = os.environ.get("STATA_AGENT_EXECUTOR", "").strip().lower()
    if executor_kind == "stata":
        executor = "真 Stata"
    elif executor_kind in {"fake", "demo"} or (_demo_enabled() and not executor_kind):
        executor = "演示(Fake)"
    else:
        executor = "未配置"
    provider = "未配置"
    if live_available():
        provider = "deepseek" if os.environ.get("DEEPSEEK_API_KEY") else "qwen"
    lib = os.environ.get("STATA_AGENT_LIBRARY", "")
    skills = list(_skills().keys())
    return {
        "provider": provider,
        "executor": executor,
        "privacy_mode": _privacy_mode(),
        "library": lib or "(未配置文献库)",
        "skills": skills,
        "skill_errors": list(_SKILL_ERRORS),
        "workspace_db": str(DEFAULT_DB),
        "compaction_summary": compaction_summary_mode(),
        "memory_extraction": memory_extraction_mode(),
    }


def _memory():
    """项目记忆（约束，非证据）；没有就 None，工具会优雅降级。"""
    try:
        from .memory.sqlite_store import SQLiteMemoryStore

        _migrate_legacy_memory_once()
        return SQLiteMemoryStore(DEFAULT_DB)
    except Exception:  # noqa: BLE001
        return None


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
    """Queue requested jobs that lack a matching terminal event.

    This explicit hook is used by process-resume code and does not run at
    import time.  It only queues work when the operator has enabled provider
    extraction; the pipeline itself performs the final fingerprint check.
    """

    from .config import memory_extraction_mode
    from .events.schema import (
        EVENT_MEMORY_EXTRACTION_COMPLETED,
        EVENT_MEMORY_EXTRACTION_DENIED,
        EVENT_MEMORY_EXTRACTION_FAILED,
        EVENT_MEMORY_EXTRACTION_NOOP,
        EVENT_MEMORY_EXTRACTION_REQUESTED,
    )

    if memory_extraction_mode() != "provider":
        return 0
    idea = _resolve_workspace(ws) if ws is not None else None
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


def _context_budget():
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


def _rag():
    """文献混合检索；目录缺失则 None and unchanged indexes are reused."""
    lib = os.environ.get("STATA_AGENT_LIBRARY", "").strip()
    library = Path(lib).expanduser() if lib else None
    if library is None or not library.exists():
        return None
    cache_path = DEFAULT_DB.parent / "rag_cache.json"
    key = (str(library.resolve()), str(cache_path.resolve()))
    try:
        from .rag.index import build_hybrid

        index = build_hybrid(library, cache_path=cache_path)
        with _RAG_CACHE_LOCK:
            _RAG_CACHE[key] = index
        return index
    except Exception:  # noqa: BLE001
        return None


def _skills() -> dict:
    """Load the configured skill root, including packaged defaults."""
    global _SKILL_ERRORS
    from .skills.loader import load_skill_dir

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


def _matched_skills(text: str) -> list:
    from .skills.loader import match_skills

    return match_skills(_skills(), text)


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
        return {"text": str(payload.get("text", ""))[:2000]}
    if kind == EVENT_AGENT_STEP:
        acts = payload.get("acts") or []
        return {
            "decision_summary": str(payload.get("decision_summary") or "")[:500],
            "ask": str(payload.get("ask") or "")[:1000],
            "stop_reason": payload.get("stop_reason"),
            "acts": [str(a.get("act_type", "")) for a in acts if isinstance(a, dict)][:12],
        }
    keys = {
        "request_id",
        "act",
        "tool",
        "reason",
        "note",
        "error",
        "run_id",
        "result_id",
        "spec_id",
        "operation_id",
        "issues",
        "status",
        "ok",
    }
    out: dict[str, Any] = {key: payload[key] for key in keys if key in payload}
    if kind in {EVENT_RUN_SUCCEEDED, EVENT_RUN_FAILED, EVENT_RUN_UNCERTAIN}:
        machine = payload.get("machine")
        if isinstance(machine, dict):
            out["machine"] = {str(k): machine[k] for k in list(machine)[:12]}
    if kind == EVENT_APPROVAL_REQ and isinstance(payload.get("target"), dict):
        out["target_keys"] = sorted(str(k) for k in payload["target"].keys())[:12]
    return out


def _public_event(event: Event) -> dict[str, Any]:
    """Stable, compact event shape retained for old clients and trace UI."""

    payload = event.payload or {}
    object_value = next(
        (
            payload.get(key)
            for key in ("object", "claim_id", "card_id", "run_id", "result_id", "spec_id", "request_id")
            if payload.get(key) is not None
        ),
        None,
    )
    if object_value is None and isinstance(payload.get("target"), dict):
        object_value = payload.get("target")
    summary = next(
        (
            str(payload.get(key))
            for key in ("summary", "decision_summary", "reason", "note", "message", "text")
            if payload.get(key)
        ),
        "",
    )[:240]

    return {
        "seq": event.seq,
        "type": event.event_type,
        "event_type": event.event_type,
        "actor": event.actor,
        "phase": event.phase,
        "op": event.operation_id,
        "operation_id": event.operation_id,
        "created_at": event.created_at,
        "object": object_value,
        "summary": summary,
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
                "modified" if decision == "modify"
                else "approved" if event.event_type == EVENT_APPROVAL_GRANT else "rejected"
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
            return "paused", "已达到本轮预算上限，可从停点续跑"
        if event.event_type == EVENT_HEALTH and payload.get("ok") is False:
            return "paused", "健康检查未通过，已停在最近一致状态"
        if event.event_type == EVENT_RUN_FAILED:
            return "failed", "最近一次运行未完成，请查看运行记录"
        if event.event_type == EVENT_RUN_UNCERTAIN:
            return "failed", "最近一次运行状态不确定，请先核对运行记录"
        if event.event_type == EVENT_AGENT_STEP:
            if payload.get("ask"):
                return "awaiting_user", "等待你补充研究信息"
            break
    if any(event.event_type == EVENT_RUN_SUCCEEDED for event in events):
        return "idle", "最近一次运行已完成"
    if events:
        return "idle", "等待你的下一条研究指示"
    return "idle", "等待你的第一条研究问题"


def _conversation(events: list[Event]) -> list[dict[str, Any]]:
    """Project durable conversation/replay blocks from append-only events."""

    messages: list[dict[str, Any]] = []
    pending_tools: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        payload = event.payload or {}
        base = {"seq": event.seq, "phase": event.phase, "created_at": event.created_at}
        if event.event_type == EVENT_USER:
            messages.append({**base, "role": "user", "kind": "text", "text": str(payload.get("text") or "")})
        elif event.event_type == EVENT_AGENT_STEP:
            ask = str(payload.get("ask") or "")
            reply = str(payload.get("reply") or "")
            summary = str(payload.get("decision_summary") or "")
            acts = payload.get("acts") if isinstance(payload.get("acts"), list) else []
            messages.append(
                {
                    **base,
                    "role": "assistant",
                    "kind": "agent",
                    "text": reply or ask or summary or "Agent 已完成一轮判断。",
                    "reply": reply,
                    "summary": summary,
                    "ask": ask,
                    "stop_reason": payload.get("stop_reason"),
                    "acts": [
                        {"act_type": str(a.get("act_type") or ""), "reason": str(a.get("reason") or "")}
                        for a in acts
                        if isinstance(a, dict)
                    ][:12],
                }
            )
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
                        else "approved" if event.event_type == EVENT_APPROVAL_GRANT else "rejected"
                    ),
                    "note": str(payload.get("note") or ""),
                }
            )
        elif event.event_type == EVENT_TOOL_INVOKED:
            tool_name = str(payload.get("tool") or "未知工具")
            args = payload.get("args") if isinstance(payload.get("args"), dict) else {}
            item = {
                **base,
                "role": "system",
                "kind": "tool",
                "tool_id": f"seq-{event.seq}",
                "tool_name": tool_name,
                "status": "running",
                "ok": None,
                "args_keys": sorted(str(key) for key in args)[:16],
            }
            messages.append(item)
            pending_tools.setdefault(tool_name, []).append(item)
        elif event.event_type == EVENT_TOOL_DONE:
            tool_name = str(payload.get("tool") or "未知工具")
            result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
            ok = bool(payload.get("ok"))
            pending = pending_tools.get(tool_name) or []
            item = pending.pop(0) if pending else None
            if item is None:
                item = {
                    **base,
                    "role": "system",
                    "kind": "tool",
                    "tool_id": f"seq-{event.seq}",
                    "tool_name": tool_name,
                    "args_keys": [],
                }
                messages.append(item)
            item.update(
                {
                    "status": "succeeded" if ok else "failed",
                    "ok": ok,
                    "completed_seq": event.seq,
                    "error": str(result.get("error") or result.get("detail") or "")[:800],
                }
            )
        elif event.event_type == EVENT_RUN_SUCCEEDED:
            provenance = payload.get("provenance") if isinstance(payload.get("provenance"), dict) else {}
            machine = payload.get("machine") if isinstance(payload.get("machine"), dict) else {}
            messages.append(
                {
                    **base,
                    "role": "assistant",
                    "kind": "run",
                    "status": "succeeded",
                    "run_id": str(payload.get("run_id") or payload.get("result_id") or ""),
                    "machine": machine,
                    "provenance": provenance,
                    "text": "运行完成，机器层结果已签入证据链。",
                }
            )
        elif event.event_type in {EVENT_RUN_FAILED, EVENT_RUN_UNCERTAIN}:
            messages.append(
                {
                    **base,
                    "role": "assistant",
                    "kind": "error",
                    "status": "uncertain" if event.event_type == EVENT_RUN_UNCERTAIN else "failed",
                    "run_id": str(payload.get("run_id") or payload.get("result_id") or ""),
                    "text": "主回归未完成；请查看最近运行记录，再决定从停点续跑还是调整口径。",
                    "detail": str(payload.get("error") or payload.get("reason") or "")[:800],
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
                    "detail": "可从停点续跑，或发送新的研究口径。",
                }
            )
        elif event.event_type == EVENT_HEALTH and payload.get("ok") is False:
            messages.append(
                {
                    **base,
                    "role": "assistant",
                    "kind": "error",
                    "status": "paused",
                    "text": "健康检查未通过，自动推进已暂停。",
                    "detail": str(payload.get("issues") or "请查看 Trace 中的健康事件。"),
                }
            )
    return messages


def _summary(store: SQLiteStore, idea: str = _IDEA) -> dict[str, Any]:
    events = _event_list(store, idea)
    proj = store.project(idea)
    rs = proj.research_state
    approvals = _approval_records(events)
    run_status, status_detail = _run_state(events, approvals)
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
        "active_request": _request_control_public(_latest_active_request(idea)),
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
        "health": {"ok": run_status not in {"failed", "paused"}, "detail": status_detail},
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
    method = ("本研究用双重差分（DID）识别：结果变量对 处理组×期后 交互回归，"
              "标准误聚类到处理相关层级；样本为双期店面（长表）。")
    limits = ("数据仅两期，无法做事件研究/动态效应；平行趋势改用基线特征平衡与多个"
              "稳健性口径替代，并在解读时保留因果表述的审慎。")
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
    return {"workspace": {**row, "events": 0, "last_seq": None, "run_status": "idle", "pending_approvals": 0},
            "items": rows,
            "active": idea}


@app.get("/api/state")
def state(ws: str = _IDEA):
    idea = _resolve_workspace(ws)
    s = _store()
    try:
        return _summary(s, idea)
    finally:
        s.close()


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
                row
                for row in ordered
                if row.get("seq") is not None and int(row["seq"]) < int(before_seq)
            ]
        safe_limit = max(1, min(int(limit), 500))
        page = ordered[:safe_limit]
        next_before = page[-1]["seq"] if len(page) == safe_limit and page else None
        return {
            "items": page,
            "next_before_seq": next_before,
            "total": len(filtered),
            "workspace": idea,
        }
    finally:
        s.close()


class ChatIn(BaseModel):
    text: str
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
        store.append(Event(
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
        ))
        store.append(Event(
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
        ))
        event_seq = store.append(Event(
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
        ))
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
                    (event for event in reversed(candidate_events)
                     if event.event_type == EVENT_MEMORY_REVIEW_COMPLETED),
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
                        "memory": ({"id": str(payload["memory_id"])}
                                    if payload.get("memory_id") else None),
                    }
                requested = next(
                    (event for event in reversed(candidate_events)
                     if event.event_type == EVENT_MEMORY_REVIEW_REQUESTED),
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
                    raise HTTPException(status_code=503, detail="记忆审核记录暂不可写，请稍后重试。") from error
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
            return {"ok": True, "request_id": None, "workspace": idea,
                    "status": "idle", "cancel_requested": False}
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


@app.get("/api/health")
def health(ws: str = _IDEA):
    idea = _resolve_workspace(ws)
    s = _store()
    try:
        summary = _summary(s, idea)
        privacy = _privacy_mode()
        config = summary.get("config") or {}
        skill_errors = config.get("skill_errors") or []
        return {
            "ok": bool(summary["health"]["ok"]) and not skill_errors,
            "local_strict": privacy == "local_strict",
            "privacy_mode": privacy,
            "network_available": privacy != "local_strict",
            "executor": config.get("executor", "未配置"),
            "skill_errors": skill_errors,
            "status": summary["run_status"],
            "detail": skill_errors[0] if skill_errors else summary["status_detail"],
        }
    finally:
        s.close()


def _run_chat_sync(
    idea: str,
    text: str,
    mode: str,
    on_event=None,
    *,
    request_id: str | None = None,
    cancel_event: threading.Event | None = None,
):
    """Run one chat turn and close any per-turn executor before returning."""

    request_id = request_id or uuid.uuid4().hex
    s = _store()
    executor = None
    memory = None
    try:
        if cancel_event is not None and cancel_event.is_set():
            return "", None, _summary(s, idea)
        bootstrap_idea(s, idea, text)
        _touch_workspace_name(idea, text)
        user_seq = s.append(
            Event(
                idea_id=idea,
                event_type=EVENT_USER,
                actor=ACTOR_USER,
                source=ACTOR_USER,
                payload={"text": text, "request_id": request_id},
            )
        )

        from .harness.agent_loop import run_loop
        from .toolkit import ToolContext, default_tools

        provider = _provider()
        pm = _privacy_mode()
        executor = _executor(s)
        workspace_id = _workspace_id(idea)
        memory = _memory()
        summary_provider, extraction_provider = _feature_adapters(provider, pm)
        ctx = ToolContext(
            idea=idea, store=s, executor=executor,
            workspace_id=workspace_id,
            context_budget=_context_budget(),
            compaction_summarizer=summary_provider,
            rag=_rag(), memory=memory,
            run_root=(DEFAULT_DB.parent / "runs"),
            privacy_mode=pm,
            network_available=(pm != "local_strict"),
            phase=s.project(idea).phase,
        )
        # Runtime-control may add a typed cancellation field later.  Setting
        # the attribute keeps this UI compatible with both the current
        # ToolContext and that future implementation without widening the
        # toolkit ownership boundary.
        if cancel_event is not None:
            setattr(ctx, "cancel_event", cancel_event)
        if cancel_event is not None and cancel_event.is_set():
            return "", None, _summary(s, idea)
        # ``interactive`` is deliberately one model step; ``goal`` may use
        # the full bounded loop.  The distinction is now observable and is
        # also reflected in the returned payload/UI toggle.
        max_steps = 1 if mode == "interactive" else 12
        loop_store = _HistoryStoreView(s, hidden_seq=user_seq)
        res = run_loop(
            loop_store,
            provider,
            default_tools(),
            ctx,
            user_text=text,
            max_steps=max_steps,
            privacy_mode=pm,
            skills=_matched_skills(text),
            on_event=on_event,
        )
        reply = res.ask or res.reply or ""
        if (
            extraction_provider is not None
            and not res.cancelled
            and res.terminal_reason
            not in {
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
            # Prepare and append the request while this turn still owns the
            # ledger lease.  Queue admission happens only after that durable
            # boundary, so a crash or full queue leaves resumable work.
            from .memory.pipeline import MemoryExtractionPipeline

            prepared_request = None
            try:
                with _memory_transaction():
                    prepared_request = MemoryExtractionPipeline().prepare(
                        store=s,
                        idea_id=idea,
                        workspace_id=workspace_id,
                    )
            except Exception:
                # The completed chat turn remains successful; an operator can
                # retry preparation through the explicit resume hook.
                prepared_request = None
            if prepared_request is not None:
                _submit_memory_extraction_task(
                    key=f"{idea}:{workspace_id}:{prepared_request.fingerprint}",
                    callback=lambda: _run_memory_extraction_job(
                        idea=idea,
                        workspace_id=workspace_id,
                        provider=extraction_provider,
                        privacy_mode=pm,
                        prepared_request=prepared_request,
                    ),
                )
        return reply, res.ask, _summary(s, idea)
    finally:
        _close_memory(memory)
        if executor is not None:
            close = getattr(executor, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001
                    pass
        s.close()


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
    request_id = uuid.uuid4().hex
    try:
        reply, ask, state = _run_chat_sync(idea, text, body.mode, request_id=request_id)
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
    request_id = uuid.uuid4().hex

    def sse(obj: dict) -> str:
        return f"data: {_json.dumps({**obj, 'request_id': request_id}, ensure_ascii=False)}\n\n"

    async def gen():
        evq: _queue.Queue = _queue.Queue()
        cancel_event = threading.Event()
        _register_request_control(request_id, idea, cancel_event)
        yield sse({"type": "start", "status": "running", "schema": "sse.v1"})

        def on_event(ev):
            if cancel_event.is_set():
                return
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
                )
                _finish_request_control(
                    request_id,
                    status="cancelled" if cancel_event.is_set() else "completed",
                )
                return result
            except Exception as error:  # noqa: BLE001
                _finish_request_control(
                    request_id,
                    status="cancelled" if cancel_event.is_set() else "failed",
                )
                return None, None, {"__error__": str(error)[:200]}

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

        def render_event(ev: dict) -> str | None:
            nonlocal tool_counter
            kind = ev.get("type")
            if kind == "text_delta":
                return sse({"type": "token", "text": ev.get("text", "")})
            if kind == "tool_started":
                name = str(ev.get("name") or ev.get("tool") or "未知工具")
                tool_counter += 1
                tool_id = str(ev.get("tool_id") or f"{request_id}:tool:{tool_counter}")
                pending_tool_ids.setdefault(name, []).append(tool_id)
                return sse({
                    "type": "tool_started",
                    "tool_id": tool_id,
                    "tool": name,
                    "name": name,
                    "status": "running",
                })
            if kind == "tool_completed":
                name = str(ev.get("name") or ev.get("tool") or "未知工具")
                ids = pending_tool_ids.get(name) or []
                tool_id = str(ev.get("tool_id") or (ids.pop(0) if ids else f"{request_id}:tool:{tool_counter + 1}"))
                ok = bool(ev.get("ok"))
                result = {
                    "type": "tool_completed",
                    "tool_id": tool_id,
                    "tool": name,
                    "name": name,
                    "ok": ok,
                    "status": "succeeded" if ok else "failed",
                }
                if not ok:
                    result["error"] = {
                        "code": "tool_failed",
                        "message": str(ev.get("error") or "工具执行失败。")[0:500],
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
                    if state and "__error__" in state:
                        detail = str(state["__error__"])
                        yield sse({"type": "error", "error": {"code": "request_failed", "message": detail}, "detail": detail})
                    else:
                        control = _request_control_snapshot(request_id) or {}
                        yield sse({
                            "type": "done",
                            "ask": ask,
                            "state": state,
                            "status": control.get("status", "completed"),
                            "cancel_requested": bool(control.get("cancel_requested")),
                        })
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
