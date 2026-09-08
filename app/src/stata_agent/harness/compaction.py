"""Append-only semantic compaction boundaries.

The ledger remains the source of truth.  A compaction boundary is a derived
checkpoint used by context projection; it never replaces, edits, or deletes
the events it summarizes.  The V2 implementation deliberately keeps the
format small and deterministic so a replay can validate it without a model.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable

from ..domain.reducers import Projection, fold, unclosed_operations
from ..events.schema import (
    ACTOR_ORCH,
    EVENT_AGENT_STEP,
    EVENT_APPROVAL_GRANT,
    EVENT_APPROVAL_REJECT,
    EVENT_APPROVAL_REQ,
    EVENT_COMPACTION,
    EVENT_RUN_FAILED,
    EVENT_RUN_REQ,
    EVENT_RUN_SUCCEEDED,
    EVENT_RUN_UNCERTAIN,
    EVENT_SPEC_FREEZE,
    EVENT_TOOL_CALL,
    EVENT_TOOL_DONE,
    EVENT_TOOL_INVOKED,
    EVENT_TOOL_RESULT,
    Event,
)
from ..storage.sqlite_store import SQLiteStore


COMPACTION_VERSION = 2


class CompactionValidationError(ValueError):
    """A boundary is malformed or would cut an incomplete semantic unit."""


@dataclass(frozen=True)
class Boundary:
    """A validated boundary event and its normalized payload."""

    event: Event
    payload: dict[str, Any]

    @property
    def seq(self) -> int:
        return int(self.event.seq or 0)

    @property
    def version(self) -> int:
        return int(self.payload.get("version", 1))

    @property
    def from_seq(self) -> int:
        return int(self.payload["from_seq"])

    @property
    def to_seq(self) -> int:
        return int(self.payload["to_seq"])

    @property
    def retained_from_seq(self) -> int | None:
        value = self.payload.get("retained_from_seq")
        return int(value) if isinstance(value, int) and not isinstance(value, bool) else None


def _seq(event: Event) -> int:
    return int(event.seq or 0)


def _as_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CompactionValidationError(f"boundary {field} 必须是整数")
    return int(value)


def _normalise_v1(payload: dict[str, Any]) -> dict[str, Any]:
    """Read the original boundary shape without rewriting it."""

    from_seq = _as_int(payload.get("from_seq"), "from_seq")
    to_seq = _as_int(payload.get("to_seq"), "to_seq")
    if from_seq <= 0 or to_seq < from_seq:
        raise CompactionValidationError("V1 boundary range 非法")
    summary = payload.get("summary", "")
    if not isinstance(summary, str):
        summary = str(summary)
    return {
        "version": 1,
        "from_seq": from_seq,
        "to_seq": to_seq,
        "summary": summary,
        "retained_from_seq": None,
    }


def _normalise_v2(payload: dict[str, Any]) -> dict[str, Any]:
    version = _as_int(payload.get("version"), "version")
    if version != COMPACTION_VERSION:
        raise CompactionValidationError(f"不支持的 compaction boundary version={version}")
    from_seq = _as_int(payload.get("from_seq"), "from_seq")
    to_seq = _as_int(payload.get("to_seq"), "to_seq")
    if from_seq <= 0 or to_seq < from_seq:
        raise CompactionValidationError("V2 boundary range 非法")
    previous = payload.get("previous_boundary_seq")
    if previous is not None:
        _as_int(previous, "previous_boundary_seq")
    summary = payload.get("summary")
    if not isinstance(summary, dict):
        raise CompactionValidationError("V2 boundary summary 必须是 object")
    required = {"objective", "constraints", "decisions", "open_items", "research_state", "evidence_refs"}
    if not required.issubset(summary):
        missing = sorted(required - set(summary))
        raise CompactionValidationError(f"V2 summary 缺字段: {missing}")
    for field in ("constraints", "decisions", "open_items", "evidence_refs"):
        if not isinstance(summary[field], list) or not all(isinstance(x, str) for x in summary[field]):
            raise CompactionValidationError(f"V2 summary.{field} 必须是字符串数组")
    if not isinstance(summary["objective"], str) or not isinstance(summary["research_state"], str):
        raise CompactionValidationError("V2 summary objective/research_state 必须是字符串")
    retained = payload.get("retained_from_seq")
    if retained is None:
        retained = to_seq + 1
    retained = _as_int(retained, "retained_from_seq")
    if retained < from_seq or retained > to_seq + 1:
        raise CompactionValidationError("V2 retained_from_seq 必须落在 [from_seq, to_seq + 1]")
    return {
        "version": COMPACTION_VERSION,
        "from_seq": from_seq,
        "to_seq": to_seq,
        "previous_boundary_seq": previous,
        "summary": summary,
        "retained_from_seq": retained,
        "reason": str(payload.get("reason") or "token_pressure"),
    }


def normalize_boundary_payload(payload: Any) -> dict[str, Any]:
    """Return a validated V1/V2 payload without changing the caller's object."""

    if not isinstance(payload, dict):
        raise CompactionValidationError("boundary payload 必须是 object")
    version = payload.get("version", 1)
    if version in (None, 1):
        return _normalise_v1(payload)
    return _normalise_v2(payload)


