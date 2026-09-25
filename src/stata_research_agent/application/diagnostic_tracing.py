"""Fail-safe local span tracing over the non-authoritative Diagnostic sink."""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from contextvars import ContextVar, Token
from dataclasses import dataclass
from types import TracebackType
from typing import Literal

from .diagnostic_service import DiagnosticService
from .diagnostics import DiagnosticEventCandidate

SpanKind = Literal["internal", "client", "worker", "tool"]
_ERROR_STATUSES = {"error", "failed", "delivery_unknown", "integrity_violation"}


@dataclass(frozen=True, slots=True)
class DiagnosticSpanContext:
    trace_id: str
    span_id: str
    workspace_ref: str | None
    turn_id: str | None
    operation_id: str | None
    attempt_id: str | None


_CURRENT_SPAN: ContextVar[DiagnosticSpanContext | None] = ContextVar(
    "stata_agent_diagnostic_span", default=None
)


class DiagnosticSpan:
    """One bounded span. Closing diagnostics can never change application control flow."""

    def __init__(
        self,
        service: DiagnosticService,
        *,
        name: str,
        kind: SpanKind,
        component: str,
        process_role: str,
        workspace_ref: str | None,
        turn_id: str | None,
        operation_id: str | None,
        attempt_id: str | None,
        domain_ref_type: str,
        domain_ref_id: str | None,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        parent = _CURRENT_SPAN.get()
        self._service = service
        self._name = name
        self._kind = kind
        self._component = component
        self._process_role = process_role
        self._domain_ref_type = domain_ref_type
        self._domain_ref_id = domain_ref_id
        self._clock = monotonic_clock
        self._started = monotonic_clock()
        self._context = DiagnosticSpanContext(
            parent.trace_id if parent is not None else secrets.token_hex(16),
            secrets.token_hex(8),
            workspace_ref
            if workspace_ref is not None
            else (None if parent is None else parent.workspace_ref),
            turn_id if turn_id is not None else (None if parent is None else parent.turn_id),
            operation_id
            if operation_id is not None
            else (None if parent is None else parent.operation_id),
            attempt_id
            if attempt_id is not None
            else (None if parent is None else parent.attempt_id),
        )
        self._parent_span_id = None if parent is None else parent.span_id
        self._token: Token[DiagnosticSpanContext | None] | None = None
        self._closed = False
        self._status_code = "ok"
        self._error_type: str | None = None

    @property
    def context(self) -> DiagnosticSpanContext:
        return self._context

    def set_status(self, status_code: str) -> None:
        if not status_code or any(character.isspace() for character in status_code):
            raise ValueError("span status_code must be a stable token")
        self._status_code = status_code[:128]

    def set_domain_ref(self, ref_type: str, ref_id: str) -> None:
        self._domain_ref_type = ref_type[:128]
        self._domain_ref_id = ref_id[:512]

    def __enter__(self) -> DiagnosticSpan:
        if self._token is not None:
            raise RuntimeError("Diagnostic span cannot be entered twice")
        self._token = _CURRENT_SPAN.set(self._context)
        return self

    async def __aenter__(self) -> DiagnosticSpan:
        return self.__enter__()

    def __exit__(
        self,
        error_type: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        del error, traceback
        if error_type is not None:
            self._status_code = "error"
            self._error_type = error_type.__name__[:128]
        self.close()
        return False

    async def __aexit__(
        self,
        error_type: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        return self.__exit__(error_type, error, traceback)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        duration_ms = max(0.0, (self._clock() - self._started) * 1000.0)
        is_error = self._status_code in _ERROR_STATUSES
        try:
            self._service.record(
                DiagnosticEventCandidate(
                    "agent.span",
                    17 if is_error else 9,
                    "ERROR" if is_error else "INFO",
                    "SPAN_FAILED" if is_error else "SPAN_COMPLETED",
                    self._component,
                    self._process_role,
                    {
                        "span_name": self._name,
                        "span_kind": self._kind,
                        "status_code": self._status_code,
                        "domain_ref_type": self._domain_ref_type,
                        "domain_ref_id": self._domain_ref_id,
                        "error_type": self._error_type,
                    },
                    trace_id=self._context.trace_id,
                    span_id=self._context.span_id,
                    workspace_ref=self._context.workspace_ref,
                    turn_id=self._context.turn_id,
                    operation_id=self._context.operation_id,
                    attempt_id=self._context.attempt_id,
                    duration_ms=duration_ms,
                    parent_span_id=self._parent_span_id,
                )
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            pass
        finally:
            if self._token is not None:
                _CURRENT_SPAN.reset(self._token)
                self._token = None


class DiagnosticTracer:
    def __init__(self, service: DiagnosticService) -> None:
        self._service = service

    @staticmethod
    def current_context() -> DiagnosticSpanContext | None:
        return _CURRENT_SPAN.get()

    def span(
        self,
        name: str,
        *,
        kind: SpanKind = "internal",
        component: str,
        process_role: str = "main_service",
        workspace_ref: str | None = None,
        turn_id: str | None = None,
        operation_id: str | None = None,
        attempt_id: str | None = None,
        domain_ref_type: str,
        domain_ref_id: str | None = None,
    ) -> DiagnosticSpan:
        return DiagnosticSpan(
            self._service,
            name=name,
            kind=kind,
            component=component,
            process_role=process_role,
            workspace_ref=workspace_ref,
            turn_id=turn_id,
            operation_id=operation_id,
            attempt_id=attempt_id,
            domain_ref_type=domain_ref_type,
            domain_ref_id=domain_ref_id,
        )
