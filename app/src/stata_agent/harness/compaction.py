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
    EVENT_IDEA,
    EVENT_RUN_FAILED,
    EVENT_RUN_REQ,
    EVENT_RUN_SUCCEEDED,
    EVENT_RUN_UNCERTAIN,
    EVENT_SPEC_FREEZE,
    EVENT_STEERING,
    EVENT_TOOL_CALL,
    EVENT_TOOL_DONE,
    EVENT_TOOL_INVOKED,
    EVENT_TOOL_RESULT,
    EVENT_USER,
    Event,
)
from ..storage.sqlite_store import SQLiteStore
from .summary_service import (
    SUMMARY_MODES,
    SUMMARY_FIELDS,
    SUMMARY_PROMPT_VERSION,
    MAX_SOURCE_CHARS,
    MAX_SOURCE_ITEMS,
    MAX_SOURCE_TEXT_CHARS,
    CompactionSummaryProvider,
    CompactionSummaryRequest,
    SummarySource,
    SummaryValidationError,
    provenance_for_summary,
    source_digest,
    validate_model_summary,
    validate_summary_source,
)


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


@dataclass(frozen=True)
class CheckpointHealth:
    """Result of a pure checkpoint/recovery health probe.

    The probe deliberately exposes only stable projection signatures and
    reason codes.  It never returns summary text, event payloads, tool output,
    or provider material.  ``ok`` is the single release decision; callers can
    use ``bool(result)`` or ``result.as_dict()`` without depending on a
    particular storage implementation.
    """

    ok: bool
    issues: tuple[str, ...] = ()
    boundary_seq: int | None = None
    expected_projection: tuple | None = None
    recovered_projection: tuple | None = None

    def __bool__(self) -> bool:
        return self.ok

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "issues": list(self.issues),
            "boundary_seq": self.boundary_seq,
            "expected_projection": self.expected_projection,
            "recovered_projection": self.recovered_projection,
        }

    def __getitem__(self, key: str) -> Any:
        """Small mapping compatibility helper for status/telemetry callers."""

        return self.as_dict()[key]


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
    normalized = {
        "version": COMPACTION_VERSION,
        "from_seq": from_seq,
        "to_seq": to_seq,
        "previous_boundary_seq": previous,
        "summary": summary,
        "retained_from_seq": retained,
        "reason": str(payload.get("reason") or "token_pressure"),
    }
    # Optional model-summary metadata is deliberately a small allow-list.  It
    # makes the source provenance/audit mode readable on replay while ensuring
    # that the boundary normalizer never persists a prompt or provider body.
    mode = payload.get("summary_mode")
    if mode is not None:
        if not isinstance(mode, str) or mode not in SUMMARY_MODES:
            raise CompactionValidationError("summary_mode 无效")
        normalized["summary_mode"] = mode
    for key in ("provider", "provider_name", "prompt_version", "source_digest", "summary_error_code"):
        value = payload.get(key)
        if value is not None:
            if not isinstance(value, str) or not value.strip() or len(value) > 256:
                raise CompactionValidationError(f"{key} 无效")
            normalized[key] = value
    provenance = payload.get("summary_provenance")
    if provenance is not None:
        if not isinstance(provenance, dict) or set(provenance) - {
            "objective", "constraints", "decisions", "open_items"
        }:
            raise CompactionValidationError("summary_provenance 无效")
        clean_provenance: dict[str, list[str]] = {}
        for field, refs in provenance.items():
            if not isinstance(refs, list) or not all(
                isinstance(ref, str) and ref.strip() and len(ref) <= 160 for ref in refs
            ):
                raise CompactionValidationError("summary_provenance source ids 无效")
            clean_provenance[field] = list(dict.fromkeys(refs))
        normalized["summary_provenance"] = clean_provenance
    source_ids = payload.get("summary_source_ids")
    if source_ids is not None:
        if not isinstance(source_ids, list) or not all(
            isinstance(ref, str) and ref.strip() and len(ref) <= 160 for ref in source_ids
        ):
            raise CompactionValidationError("summary_source_ids 无效")
        normalized["summary_source_ids"] = list(dict.fromkeys(source_ids))
    return normalized