def _known_ids(events: Iterable[Event], proj: Projection | None = None) -> set[str]:
    known: set[str] = set()
    if proj is not None:
        known.update(str(x) for x in proj.cards)
        known.update(str(x) for x in proj.claims)
        known.update(str(x) for x in proj.runs)
    interesting = {
        "card_id", "claim_id", "run_id", "event_id", "operation_id", "call_id", "request_id", "id"
    }
    for event in events:
        if event.event_id:
            known.add(str(event.event_id))
        payload = event.payload if isinstance(event.payload, dict) else {}
        stack: list[Any] = [payload]
        while stack:
            value = stack.pop()
            if isinstance(value, dict):
                for key, item in value.items():
                    if key in interesting and isinstance(item, str) and item:
                        known.add(item)
                    if isinstance(item, (dict, list)):
                        stack.append(item)
            elif isinstance(value, list):
                stack.extend(value)
    return known


def validate_boundary(
    event: Event,
    *,
    events: Iterable[Event] | None = None,
    previous: Boundary | None = None,
) -> Boundary:
    """Validate one boundary and its references.

    Validation is intentionally independent of ``SQLiteStore.append`` so a
    replay/importer can reject a bad boundary before it is made visible.
    """

    if event.event_type != EVENT_COMPACTION:
        raise CompactionValidationError("不是 compaction.boundary 事件")
    payload = normalize_boundary_payload(event.payload)
    event_seq = _seq(event)
    if event_seq and payload["to_seq"] >= event_seq:
        raise CompactionValidationError("boundary to_seq 必须早于 boundary 事件自身")
    if previous is not None:
        if payload["from_seq"] != previous.to_seq + 1:
            raise CompactionValidationError("连续 boundary 的 from_seq 不单调")
        if payload.get("previous_boundary_seq") not in {None, previous.seq} and payload["version"] >= 2:
            raise CompactionValidationError("previous_boundary_seq 与上一个 boundary 不一致")
        if payload["to_seq"] <= previous.to_seq:
            raise CompactionValidationError("连续 boundary 的 to_seq 必须递增")
    event_list = list(events or ())
    if event_list:
        seqs = [_seq(item) for item in event_list if _seq(item) > 0]
        if seqs and (payload["from_seq"] < min(seqs) or payload["to_seq"] > max(seqs)):
            raise CompactionValidationError("boundary range 超出 ledger")
        known = _known_ids(event_list)
        if payload["version"] >= 2:
            unknown = sorted(set(payload["summary"]["evidence_refs"]) - known)
            if unknown:
                raise CompactionValidationError(f"summary 引用了不存在的 evidence id: {unknown}")
        safe_end = _safe_cut(event_list, payload["to_seq"], lower=payload["from_seq"])
        if safe_end != payload["to_seq"]:
            raise CompactionValidationError("boundary to_seq 切分了未闭合的 semantic unit")
    return Boundary(event=event, payload=payload)


