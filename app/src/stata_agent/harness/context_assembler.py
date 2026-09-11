"""Deterministic, auditable context projection for model requests.

The assembler is intentionally independent from the provider and from the
agent loop.  It converts the immutable ledger, a structured checkpoint, and
query-relevant memory into bounded messages.  A caller can therefore test the
projection without making a network request and can keep the same manifest
for an audit trail.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..domain.reducers import Projection
from ..events.schema import (
    ACTOR_ORCH,
    EVENT_AGENT_STEP,
    EVENT_APPROVAL_GRANT,
    EVENT_APPROVAL_REJECT,
    EVENT_APPROVAL_REQ,
    EVENT_CONTEXT_ASSEMBLED,
    EVENT_COMPACTION,
    EVENT_STEERING,
    EVENT_TOOL_CALL,
    EVENT_TOOL_DONE,
    EVENT_TOOL_INVOKED,
    EVENT_TOOL_RESULT,
    EVENT_USER,
    Event,
)
from .compaction import Boundary, latest_valid_boundary, render_summary
from .context import build_context


CONTEXT_BUDGET_SNAPSHOT_VERSION = 1


@dataclass(frozen=True)
class ContextBudget:
    """Input budget and independent caps for memory and recent raw history."""

    max_input_tokens: int = 16_000
    reserve_output_tokens: int = 4_000
    recent_tail_tokens: int = 4_000
    memory_tokens: int = 1_500
    chars_per_token: int = 4
    # The request hard limit is the model context window minus output reserve
    # unless an operator supplies a stricter cap.  The soft limit leaves an
    # explicit compaction buffer so the loop can compact before provider I/O.
    hard_limit_tokens: int | None = None
    soft_limit_tokens: int | None = None
    compaction_buffer_tokens: int | None = None

    def __post_init__(self) -> None:
        for name in (
            "max_input_tokens",
            "reserve_output_tokens",
            "recent_tail_tokens",
            "memory_tokens",
            "chars_per_token",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{name} 必须是整数")
            if value < 0 or (name == "chars_per_token" and value == 0):
                raise ValueError(f"{name} 必须为正数" if name == "chars_per_token" else f"{name} 不能为负")
        if self.reserve_output_tokens > self.max_input_tokens:
            raise ValueError("reserve_output_tokens 不能超过 max_input_tokens")
        base_limit = self.max_input_tokens - self.reserve_output_tokens
        for name in ("hard_limit_tokens", "soft_limit_tokens", "compaction_buffer_tokens"):
            value = getattr(self, name)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} 必须是非负整数")
        hard = base_limit if self.hard_limit_tokens is None else self.hard_limit_tokens
        if hard > base_limit:
            raise ValueError("hard_limit_tokens 不能超过可用输入预算")
        if self.soft_limit_tokens is not None and self.soft_limit_tokens > hard:
            raise ValueError("soft_limit_tokens 不能超过 hard_limit_tokens")
        if self.compaction_buffer_tokens is not None and self.compaction_buffer_tokens > hard:
            raise ValueError("compaction_buffer_tokens 不能超过 hard_limit_tokens")

    @property
    def input_tokens(self) -> int:
        return self.hard_input_tokens

    @property
    def hard_input_tokens(self) -> int:
        """Maximum deterministic request input tokens, excluding output reserve."""

        base_limit = self.max_input_tokens - self.reserve_output_tokens
        return base_limit if self.hard_limit_tokens is None else self.hard_limit_tokens

    @property
    def compaction_buffer(self) -> int:
        """Reserved headroom before the hard limit.

        A 15% default mirrors the architecture contract while keeping the
        value deterministic for small test budgets.  Explicit configuration
        always wins.
        """

        if self.compaction_buffer_tokens is not None:
            return self.compaction_buffer_tokens
        return max(0, (self.hard_input_tokens * 15) // 100)

    @property
    def soft_input_tokens(self) -> int:
        if self.soft_limit_tokens is not None:
            return self.soft_limit_tokens
        return max(0, self.hard_input_tokens - self.compaction_buffer)


@dataclass(frozen=True)
class ContextBudgetSnapshot:
    """Auditable cost snapshot for one complete provider request.

    The counter is intentionally provider-neutral.  ``message_tokens`` is the
    stable JSON body estimate; framing and schema/tool-call protocol overhead
    are tracked separately so callers can recompute the total without seeing
    prompt text in telemetry.  A provider-specific tokenizer may replace this
    later, but must preserve the fields and version.
    """

    version: int
    message_tokens: int
    chat_framing_tokens: int
    tool_schema_tokens: int
    tool_call_framing_tokens: int
    estimated_input_tokens: int
    output_reserve_tokens: int
    compaction_buffer_tokens: int
    soft_limit_tokens: int
    hard_limit_tokens: int
    soft_exceeded: bool
    overflow: bool
    counter_source: str
    counter_metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.estimated_input_tokens

    @property
    def input_tokens(self) -> int:
        return self.estimated_input_tokens

    @property
    def tool_definition_tokens(self) -> int:
        """Compatibility alias for callers that call schemas definitions."""

        return self.tool_schema_tokens

    @property
    def framing_tokens(self) -> int:
        return self.chat_framing_tokens + self.tool_call_framing_tokens

    @property
    def hard_limit(self) -> int:
        return self.hard_limit_tokens

    @property
    def soft_limit(self) -> int:
        return self.soft_limit_tokens

    @property
    def within_hard_limit(self) -> bool:
        return not self.overflow

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "message_tokens": self.message_tokens,
            "chat_framing_tokens": self.chat_framing_tokens,
            "tool_schema_tokens": self.tool_schema_tokens,
            "tool_call_framing_tokens": self.tool_call_framing_tokens,
            "estimated_input_tokens": self.estimated_input_tokens,
            "output_reserve_tokens": self.output_reserve_tokens,
            "compaction_buffer_tokens": self.compaction_buffer_tokens,
            "soft_limit_tokens": self.soft_limit_tokens,
            "hard_limit_tokens": self.hard_limit_tokens,
            "soft_exceeded": self.soft_exceeded,
            "overflow": self.overflow,
            "counter_source": self.counter_source,
            "counter_metadata": dict(self.counter_metadata),
        }


class ContextBudgetExceeded(RuntimeError):
    """The non-droppable system/current-user portion cannot fit the budget."""


@dataclass(frozen=True)
class ContextManifestItem:
    layer: str
    source_ids: tuple[str, ...]
    estimated_tokens: int
    truncated: bool = False
    requested_count: int = 0
    selected_count: int = 0
    dropped_count: int = 0
    truncated_count: int = 0
    reason_codes: tuple[str, ...] = ()


@dataclass
class AssembledContext:
    messages: list[dict]
    manifest: list[ContextManifestItem]
    estimated_tokens: int
    compacted_through_seq: int | None
    budget_snapshot: ContextBudgetSnapshot | None = None


@dataclass
class _Unit:
    messages: list[dict]
    source_ids: tuple[str, ...]
    estimated_tokens: int
    truncated: bool = False
    first_seq: int = 0


@dataclass
class _Pack:
    layer: str
    units: list[_Unit] = field(default_factory=list)
    truncated: bool = False
    requested_count: int | None = None
    dropped_count: int = 0
    truncated_count: int = 0
    reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.requested_count is None:
            self.requested_count = len(self.units)

    @property
    def estimated_tokens(self) -> int:
        return sum(unit.estimated_tokens for unit in self.units)

    @property
    def source_ids(self) -> tuple[str, ...]:
        out: list[str] = []
        seen: set[str] = set()
        for unit in self.units:
            for source_id in unit.source_ids:
                if source_id not in seen:
                    out.append(source_id)
                    seen.add(source_id)
        return tuple(out)


def _with_reason(pack: _Pack, reason: str) -> None:
    if reason not in pack.reason_codes:
        pack.reason_codes = (*pack.reason_codes, reason)


def _flatten_packs(packs: Sequence[_Pack]) -> list[dict]:
    messages: list[dict] = []
    for pack in packs:
        for unit in pack.units:
            messages.extend(dict(message) for message in unit.messages)
    return messages


def estimate_message_tokens(message: Mapping[str, Any], *, chars_per_token: int = 4) -> int:
    """Estimate one message deterministically using stable JSON characters."""

    if isinstance(chars_per_token, bool) or not isinstance(chars_per_token, int) or chars_per_token <= 0:
        raise ValueError("chars_per_token 必须为正整数")
    try:
        text = json.dumps(dict(message), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        text = str(dict(message))
    return _estimate_text(text, chars_per_token)


def _estimate_text(value: Any, chars_per_token: int) -> int:
    text = str(value if value is not None else "")
    if not text:
        return 0
    return max(1, (len(text) + chars_per_token - 1) // chars_per_token)


def _normalise_tool_schemas(tool_schemas: Iterable[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    """Return a stable, JSON-safe copy of active provider tool schemas."""

    if tool_schemas is None:
        return []
    out: list[dict[str, Any]] = []
    for schema in tool_schemas:
        if isinstance(schema, Mapping):
            out.append(dict(schema))
        else:
            # A malformed schema must still have a deterministic accounting
            # cost; the provider will reject it later through its own API.
            out.append({"schema": str(schema)})
    return out


def _estimate_chat_framing(messages: Sequence[Mapping[str, Any]]) -> int:
    """Estimate provider chat envelope overhead independently of message body."""

    # OpenAI-compatible adapters add a small per-message envelope plus one
    # request-level marker.  Keep this intentionally conservative and stable;
    # exact provider tokenizers can be added behind the same snapshot contract.
    return (len(messages) * 2) + (1 if messages else 0)


def _estimate_tool_call_framing(messages: Sequence[Mapping[str, Any]]) -> int:
    """Count protocol framing for assistant calls and tool result messages."""

    calls = 0
    results = 0
    for message in messages:
        if isinstance(message.get("tool_calls"), list):
            calls += sum(1 for item in message["tool_calls"] if isinstance(item, Mapping))
        if message.get("role") == "tool":
            results += 1
    # The call/result metadata is already represented in message body JSON;
    # this small explicit allowance accounts for provider protocol delimiters.
    return (calls * 2) + results


def build_budget_snapshot(
    messages: Sequence[Mapping[str, Any]],
    *,
    tool_schemas: Iterable[Mapping[str, Any]] | None = None,
    budget: ContextBudget | None = None,
) -> ContextBudgetSnapshot:
    """Build one deterministic snapshot for a complete provider request.

    ``messages`` and active ``tool_schemas`` are both included.  This helper
    is deliberately public so the loop and focused tests can use exactly the
    same accounting entry point for initial and subsequent turns.
    """

    effective = budget or ContextBudget()
    if not isinstance(effective, ContextBudget):
        if isinstance(effective, Mapping):
            effective = ContextBudget(**dict(effective))
        else:
            raise TypeError("budget 必须是 ContextBudget")
    schemas = _normalise_tool_schemas(tool_schemas)
    message_tokens = sum(
        estimate_message_tokens(message, chars_per_token=effective.chars_per_token)
        for message in messages
    )
    chat_framing_tokens = _estimate_chat_framing(messages)
    tool_schema_tokens = _estimate_text(_json(schemas), effective.chars_per_token) if schemas else 0
    tool_call_framing_tokens = _estimate_tool_call_framing(messages)
    estimated = message_tokens + chat_framing_tokens + tool_schema_tokens + tool_call_framing_tokens
    hard = effective.hard_input_tokens
    soft = effective.soft_input_tokens
    return ContextBudgetSnapshot(
        version=CONTEXT_BUDGET_SNAPSHOT_VERSION,
        message_tokens=message_tokens,
        chat_framing_tokens=chat_framing_tokens,
        tool_schema_tokens=tool_schema_tokens,
        tool_call_framing_tokens=tool_call_framing_tokens,
        estimated_input_tokens=estimated,
        output_reserve_tokens=effective.reserve_output_tokens,
        compaction_buffer_tokens=effective.compaction_buffer,
        soft_limit_tokens=soft,
        hard_limit_tokens=hard,
        soft_exceeded=estimated > soft,
        overflow=estimated > hard,
        counter_source="deterministic_json_chars_v1",
        counter_metadata={
            "chars_per_token": effective.chars_per_token,
            "message_count": len(messages),
            "tool_schema_count": len(schemas),
            "tool_call_count": sum(
                len(message.get("tool_calls") or [])
                for message in messages
                if isinstance(message.get("tool_calls"), list)
            ),
            "tool_result_count": sum(1 for message in messages if message.get("role") == "tool"),
        },
    )


def _event_seq(event: Event) -> int:
    return int(event.seq or 0)


def _event_source(event: Event) -> str:
    return str(event.event_id or f"seq:{_event_seq(event)}")


def _payload(event: Event) -> dict[str, Any]:
    return event.payload if isinstance(event.payload, dict) else {}


def _json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(value)


def _tool_call_message(payload: dict[str, Any], *, fallback_id: str) -> dict:
    name = str(payload.get("tool") or payload.get("name") or "")
    arguments = payload.get("args", payload.get("arguments", {}))
    argument_text = arguments if isinstance(arguments, str) else _json(arguments)
    call_id = str(payload.get("call_id") or payload.get("id") or fallback_id)
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [{
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": argument_text},
        }],
    }


def _tool_result_message(payload: dict[str, Any], *, fallback_id: str) -> dict:
    call_id = str(payload.get("call_id") or payload.get("id") or fallback_id)
    value = payload.get("result", payload.get("content", payload.get("summary", payload)))
    content = value if isinstance(value, str) else _json(value)
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def _event_message(event: Event) -> dict | None:
    payload = _payload(event)
    kind = event.event_type
    source = _event_source(event)
    if kind == EVENT_USER:
        text = str(payload.get("text") or "")
        return {"role": "user", "content": text} if text else None
    if kind == EVENT_AGENT_STEP:
        tool_call = payload.get("tool_call")
        if isinstance(tool_call, dict):
            return _tool_call_message(tool_call, fallback_id=source)
        calls = payload.get("tool_calls")
        if isinstance(calls, list) and calls:
            return {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    _tool_call_message(item if isinstance(item, dict) else {}, fallback_id=f"{source}:{i}")["tool_calls"][0]
                    for i, item in enumerate(calls)
                ],
            }
        text = str(payload.get("reply") or payload.get("ask") or payload.get("decision_summary") or "")
        return {"role": "assistant", "content": text} if text else None
    if kind in {EVENT_TOOL_INVOKED, EVENT_TOOL_CALL}:
        return _tool_call_message(payload, fallback_id=source)
    if kind in {EVENT_TOOL_DONE, EVENT_TOOL_RESULT}:
        return _tool_result_message(payload, fallback_id=source)
    if kind == EVENT_STEERING:
        text = str(payload.get("text") or payload.get("note") or "")
        return {"role": "user", "content": text} if text else None
    if kind in {EVENT_APPROVAL_REQ, EVENT_APPROVAL_GRANT, EVENT_APPROVAL_REJECT}:
        action = str(payload.get("action") or payload.get("question") or payload.get("decision") or kind)
        note = str(payload.get("note") or payload.get("reason") or "")
        text = f"[approval] {action}" + (f" — {note}" if note else "")
        return {"role": "system", "content": text}
    return None


def _tool_key(event: Event) -> str:
    payload = _payload(event)
    return str(payload.get("call_id") or payload.get("operation_id") or payload.get("tool") or "")


def _tail_units(events: list[Event], *, chars_per_token: int = 4) -> list[_Unit]:
    """Convert events to complete message units, never dangling tool pairs."""

    units: list[_Unit] = []
    pending_tools: list[tuple[Event, dict]] = []
    pending_approvals: dict[str, list[Event]] = {}
    claimed: set[int] = set()

    def append_events(items: list[Event]) -> None:
        messages = [message for message in (_event_message(item) for item in items) if message is not None]
        if not messages:
            return
        ids = tuple(_event_source(item) for item in items)
        units.append(_Unit(messages=messages, source_ids=ids, estimated_tokens=0,
                           first_seq=min(_event_seq(item) for item in items)))

    for event in events:
        seq = _event_seq(event)
        if seq <= 0 or event.event_type in {EVENT_COMPACTION, EVENT_CONTEXT_ASSEMBLED}:
            continue
        kind = event.event_type
        key = _tool_key(event)
        if kind in {EVENT_TOOL_INVOKED, EVENT_TOOL_CALL}:
            pending_tools.append((event, _payload(event)))
            continue
        if kind in {EVENT_TOOL_DONE, EVENT_TOOL_RESULT}:
            index = len(pending_tools) - 1
            if key:
                for i in range(len(pending_tools) - 1, -1, -1):
                    if _tool_key(pending_tools[i][0]) == key:
                        index = i
                        break
            if pending_tools:
                start, _ = pending_tools.pop(index)
                append_events([start, event])
                claimed.update({_event_seq(start), seq})
            continue
        if kind == EVENT_APPROVAL_REQ:
            request_id = str(_payload(event).get("request_id") or _payload(event).get("id") or f"seq:{seq}")
            pending_approvals[request_id] = [event]
            continue
        if kind in {EVENT_APPROVAL_GRANT, EVENT_APPROVAL_REJECT}:
            request_id = str(_payload(event).get("request_id") or _payload(event).get("id") or "")
            selected = pending_approvals.pop(request_id, None)
            if selected is None and pending_approvals:
                _, selected = next(iter(pending_approvals.items()))
                pending_approvals.pop(next(iter(pending_approvals)))
            if selected is not None:
                append_events([*selected, event])
                claimed.update({_event_seq(item) for item in [*selected, event]})
            continue
        if seq not in claimed:
            append_events([event])

    # Do not append pending tool/approval requests.  They remain in the
    # canonical ledger and will be visible once their resolution is recorded.
    units.sort(key=lambda unit: unit.first_seq)
    for unit in units:
        unit.estimated_tokens = sum(
            estimate_message_tokens(message, chars_per_token=chars_per_token) for message in unit.messages
        )
    return units


def _clip_message(message: dict, token_cap: int, chars_per_token: int) -> dict:
    """Clip textual fields while retaining the message protocol shape."""

    result = dict(message)
    if token_cap <= 0:
        return result
    if isinstance(result.get("content"), str):
        original = result["content"]
        if estimate_message_tokens(result, chars_per_token=chars_per_token) > token_cap:
            marker = "…[truncated]"
            lo, hi = 0, len(original)
            best = ""
            while lo <= hi:
                mid = (lo + hi) // 2
                candidate = original[:mid] + marker
                trial = dict(result)
                trial["content"] = candidate
                if estimate_message_tokens(trial, chars_per_token=chars_per_token) <= token_cap:
                    best = candidate
                    lo = mid + 1
                else:
                    hi = mid - 1
            result["content"] = best or marker[: max(1, token_cap * chars_per_token)]
    elif isinstance(result.get("tool_calls"), list):
        calls = [dict(call) for call in result["tool_calls"] if isinstance(call, dict)]
        for call in calls:
            function = dict(call.get("function") or {})
            arguments = function.get("arguments")
            if isinstance(arguments, str):
                function["arguments"] = arguments[: max(1, token_cap * chars_per_token)] + "…[truncated]"
            call["function"] = function
        result["tool_calls"] = calls
    return result


def _truncate_unit(unit: _Unit, token_cap: int, chars_per_token: int) -> _Unit | None:
    if token_cap <= 0:
        return None
    if unit.estimated_tokens <= token_cap:
        return unit
    # A protocol unit may be dropped as a whole, but its call id, arguments or
    # result must never be character-clipped.  A clipped tool call can no
    # longer be replayed safely even when the assistant/tool roles remain.
    if any(
        message.get("role") == "tool" or isinstance(message.get("tool_calls"), list)
        for message in unit.messages
    ):
        return None
    messages: list[dict] = []
    remaining = token_cap
    for message in unit.messages:
        if remaining <= 0:
            # Keep the protocol pair intact.  A tiny placeholder is cheaper
            # than silently dropping the tool result after an assistant call.
            clipped = _clip_message(message, 1, chars_per_token)
            messages.append(clipped)
            continue
        clipped = _clip_message(message, remaining, chars_per_token)
        messages.append(clipped)
        remaining -= min(remaining, estimate_message_tokens(clipped, chars_per_token=chars_per_token))
    estimated = sum(estimate_message_tokens(message, chars_per_token=chars_per_token) for message in messages)
    if estimated > token_cap:
        # The protocol metadata itself can be larger than an extremely small
        # tail budget.  Dropping the whole semantic unit is safer than
        # returning a prompt that violates the advertised cap or a dangling
        # tool pair.
        return None
    return _Unit(
        messages=messages,
        source_ids=unit.source_ids,
        estimated_tokens=estimated,
        truncated=True,
        first_seq=unit.first_seq,
    )


def _pack_from_messages(
    layer: str,
    messages: list[dict],
    source_ids: Iterable[str],
    chars_per_token: int,
) -> _Pack:
    if not messages:
        return _Pack(layer=layer)
    unit = _Unit(
        messages=[dict(message) for message in messages],
        source_ids=tuple(str(source_id) for source_id in source_ids),
        estimated_tokens=sum(estimate_message_tokens(message, chars_per_token=chars_per_token) for message in messages),
    )
    return _Pack(layer=layer, units=[unit], requested_count=1, reason_codes=("selected",))


def _normalise_skill(skill: Any) -> tuple[str, str]:
    if isinstance(skill, Mapping):
        return str(skill.get("slug") or skill.get("name") or "skill"), str(skill.get("body") or skill.get("text") or "")
    return str(getattr(skill, "slug", getattr(skill, "name", "skill"))), str(getattr(skill, "body", ""))


def _memory_items(memory: Any, query: str, workspace_id: Any, cap: int) -> list[tuple[str, str]]:
    if memory is None or not query.strip() or cap <= 0:
        return []
    selected: Any = None
    method = getattr(memory, "select_for_context", None)
    if callable(method):
        attempts = (
            lambda: method(query, workspace_id=workspace_id, max_tokens=cap),
            lambda: method(query=query, workspace_id=workspace_id, max_tokens=cap),
            lambda: method(query, max_tokens=cap),
            lambda: method(query=query, max_tokens=cap),
        )
        for attempt in attempts:
            try:
                selected = attempt()
                break
            except TypeError:
                continue
            except Exception:
                return []
    if selected is None:
        method = getattr(memory, "search", None)
        if callable(method):
            search_attempts = (
                lambda: method(query, workspace_id=workspace_id, limit=8),
                lambda: method(query=query, workspace_id=workspace_id, limit=8),
                lambda: method(query, limit=8),
            )
            for attempt in search_attempts:
                try:
                    selected = attempt()
                    break
                except TypeError:
                    continue
                except Exception:
                    return []
    if selected is None:
        return []
    if isinstance(selected, Mapping):
        selected = selected.get("records", selected.get("items", selected.get("data", [selected])))
    if isinstance(selected, str):
        selected = [selected]
    if not isinstance(selected, Sequence) or isinstance(selected, (str, bytes)):
        return []
    out: list[tuple[str, str]] = []
    for index, item in enumerate(selected):
        if isinstance(item, Mapping):
            memory_id = str(item.get("id") or item.get("memory_id") or f"memory:{index}")
            text = str(item.get("context") or item.get("text") or item.get("content") or "")
            kind = str(item.get("kind") or "working_knowledge")
            source_ids = item.get("source_ids") or item.get("sources") or []
            provenance = ", ".join(str(value) for value in source_ids) if isinstance(source_ids, list) else str(source_ids)
            label = f"[memory id={memory_id} kind={kind} — constraints/working knowledge]"
            if provenance:
                label += f" sources={provenance}"
            text = f"{label}\n{text}" if text else label
        else:
            memory_id = f"memory:{index}"
            text = str(item)
        if text.strip():
            out.append((memory_id, text))
    return out


def _trim_pack_to_cap(pack: _Pack, cap: int, chars_per_token: int) -> _Pack:
    if cap <= 0 or not pack.units:
        dropped = len(pack.units)
        reasons = list(pack.reason_codes)
        if dropped:
            if "dropped_layer_cap" not in reasons:
                reasons.append("dropped_layer_cap")
        return _Pack(
            layer=pack.layer,
            requested_count=pack.requested_count,
            dropped_count=pack.dropped_count + dropped,
            truncated_count=pack.truncated_count,
            reason_codes=tuple(reasons),
        )
    selected: list[_Unit] = []
    used = 0
    # Keep the newest units.  Reversing here ensures old turns are dropped
    # first, while the final list remains chronological.
    for unit in reversed(pack.units):
        if used + unit.estimated_tokens <= cap:
            selected.append(unit)
            used += unit.estimated_tokens
        elif not selected and unit.estimated_tokens > cap:
            clipped = _truncate_unit(unit, cap, chars_per_token)
            if clipped is not None and clipped.estimated_tokens <= cap:
                selected.append(clipped)
                used += clipped.estimated_tokens
    selected.reverse()
    dropped = len(pack.units) - len(selected)
    reasons = list(pack.reason_codes)
    if dropped and "dropped_layer_cap" not in reasons:
        reasons.append("dropped_layer_cap")
    if any(unit.truncated for unit in selected) and "truncated_budget" not in reasons:
        reasons.append("truncated_budget")
    return _Pack(
        layer=pack.layer,
        units=selected,
        truncated=pack.truncated or any(unit.truncated for unit in selected),
        requested_count=pack.requested_count,
        dropped_count=pack.dropped_count + dropped,
        truncated_count=pack.truncated_count + sum(1 for unit in selected if unit.truncated),
        reason_codes=tuple(reasons),
    )


def _drop_oldest(pack: _Pack) -> bool:
    if not pack.units:
        return False
    pack.units.pop(0)
    pack.dropped_count += 1
    _with_reason(pack, "dropped_budget_low_priority")
    return True


def _truncate_pack(pack: _Pack, cap: int, chars_per_token: int) -> _Pack:
    if pack.estimated_tokens <= cap:
        return pack
    if not pack.units:
        return pack
    # Summary/state packs contain a single unit; for the tail this function is
    # only a last resort and preserves all messages in the final unit.
    if len(pack.units) > 1:
        while len(pack.units) > 1 and pack.estimated_tokens > cap:
            pack.units.pop(0)
    if pack.estimated_tokens > cap:
        clipped = _truncate_unit(pack.units[-1], cap, chars_per_token)
        pack.units[-1:] = [clipped] if clipped is not None else []
        pack.truncated = True
        pack.truncated_count += 1
        _with_reason(pack, "truncated_budget")
        if clipped is None:
            pack.dropped_count += 1
            _with_reason(pack, "dropped_budget_unfit")
    return pack


def _pack_manifest(pack: _Pack) -> ContextManifestItem | None:
    requested = int(pack.requested_count or 0)
    selected = len(pack.units)
    if requested <= 0 and selected <= 0 and pack.dropped_count <= 0:
        return None
    reasons = list(pack.reason_codes)
    if selected and "selected" not in reasons:
        reasons.insert(0, "selected")
    if not selected and pack.dropped_count and "dropped_all" not in reasons:
        reasons.append("dropped_all")
    return ContextManifestItem(
        layer=pack.layer,
        source_ids=pack.source_ids,
        estimated_tokens=pack.estimated_tokens,
        truncated=pack.truncated or pack.truncated_count > 0,
        requested_count=requested,
        selected_count=selected,
        dropped_count=pack.dropped_count,
        truncated_count=pack.truncated_count,
        reason_codes=tuple(dict.fromkeys(reasons)),
    )


def _message_units(messages: Sequence[Mapping[str, Any]], chars_per_token: int) -> list[_Unit]:
    """Group an existing request transcript into drop-safe semantic units."""

    units: list[_Unit] = []
    index = 0
    while index < len(messages):
        message = dict(messages[index])
        group = [message]
        if message.get("role") == "assistant" and isinstance(message.get("tool_calls"), list):
            end = index + 1
            while end < len(messages) and messages[end].get("role") == "tool":
                group.append(dict(messages[end]))
                end += 1
            index = end
        else:
            index += 1
        units.append(_Unit(
            messages=group,
            source_ids=(f"message:{index - len(group)}",),
            estimated_tokens=sum(
                estimate_message_tokens(item, chars_per_token=chars_per_token) for item in group
            ),
            first_seq=index - len(group),
        ))
    return units


def _dedupe_current_user(messages: Sequence[Mapping[str, Any]], user_text: str) -> list[dict]:
    """Keep exactly the newest copy of the current user anchor."""

    copied = [dict(message) for message in messages]
    matches = [
        index
        for index, message in enumerate(copied)
        if message.get("role") == "user" and str(message.get("content") or "") == user_text
    ]
    if matches:
        keep = matches[-1]
        copied = [
            message
            for index, message in enumerate(copied)
            if index == keep or index not in matches
        ]
    else:
        copied.append({"role": "user", "content": user_text})
    return copied


class ContextAssembler:
    """Build one deterministic bounded prompt and best-effort telemetry."""

    def assemble(
        self,
        *,
        store: Any,
        ctx: Any,
        user_text: str,
        system: str,
        skills: Iterable[Any] = (),
        budget: ContextBudget | None = None,
        tool_schemas: Iterable[Mapping[str, Any]] = (),
    ) -> AssembledContext:
        effective_budget = budget or getattr(ctx, "context_budget", None) or ContextBudget()
        if not isinstance(effective_budget, ContextBudget):
            if isinstance(effective_budget, Mapping):
                effective_budget = ContextBudget(**dict(effective_budget))
            else:
                raise TypeError("budget 必须是 ContextBudget")
        idea = str(getattr(ctx, "idea", getattr(ctx, "idea_id", "ui")))
        current_text = str(user_text if user_text is not None else "")
        skill_items = [_normalise_skill(skill) for skill in skills]
        system_text = str(system or "")
        if skill_items:
            system_text += "\n\n" + "\n\n".join(
                f"【当前任务方法论：{slug}】\n{body}" for slug, body in skill_items
            )
        system_pack = _pack_from_messages(
            "system",
            [{"role": "system", "content": system_text}],
            ["system", *(slug for slug, _ in skill_items)],
            effective_budget.chars_per_token,
        )
        _with_reason(system_pack, "mandatory_system")
        current_pack = _pack_from_messages(
            "current_user",
            [{"role": "user", "content": current_text}],
            [f"current:{idea}"],
            effective_budget.chars_per_token,
        )
        _with_reason(current_pack, "mandatory_current_user")
        schemas = _normalise_tool_schemas(tool_schemas)
        mandatory_messages = _flatten_packs([system_pack, current_pack])
        mandatory_snapshot = build_budget_snapshot(
            mandatory_messages,
            tool_schemas=schemas,
            budget=effective_budget,
        )
        if mandatory_snapshot.overflow:
            raise ContextBudgetExceeded(
                "system/current user/tool schemas exceed context budget "
                f"({mandatory_snapshot.estimated_input_tokens}>{mandatory_snapshot.hard_limit_tokens})"
            )

        try:
            events = list(store.scan(idea))
        except Exception:
            events = []
        boundary: Boundary | None = latest_valid_boundary(events)
        compacted_through = boundary.to_seq if boundary else None
        proj: Projection | None
        try:
            proj = store.project(idea)
        except Exception:
            proj = None

        if proj is not None:
            state_text = build_context(proj)
            state_ids = tuple(
                _event_source(event)
                for event in events
                if event.event_type not in {EVENT_COMPACTION, EVENT_CONTEXT_ASSEMBLED}
            )[-32:]
        else:
            state_text = f"[L2] idea={idea}"
            state_ids = (f"idea:{idea}",)
        state_pack = _pack_from_messages(
            "research_state",
            [{"role": "system", "content": state_text}],
            state_ids or (f"idea:{idea}",),
            effective_budget.chars_per_token,
        )

        summary_pack = _Pack(layer="compaction")
        if boundary is not None:
            summary = render_summary(boundary.payload.get("summary", ""))
            summary_pack = _pack_from_messages(
                "compaction",
                [{"role": "system", "content": f"[compaction {boundary.from_seq}-{boundary.to_seq}]\n{summary}"}],
                (_event_source(boundary.event), f"seq:{boundary.from_seq}-{boundary.to_seq}"),
                effective_budget.chars_per_token,
            )

        workspace_id = getattr(ctx, "workspace_id", None)
        memory = getattr(ctx, "memory", None)
        memory_pack = _Pack(layer="memory")
        memory_units: list[_Unit] = []
        for memory_id, text in _memory_items(memory, current_text, workspace_id, effective_budget.memory_tokens):
            message = {"role": "system", "content": text}
            memory_units.append(_Unit(
                messages=[message],
                source_ids=(memory_id,),
                estimated_tokens=estimate_message_tokens(message, chars_per_token=effective_budget.chars_per_token),
            ))
        memory_pack = _trim_pack_to_cap(
            _Pack(
                layer="memory",
                units=memory_units,
                requested_count=len(memory_units),
                reason_codes=("selected",) if memory_units else (),
            ),
            effective_budget.memory_tokens,
            effective_budget.chars_per_token,
        )

        tail_events = events
        if boundary is not None:
            retained = boundary.retained_from_seq
            if retained is None:
                retained = boundary.seq + 1
            tail_events = [event for event in events if _event_seq(event) >= retained]
        # Do not duplicate the UI's already-recorded current message in the
        # historical tail; an older identical turn remains valid history.
        for index in range(len(tail_events) - 1, -1, -1):
            event = tail_events[index]
            if event.event_type == EVENT_USER and str(_payload(event).get("text") or "") == current_text:
                tail_events = tail_events[:index] + tail_events[index + 1 :]
                break
        tail_units = _tail_units(tail_events, chars_per_token=effective_budget.chars_per_token)
        tail_pack = _Pack(
            layer="conversation_tail",
            units=tail_units,
            requested_count=len(tail_units),
            reason_codes=("selected",) if tail_units else (),
        )
        tail_pack = _trim_pack_to_cap(
            tail_pack, effective_budget.recent_tail_tokens, effective_budget.chars_per_token
        )

        packs = [system_pack, state_pack, summary_pack, memory_pack, tail_pack, current_pack]
        def current_snapshot() -> ContextBudgetSnapshot:
            return build_budget_snapshot(
                _flatten_packs(packs),
                tool_schemas=schemas,
                budget=effective_budget,
            )

        snapshot = current_snapshot()
        # Oldest raw tail items are lowest priority, then memory records.  A
        # final character clip is allowed only after whole units are removed.
        while snapshot.overflow and _drop_oldest(tail_pack):
            snapshot = current_snapshot()
        while snapshot.overflow and _drop_oldest(memory_pack):
            snapshot = current_snapshot()
        if snapshot.overflow:
            current_without_summary = snapshot.estimated_input_tokens - summary_pack.estimated_tokens
            _truncate_pack(
                summary_pack,
                max(0, effective_budget.hard_input_tokens - current_without_summary),
                effective_budget.chars_per_token,
            )
            snapshot = current_snapshot()
        if snapshot.overflow:
            current_without_state = snapshot.estimated_input_tokens - state_pack.estimated_tokens
            _truncate_pack(
                state_pack,
                max(0, effective_budget.hard_input_tokens - current_without_state),
                effective_budget.chars_per_token,
            )
            snapshot = current_snapshot()
        # Any remaining overflow can only be caused by a malformed message or
        # protocol metadata larger than an extremely small budget.  Fail
        # before provider I/O rather than sending a request that cannot fit.
        if snapshot.overflow:
            raise ContextBudgetExceeded(
                f"context 需要 {snapshot.estimated_input_tokens} tokens，预算仅 {snapshot.hard_limit_tokens}"
            )

        messages = _flatten_packs(packs)
        manifest = [item for pack in packs if (item := _pack_manifest(pack)) is not None]
        estimated = snapshot.estimated_input_tokens
        # Best effort only: context construction must not be made unavailable
        # by a read-only/replay store or a telemetry write failure.
        self._record_telemetry(
            store,
            idea,
            manifest,
            estimated,
            compacted_through,
            effective_budget,
            snapshot=snapshot,
            correlation_id=getattr(ctx, "request_id", None),
        )
        return AssembledContext(
            messages=messages,
            manifest=manifest,
            estimated_tokens=estimated,
            compacted_through_seq=compacted_through,
            budget_snapshot=snapshot,
        )

    def bound(
        self,
        *,
        messages: Sequence[Mapping[str, Any]],
        user_text: str,
        tool_schemas: Iterable[Mapping[str, Any]] = (),
        budget: ContextBudget | None = None,
        store: Any = None,
        ctx: Any = None,
        compacted_through_seq: int | None = None,
    ) -> AssembledContext:
        """Apply the same bounded semantic-unit policy to later loop turns.

        The first projection is assembled from the ledger; subsequent turns
        add ephemeral assistant/tool messages in memory.  This method is the
        common request entry point for both cases: exact current-user
        deduplication, whole tool units, deterministic budget accounting and a
        safe manifest all happen here before provider I/O.
        """

        effective_budget = budget or getattr(ctx, "context_budget", None) or ContextBudget()
        if not isinstance(effective_budget, ContextBudget):
            if isinstance(effective_budget, Mapping):
                effective_budget = ContextBudget(**dict(effective_budget))
            else:
                raise TypeError("budget 必须是 ContextBudget")
        current_text = str(user_text if user_text is not None else "")
        schemas = _normalise_tool_schemas(tool_schemas)
        normalized = _dedupe_current_user(messages, current_text)
        units = _message_units(normalized, effective_budget.chars_per_token)
        if not units:
            normalized = [{"role": "user", "content": current_text}]
            units = _message_units(normalized, effective_budget.chars_per_token)

        mandatory_indices: set[int] = set()
        for index, message in enumerate(normalized):
            if message.get("role") == "system":
                mandatory_indices.add(index)
                break
        user_indices = [
            index
            for index, message in enumerate(normalized)
            if message.get("role") == "user" and str(message.get("content") or "") == current_text
        ]
        if user_indices:
            mandatory_indices.add(user_indices[-1])
        # The newest assistant/tool interaction belongs to the active logical
        # turn.  Keep the whole protocol unit mandatory so a later request can
        # never silently drop one side after executing a tool.
        current_tool_unit = next(
            (
                unit
                for unit in reversed(units)
                if any(isinstance(message.get("tool_calls"), list) for message in unit.messages)
            ),
            None,
        )
        if current_tool_unit is not None:
            mandatory_indices.update(
                range(current_tool_unit.first_seq, current_tool_unit.first_seq + len(current_tool_unit.messages))
            )

        def contains_mandatory(unit: _Unit) -> bool:
            return any(
                unit.first_seq <= index < unit.first_seq + len(unit.messages)
                for index in mandatory_indices
            )

        mandatory_units = [unit for unit in units if contains_mandatory(unit)]
        optional_units = [unit for unit in units if not contains_mandatory(unit)]
        selected_units = list(mandatory_units)
        mandatory_messages = [
            message
            for unit in mandatory_units
            for message in unit.messages
        ]
        mandatory_snapshot = build_budget_snapshot(
            mandatory_messages,
            tool_schemas=schemas,
            budget=effective_budget,
        )
        if mandatory_snapshot.overflow:
            raise ContextBudgetExceeded(
                "system/current user/tool schemas exceed context budget "
                f"({mandatory_snapshot.estimated_input_tokens}>{mandatory_snapshot.hard_limit_tokens})"
            )

        # Newest complete units have priority; no unit is partially selected.
        for unit in reversed(optional_units):
            candidate = [*selected_units, unit]
            candidate_messages = [
                message
                for selected_unit in candidate
                for message in selected_unit.messages
            ]
            candidate_snapshot = build_budget_snapshot(
                candidate_messages,
                tool_schemas=schemas,
                budget=effective_budget,
            )
            if not candidate_snapshot.overflow:
                selected_units.append(unit)

        selected_units.sort(key=lambda unit: unit.first_seq)
        selected_messages = [
            message
            for unit in selected_units
            for message in unit.messages
        ]
        snapshot = build_budget_snapshot(
            selected_messages,
            tool_schemas=schemas,
            budget=effective_budget,
        )
        if snapshot.overflow:
            # This should only be reachable for malformed protocol metadata;
            # fail closed rather than truncating a current-turn unit.
            raise ContextBudgetExceeded(
                f"context 需要 {snapshot.estimated_input_tokens} tokens，预算仅 {snapshot.hard_limit_tokens}"
            )

        dropped_count = max(0, len(units) - len(selected_units))
        current_units = [
            unit
            for unit in selected_units
            if any(
                unit.first_seq <= index < unit.first_seq + len(unit.messages)
                for index in user_indices
            )
        ]
        system_units = [
            unit
            for unit in selected_units
            if any(
                unit.first_seq <= index < unit.first_seq + len(unit.messages)
                for index in range(len(normalized))
                if normalized[index].get("role") == "system"
            )
        ]
        tail_units = [unit for unit in selected_units if unit not in system_units and unit not in current_units]

        def manifest_item(layer: str, layer_units: list[_Unit], *, mandatory: bool = False) -> ContextManifestItem | None:
            if not layer_units and not (mandatory and layer):
                return None
            reason_codes = ["selected"] if layer_units else []
            if mandatory:
                reason_codes.append("mandatory_system" if layer == "system" else "mandatory_current_user")
            if dropped_count and layer == "conversation_tail":
                reason_codes.append("dropped_budget_low_priority")
            return ContextManifestItem(
                layer=layer,
                source_ids=tuple(source_id for unit in layer_units for source_id in unit.source_ids),
                estimated_tokens=sum(unit.estimated_tokens for unit in layer_units),
                truncated=False,
                requested_count=len(units) if layer == "conversation_tail" else len(layer_units),
                selected_count=len(layer_units),
                dropped_count=dropped_count if layer == "conversation_tail" else 0,
                truncated_count=0,
                reason_codes=tuple(dict.fromkeys(reason_codes)),
            )

        manifest: list[ContextManifestItem] = []
        system_item = manifest_item("system", system_units, mandatory=True)
        if system_item is not None:
            manifest.append(system_item)
        tail_item = manifest_item("conversation_tail", tail_units)
        if tail_item is not None:
            manifest.append(tail_item)
        current_item = manifest_item("current_user", current_units, mandatory=True)
        if current_item is not None:
            manifest.append(current_item)

        if store is not None and ctx is not None:
            idea = str(getattr(ctx, "idea", getattr(ctx, "idea_id", "ui")))
            self._record_telemetry(
                store,
                idea,
                manifest,
                snapshot.estimated_input_tokens,
                compacted_through_seq,
                effective_budget,
                snapshot=snapshot,
                correlation_id=getattr(ctx, "request_id", None),
            )
        return AssembledContext(
            messages=[dict(message) for message in selected_messages],
            manifest=manifest,
            estimated_tokens=snapshot.estimated_input_tokens,
            compacted_through_seq=compacted_through_seq,
            budget_snapshot=snapshot,
        )

    @staticmethod
    def _record_telemetry(
        store: Any,
        idea: str,
        manifest: list[ContextManifestItem],
        estimated: int,
        compacted_through: int | None,
        budget: ContextBudget,
        *,
        snapshot: ContextBudgetSnapshot | None = None,
        correlation_id: str | None = None,
    ) -> None:
        append = getattr(store, "append", None)
        if not callable(append):
            return
        payload: dict[str, Any] = {
            "estimated_tokens": estimated,
            "budget_tokens": budget.input_tokens,
            "compacted_through_seq": compacted_through,
            "layers": [
                {
                    "layer": item.layer,
                    "source_ids": list(item.source_ids),
                    "estimated_tokens": item.estimated_tokens,
                    "truncated": item.truncated,
                    "requested_count": item.requested_count,
                    "selected_count": item.selected_count,
                    "dropped_count": item.dropped_count,
                    "truncated_count": item.truncated_count,
                    "reason_codes": list(item.reason_codes),
                }
                for item in manifest
            ],
            "source_ids": [source_id for item in manifest for source_id in item.source_ids],
        }
        if snapshot is not None:
            payload["budget_snapshot"] = snapshot.as_dict()
        try:
            append(Event(
                idea_id=idea,
                event_type=EVENT_CONTEXT_ASSEMBLED,
                actor=ACTOR_ORCH,
                source=ACTOR_ORCH,
                correlation_id=correlation_id,
                payload=payload,
            ))
        except Exception:
            return


__all__ = [
    "AssembledContext",
    "CONTEXT_BUDGET_SNAPSHOT_VERSION",
    "ContextAssembler",
    "ContextBudget",
    "ContextBudgetSnapshot",
    "ContextBudgetExceeded",
    "ContextManifestItem",
    "build_budget_snapshot",
    "estimate_message_tokens",
]