def normalize_boundary_payload(payload: Any) -> dict[str, Any]:
    """Return a validated V1/V2 payload without changing the caller's object."""

    if not isinstance(payload, dict):
        raise CompactionValidationError("boundary payload 必须是 object")
    version = payload.get("version", 1)
    if version in (None, 1):
        return _normalise_v1(payload)
    return _normalise_v2(payload)


def _canonical_events(events: Iterable[Event], *, upto_seq: int | None = None) -> list[Event]:
    """Return the canonical event view used by checkpoint validation.

    A boundary is a projection of the ledger *at* ``to_seq``.  Looking at
    events after that point would make a future claim/run appear to justify a
    stale checkpoint, so compaction validation must never use the full live
    ledger as its reference set.
    """

    result = [event for event in events if event.event_type != EVENT_COMPACTION]
    if upto_seq is not None:
        result = [event for event in result if 0 < _seq(event) <= upto_seq]
    return sorted(result, key=_seq)


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
        if _seq(event) > 0:
            known.add(f"seq:{_seq(event)}")
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
    elif payload["version"] >= 2 and payload.get("previous_boundary_seq") is not None:
        raise CompactionValidationError("首个 boundary 不得声明 previous_boundary_seq")
    event_list = list(events or ())
    if event_list:
        # Only the canonical prefix can prove a checkpoint.  In particular,
        # future claims must not make a stale boundary's evidence_refs look
        # valid after a partial/replayed scan.
        canonical_events = _canonical_events(event_list, upto_seq=payload["to_seq"])
        seqs = [_seq(item) for item in canonical_events if _seq(item) > 0]
        if not seqs:
            raise CompactionValidationError("boundary range 没有 canonical events")
        if payload["from_seq"] < min(seqs) or payload["to_seq"] > max(seqs):
            raise CompactionValidationError("boundary range 超出 ledger")
        known = _known_ids(canonical_events)
        if payload["version"] >= 2:
            unknown = sorted(set(payload["summary"]["evidence_refs"]) - known)
            if unknown:
                raise CompactionValidationError(f"summary 引用了不存在的 evidence id: {unknown}")
            provenance = payload.get("summary_provenance")
            if isinstance(provenance, dict):
                source_refs = {
                    str(ref)
                    for refs in provenance.values()
                    if isinstance(refs, list)
                    for ref in refs
                }
                unknown_sources = sorted(source_refs - known)
                if unknown_sources:
                    raise CompactionValidationError(
                        f"summary 引用了不存在的 source id: {unknown_sources}"
                    )
        safe_end = _safe_cut(canonical_events, payload["to_seq"], lower=payload["from_seq"])
        if safe_end != payload["to_seq"]:
            raise CompactionValidationError("boundary to_seq 切分了未闭合的 semantic unit")
        retained = payload.get("retained_from_seq")
        if payload["version"] >= 2 and retained is not None and retained <= payload["to_seq"]:
            adjusted = _adjust_tail_start(
                canonical_events,
                retained,
                lower=payload["from_seq"],
                upper=payload["to_seq"],
            )
            if adjusted != retained:
                raise CompactionValidationError("boundary retained_from_seq 切分了未闭合的 semantic unit")
        if payload["version"] >= 2:
            issues = _checkpoint_fidelity_issues(
                event,
                payload=payload,
                canonical_events=canonical_events,
                previous=previous,
            )
            if issues:
                raise CompactionValidationError("; ".join(issues))
    return Boundary(event=event, payload=payload)


def _validated_boundaries(events: list[Event]) -> list[Boundary]:
    boundaries: list[Boundary] = []
    for event in events:
        if event.event_type != EVENT_COMPACTION:
            continue
        try:
            boundary = validate_boundary(event, events=events, previous=boundaries[-1] if boundaries else None)
        except CompactionValidationError:
            # A malformed boundary terminates the trusted chain.  A later
            # boundary might be syntactically valid in isolation but cannot be
            # used as a recovery base because its predecessor is unknown.
            break
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
    research_state = _summary_state_text(proj, refs)
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


