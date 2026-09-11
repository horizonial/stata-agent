"""Read-only, human-oriented projection of the append-only event ledger.

The ledger remains the source of truth.  This module deliberately contains no
HTTP or DOM code and never writes events.  It folds a bounded chronological
sequence into request cards for the Activity Timeline while retaining enough
opaque identifiers for an explicitly expanded technical view.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from heapq import heapreplace, heappush
from typing import Any

from ..events.schema import (
    EVENT_AGENT_STEP,
    EVENT_APPROVAL_GRANT,
    EVENT_APPROVAL_REJECT,
    EVENT_APPROVAL_REQ,
    EVENT_ARTIFACT,
    EVENT_BUDGET,
    EVENT_CARD_SIGNED,
    EVENT_CLAIM_RETRACT,
    EVENT_CLAIM_SIGNED,
    EVENT_CONTEXT_ASSEMBLED,
    EVENT_PROVIDER_TURN_COMPLETED,
    EVENT_PROVIDER_TURN_FAILED,
    EVENT_PROVIDER_TURN_STARTED,
    EVENT_RUN_FAILED,
    EVENT_RUN_REQ,
    EVENT_RUN_SUCCEEDED,
    EVENT_RUN_UNCERTAIN,
    EVENT_TOOL_CALL,
    EVENT_TOOL_DONE,
    EVENT_TOOL_INVOKED,
    EVENT_TOOL_RESULT,
    EVENT_USER,
    Event,
)


SCHEMA = "stata-agent.trace-activity.v1"
MAX_GROUPS = 50
MAX_EVENTS = 1200
MAX_STEPS = 64
MAX_CHILDREN = 32
MAX_TEXT = 240
MAX_TECHNICAL_IDS = 128

_ID_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,128}$")
_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,95}$")
_SECRET_RE = re.compile(
    r"(?i)(api[_ -]?key|access[_ -]?token|password|passwd|secret|bearer|authorization)"
    r"\s*[:=]\s*[^\s,;]+"
)
_PATH_RE = re.compile(r"(?i)(?:[A-Za-z]:\\|\\\\|/(?:home|Users|users|tmp|private)/)[^\s,;]+")


def _safe_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if _ID_RE.fullmatch(value) else None


def _safe_label(value: Any, fallback: str = "unknown") -> str:
    text = str(value or "").strip()
    return text if _LABEL_RE.fullmatch(text) else fallback


def _redact_text(value: Any, *, limit: int = MAX_TEXT) -> str:
    """Bound user/model text before it crosses the activity API boundary."""

    if not isinstance(value, str):
        return ""
    text = " ".join(value.split())
    text = _SECRET_RE.sub(lambda m: f"{m.group(1)}=[已隐藏]", text)
    text = _PATH_RE.sub("[本地路径]", text)
    return text[:limit]


def _seq(event: Event, fallback: int) -> int:
    value = event.seq
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else fallback


def _timestamp(event: Event) -> int | None:
    value = event.created_at
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _nonnegative_int(value: Any, maximum: int = 10_000_000) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if 0 <= parsed <= maximum else None


def _payload(event: Event) -> Mapping[str, Any]:
    return event.payload if isinstance(event.payload, Mapping) else {}


def _payload_id(event: Event, *keys: str) -> str | None:
    payload = _payload(event)
    for key in keys:
        value = _safe_id(payload.get(key))
        if value is not None:
            return value
    return None


def _nested_payload_id(event: Event, container: str, *keys: str) -> str | None:
    """Read an allow-listed identity from the signed object envelope.

    Evidence events persist the immutable card/claim under ``card`` or
    ``claim`` rather than duplicating the id at the event root.  Keeping this
    helper narrowly allow-listed lets expanded technical details expose the
    durable id without serialising the signed object itself.
    """

    nested = _payload(event).get(container)
    if not isinstance(nested, Mapping):
        return None
    for key in keys:
        value = _safe_id(nested.get(key))
        if value is not None:
            return value
    return None


def _duration_ms(payload: Mapping[str, Any]) -> int | None:
    for key in ("duration_ms", "latency_ms", "duration"):
        value = _nonnegative_int(payload.get(key), maximum=86_400_000)
        if value is not None:
            return value
    return None


def _status_label(status: str) -> str:
    return {
        "running": "进行中",
        "completed": "已完成",
        "failed": "失败",
        "paused": "已暂停",
        "cancelled": "已停止",
        "uncertain": "状态待确认",
        "pending": "未闭合",
    }.get(status, "状态未知")


def _step_status_from_reason(reason: Any) -> str:
    value = str(reason or "").strip().lower()
    if value in {"cancelled", "cancel_requested", "run_cancelled"}:
        return "cancelled"
    if value in {"context_budget", "max_steps", "tool_calls", "budget_limit", "paused"}:
        return "paused"
    if value in {"model_stop", "ask_user", "completed", "success"}:
        return "completed"
    if value in {"uncertain", "run_uncertain"}:
        return "uncertain"
    return "failed"


def _safe_failure_code(payload: Mapping[str, Any]) -> str | None:
    candidate = payload.get("error")
    if isinstance(candidate, Mapping):
        for key in ("code", "type", "error_code", "failure_code"):
            value = _safe_label(candidate.get(key), "")
            if value:
                return value.lower()
    for key in ("error_code", "failure_code", "terminal_reason", "reason", "kind"):
        value = _safe_label(payload.get(key), "")
        if value:
            return value.lower()
    return None


def _safe_machine_available(payload: Mapping[str, Any]) -> bool:
    machine = payload.get("machine")
    return isinstance(machine, Mapping) and bool(machine)


def _command_family(payload: Mapping[str, Any]) -> str:
    """Map executor metadata to a closed, non-sensitive Stata label."""

    code_head = payload.get("code_head")
    if not isinstance(code_head, str):
        return "执行 Stata 命令"
    first = code_head.strip().lower().lstrip(";").split(maxsplit=1)[0] if code_head.strip() else ""
    if first in {"sysuse", "use", "import"}:
        return "载入示例数据" if first == "sysuse" else "载入数据"
    if first in {"describe", "codebook", "ds", "inspect"}:
        return "查看数据结构"
    if first in {"summarize", "sum", "tabstat"}:
        return "生成描述统计"
    if first in {"reg", "regress", "reghdfe", "ivregress", "xtreg", "logit", "probit"}:
        return "执行回归估计"
    if first in {"count", "tab", "table"}:
        return "汇总样本信息"
    if first in {"di", "display"} and "sta_env" in code_head.lower():
        return "环境核验"
    return "执行 Stata 命令"


@dataclass(frozen=True, slots=True)
class TraceTechnicalDetails:
    """Opaque identities available only after the user expands details."""

    request_id: str | None = None
    correlation_id: str | None = None
    operation_id: str | None = None
    run_id: str | None = None
    call_id: str | None = None
    card_id: str | None = None
    claim_id: str | None = None
    event_seqs: tuple[int, ...] = ()
    fingerprint: str | None = None

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key in (
            "request_id", "correlation_id", "operation_id", "run_id", "call_id", "card_id", "claim_id", "fingerprint"
        ):
            value = getattr(self, key)
            if value is not None:
                result[key] = value
        if self.event_seqs:
            result["event_seqs"] = list(self.event_seqs[:MAX_TECHNICAL_IDS])
        return result

    to_dict = as_dict


@dataclass(slots=True)
class TraceActivityStep:
    ordinal: int
    kind: str
    label: str
    summary: str
    status: str = "pending"
    started_at: int | None = None
    completed_at: int | None = None
    duration_ms: int | None = None
    children: list[dict[str, Any]] = field(default_factory=list)
    warning: str | None = None
    links: dict[str, str] = field(default_factory=dict)
    technical: TraceTechnicalDetails = field(default_factory=TraceTechnicalDetails)
    _event_seqs: list[int] = field(default_factory=list, repr=False)

    def add_event(self, event: Event) -> None:
        value = _seq(event, 0)
        if value and value not in self._event_seqs:
            self._event_seqs.append(value)
            self._event_seqs.sort()
        timestamp = _timestamp(event)
        if timestamp is not None:
            if self.started_at is None or timestamp < self.started_at:
                self.started_at = timestamp
            if self.completed_at is None or timestamp > self.completed_at:
                self.completed_at = timestamp
        duration = _duration_ms(_payload(event))
        if duration is not None:
            self.duration_ms = duration if self.duration_ms is None else max(self.duration_ms, duration)

    def as_dict(self) -> dict[str, Any]:
        technical = self.technical.as_dict()
        # Sequence numbers are useful when a support operator expands a
        # semantic step.  Keep them in the technical section so collapsed
        # cards remain free of ledger internals.
        if self._event_seqs:
            technical.setdefault("event_seqs", list(self._event_seqs[:MAX_TECHNICAL_IDS]))
        result: dict[str, Any] = {
            "ordinal": self.ordinal,
            "kind": self.kind,
            "label": self.label,
            "summary": _redact_text(self.summary),
            "status": self.status,
            "status_label": _status_label(self.status),
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_ms": self.duration_ms,
            "children": list(self.children[:MAX_CHILDREN]),
            "technical": technical,
        }
        if self.warning:
            result["warning"] = _redact_text(self.warning)
        if self.links:
            result["links"] = dict(self.links)
        return result

    to_dict = as_dict


@dataclass(slots=True)
class TraceActivityGroup:
    request_id: str
    display_ordinal: int
    title: str
    started_at: int | None = None
    completed_at: int | None = None
    duration_ms: int | None = None
    status: str = "running"
    steps: list[TraceActivityStep] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=lambda: {
        "provider_turns": 0, "tools": 0, "runs": 0, "evidence": 0, "warnings": 0,
    })
    budget: dict[str, int] = field(default_factory=dict)
    context: dict[str, Any] = field(default_factory=dict)
    evidence_status: str = "none"
    technical_ids: dict[str, Any] = field(default_factory=dict)
    _events: list[Event] = field(default_factory=list, repr=False)

    @property
    def status_label(self) -> str:
        return _status_label(self.status)

    def _finalize(self) -> None:
        """Sort and bound the visible read model after event folding."""

        self.steps.sort(key=lambda step: min(step._event_seqs) if step._event_seqs else 0)
        for ordinal, step in enumerate(self.steps[:MAX_STEPS], start=1):
            step.ordinal = ordinal
            if len(step.children) > MAX_CHILDREN:
                step.children = step.children[:MAX_CHILDREN]
                step.warning = step.warning or "内部步骤已按上限折叠。"
        terminal_statuses = [
            step.status
            for step in self.steps
            if step.kind in {"system", "failure"}
            and step.status in {"completed", "failed", "paused", "cancelled", "uncertain"}
        ]
        if terminal_statuses:
            self.status = terminal_statuses[-1]
        elif any(step.status == "uncertain" for step in self.steps):
            self.status = "uncertain"
        elif any(step.status == "failed" for step in self.steps):
            self.status = "failed"
        else:
            non_context_steps = [step for step in self.steps if step.kind != "context"]
            self.status = "completed" if non_context_steps and all(
                step.status == "completed" for step in non_context_steps
            ) else "running"
        if self.started_at is not None and self.completed_at is not None:
            self.duration_ms = max(0, (self.completed_at - self.started_at) * 1000)
        else:
            durations = [step.duration_ms for step in self.steps if step.duration_ms is not None]
            if durations:
                self.duration_ms = sum(durations)

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "request_id": self.request_id,
            "display_ordinal": self.display_ordinal,
            "title": _redact_text(self.title),
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_ms": self.duration_ms,
            "status": self.status,
            "status_label": self.status_label,
            "counts": dict(self.counts),
            "budget": dict(self.budget),
            "context": dict(self.context),
            "evidence_status": self.evidence_status,
            "steps": [step.as_dict() for step in self.steps[:MAX_STEPS]],
            "technical_ids": dict(self.technical_ids),
        }
        return result

    to_dict = as_dict


@dataclass(frozen=True, slots=True)
class TraceActivityQuery:
    limit: int = 20
    before_seq: int | None = None
    category: str = "all"
    status: str | None = None
    search: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.limit, bool) or not isinstance(self.limit, int):
            raise ValueError("limit must be an integer")
        if not 1 <= self.limit <= MAX_GROUPS:
            raise ValueError(f"limit must be between 1 and {MAX_GROUPS}")
        if self.before_seq is not None and (
            isinstance(self.before_seq, bool) or not isinstance(self.before_seq, int) or self.before_seq < 0
        ):
            raise ValueError("before_seq must be a non-negative integer")
        if self.category not in {"all", "model", "tool", "run", "evidence", "approval", "failure"}:
            raise ValueError("unknown trace activity category")
        if self.status is not None and self.status not in {
            "running", "completed", "failed", "paused", "uncertain", "cancelled", "pending",
        }:
            raise ValueError("unknown trace activity status")
        if self.search is not None and len(str(self.search)) > 120:
            raise ValueError("search is too long")


@dataclass(frozen=True, slots=True)
class TraceActivityPage:
    items: tuple[TraceActivityGroup, ...]
    next_before_seq: int | None
    total_groups: int
    workspace: str | None
    legacy_uncorrelated_count: int
    truncated: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "items": [item.as_dict() for item in self.items],
            "next_before_seq": self.next_before_seq,
            "total_groups": self.total_groups,
            # `total` is a compatibility-friendly alias for clients that use
            # the raw trace response convention.
            "total": self.total_groups,
            "workspace": self.workspace,
            "legacy_uncorrelated_count": self.legacy_uncorrelated_count,
            "truncated": self.truncated,
        }

    to_dict = as_dict


class TraceProjectionService:
    """Deterministic event-to-activity read model."""

    def __init__(self, *, max_events: int = MAX_EVENTS) -> None:
        if isinstance(max_events, bool) or not 1 <= int(max_events) <= MAX_EVENTS:
            raise ValueError("max_events out of bounds")
        self.max_events = int(max_events)

    def project(
        self,
        events: Iterable[Event],
        *,
        workspace: str | None = None,
        query: TraceActivityQuery | None = None,
        limit: int | None = None,
        before_seq: int | None = None,
        category: str = "all",
        status: str | None = None,
        search: str | None = None,
    ) -> TraceActivityPage:
        if query is None:
            query = TraceActivityQuery(
                limit=20 if limit is None else limit,
                before_seq=before_seq,
                category=category,
                status=status,
                search=search,
            )
        elif any(value is not None for value in (limit, before_seq, search)) or category != "all" or status is not None:
            raise ValueError("query cannot be combined with individual filters")

        ordered, truncated = self._bounded_events(events)
        by_correlation: dict[str, list[Event]] = {}
        legacy: list[Event] = []
        for event in ordered:
            correlation = _safe_id(event.correlation_id)
            if correlation is None:
                legacy.append(event)
            else:
                by_correlation.setdefault(correlation, []).append(event)

        groups = [self._group(correlation, rows, ordered) for correlation, rows in by_correlation.items()]
        groups.sort(key=self._group_first_seq, reverse=True)
        matched = [group for group in groups if self._matches(group, query)]
        total_groups = len(matched)
        filtered = matched
        if query.before_seq is not None:
            filtered = [
                group for group in filtered
                if self._group_first_seq(group) < query.before_seq
            ]
        page = filtered[: query.limit]
        next_before = self._group_first_seq(page[-1]) if len(page) == query.limit and len(filtered) > query.limit else None
        return TraceActivityPage(
            items=tuple(page),
            next_before_seq=next_before,
            # Totals describe the complete filtered result set, not only the
            # current cursor page.  This keeps pagination honest when older
            # request groups are loaded.
            total_groups=total_groups,
            workspace=_safe_id(workspace),
            legacy_uncorrelated_count=len(legacy),
            truncated=truncated,
        )

    def build(self, events: Iterable[Event], **kwargs: Any) -> dict[str, Any]:
        """Convenience dict API for transports and compatibility callers."""

        return self.project(events, **kwargs).as_dict()

    project_dict = build

    def _bounded_events(self, events: Iterable[Event]) -> tuple[list[Event], bool]:
        # Keep only the newest bounded window while iterating.  This avoids
        # materialising an unbounded ledger in the application read model.
        heap: list[tuple[tuple[int, int], Event]] = []
        seen = 0
        for index, event in enumerate(events, start=1):
            seen += 1
            key = (_seq(event, index), index)
            item = (key, event)
            if len(heap) < self.max_events:
                heappush(heap, item)
            elif key > heap[0][0]:
                heapreplace(heap, item)
        heap.sort(key=lambda item: item[0])
        return [event for _, event in heap], seen > self.max_events

    @staticmethod
    def _group_first_seq(group: TraceActivityGroup) -> int:
        return min((_seq(event, 0) for event in group._events), default=0)

    def _matches(self, group: TraceActivityGroup, query: TraceActivityQuery) -> bool:
        if query.status and group.status != query.status:
            return False
        if query.category == "model" and group.counts.get("provider_turns", 0) == 0:
            return False
        if query.category == "tool" and group.counts.get("tools", 0) == 0:
            return False
        if query.category == "run" and group.counts.get("runs", 0) == 0:
            return False
        if query.category == "evidence" and group.counts.get("evidence", 0) == 0:
            return False
        if query.category == "approval" and not any(event.event_type in {EVENT_APPROVAL_REQ, EVENT_APPROVAL_GRANT, EVENT_APPROVAL_REJECT} for event in group._events):
            return False
        if query.category == "failure" and group.status not in {"failed", "uncertain", "cancelled", "paused"}:
            return False
        if query.search:
            needle = _redact_text(query.search, limit=120).lower()
            haystack = " ".join(
                [group.title, group.status_label]
                + [step.label + " " + step.summary for step in group.steps]
            ).lower()
            if needle not in haystack:
                return False
        return True

    def _group(self, correlation: str, events: list[Event], all_events: list[Event]) -> TraceActivityGroup:
        events = sorted(events, key=lambda event: _seq(event, 0))
        display_ordinal = self._request_ordinal(correlation, all_events)
        title = self._title(events)
        group = TraceActivityGroup(request_id=correlation, display_ordinal=display_ordinal, title=title, _events=events)
        timestamps = [timestamp for event in events if (timestamp := _timestamp(event)) is not None]
        group.started_at = min(timestamps, default=None)
        group.completed_at = max(timestamps, default=None)
        group.technical_ids["correlation_id"] = correlation
        group.technical_ids["event_seqs"] = [
            _seq(event, 0) for event in events[:MAX_TECHNICAL_IDS] if _seq(event, 0) > 0
        ]
        first_user = next((event for event in events if event.event_type == EVENT_USER), None)
        if first_user is not None:
            request_id = _payload_id(first_user, "request_id")
            if request_id:
                group.technical_ids["request_id"] = request_id

        step_by_key: dict[tuple[str, str], TraceActivityStep] = {}
        run_steps: dict[str, TraceActivityStep] = {}
        # A provider retry is a physical attempt of the same logical model
        # turn.  Keep one visible step per turn while retaining every event
        # sequence in its technical details.
        provider_steps: dict[int, TraceActivityStep] = {}
        provider_attempts: dict[int, set[int]] = {}
        context_last: tuple[Any, ...] | None = None
        context_step: TraceActivityStep | None = None
        tool_ord = run_ord = 0
        evidence_cards = 0
        claims = 0
        has_structured = False
        executed = False

        def add_step(key: tuple[str, str], step: TraceActivityStep, event: Event) -> TraceActivityStep:
            step.add_event(event)
            group.steps.append(step)
            step_by_key[key] = step
            return step

        for event in events:
            payload = _payload(event)
            kind = event.event_type
            if kind != EVENT_CONTEXT_ASSEMBLED:
                # Deduplication is intentionally adjacency-based.  A later
                # identical snapshot after a model/tool event is a new
                # context observation, not another repeat of the old one.
                context_last = None
                context_step = None
            if kind == EVENT_USER:
                continue
            if kind == EVENT_PROVIDER_TURN_STARTED:
                turn = _nonnegative_int(payload.get("turn_index"), 10_000) or 1
                attempt = _nonnegative_int(payload.get("attempt_index"), 100) or 1
                provider_attempts.setdefault(turn, set()).add(attempt)
                step = provider_steps.get(turn)
                if step is None:
                    step = TraceActivityStep(
                        ordinal=0, kind="model", label=f"模型回合 {turn}", summary="模型调用进行中", status="running",
                        technical=TraceTechnicalDetails(
                            correlation_id=correlation,
                            operation_id=_safe_id(event.operation_id),
                            event_seqs=(),
                        ),
                    )
                    provider_steps[turn] = step
                    group.steps.append(step)
                else:
                    # A retry reopens the same logical turn; do not expose a
                    # transient failed attempt as the request's final state.
                    step.status = "running"
                step.add_event(event)
                continue
            if kind in {EVENT_PROVIDER_TURN_COMPLETED, EVENT_PROVIDER_TURN_FAILED}:
                turn = _nonnegative_int(payload.get("turn_index"), 10_000) or 1
                attempt = _nonnegative_int(payload.get("attempt_index"), 100) or 1
                provider_attempts.setdefault(turn, set()).add(attempt)
                step = provider_steps.get(turn)
                if step is None:
                    step = TraceActivityStep(0, "model", f"模型回合 {turn}", "模型调用结果未知", "pending")
                    provider_steps[turn] = step
                    group.steps.append(step)
                step.add_event(event)
                if kind == EVENT_PROVIDER_TURN_COMPLETED:
                    response_kind = _safe_label(payload.get("response_kind"), "empty")
                    count = _nonnegative_int(payload.get("tool_call_count"), 256) or 0
                    step.status = "completed"
                    step.warning = None
                    step.summary = f"模型选择了 {count} 个工具" if response_kind == "tool_calls" else "模型生成答复"
                else:
                    step.status = "failed"
                    code = _safe_failure_code(payload) or "provider_error"
                    step.summary = f"模型调用失败（{code}）"
                    step.warning = "请查看技术详情或重试本轮。"
                    group.counts["warnings"] += 1
                attempts = len(provider_attempts.get(turn, ()))
                if attempts > 1:
                    step.summary = f"{step.summary}（{attempts} 次尝试）"
                continue
            if kind == EVENT_TOOL_INVOKED:
                tool_ord += 1
                tool = _safe_label(payload.get("tool"), "未知工具")
                label = "Stata 运行" if tool == "run_stata" else "读取运行结果" if tool == "read_artifact" else f"调用工具：{tool}"
                call_key = _payload_id(event, "call_id", "tool_id") or str(_seq(event, 0))
                existing = step_by_key.get(("tool", call_key))
                if existing is not None:
                    existing.add_event(event)
                    # A malformed/reordered ledger may contain tool.done
                    # before tool.invoked.  Keep the observed terminal fact
                    # instead of reopening it as an active call.
                    if existing.status not in {"completed", "failed"}:
                        existing.status = "running"
                    else:
                        existing.warning = existing.warning or "事件顺序异常，已按账本事实保留终态。"
                    continue
                step = TraceActivityStep(
                    0, "tool", f"{label} {tool_ord if tool in {'run_stata', 'read_artifact'} else ''}".strip(),
                    f"开始{label}", "running",
                    technical=TraceTechnicalDetails(
                        correlation_id=correlation,
                        call_id=_payload_id(event, "call_id", "tool_id"),
                        event_seqs=(),
                    ),
                )
                add_step(("tool", call_key), step, event)
                group.counts["tools"] += 1
                continue
            if kind == EVENT_TOOL_DONE:
                call_id = _payload_id(event, "call_id", "tool_id")
                step = step_by_key.get(("tool", call_id or ""))
                if step is None:
                    tool_ord += 1
                    tool = _safe_label(payload.get("tool"), "未知工具")
                    label = "Stata 运行" if tool == "run_stata" else "读取运行结果" if tool == "read_artifact" else f"调用工具：{tool}"
                    step = TraceActivityStep(
                        0, "tool", f"{label} {tool_ord if tool in {'run_stata', 'read_artifact'} else ''}".strip(),
                        "已完成" if payload.get("ok") is True else "工具执行失败", "completed" if payload.get("ok") is True else "failed",
                        technical=TraceTechnicalDetails(correlation_id=correlation, call_id=call_id, event_seqs=()),
                    )
                    add_step(("tool", call_id or str(_seq(event, 0))), step, event)
                    group.counts["tools"] += 1
                if step is not None:
                    step.add_event(event)
                    ok = payload.get("ok") is True
                    step.status = "completed" if ok else "failed"
                    step.summary = "已完成" if ok else "工具执行失败"
                    if not ok:
                        step.warning = "工具未成功完成，请查看失败原因。"
                        group.counts["warnings"] += 1
                continue
            if kind == EVENT_RUN_REQ:
                run_ord += 1
                requested_run_id = _payload_id(event, "run_id") or _safe_id(event.operation_id) or f"run-{run_ord}"
                operation_id = _safe_id(event.operation_id) or _payload_id(event, "operation_id")
                step = TraceActivityStep(
                    0, "run", f"Stata 运行 {run_ord}", "开始运行 Stata", "running",
                    technical=TraceTechnicalDetails(
                        correlation_id=correlation, operation_id=operation_id, run_id=requested_run_id, event_seqs=(),
                    ),
                )
                add_step(("run", requested_run_id), step, event)
                run_steps[requested_run_id] = step
                group.counts["runs"] += 1
                continue
            if kind in {EVENT_TOOL_CALL, EVENT_TOOL_RESULT}:
                run_id = _payload_id(event, "run_id")
                operation_id = _safe_id(event.operation_id) or _payload_id(event, "operation_id")
                parent = run_steps.get(run_id or "")
                if parent is None and operation_id:
                    parent = next((item for item in run_steps.values() if item.technical.operation_id == operation_id), None)
                if parent is None:
                    run_ord += 1
                    parent = TraceActivityStep(0, "run", f"Stata 运行 {run_ord}", "运行记录未闭合", "pending")
                    parent.technical = TraceTechnicalDetails(correlation_id=correlation, operation_id=operation_id, run_id=run_id)
                    group.steps.append(parent)
                    if run_id:
                        run_steps[run_id] = parent
                    group.counts["runs"] += 1
                parent.add_event(event)
                call_id = _payload_id(event, "call_id")
                if kind == EVENT_TOOL_CALL:
                    child = {
                        "label": _command_family(payload),
                        "status": "running",
                        "sequence": _seq(event, 0),
                        "technical": {"call_id": call_id, "operation_id": operation_id},
                    }
                    parent.children.append(child)
                else:
                    paired = False
                    for child in reversed(parent.children):
                        technical = child.get("technical")
                        child_call_id = technical.get("call_id") if isinstance(technical, Mapping) else None
                        if call_id is not None and child_call_id == call_id and child.get("status") == "running":
                            child["status"] = "completed" if payload.get("rc") in (None, 0) and not payload.get("is_error") else "failed"
                            child["completed_sequence"] = _seq(event, 0)
                            paired = True
                            break
                    if not paired:
                        # Do not guess which internal call produced a result
                        # when the durable call id is absent or unmatched.
                        parent.children.append(
                            {
                                "label": _command_family(payload),
                                "status": "completed" if payload.get("rc") in (None, 0) and not payload.get("is_error") else "failed",
                                "sequence": _seq(event, 0),
                                "completed_sequence": _seq(event, 0),
                                "warning": "未找到可配对的内部调用。",
                                "technical": {"call_id": call_id, "operation_id": operation_id},
                            }
                        )
                continue
            if kind in {EVENT_RUN_SUCCEEDED, EVENT_RUN_FAILED, EVENT_RUN_UNCERTAIN}:
                event_run_id = _payload_id(event, "run_id")
                parent = run_steps.get(event_run_id or "")
                if parent is None and event.operation_id:
                    parent = next((item for item in run_steps.values() if item.technical.operation_id == event.operation_id), None)
                if parent is None:
                    run_ord += 1
                    parent = TraceActivityStep(0, "run", f"Stata 运行 {run_ord}", "运行记录未闭合", "pending")
                    group.steps.append(parent)
                parent.add_event(event)
                if kind == EVENT_RUN_SUCCEEDED:
                    parent.status = "completed"
                    executed = True
                    parent.summary = "执行成功"
                    if _safe_machine_available(payload):
                        has_structured = True
                    else:
                        parent.warning = "执行成功，但未生成结构化可验证结果。"
                elif kind == EVENT_RUN_UNCERTAIN:
                    parent.status = "uncertain"
                    parent.summary = "执行状态待确认"
                    parent.warning = "请先核对运行记录，再决定是否继续。"
                    group.counts["warnings"] += 1
                else:
                    parent.status = "failed"
                    parent.summary = "执行失败"
                    parent.warning = "请查看安全失败原因或重试。"
                    group.counts["warnings"] += 1
                continue
            if kind == EVENT_CONTEXT_ASSEMBLED:
                snapshot_value = payload.get("budget_snapshot")
                snapshot: Mapping[str, Any] = snapshot_value if isinstance(snapshot_value, Mapping) else payload
                estimated_value = snapshot.get("estimated_input_tokens")
                if estimated_value is None:
                    estimated_value = snapshot.get("estimated_tokens")
                budget_value = snapshot.get("hard_limit_tokens")
                if budget_value is None:
                    budget_value = snapshot.get("budget_tokens")
                estimated = _nonnegative_int(estimated_value)
                budget = _nonnegative_int(budget_value)
                signature = (estimated, budget, _nonnegative_int(snapshot.get("tool_schema_tokens")), _nonnegative_int(snapshot.get("message_count")))
                if signature == context_last and context_step is not None:
                    context_step.add_event(event)
                    context_step.summary = f"上下文保持不变（{len(context_step._event_seqs)} 次）"
                else:
                    context_last = signature
                    label = f"上下文 {estimated:,} / {budget:,} tokens" if estimated is not None and budget is not None else "上下文准备"
                    context_step = TraceActivityStep(0, "context", label, label, "completed")
                    context_step.add_event(event)
                    group.steps.append(context_step)
                if estimated is not None:
                    previous_peak = _nonnegative_int(group.context.get("peak_tokens")) or 0
                    group.context["peak_tokens"] = max(previous_peak, estimated)
                    group.context["latest_tokens"] = estimated
                if budget is not None:
                    group.context["budget_tokens"] = budget
                continue
            if kind == EVENT_BUDGET:
                limit_value = _nonnegative_int(payload.get("limit"))
                used = _nonnegative_int(payload.get("tool_calls"))
                step = TraceActivityStep(0, "failure", "本轮预算", "达到本轮请求预算上限", "paused")
                step.add_event(event)
                group.steps.append(step)
                group.budget["limit"] = limit_value if limit_value is not None else 0
                if used is not None:
                    group.budget["used"] = used
                group.counts["warnings"] += 1
                continue
            if kind in {EVENT_CARD_SIGNED, EVENT_CLAIM_SIGNED, EVENT_CLAIM_RETRACT}:
                if kind == EVENT_CARD_SIGNED:
                    evidence_cards += 1
                    label = f"证据卡 {evidence_cards}"
                    card_id = _payload_id(event, "card_id") or _nested_payload_id(event, "card", "card_id")
                    claim_id = None
                elif kind == EVENT_CLAIM_SIGNED:
                    claims += 1
                    label = f"研究结论 {claims}"
                    card_id = None
                    claim_id = _payload_id(event, "claim_id") or _nested_payload_id(event, "claim", "claim_id")
                else:
                    label = "证据撤回"
                    card_id = None
                    claim_id = _payload_id(event, "claim_id") or _nested_payload_id(event, "claim", "claim_id")
                step = TraceActivityStep(0, "evidence", label, "已签入证据" if kind != EVENT_CLAIM_RETRACT else "证据已撤回", "completed" if kind != EVENT_CLAIM_RETRACT else "failed")
                step.technical = TraceTechnicalDetails(
                    correlation_id=correlation,
                    card_id=card_id,
                    claim_id=claim_id,
                )
                step.add_event(event)
                group.steps.append(step)
                continue
            if kind in {EVENT_APPROVAL_REQ, EVENT_APPROVAL_GRANT, EVENT_APPROVAL_REJECT}:
                step = TraceActivityStep(0, "approval", "审批请求" if kind == EVENT_APPROVAL_REQ else "审批已处理", "等待人工决定" if kind == EVENT_APPROVAL_REQ else "审批决定已记录", "pending" if kind == EVENT_APPROVAL_REQ else "completed")
                step.add_event(event)
                group.steps.append(step)
                continue
            if kind == EVENT_ARTIFACT:
                # Artifact events are intentionally semantic and bounded; the
                # actual file/detail remains behind its existing authorization.
                step = TraceActivityStep(0, "system", "读取运行结果", "结果产物已登记", "completed")
                step.add_event(event)
                group.steps.append(step)
                continue
            if kind == EVENT_AGENT_STEP:
                reason = payload.get("terminal_reason") or payload.get("stop_reason")
                if reason:
                    status_value = _step_status_from_reason(reason)
                    reply = _redact_text(payload.get("reply") or payload.get("ask") or payload.get("decision_summary"))
                    label = "已完成" if status_value == "completed" else "本轮已停止" if status_value == "cancelled" else "本轮已暂停" if status_value == "paused" else "本轮失败"
                    step = TraceActivityStep(0, "failure" if status_value not in {"completed"} else "system", label, reply or _status_label(status_value), status_value)
                    step.add_event(event)
                    if status_value != "completed":
                        group.counts["warnings"] += 1
                    group.steps.append(step)
                continue

        group.counts["provider_turns"] = len(provider_steps)
        group.counts["evidence"] = evidence_cards + claims
        if evidence_cards or claims:
            group.evidence_status = "verified"
        elif has_structured:
            group.evidence_status = "structured"
        elif executed:
            group.evidence_status = "executed_only"
        else:
            group.evidence_status = "none"
        if group.evidence_status != "verified":
            # A model-authored sentence is not proof of a signature.  Keep
            # the raw reply in the conversation view, but prevent an
            # activity card from repeating an unsupported “已签入” claim.
            for step in group.steps:
                if step.kind == "system" and any(marker in step.summary for marker in ("签入", "证据链", "证据卡")):
                    step.summary = "答复已生成；证据状态以实际签名事件为准。"
                    step.warning = step.warning or "未发现对应的证据签名事件。"
        group._finalize()
        return group

    @staticmethod
    def _request_ordinal(correlation: str, all_events: list[Event]) -> int:
        correlations: list[str] = []
        first_seen: dict[str, int] = {}
        for index, event in enumerate(sorted(all_events, key=lambda item: _seq(item, 0)), start=1):
            current = _safe_id(event.correlation_id)
            if current is None:
                continue
            first_seen.setdefault(current, _seq(event, index))
            if event.event_type == EVENT_USER and current not in correlations:
                correlations.append(current)
        if correlation in correlations:
            return correlations.index(correlation) + 1
        # Groups without a user event remain deterministic and appear after
        # explicit requests; do not infer ownership from proximity.
        orphaned = sorted(
            ((first_seq, current) for current, first_seq in first_seen.items() if current not in correlations),
            key=lambda item: (item[0], item[1]),
        )
        for offset, (_, current) in enumerate(orphaned, start=1):
            if current == correlation:
                return len(correlations) + offset
        return len(correlations) + len(orphaned) + 1

    @staticmethod
    def _title(events: Sequence[Event]) -> str:
        user = next((event for event in events if event.event_type == EVENT_USER), None)
        if user is not None:
            text = _redact_text(_payload(user).get("text"), limit=120)
            if text:
                return text
        first = events[0].event_type if events else "未关联请求"
        return {
            EVENT_PROVIDER_TURN_STARTED: "模型请求",
            EVENT_RUN_REQ: "Stata 运行请求",
            EVENT_TOOL_INVOKED: "工具请求",
        }.get(first, "未命名请求")
__all__ = [
    "SCHEMA",
    "TraceActivityGroup",
    "TraceActivityPage",
    "TraceActivityQuery",
    "TraceActivityStep",
    "TraceProjectionService",
    "TraceTechnicalDetails",
]