def _validated_boundaries(events: list[Event]) -> list[Boundary]:
    boundaries: list[Boundary] = []
    for event in events:
        if event.event_type != EVENT_COMPACTION:
            continue
        try:
            boundary = validate_boundary(event, events=events, previous=boundaries[-1] if boundaries else None)
        except CompactionValidationError:
            # A malformed historical boundary must not become a source for a
            # new one.  Keep scanning so a later valid boundary can still be
            # read by a diagnostic tool, but do not silently trust it.
            continue
        boundaries.append(boundary)
    return boundaries


def latest_valid_boundary(events: Iterable[Event]) -> Boundary | None:
    """Return the latest monotonic V1/V2 boundary, or ``None``."""

    boundaries = _validated_boundaries(list(events))
    return boundaries[-1] if boundaries else None


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _first_text(payload: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = _text(payload.get(key))
        if value:
            return value
    return ""


def _objective(events: Iterable[Event], previous: dict[str, Any] | None = None) -> str:
    old = _text((previous or {}).get("objective"))
    candidate = old
    for event in events:
        payload = event.payload if isinstance(event.payload, dict) else {}
        value = _first_text(payload, ("question", "objective", "research_question"))
        if value:
            candidate = value
    return candidate


def _list_from_previous(previous: dict[str, Any] | None, field: str) -> list[str]:
    value = (previous or {}).get(field, [])
    return [str(item).strip() for item in value if str(item).strip()] if isinstance(value, list) else []


def _structured_summary(
    proj: Projection,
    events: list[Event],
    *,
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    constraints = _list_from_previous(previous, "constraints")
    decisions = _list_from_previous(previous, "decisions")
    open_items = _list_from_previous(previous, "open_items")

    for event in events:
        payload = event.payload if isinstance(event.payload, dict) else {}
        values = payload.get("constraints")
        if isinstance(values, list):
            constraints.extend(_text(value) for value in values if _text(value))
        value = _first_text(payload, ("constraint",))
        if value:
            constraints.append(value)
        if event.event_type == EVENT_AGENT_STEP:
            value = _first_text(payload, ("decision_summary", "summary"))
            if value:
                decisions.append(value)
        elif event.event_type in {EVENT_APPROVAL_GRANT, EVENT_APPROVAL_REJECT}:
            value = _first_text(payload, ("note", "reason", "decision"))
            if value:
                decisions.append(value)
        elif event.event_type == EVENT_SPEC_FREEZE:
            value = _first_text(payload, ("spec_id",))
            if value:
                decisions.append(f"spec frozen: {value}")
        if event.event_type == EVENT_APPROVAL_REQ:
            value = _first_text(payload, ("question", "action", "request_id"))
            if value:
                open_items.append(value)

    if proj.pending:
        open_items.extend(f"未闭合 operation: {op}" for op in sorted(unclosed_operations(proj)))

    def unique(values: list[str], cap: int = 32) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for value in values:
            value = str(value).strip()
            if value and value not in seen:
                out.append(value[:1000])
                seen.add(value)
        return out[-cap:]

    rs = proj.research_state
    refs: list[str] = []
    if rs:
        refs.extend(str(ref) for ref in rs.evidence_refs if str(ref).strip())
    refs.extend(sorted(str(card_id) for card_id in proj.cards))
    refs.extend(sorted(str(claim_id) for claim_id in proj.claims))
    refs.extend(sorted(str(run_id) for run_id in proj.runs))
    known = _known_ids(events, proj)
    refs = unique([ref for ref in refs if ref in known], cap=128)
    research_state = "; ".join([
        f"phase={proj.phase or ''}",
        f"current_spec={rs.current_spec_id if rs else ''}",
        f"current_family={rs.current_family_id if rs else ''}",
        f"sample_sig={rs.sample_sig if rs else ''}",
        f"claims={len(proj.claims)}",
        f"cards={len(proj.cards)}",
        f"runs={len(proj.runs)}",
        f"evidence_refs={len(refs)}",
    ])
    return {
        "objective": _objective(events, previous),
        "constraints": unique(constraints),
        "decisions": unique(decisions),
        "open_items": unique(open_items),
        "research_state": research_state,
        "evidence_refs": refs,
    }


def render_summary(summary: Any) -> str:
    """Stable human-readable rendering for V1 and V2 summaries."""

    if isinstance(summary, str):
        return summary
    if not isinstance(summary, dict):
        return _text(summary)
    lines = [
        f"目标: {summary.get('objective', '')}",
        f"约束: {_join_values(summary.get('constraints'))}",
        f"决定: {_join_values(summary.get('decisions'))}",
        f"待办: {_join_values(summary.get('open_items'))}",
        f"研究状态: {summary.get('research_state', '')}",
        f"证据 refs: {_join_values(summary.get('evidence_refs'))}",
        # This compatibility label is intentionally kept for V1 callers and
        # operators who search compacted text for confirmed claims.
        f"已确认 claim refs: {_join_values(summary.get('evidence_refs'))}",
    ]
    return "\n".join(lines)


def _join_values(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    return _text(value)


def _tool_key(event: Event) -> str:
    payload = event.payload if isinstance(event.payload, dict) else {}
    return str(payload.get("call_id") or payload.get("operation_id") or payload.get("tool") or "")


def _semantic_units(events: list[Event]) -> list[tuple[int, int]]:
    """Return complete semantic intervals (inclusive seqs).

    Intervals cover tool/run chains, approval request/resolution pairs, and
    ordinary standalone events.  A pending chain is intentionally omitted so
    compaction cannot claim a boundary in its middle.
    """

    units: list[tuple[int, int]] = []
    pending_tools: list[tuple[int, str]] = []
    pending_approvals: dict[str, int] = {}
    pending_runs: dict[str, int] = {}
    claimed: set[int] = set()

    for event in events:
        seq = _seq(event)
        if seq <= 0 or event.event_type == EVENT_COMPACTION:
            continue
        kind = event.event_type
        key = _tool_key(event)
        if kind in {EVENT_TOOL_INVOKED, EVENT_TOOL_CALL}:
            pending_tools.append((seq, key))
            continue
        if kind in {EVENT_TOOL_DONE, EVENT_TOOL_RESULT}:
            index = len(pending_tools) - 1
            if key:
                for i in range(len(pending_tools) - 1, -1, -1):
                    if pending_tools[i][1] == key:
                        index = i
                        break
            if pending_tools:
                start, _ = pending_tools.pop(index)
                units.append((start, seq))
                claimed.update(range(start, seq + 1))
            continue
        if kind == EVENT_RUN_REQ:
            payload = event.payload if isinstance(event.payload, dict) else {}
            op = str(event.operation_id or payload.get("operation_id") or payload.get("run_id") or "")
            if op:
                pending_runs[op] = seq
            continue
        if kind in {EVENT_RUN_SUCCEEDED, EVENT_RUN_FAILED, EVENT_RUN_UNCERTAIN}:
            payload = event.payload if isinstance(event.payload, dict) else {}
            op = str(event.operation_id or payload.get("operation_id") or payload.get("run_id") or "")
            run_start: int | None = pending_runs.pop(op) if op in pending_runs else None
            if run_start is not None:
                units.append((run_start, seq))
                claimed.update(range(run_start, seq + 1))
            continue
        if kind == EVENT_APPROVAL_REQ:
            payload = event.payload if isinstance(event.payload, dict) else {}
            request_id = str(payload.get("request_id") or payload.get("id") or f"seq:{seq}")
            pending_approvals[request_id] = seq
            continue
        if kind in {EVENT_APPROVAL_GRANT, EVENT_APPROVAL_REJECT}:
            payload = event.payload if isinstance(event.payload, dict) else {}
            request_id = str(payload.get("request_id") or payload.get("id") or "")
            approval_start: int | None = (
                pending_approvals.pop(request_id) if request_id in pending_approvals else None
            )
            if approval_start is None and pending_approvals:
                request_id, approval_start = next(iter(pending_approvals.items()))
                del pending_approvals[request_id]
            if approval_start is not None:
                units.append((approval_start, seq))
                claimed.update(range(approval_start, seq + 1))
            continue
        if seq not in claimed:
            units.append((seq, seq))

    merged: list[list[int]] = []
    for start, end in sorted(units):
        if merged and start <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def _safe_cut(events: list[Event], candidate: int, *, lower: int) -> int | None:
    """Find the latest endpoint that closes every chain before it.

    Looking only at complete units is not enough: an unresolved approval at
    seq=3 followed by an ordinary message at seq=4 would otherwise make 4
    look safe.  Walk candidates backwards and reject any range containing a
    pending execution/approval/tool chain.
    """

    available = sorted({
        _seq(event) for event in events
        if lower <= _seq(event) <= candidate and event.event_type != EVENT_COMPACTION
    }, reverse=True)
    units = _semantic_units([e for e in events if lower <= _seq(e) <= candidate])
    complete_ends = {end for _start, end in units}
    for endpoint in available:
        if endpoint not in complete_ends:
            continue
        pending_tools = 0
        pending_approvals: set[str] = set()
        pending_runs: set[str] = set()
        for event in events:
            seq = _seq(event)
            if seq < lower or seq > endpoint or event.event_type == EVENT_COMPACTION:
                continue
            kind = event.event_type
            if kind in {EVENT_TOOL_INVOKED, EVENT_TOOL_CALL}:
                pending_tools += 1
            elif kind in {EVENT_TOOL_DONE, EVENT_TOOL_RESULT}:
                pending_tools = max(0, pending_tools - 1)
            elif kind == EVENT_APPROVAL_REQ:
                payload = event.payload if isinstance(event.payload, dict) else {}
                pending_approvals.add(str(payload.get("request_id") or payload.get("id") or f"seq:{seq}"))
            elif kind in {EVENT_APPROVAL_GRANT, EVENT_APPROVAL_REJECT}:
                payload = event.payload if isinstance(event.payload, dict) else {}
                request_id = str(payload.get("request_id") or payload.get("id") or "")
                if request_id in pending_approvals:
                    pending_approvals.remove(request_id)
                elif pending_approvals:
                    pending_approvals.pop()
            elif kind == EVENT_RUN_REQ:
                payload = event.payload if isinstance(event.payload, dict) else {}
                pending_runs.add(str(event.operation_id or payload.get("operation_id") or payload.get("run_id") or ""))
            elif kind in {EVENT_RUN_SUCCEEDED, EVENT_RUN_FAILED, EVENT_RUN_UNCERTAIN}:
                payload = event.payload if isinstance(event.payload, dict) else {}
                op = str(event.operation_id or payload.get("operation_id") or payload.get("run_id") or "")
                pending_runs.discard(op)
        if not pending_tools and not pending_approvals and not pending_runs:
            return endpoint
    return None


def _adjust_tail_start(events: list[Event], requested: int, *, lower: int, upper: int) -> int:
    value = max(lower, min(int(requested), upper + 1))
    for start, end in _semantic_units([e for e in events if lower <= _seq(e) <= upper]):
        if start < value <= end:
            return start
    return value


def _projection_at(events: list[Event], idea: str, seq: int) -> Projection:
    selected = [event for event in events if _seq(event) <= seq]
    return fold(selected, idea_id=idea)


def compact(
    store: SQLiteStore,
    idea: str,
    *,
    reason: str = "token_pressure",
    phase_scope: str | None = None,
    retained_from_seq: int | None = None,
    upto_seq: int | None = None,
) -> dict[str, Any]:
    """Append one V2 boundary after the latest valid boundary.

    ``retained_from_seq`` marks the beginning of the raw overlap retained for
    context.  It may be inside the summarized range (the default), which lets
    the context assembler show a recent exact tail while still carrying a
    complete structured checkpoint.  ``upto_seq`` is an optional deterministic
    test/replay cut; unsafe endpoints are moved back to a complete semantic
    unit.
    """

    events = list(store.scan(idea))
    if not events:
        raise ValueError("无可压缩事件")
    previous = latest_valid_boundary(events)
    previous_to = previous.to_seq if previous else 0
    previous_event_seq = previous.seq if previous else 0
    source_start = previous_to + 1 if previous else min(_seq(event) for event in events if _seq(event) > 0)
    source_events = [
        event for event in events
        if source_start <= _seq(event) and event.event_type != EVENT_COMPACTION
    ]
    if not source_events:
        raise ValueError("上一个 boundary 之后没有新的事件")
    latest = max(_seq(event) for event in source_events)
    requested_end = latest if upto_seq is None else min(latest, int(upto_seq))
    if requested_end < source_start:
        raise ValueError("upto_seq 早于本次 compaction 起点")
    safe_end = _safe_cut(events, requested_end, lower=source_start)
    if safe_end is None:
        raise ValueError("没有可安全压缩的完整 semantic unit")
    to_seq = safe_end
    if to_seq <= previous_to:
        raise ValueError("本次 compaction 没有推进 ledger range")
    if retained_from_seq is None:
        retained_from_seq = max(source_start, to_seq - 5)
    retained = _adjust_tail_start(events, retained_from_seq, lower=source_start, upper=to_seq)
    previous_summary = previous.payload.get("summary") if previous else None
    if isinstance(previous_summary, str):
        previous_summary = {"legacy_summary": previous_summary}
    projection = _projection_at(events, idea, to_seq)
    delta_events = [
        event for event in events
        if source_start <= _seq(event) <= to_seq and event.event_type != EVENT_COMPACTION
    ]
    summary = _structured_summary(
        projection,
        delta_events,
        previous=previous_summary if isinstance(previous_summary, dict) else None,
    )
    payload: dict[str, Any] = {
        "version": COMPACTION_VERSION,
        "from_seq": source_start,
        "to_seq": to_seq,
        "previous_boundary_seq": previous_event_seq or None,
        "summary": summary,
        "retained_from_seq": retained,
        "reason": str(reason or "token_pressure"),
    }
    if phase_scope is not None:
        payload["phase_scope"] = str(phase_scope)
    candidate = Event(
        idea_id=idea,
        event_type=EVENT_COMPACTION,
        actor=ACTOR_ORCH,
        source=ACTOR_ORCH,
        phase=projection.phase,
        payload=payload,
    )
    normalize_boundary_payload(payload)
    seq = store.append(candidate)
    stored_events = list(store.scan(idea))
    stored = next((event for event in reversed(stored_events) if event.event_type == EVENT_COMPACTION), None)
    if stored is not None:
        try:
            validate_boundary(stored, events=stored_events, previous=previous)
        except CompactionValidationError as exc:
            raise ValueError(f"append 后 boundary 校验失败: {exc}") from exc
    rendered = render_summary(summary)
    return {
        "seq": seq,
        "version": COMPACTION_VERSION,
        "from_seq": source_start,
        "to_seq": to_seq,
        "previous_boundary_seq": previous_event_seq or None,
        "retained_from_seq": retained,
        # Keep the legacy string return for research_turn callers.  The
        # persisted payload is the authoritative structured V2 summary.
        "summary": rendered,
        "summary_payload": summary,
        "claim_ids": sorted(projection.claims),
    }


def build_context_after(store: SQLiteStore, idea: str) -> str:
    """Render a compacted checkpoint and a bounded raw tail for V1 callers."""

    from .context import build_context

    events = list(store.scan(idea))
    boundary = latest_valid_boundary(events)
    proj = store.project(idea)
    if boundary is None:
        return build_context(proj)
    payload = boundary.payload
    retained = payload.get("retained_from_seq")
    if not isinstance(retained, int):
        retained = boundary.seq + 1
    tail = [
        f"#seq{event.seq} {event.event_type}"
        for event in events
        if _seq(event) >= retained and event.event_type != EVENT_COMPACTION
    ][-6:]
    head = f"[compacted {payload.get('from_seq')}-{payload.get('to_seq')}]\n{render_summary(payload.get('summary', ''))}"
    return head + "\n[tail]\n" + "\n".join(tail)


__all__ = [
    "Boundary",
    "COMPACTION_VERSION",
    "CompactionValidationError",
    "build_context_after",
    "compact",
    "latest_valid_boundary",
    "normalize_boundary_payload",
    "render_summary",
    "validate_boundary",
]