def _strict_tool_key(event: Event) -> str:
    """Resolve a tool identity without using the human-readable tool name."""

    payload = event.payload if isinstance(event.payload, dict) else {}
    return str(payload.get("call_id") or event.operation_id or payload.get("operation_id") or "")


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


def _semantic_boundary_issues(events: list[Event], *, lower: int, endpoint: int) -> list[str]:
    """Check closure and identity of every semantic chain in one prefix.

    ``_semantic_units`` is intentionally permissive for rendering legacy
    transcripts.  A write boundary is stricter: an unmatched terminal or a
    request resolved by the wrong id is not a safe checkpoint even when the
    surrounding events otherwise look like ordinary messages.
    """

    pending_tools: list[tuple[int, str]] = []
    pending_approvals: dict[str, int] = {}
    pending_runs: dict[str, int] = {}
    issues: list[str] = []
    for event in sorted(events, key=_seq):
        seq = _seq(event)
        if seq < lower or seq > endpoint or event.event_type == EVENT_COMPACTION:
            continue
        kind = event.event_type
        key = _strict_tool_key(event)
        if kind in {EVENT_TOOL_INVOKED, EVENT_TOOL_CALL}:
            if not key:
                issues.append("tool_start_missing_identity")
            else:
                pending_tools.append((seq, key))
            continue
        if kind in {EVENT_TOOL_DONE, EVENT_TOOL_RESULT}:
            if not pending_tools:
                issues.append("tool_terminal_without_start")
                continue
            if key:
                matches = [index for index, (_start, pending_key) in enumerate(pending_tools) if pending_key == key]
                if not matches:
                    issues.append("tool_terminal_identity_mismatch")
                    continue
                del pending_tools[matches[-1]]
            elif len(pending_tools) == 1:
                pending_tools.pop()
            else:
                issues.append("tool_terminal_ambiguous_identity")
            continue
        if kind == EVENT_APPROVAL_REQ:
            payload = event.payload if isinstance(event.payload, dict) else {}
            request_id = str(payload.get("request_id") or payload.get("id") or "")
            if not request_id:
                issues.append("approval_request_missing_identity")
            else:
                pending_approvals[request_id] = seq
            continue
        if kind in {EVENT_APPROVAL_GRANT, EVENT_APPROVAL_REJECT}:
            payload = event.payload if isinstance(event.payload, dict) else {}
            request_id = str(payload.get("request_id") or payload.get("id") or "")
            if not request_id:
                issues.append("approval_terminal_missing_identity")
            elif request_id not in pending_approvals:
                issues.append("approval_terminal_identity_mismatch")
            else:
                pending_approvals.pop(request_id)
            continue
        if kind == EVENT_RUN_REQ:
            payload = event.payload if isinstance(event.payload, dict) else {}
            operation = str(event.operation_id or payload.get("operation_id") or payload.get("run_id") or "")
            if not operation:
                issues.append("run_request_missing_identity")
            else:
                pending_runs[operation] = seq
            continue
        if kind in {EVENT_RUN_SUCCEEDED, EVENT_RUN_FAILED, EVENT_RUN_UNCERTAIN}:
            payload = event.payload if isinstance(event.payload, dict) else {}
            operation = str(event.operation_id or payload.get("operation_id") or payload.get("run_id") or "")
            if not operation or operation not in pending_runs:
                issues.append("run_terminal_identity_mismatch")
            else:
                pending_runs.pop(operation)

    if pending_tools:
        issues.append("tool_chain_unclosed")
    if pending_approvals:
        issues.append("approval_chain_unclosed")
    if pending_runs:
        issues.append("run_chain_unclosed")
    return list(dict.fromkeys(issues))


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
        if not _semantic_boundary_issues(events, lower=lower, endpoint=endpoint):
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


