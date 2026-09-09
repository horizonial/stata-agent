"""Framework-neutral synchronous chat application service.

``ChatService`` owns the use-case boundary around one agent-loop turn.  It
receives all environment-specific resources through factories, so an HTTP
adapter, CLI, or desktop shell can share the same lifecycle semantics without
importing the FastAPI module or a process-global database path.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from ..events.schema import ACTOR_USER, EVENT_USER, Event
from ..harness.agent_loop import LoopResult, run_loop
from ..harness.cancellation import is_cancel_requested
from ..harness.research_turn import bootstrap_idea
from ..toolkit import Tool, ToolContext, default_tools


EventCallback = Callable[[dict[str, Any]], None]


class StoreProtocol(Protocol):
    """Small store surface needed by the synchronous chat use case."""

    def append(self, event: Event) -> Event:
        ...

    def close(self) -> None:
        ...


class ClosableProtocol(Protocol):
    def close(self) -> None:
        ...


StoreFactory = Callable[[], StoreProtocol]
ProviderFactory = Callable[[], Any]
ExecutorFactory = Callable[[StoreProtocol], Any | None]
ToolsFactory = Callable[[], Mapping[str, Tool]]
Bootstrapper = Callable[[StoreProtocol, str, str], None]
LoopRunner = Callable[..., LoopResult]
ContextFactory = Callable[..., ToolContext]


@dataclass(frozen=True)
class ChatTurnRequest:
    """Input for one synchronous chat turn.

    ``cancellation`` accepts ``CancellationToken``, ``threading.Event``, or
    another event-like object understood by the existing agent loop.  The
    optional fields let adapters carry context metadata without making this
    application layer depend on any transport.
    """

    idea: str
    text: str
    mode: str = "interactive"
    request_id: str | None = None
    cancellation: Any = None
    # Compatibility spelling used by the current streaming transport.  A
    # caller should normally prefer ``cancellation``; both are forwarded to
    # the same agent-loop boundary.
    cancel_event: Any = None
    on_event: EventCallback | None = None
    system: str | None = None
    skills: Sequence[Any] = ()
    privacy_mode: str | None = None
    workspace_id: str | None = None
    context_budget: Any = None


@dataclass(frozen=True)
class ChatTurnResult:
    """Stable, transport-neutral result for one chat turn."""

    request_id: str
    reply: str
    ask: str | None
    loop_result: LoopResult

    @property
    def cancelled(self) -> bool:
        return bool(self.loop_result.cancelled)

    @property
    def terminal_reason(self) -> str | None:
        return self.loop_result.terminal_reason

    @property
    def tool_calls(self) -> int:
        return self.loop_result.tool_calls


def _default_context(
    request: ChatTurnRequest,
    store: StoreProtocol,
    executor: Any | None,
    cancellation: Any,
) -> ToolContext:
    return ToolContext(
        idea=request.idea,
        workspace_id=request.workspace_id,
        context_budget=request.context_budget,
        store=store,
        executor=executor,
        privacy_mode=request.privacy_mode or "local_strict",
        cancellation=cancellation,
    )


def _default_bootstrap(store: StoreProtocol, idea: str, text: str) -> None:
    """Keep the existing bootstrap semantics without exposing SQLite here."""

    bootstrap_idea(store, idea, text)  # type: ignore[arg-type]


class ChatService:
    """Run one bounded chat turn with deterministic resource cleanup."""

    def __init__(
        self,
        *,
        store_factory: StoreFactory,
        provider_factory: ProviderFactory,
        executor_factory: ExecutorFactory | None = None,
        context_factory: ContextFactory | None = None,
        tools_factory: ToolsFactory | None = None,
        loop_runner: LoopRunner = run_loop,
        bootstrapper: Bootstrapper | None = None,
        goal_max_steps: int = 12,
        max_tool_calls: int = 32,
    ) -> None:
        if goal_max_steps < 1:
            raise ValueError("goal_max_steps must be positive")
        if max_tool_calls < 1:
            raise ValueError("max_tool_calls must be positive")
        self._store_factory = store_factory
        self._provider_factory = provider_factory
        self._executor_factory = executor_factory or (lambda _store: None)
        self._context_factory = context_factory or _default_context
        self._tools_factory = tools_factory or default_tools
        self._loop_runner = loop_runner
        self._bootstrapper = bootstrapper or _default_bootstrap
        self._goal_max_steps = goal_max_steps
        self._max_tool_calls = max_tool_calls

    def run(self, request: ChatTurnRequest, *, on_event: EventCallback | None = None) -> ChatTurnResult:
        """Execute one chat turn and close store/executor on every path."""

        self._validate_request(request)
        request_id = request.request_id or uuid.uuid4().hex
        callback = on_event or request.on_event
        cancellation = request.cancellation or request.cancel_event
        store: StoreProtocol | None = None
        executor: Any | None = None
        try:
            store = self._store_factory()
            if is_cancel_requested(cancellation):
                return self._cancelled_result(request_id, cancellation)

            self._bootstrapper(store, request.idea, request.text)
            store.append(
                Event(
                    idea_id=request.idea,
                    event_type=EVENT_USER,
                    actor=ACTOR_USER,
                    source=ACTOR_USER,
                    payload={"text": request.text, "request_id": request_id},
                )
            )
            if is_cancel_requested(cancellation):
                return self._cancelled_result(request_id, cancellation)

            provider = self._provider_factory()
            executor = self._executor_factory(store)
            context = self._context_factory(
                request=request,
                store=store,
                executor=executor,
                cancellation=cancellation,
            )
            tools = dict(self._tools_factory())
            result = self._loop_runner(
                store,
                provider,
                tools,
                context,
                user_text=request.text,
                system=request.system,
                max_steps=1 if request.mode == "interactive" else self._goal_max_steps,
                max_tool_calls=self._max_tool_calls,
                privacy_mode=request.privacy_mode,
                skills=list(request.skills),
                on_event=callback,
                cancellation=cancellation,
            )
            return ChatTurnResult(
                request_id=request_id,
                reply=str(result.ask or result.reply or ""),
                ask=result.ask,
                loop_result=result,
            )
        finally:
            self._close(executor)
            self._close(store)

    # Explicit aliases make the application contract pleasant for adapters
    # while keeping one implementation and one cleanup path.
    chat = run
    run_sync = run

    @staticmethod
    def _validate_request(request: ChatTurnRequest) -> None:
        if not isinstance(request.idea, str) or not request.idea.strip():
            raise ValueError("idea must not be empty")
        if not isinstance(request.text, str) or not request.text.strip():
            raise ValueError("text must not be empty")
        if request.mode not in {"interactive", "goal"}:
            raise ValueError("mode must be interactive or goal")

    @staticmethod
    def _cancelled_result(request_id: str, cancellation: Any) -> ChatTurnResult:
        loop_result = LoopResult(
            reply="",
            terminal_reason="cancelled",
            cancelled=True,
        )
        return ChatTurnResult(
            request_id=request_id,
            reply="",
            ask=None,
            loop_result=loop_result,
        )

    @staticmethod
    def _close(resource: Any | None) -> None:
        if resource is None:
            return
        close = getattr(resource, "close", None)
        if not callable(close):
            return
        try:
            close()
        except Exception:  # noqa: BLE001 - cleanup must not hide the use-case outcome
            return


__all__ = [
    "Bootstrapper",
    "ChatService",
    "ChatTurnRequest",
    "ChatTurnResult",
    "ClosableProtocol",
    "ContextFactory",
    "EventCallback",
    "ExecutorFactory",
    "LoopRunner",
    "ProviderFactory",
    "StoreFactory",
    "StoreProtocol",
    "ToolsFactory",
]
