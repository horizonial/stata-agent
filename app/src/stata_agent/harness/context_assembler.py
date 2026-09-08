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


@dataclass(frozen=True)
class ContextBudget:
    """Input budget and independent caps for memory and recent raw history."""

    max_input_tokens: int = 16_000
    reserve_output_tokens: int = 4_000
    recent_tail_tokens: int = 4_000
    memory_tokens: int = 1_500
    chars_per_token: int = 4

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

    @property
    def input_tokens(self) -> int:
        return self.max_input_tokens - self.reserve_output_tokens


class ContextBudgetExceeded(RuntimeError):
    """The non-droppable system/current-user portion cannot fit the budget."""


@dataclass(frozen=True)
class ContextManifestItem:
    layer: str
    source_ids: tuple[str, ...]
    estimated_tokens: int
    truncated: bool = False


@dataclass
class AssembledContext:
    messages: list[dict]
    manifest: list[ContextManifestItem]
    estimated_tokens: int
    compacted_through_seq: int | None


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
    return _Pack(layer=layer, units=[unit])


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
        return _Pack(layer=pack.layer)
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
    return _Pack(layer=pack.layer, units=selected, truncated=pack.truncated or any(unit.truncated for unit in selected))


def _drop_oldest(pack: _Pack) -> bool:
    if not pack.units:
        return False
    pack.units.pop(0)
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
    return pack


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
        current_pack = _pack_from_messages(
            "current_user",
            [{"role": "user", "content": current_text}],
            [f"current:{idea}"],
            effective_budget.chars_per_token,
        )
        mandatory = system_pack.estimated_tokens + current_pack.estimated_tokens
        if mandatory > effective_budget.input_tokens:
            raise ContextBudgetExceeded(
                f"system/current user 需要 {mandatory} tokens，预算仅 {effective_budget.input_tokens}"
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
            _Pack(layer="memory", units=memory_units), effective_budget.memory_tokens, effective_budget.chars_per_token
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
        tail_pack = _Pack(
            layer="conversation_tail",
            units=_tail_units(tail_events, chars_per_token=effective_budget.chars_per_token),
        )
        tail_pack = _trim_pack_to_cap(
            tail_pack, effective_budget.recent_tail_tokens, effective_budget.chars_per_token
        )

        optional = [state_pack, summary_pack, memory_pack, tail_pack]
        total = mandatory + sum(pack.estimated_tokens for pack in optional)
        # Oldest raw tail items are lowest priority, then memory records.  A
        # final character clip is allowed only after whole units are removed.
        while total > effective_budget.input_tokens and _drop_oldest(tail_pack):
            total = mandatory + sum(pack.estimated_tokens for pack in optional)
        while total > effective_budget.input_tokens and _drop_oldest(memory_pack):
            total = mandatory + sum(pack.estimated_tokens for pack in optional)
        if total > effective_budget.input_tokens:
            _truncate_pack(summary_pack, max(0, effective_budget.input_tokens - mandatory - state_pack.estimated_tokens), effective_budget.chars_per_token)
            total = mandatory + sum(pack.estimated_tokens for pack in optional)
        if total > effective_budget.input_tokens:
            _truncate_pack(state_pack, max(0, effective_budget.input_tokens - mandatory), effective_budget.chars_per_token)
            total = mandatory + sum(pack.estimated_tokens for pack in optional)
        # Any remaining overflow can only be caused by a malformed message
        # with protocol metadata larger than its content; fail before I/O.
        if total > effective_budget.input_tokens:
            raise ContextBudgetExceeded(f"context 需要 {total} tokens，预算仅 {effective_budget.input_tokens}")

        packs = [system_pack, state_pack, summary_pack, memory_pack, tail_pack, current_pack]
        messages: list[dict] = []
        manifest: list[ContextManifestItem] = []
        for pack in packs:
            if not pack.units:
                continue
            for unit in pack.units:
                messages.extend(dict(message) for message in unit.messages)
            manifest.append(ContextManifestItem(
                layer=pack.layer,
                source_ids=pack.source_ids,
                estimated_tokens=pack.estimated_tokens,
                truncated=pack.truncated or any(unit.truncated for unit in pack.units),
            ))
        estimated = sum(estimate_message_tokens(message, chars_per_token=effective_budget.chars_per_token) for message in messages)
        # Best effort only: context construction must not be made unavailable
        # by a read-only/replay store or a telemetry write failure.
        self._record_telemetry(store, idea, manifest, estimated, compacted_through, effective_budget)
        return AssembledContext(
            messages=messages,
            manifest=manifest,
            estimated_tokens=estimated,
            compacted_through_seq=compacted_through,
        )

    @staticmethod
    def _record_telemetry(
        store: Any,
        idea: str,
        manifest: list[ContextManifestItem],
        estimated: int,
        compacted_through: int | None,
        budget: ContextBudget,
    ) -> None:
        append = getattr(store, "append", None)
        if not callable(append):
            return
        payload = {
            "estimated_tokens": estimated,
            "budget_tokens": budget.input_tokens,
            "compacted_through_seq": compacted_through,
            "layers": [
                {
                    "layer": item.layer,
                    "source_ids": list(item.source_ids),
                    "estimated_tokens": item.estimated_tokens,
                    "truncated": item.truncated,
                }
                for item in manifest
            ],
            "source_ids": [source_id for item in manifest for source_id in item.source_ids],
        }
        try:
            append(Event(
                idea_id=idea,
                event_type=EVENT_CONTEXT_ASSEMBLED,
                actor=ACTOR_ORCH,
                source=ACTOR_ORCH,
                payload=payload,
            ))
        except Exception:
            return


__all__ = [
    "AssembledContext",
    "ContextAssembler",
    "ContextBudget",
    "ContextBudgetExceeded",
    "ContextManifestItem",
    "estimate_message_tokens",
]