def _projection_signature(projection: Projection) -> tuple:
    """Return a stable, non-content projection signature for health checks."""

    state = projection.research_state
    refs = tuple(sorted(str(ref) for ref in (state.evidence_refs if state else [])))
    return (
        projection.idea_id,
        projection.phase,
        state.current_spec_id if state else None,
        state.current_family_id if state else None,
        state.sample_sig if state else None,
        tuple(sorted((str(claim_id), str(claim.status)) for claim_id, claim in projection.claims.items())),
        tuple(sorted(str(card_id) for card_id in projection.cards)),
        tuple(sorted((str(run_id), str(run.status)) for run_id, run in projection.runs.items())),
        refs,
    )


def _summary_state_text(projection: Projection, evidence_refs: list[str]) -> str:
    """Build the canonical research-state line used in V2 summaries."""

    state = projection.research_state
    return "; ".join([
        f"phase={projection.phase or ''}",
        f"current_spec={state.current_spec_id if state else ''}",
        f"current_family={state.current_family_id if state else ''}",
        f"sample_sig={state.sample_sig if state else ''}",
        f"claims={len(projection.claims)}",
        f"cards={len(projection.cards)}",
        f"runs={len(projection.runs)}",
        f"evidence_refs={len(evidence_refs)}",
    ])


def _summary_event_text(event: Event) -> str:
    """Extract one small narrative field without forwarding raw payloads."""

    payload = event.payload if isinstance(event.payload, dict) else {}
    keys: tuple[str, ...]
    if event.event_type == EVENT_IDEA:
        keys = ("question", "objective", "research_question")
    elif event.event_type in {EVENT_USER, EVENT_STEERING}:
        keys = ("text", "message", "content", "question")
    elif event.event_type in {EVENT_APPROVAL_GRANT, EVENT_APPROVAL_REJECT}:
        keys = ("note", "decision", "reason")
    elif event.event_type == EVENT_APPROVAL_REQ:
        keys = ("question", "action", "request_id")
    elif event.event_type == EVENT_AGENT_STEP:
        keys = ("decision_summary", "ask", "reply", "summary")
    elif event.event_type == EVENT_SPEC_FREEZE:
        keys = ("reason", "spec_id")
    else:
        return ""
    return _first_text(payload, keys)[:MAX_SOURCE_TEXT_CHARS]


def _summary_event_role(event: Event) -> str | None:
    if event.event_type in {EVENT_IDEA, EVENT_USER, EVENT_STEERING}:
        return "user_message"
    if event.event_type == EVENT_APPROVAL_GRANT:
        return "approved_decision"
    if event.event_type == EVENT_APPROVAL_REJECT:
        return "rejection_note"
    if event.event_type in {EVENT_APPROVAL_REQ, EVENT_AGENT_STEP}:
        return "assistant_message"
    if event.event_type == EVENT_SPEC_FREEZE:
        return "decision"
    return None


def _summary_sources(events: list[Event]) -> tuple[SummarySource, ...]:
    """Build a bounded source set from narrative events only.

    Tool/evidence events are intentionally absent.  We retain the first
    objective source plus the newest narrative sources when the delta is
    larger than the provider input budget.
    """

    candidates: list[SummarySource] = []
    for event in events:
        role = _summary_event_role(event)
        if role is None:
            continue
        text = _summary_event_text(event)
        if not text:
            continue
        seq = _seq(event)
        if seq <= 0:
            continue
        candidates.append(SummarySource(source_id=f"seq:{seq}", role=role, text=text))
    if len(candidates) > MAX_SOURCE_ITEMS:
        candidates = [candidates[0], *candidates[-(MAX_SOURCE_ITEMS - 1):]]
    bounded: list[SummarySource] = []
    remaining = MAX_SOURCE_CHARS
    for source in candidates:
        if remaining <= 0:
            break
        text = source.text[: min(MAX_SOURCE_TEXT_CHARS, remaining)]
        if not text:
            continue
        bounded.append(SummarySource(source_id=source.source_id, role=source.role, text=text))
        remaining -= len(text)
    return tuple(bounded)


def _summary_provider_name(summarizer: object) -> str:
    value = getattr(summarizer, "provider_name", None) or getattr(summarizer, "provider", None)
    if value is None:
        value = summarizer.__class__.__name__
    return str(value).strip() or "unknown"


def _model_summary_payload(
    deterministic: dict[str, Any],
    candidate: dict[str, object],
) -> dict[str, Any]:
    """Drop provenance wrappers while preserving the canonical V2 shape."""

    def text_of(value: object) -> str:
        return str(value.get("text", "")).strip() if isinstance(value, dict) else ""

    def texts_of(value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        return [text_of(item) for item in value if text_of(item)]

    # The candidate owns narrative wording.  The state/evidence fields are
    # copied only from the deterministic computation and never model output.
    return {
        "objective": text_of(candidate.get("objective")) or str(deterministic.get("objective") or ""),
        "constraints": texts_of(candidate.get("constraints")),
        "decisions": texts_of(candidate.get("decisions")),
        "open_items": texts_of(candidate.get("open_items")),
        "research_state": str(deterministic.get("research_state") or ""),
        "evidence_refs": [str(ref) for ref in deterministic.get("evidence_refs", [])],
    }


def _source_event(source_id: str, events: list[Event]) -> Event | None:
    """Resolve a summary provenance id against the canonical prefix only."""

    value = str(source_id).strip()
    if value.startswith("seq:"):
        try:
            seq = int(value[4:])
        except (TypeError, ValueError):
            return None
        return next((event for event in events if _seq(event) == seq), None)
    return next((event for event in events if event.event_id == value), None)


def _validate_summary_provenance(
    payload: dict[str, Any],
    *,
    summary: dict[str, Any],
    canonical_events: list[Event],
) -> None:
    """Validate model narrative provenance without trusting model text.

    ``summary_provenance`` is an audit index, not a second source of truth.
    Every id must resolve inside the compressed canonical prefix and its event
    type must be eligible for the narrative field it supports.
    """

    provenance = payload.get("summary_provenance")
    mode = payload.get("summary_mode")
    if provenance is None:
        if mode == "model_validated":
            raise CompactionValidationError("model summary 缺 summary_provenance")
        return
    if not isinstance(provenance, dict):
        raise CompactionValidationError("summary_provenance 必须是 object")

    allowed_roles = {
        "objective": {"user_message", "approved_decision"},
        "constraints": {"user_message", "approved_decision"},
        "decisions": {
            "user_message", "approved_decision", "assistant_message", "decision", "rejection_note"
        },
        "open_items": {
            "user_message", "approved_decision", "assistant_message", "decision", "rejection_note"
        },
    }
    expected_ids: set[str] = set()
    for field, refs in provenance.items():
        if field not in SUMMARY_FIELDS:
            raise CompactionValidationError("summary_provenance 字段无效")
        if not isinstance(refs, list) or not refs:
            raise CompactionValidationError(f"summary_provenance.{field} 不能为空")
        for ref in refs:
            if not isinstance(ref, str) or not ref.strip():
                raise CompactionValidationError("summary_provenance source id 无效")
            event = _source_event(ref, canonical_events)
            if event is None:
                raise CompactionValidationError(f"summary provenance 引用了不存在的 source id: {ref}")
            role = _summary_event_role(event)
            if role not in allowed_roles[field]:
                raise CompactionValidationError(f"summary provenance.{field} source category 不允许")
            expected_ids.add(ref)

    source_ids = payload.get("summary_source_ids")
    if source_ids is not None:
        if not isinstance(source_ids, list) or any(not isinstance(ref, str) for ref in source_ids):
            raise CompactionValidationError("summary_source_ids 无效")
        if set(source_ids) != expected_ids:
            raise CompactionValidationError("summary_source_ids 与 summary_provenance 不一致")

    # A model-validated field must have an audit source whenever it contains
    # narrative text.  Empty arrays are valid and need no source entry.
    if mode == "model_validated":
        for field in SUMMARY_FIELDS:
            value = summary.get(field)
            nonempty = bool(value.strip()) if field == "objective" and isinstance(value, str) else bool(value)
            if nonempty and field not in provenance:
                raise CompactionValidationError(f"model summary.{field} 缺 provenance")


def _checkpoint_fidelity_issues(
    event: Event,
    *,
    payload: dict[str, Any],
    canonical_events: list[Event],
    previous: Boundary | None,
) -> list[str]:
    """Return deterministic checkpoint fidelity failures for a V2 payload."""

    version = payload.get("version")
    if not isinstance(version, int) or version < COMPACTION_VERSION:
        return []
    idea = event.idea_id
    try:
        projection = _projection_at(canonical_events, idea, int(payload["to_seq"]))
    except Exception as exc:
        return [f"canonical projection 非法: {exc.__class__.__name__}"]

    previous_summary = previous.payload.get("summary") if previous else None
    if isinstance(previous_summary, str):
        previous_summary = {"legacy_summary": previous_summary}
    from_seq = int(payload["from_seq"])
    to_seq = int(payload["to_seq"])
    delta = [event for event in canonical_events if from_seq <= _seq(event) <= to_seq]
    expected = _structured_summary(
        projection,
        delta,
        previous=previous_summary if isinstance(previous_summary, dict) else None,
    )
    summary = payload.get("summary")
    if not isinstance(summary, dict):
        return ["V2 summary 不是 object"]
    issues: list[str] = []
    actual_objective = summary.get("objective")
    expected_objective = expected["objective"]
    if not isinstance(actual_objective, str) or (expected_objective and not actual_objective.strip()):
        issues.append("summary objective 丢失")
    elif expected_objective and actual_objective != expected_objective:
        # Model wording may be rewritten, but only when it has an eligible
        # provenance source.  Deterministic/fallback summaries must be exact.
        if payload.get("summary_mode") != "model_validated":
            issues.append("summary objective 与 canonical projection 不一致")
    elif not expected_objective and actual_objective:
        issues.append("summary objective 新增")

    if summary.get("research_state") != expected["research_state"]:
        issues.append("summary research_state 与 canonical projection 不一致")
    actual_refs = summary.get("evidence_refs")
    if actual_refs != expected["evidence_refs"]:
        issues.append("summary evidence_refs 与 canonical projection 不一致")
    try:
        _validate_summary_provenance(
            payload,
            summary=summary,
            canonical_events=canonical_events,
        )
    except CompactionValidationError as exc:
        issues.append(str(exc))
    return issues


def checkpoint_health(
    source: SQLiteStore | Iterable[Event],
    idea: str | None = None,
) -> CheckpointHealth:
    """Run the deterministic checkpoint/recovery health probe.

    ``source`` may be a live ``SQLiteStore`` or a replayable event iterable.
    For a store, the probe compares the store projection with a fresh fold of
    the canonical ledger, which makes the same check useful after close/reopen.
    The result contains only stable signatures and reason codes.
    """

    issues: list[str] = []
    try:
        if isinstance(source, SQLiteStore):
            if not idea:
                raise ValueError("health probe 需要 idea")
            events = list(source.scan(idea))
        else:
            events = list(source)
            if idea is None and events:
                idea = events[0].idea_id
        if not idea:
            return CheckpointHealth(ok=False, issues=("missing_idea",))
    except Exception as exc:
        return CheckpointHealth(
            ok=False,
            issues=(f"scan_failed:{exc.__class__.__name__}",),
        )

    # A generic replay iterable may contain more than one workspace/idea.  A
    # probe must never let a foreign event contaminate the projection it
    # claims to validate.
    events = [event for event in events if event.idea_id == idea]

    compaction_events = sorted(
        (event for event in events if event.event_type == EVENT_COMPACTION),
        key=_seq,
    )
    boundaries = _validated_boundaries(events)
    boundary = boundaries[-1] if boundaries else None
    if compaction_events and boundary is None:
        issues.append("no_valid_boundary")
    elif boundary is not None and _seq(boundary.event) != _seq(compaction_events[-1]):
        # The latest compaction is damaged or disconnected from the trusted
        # chain; recovery must stop at the prior valid checkpoint.
        issues.append("latest_boundary_invalid")

    expected_projection: tuple | None = None
    recovered_projection: tuple | None = None
    canonical = _canonical_events(events)
    try:
        current = fold(canonical, idea_id=idea)
        recovered_projection = _projection_signature(current)
    except Exception:
        issues.append("canonical_projection_invalid")
        current = None

    if boundary is not None:
        try:
            # Re-run exactly the append preflight against the latest boundary;
            # restart and live paths therefore share one validation contract.
            previous: Boundary | None = None
            for candidate in boundaries[:-1]:
                previous = candidate
            validate_boundary(boundary.event, events=events, previous=previous)
            prefix = _canonical_events(events, upto_seq=boundary.to_seq)
            expected_projection = _projection_signature(_projection_at(prefix, idea, boundary.to_seq))
        except Exception:
            issues.append("checkpoint_fidelity_failed")

    if isinstance(source, SQLiteStore) and current is not None:
        try:
            actual = source.project(idea)
            actual_signature = _projection_signature(actual)
            if actual_signature != recovered_projection:
                issues.append("recovery_projection_mismatch")
        except Exception as exc:
            # Keep the public health surface deterministic and content-free.
            issues.append(f"recovery_projection_failed:{exc.__class__.__name__}")

    return CheckpointHealth(
        ok=not issues,
        issues=tuple(dict.fromkeys(issues)),
        boundary_seq=boundary.seq if boundary else None,
        expected_projection=expected_projection,
        recovered_projection=recovered_projection,
    )


# ``health_probe`` is the short name used by the harness design; retain the
# descriptive alias for callers that want to make the checkpoint scope clear.
health_probe = checkpoint_health


def _next_sequence(store: SQLiteStore, events: list[Event]) -> int:
    """Read the next global sequence without changing the storage contract."""

    local_next = max((_seq(event) for event in events), default=0) + 1
    connection = getattr(store, "connection", None)
    if connection is None:
        return local_next
    try:
        row = connection.execute("SELECT MAX(seq) AS seq FROM events").fetchone()
        global_last = int(row["seq"] or 0) if row is not None else 0
        return max(local_next, global_last + 1)
    except Exception:
        # A LedgerStore-compatible test double may expose no SQL connection;
        # the local sequence is still sufficient for preflight ordering.
        return local_next


def _boundary_chain_error(events: list[Event], previous: Boundary | None) -> str | None:
    """Reject a damaged boundary suffix before creating a new checkpoint."""

    compactions = [event for event in events if event.event_type == EVENT_COMPACTION]
    for event in compactions:
        if previous is not None and _seq(event) <= previous.seq:
            continue
        if previous is None or _seq(event) > previous.seq:
            return "存在无法验证的历史 compaction boundary"
    return None


def compact(
    store: SQLiteStore,
    idea: str,
    *,
    reason: str = "token_pressure",
    phase_scope: str | None = None,
    retained_from_seq: int | None = None,
    upto_seq: int | None = None,
    summarizer: CompactionSummaryProvider | None = None,
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
    revision_before = getattr(store, "revision", None)
    previous = latest_valid_boundary(events)
    chain_error = _boundary_chain_error(events, previous)
    if chain_error:
        raise CompactionValidationError(chain_error)
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
    summary_metadata: dict[str, Any] = {}
    model_provenance: dict[str, list[str]] | None = None
    if summarizer is not None:
        # Constructing the request is deterministic and bounded.  Sources are
        # checked before invoking a custom provider too; adapters cannot be
        # bypassed into receiving a secret, metric, evidence, or tool result.
        sources = _summary_sources(delta_events)
        request = CompactionSummaryRequest(
            idea_id=idea,
            from_seq=source_start,
            to_seq=to_seq,
            previous_summary=previous_summary if isinstance(previous_summary, dict) else {},
            deterministic_summary=summary,
            sources=sources,
        )
        summary_metadata = {
            "summary_mode": "model_fallback",
            "provider": _summary_provider_name(summarizer),
            "provider_name": _summary_provider_name(summarizer),
            "prompt_version": str(getattr(summarizer, "prompt_version", SUMMARY_PROMPT_VERSION)),
            "source_digest": source_digest(request),
        }
        try:
            if not sources:
                raise SummaryValidationError("没有可用的叙事 source")
            for source in sources:
                validate_summary_source(source)
            # Exactly one attempt.  Any provider exception is converted to a
            # deterministic fallback below; no summary retry is hidden here.
            model_candidate = validate_model_summary(request, summarizer.summarize(request))
            summary = _model_summary_payload(summary, model_candidate)
            model_provenance = provenance_for_summary(model_candidate)
            summary_metadata["summary_mode"] = "model_validated"
        except SummaryValidationError:
            summary_metadata["summary_error_code"] = "validation_failed"
        except Exception:
            summary_metadata["summary_error_code"] = "provider_failure"
        if model_provenance:
            summary_metadata["summary_provenance"] = model_provenance
            summary_metadata["summary_source_ids"] = list(dict.fromkeys(
                source_id for refs in model_provenance.values() for source_id in refs
            ))
    payload: dict[str, Any] = {
        "version": COMPACTION_VERSION,
        "from_seq": source_start,
        "to_seq": to_seq,
        "previous_boundary_seq": previous_event_seq or None,
        "summary": summary,
        "retained_from_seq": retained,
        "reason": str(reason or "token_pressure"),
    }
    payload.update(summary_metadata)
    if phase_scope is not None:
        payload["phase_scope"] = str(phase_scope)
    # A provider summary can take time.  Re-check the immutable input before
    # creating the candidate so a concurrent append cannot produce a boundary
    # whose summary describes an older sequence view.
    current_revision = getattr(store, "revision", None)
    current_events = list(store.scan(idea))
    if revision_before is not None and current_revision != revision_before:
        raise CompactionValidationError("ledger 在 compaction preflight 期间发生变化")
    if [(event.seq, event.event_id) for event in current_events] != [
        (event.seq, event.event_id) for event in events
    ]:
        raise CompactionValidationError("ledger sequence 在 compaction preflight 期间发生变化")
    candidate_event = Event(
        idea_id=idea,
        event_type=EVENT_COMPACTION,
        actor=ACTOR_ORCH,
        source=ACTOR_ORCH,
        phase=projection.phase,
        payload=payload,
        seq=_next_sequence(store, current_events),
    )
    # Full preflight happens before store.append.  SQLiteStore deliberately
    # accepts compaction as a reducer-neutral event, so this explicit check is
    # the guard that prevents an invalid checkpoint from entering the ledger.
    validate_boundary(candidate_event, events=current_events, previous=previous)
    seq = store.append(candidate_event)
    stored_events = list(store.scan(idea))
    stored = next((event for event in reversed(stored_events) if event.event_type == EVENT_COMPACTION), None)
    if stored is None:
        raise ValueError("append 后未找到 compaction boundary")
    try:
        validate_boundary(stored, events=stored_events, previous=previous)
    except CompactionValidationError as exc:
        raise ValueError(f"append 后 boundary 校验失败: {exc}") from exc
    if stored.seq != seq:
        raise ValueError("append 后 boundary sequence 不一致")
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
    "CheckpointHealth",
    "COMPACTION_VERSION",
    "CompactionValidationError",
    "build_context_after",
    "checkpoint_health",
    "compact",
    "health_probe",
    "latest_valid_boundary",
    "normalize_boundary_payload",
    "render_summary",
    "validate_boundary",
]
