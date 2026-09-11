"""通用 agent loop（LLM + function calling，自主多步）。

This is the single tool-routing path used by the UI.  The model may propose a
tool, but :class:`ToolEnforcer` is the only component allowed to validate or
execute it.  Every normal, failed, or budget-limited termination is recorded
as an ``agent_step`` so the loop is replayable from the ledger.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import time
from typing import Any

from ..events.schema import (
    ACTOR_AGENT,
    ACTOR_ORCH,
    EVENT_AGENT_STEP,
    EVENT_BUDGET,
    EVENT_FALLBACK,
    EVENT_PROVIDER_TURN_COMPLETED,
    EVENT_PROVIDER_TURN_FAILED,
    EVENT_PROVIDER_TURN_STARTED,
    EVENT_TOOL_DONE,
    EVENT_TOOL_INVOKED,
    Event,
)
from ..privacy.modes import (
    LOCAL_STRICT,
    normalize_mode,
    provider_is_remote,
    remote_llm_allowed,
    sanitize_messages,
    sanitize_tool_schemas,
)
from ..providers.protocol import ProviderError, provider_error_from_exception
from ..storage.sqlite_store import SQLiteStore
from ..toolkit import Tool, ToolContext
from .cancellation import (
    CancellationRequested,
    cancellation_reason,
    is_cancel_requested,
    raise_if_cancelled,
)
from .tool_enforcer import ToolEnforcer, normalize_arguments


@dataclass
class LoopResult:
    reply: str | None
    ask: str | None = None
    tool_calls: int = 0
    terminal_reason: str | None = None
    cancelled: bool = False

    def __post_init__(self):
        if self.reply is None and self.ask is None:
            self.reply = "（这轮没有产出，请再具体一点。）"


class CompatibilityToolFacade:
    """Backward-compatible facade for callers that imported the old Guard.

    New code should use :class:`ToolEnforcer` directly.  Keeping this facade
    avoids breaking integrations while ensuring they get the same checks.
    """

    def __init__(self, tools: dict[str, Tool]):
        self.enforcer = ToolEnforcer(tools)

    def validate(self, name: str, arguments: Any, ctx: ToolContext | None = None) -> str | None:
        return self.enforcer.validate(name, arguments, ctx)


# Public compatibility alias.  The implementation is an application-service
# facade over ToolEnforcer, not a transport/API guard.
Guard = CompatibilityToolFacade


def _history_messages(store: SQLiteStore, idea: str, limit: int = 16) -> list[dict]:
    """把账本投影成 OpenAI 消息（只取 user/assistant 文本）。"""

    out: list[dict] = []
    for event in store.scan(idea):
        payload = event.payload or {}
        if event.event_type == "user.message":
            out.append({"role": "user", "content": str(payload.get("text") or "")})
        elif event.event_type == EVENT_AGENT_STEP and not payload.get("tool_call"):
            content = str(payload.get("ask") or payload.get("reply") or payload.get("decision_summary") or "")
            if content:
                out.append({"role": "assistant", "content": content})
    return out[-limit:]


def _current_user_is_recorded(store: SQLiteStore, idea: str, text: str) -> bool:
    """Detect the UI's pre-appended user event without deduplicating old turns."""

    try:
        events = list(store.scan(idea))
    except Exception:  # noqa: BLE001
        return False
    if not events or events[-1].event_type != "user.message":
        return False
    return str((events[-1].payload or {}).get("text") or "") == text


def _phase(store: SQLiteStore, ctx: ToolContext) -> str | None:
    phase = getattr(ctx, "phase", None)
    if phase is not None:
        return getattr(phase, "value", str(phase))
    try:
        return store.project(ctx.idea).phase
    except Exception:  # noqa: BLE001
        return None


def _append_event(store: SQLiteStore | None, ctx: ToolContext, event_type: str, *,
                  actor: str, source: str, payload: dict) -> None:
    """Best-effort event append used by terminal paths and telemetry hooks."""

    if store is None:
        return
    try:
        store.append(Event(
            idea_id=ctx.idea,
            event_type=event_type,
            actor=actor,
            source=source,
            correlation_id=getattr(ctx, "request_id", None),
            phase=_phase(store, ctx),
            payload=payload,
        ))
    except Exception:
        # A caller may be using a read-only/replay store.  Never hide the
        # model/tool result behind a secondary ledger failure.
        return


def _finish(store: SQLiteStore | None, ctx: ToolContext, *, reply: str | None = None,
            ask: str | None = None, tool_calls: int = 0, reason: str | None = None) -> LoopResult:
    payload: dict[str, Any] = {}
    if reply is not None:
        payload["reply"] = reply
    if ask is not None:
        payload["ask"] = ask
    if reason:
        payload["terminal_reason"] = reason
    cancelled = reason in {"cancelled", "cancel_requested"}
    if cancelled:
        payload["cancel_requested"] = True
        token = getattr(ctx, "cancellation", None)
        acknowledge = getattr(token, "acknowledge", None)
        if callable(acknowledge):
            try:
                acknowledge()
            except Exception:  # noqa: BLE001 - cancellation acknowledgement is best effort
                pass
    _append_event(store, ctx, EVENT_AGENT_STEP, actor=ACTOR_AGENT, source=ACTOR_AGENT, payload=payload)
    return LoopResult(
        reply=reply,
        ask=ask,
        tool_calls=tool_calls,
        terminal_reason=reason,
        cancelled=cancelled,
    )


def _budget_finish(store: SQLiteStore | None, ctx: ToolContext, *, reason: str,
                   limit: int, tool_calls: int, message: str) -> LoopResult:
    _append_event(
        store,
        ctx,
        EVENT_BUDGET,
        actor=ACTOR_ORCH,
        source=ACTOR_ORCH,
        payload={"kind": reason, "limit": limit, "tool_calls": tool_calls},
    )
    return _finish(store, ctx, reply=message, tool_calls=tool_calls, reason=reason)


def _memory_context(ctx: ToolContext, limit: int = 6) -> list[str]:
    memory = getattr(ctx, "memory", None)
    if memory is None or not hasattr(memory, "to_context"):
        return []
    try:
        return [str(item)[:1200] for item in memory.to_context(max_items=limit)]
    except Exception:  # noqa: BLE001
        return []


class _LegacyContextBudgetExceeded(RuntimeError):
    """Local fallback used only while the V2 context module is unavailable."""


def _context_budget_error_type():
    """Return the public V2 budget exception without hard-importing it.

    The integration branch is intentionally mergeable before the context-core
    branch.  Once that branch lands, this resolves to its public exception;
    the tiny local type keeps old checkouts/tests importable in the interim.
    """

    try:
        from .context_assembler import ContextBudgetExceeded

        return ContextBudgetExceeded
    except (ImportError, AttributeError):
        return _LegacyContextBudgetExceeded


def _raise_context_budget(message: str) -> None:
    raise _context_budget_error_type()(message)


def _is_context_budget_error(error: BaseException) -> bool:
    error_type = _context_budget_error_type()
    return isinstance(error, error_type) or "contextbudget" in type(error).__name__.lower()


def _budget_limit(ctx: ToolContext) -> int | None:
    """Resolve the provider-input budget from a V2 budget-like object."""

    budget = getattr(ctx, "context_budget", None)
    if budget is None:
        return None
    if isinstance(budget, dict):
        hard = budget.get("hard_limit_tokens")
        maximum = budget.get("max_input_tokens")
        reserve = budget.get("reserve_output_tokens", 0)
        if hard is not None:
            try:
                return int(hard)
            except (TypeError, ValueError):
                return None
    else:
        hard = getattr(budget, "hard_limit_tokens", None)
        maximum = getattr(budget, "max_input_tokens", None)
        reserve = getattr(budget, "reserve_output_tokens", 0)
        if hard is not None:
            try:
                return int(hard)
            except (TypeError, ValueError):
                return None
    try:
        value = int(maximum) - int(reserve)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else value


def _message_tokens(message: dict) -> int:
    from .safety import estimate_tokens

    return estimate_tokens(_dump(message))


def _ensure_current_user(messages: list[dict], user_text: str) -> list[dict]:
    """Ensure the current user text occurs exactly once in an assembled list."""

    matches = [
        index
        for index, message in enumerate(messages)
        if message.get("role") == "user" and str(message.get("content") or "") == user_text
    ]
    if not matches:
        return [*messages, {"role": "user", "content": user_text}]
    keep = matches[-1]
    return [message for index, message in enumerate(messages) if index == keep or index not in matches]


def _legacy_initial_messages(
    store: SQLiteStore,
    ctx: ToolContext,
    user_text: str,
    system_msg: str,
    skills: list | None,
) -> list[dict]:
    """V1 projection fallback; never used after context-core is installed."""

    fallback_system = system_msg
    if skills:
        skill_text = "\n\n".join(f"【当前任务方法论：{sk.slug}】\n{sk.body}" for sk in skills)
        fallback_system += "\n\n" + skill_text
    memory_lines = _memory_context(ctx)
    if memory_lines:
        fallback_system += "\n\n[bounded research memory; constraints, not evidence]\n" + "\n".join(memory_lines)
    messages: list[dict] = [{"role": "system", "content": fallback_system}]
    messages.extend(_history_messages(store, ctx.idea))
    if not _current_user_is_recorded(store, ctx.idea, user_text):
        messages.append({"role": "user", "content": user_text})
    return _ensure_current_user(messages, user_text)


def _assemble_initial_messages(
    store: SQLiteStore,
    ctx: ToolContext,
    *,
    user_text: str,
    system_msg: str,
    skills: list | None,
    tool_schemas: list[dict] | None = None,
) -> tuple[list[dict], Any]:
    """Build the first provider projection through the V2 public API."""

    try:
        from .context_assembler import ContextAssembler
    except (ImportError, AttributeError):
        messages = _legacy_initial_messages(store, ctx, user_text, system_msg, skills)
        limit = _budget_limit(ctx)
        if limit is not None:
            messages = _bounded_messages(messages, ctx, user_text=user_text, tool_schemas=tool_schemas or [])
        return messages, None

    assembled = ContextAssembler().assemble(
        store=store,
        ctx=ctx,
        user_text=user_text,
        system=system_msg,
        skills=tuple(skills or ()),
        budget=getattr(ctx, "context_budget", None),
        tool_schemas=tuple(tool_schemas or ()),
    )
    messages = getattr(assembled, "messages", None)
    if not isinstance(messages, list) or not all(isinstance(item, dict) for item in messages):
        raise RuntimeError("ContextAssembler 返回了无效 messages")
    return _ensure_current_user(list(messages), user_text), assembled


def _bounded_messages(
    messages: list[dict],
    ctx: ToolContext,
    *,
    user_text: str,
    tool_schemas: list[dict] | None = None,
    store: SQLiteStore | None = None,
    return_projection: bool = False,
) -> list[dict] | tuple[list[dict], Any]:
    """Keep a later provider request within the input budget.

    The V2 assembler owns the semantic projection for both initial and
    subsequent requests.  The old local implementation below remains as a
    compatibility fallback for integrations that temporarily monkeypatch the
    V2 module during migration.
    """

    try:
        from .context_assembler import ContextAssembler

        bound = getattr(ContextAssembler(), "bound", None)
        if callable(bound):
            projection = bound(
                messages=messages,
                user_text=user_text,
                tool_schemas=tuple(tool_schemas or ()),
                budget=getattr(ctx, "context_budget", None),
                store=store,
                ctx=ctx if store is not None else None,
            )
            if return_projection:
                return list(projection.messages), projection
            return list(projection.messages)
    except Exception as exc:  # noqa: BLE001 - fallback below preserves legacy callers
        if _is_context_budget_error(exc):
            raise

    limit = _budget_limit(ctx)
    if limit is None:
        return (messages, None) if return_projection else messages
    if limit <= 0:
        _raise_context_budget("context budget 必须大于 reserve_output_tokens")

    system_indices = [index for index, message in enumerate(messages) if message.get("role") == "system"]
    mandatory: set[int] = set(system_indices[:1])
    user_indices = [
        index
        for index, message in enumerate(messages)
        if message.get("role") == "user"
        and (user_text == "" or str(message.get("content") or "") == user_text)
    ]
    if user_indices:
        mandatory.add(user_indices[-1])
    elif messages:
        # A custom caller may provide no exact match; the final user message
        # remains the safest current-turn anchor.
        mandatory.add(next((index for index in range(len(messages) - 1, -1, -1)
                           if messages[index].get("role") == "user"), len(messages) - 1))

    used = sum(_message_tokens(messages[index]) for index in mandatory)
    if used > limit:
        _raise_context_budget("system instructions and current user message exceed context budget")

    # Build complete units, grouping an assistant tool call with all
    # contiguous tool results.  A unit containing a mandatory item is left in
    # place and cannot be partially selected.
    units: list[list[int]] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        if message.get("role") == "assistant" and message.get("tool_calls"):
            end = index + 1
            while end < len(messages) and messages[end].get("role") == "tool":
                end += 1
            units.append(list(range(index, end)))
            index = end
        else:
            units.append([index])
            index += 1

    selected = set(mandatory)
    # Newest complete interactions have higher value than stale tail items.
    for unit in reversed(units):
        if any(item in mandatory for item in unit):
            continue
        unit_tokens = sum(_message_tokens(messages[item]) for item in unit)
        if used + unit_tokens <= limit:
            selected.update(unit)
            used += unit_tokens

    bounded = [message for index, message in enumerate(messages) if index in selected]
    return (bounded, None) if return_projection else bounded


def _is_provider_overflow(error: BaseException) -> bool:
    """Conservative provider overflow classifier for heterogeneous SDK errors."""

    if isinstance(error, ProviderError) and error.code == "context_overflow":
        return True

    name = type(error).__name__.lower()
    details = " ".join(
        str(getattr(error, attribute, "") or "")
        for attribute in ("code", "error_code", "status_code", "status", "type", "reason")
    )
    text = f"{error} {details}".lower()
    marker = (
        "context_length_exceeded",
        "context length",
        "maximum context",
        "maximum tokens",
        "max context",
        "max_tokens",
        "prompt is too long",
        "prompt too long",
        "prompt length",
        "too many tokens",
        "input token",
        "token limit",
        "input is too long",
        "request too large",
        "request_too_large",
        "payload too large",
        "length limit",
        "413",
    )
    return any(value in name or value in text for value in marker)


def _provider_attempts(provider: Any) -> int | None:
    """Read a route-owned physical attempt counter when available."""

    value = getattr(provider, "attempts_used", None)
    try:
        return max(0, int(value)) if value is not None else None
    except (TypeError, ValueError):
        return None


def _provider_begin_step(provider: Any) -> None:
    begin = getattr(provider, "begin_step", None)
    if callable(begin):
        begin()


def _provider_set_privacy(provider: Any, mode: str) -> None:
    setter = getattr(provider, "set_privacy_mode", None)
    if callable(setter):
        setter(mode)
        return
    # Compatibility for simple route adapters exposing a mutable field.
    if hasattr(provider, "privacy_mode"):
        try:
            setattr(provider, "privacy_mode", mode)
        except Exception:  # noqa: BLE001 - the gate below remains authoritative
            return


def _provider_set_request_id(provider: Any, request_id: str | None) -> None:
    setter = getattr(provider, "set_request_id", None)
    if callable(setter):
        try:
            setter(request_id)
        except Exception:  # noqa: BLE001 - request metadata cannot weaken the gate
            return
        return
    if hasattr(provider, "request_id"):
        try:
            setattr(provider, "request_id", request_id)
        except Exception:  # noqa: BLE001 - route metadata is best effort
            return


def _provider_failure_message(error: BaseException) -> str:
    if isinstance(error, ProviderError):
        return error.safe_message
    return "模型调用失败。"


_SAFE_PROVIDER_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")


def _safe_provider_label(value: Any, *, fallback: str) -> str:
    """Return a bounded catalog-like label for provider lifecycle events."""

    text = str(value or "").strip()
    return text if _SAFE_PROVIDER_LABEL.fullmatch(text) else fallback


def _provider_model_label(provider: Any) -> str:
    profile = getattr(provider, "profile", None)
    model = getattr(profile, "model", None) if profile is not None else None
    if model is None:
        model = getattr(provider, "model", None)
    return _safe_provider_label(model, fallback="model_unknown")


def _bounded_nonnegative_int(value: Any, *, maximum: int = 10_000_000) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if 0 <= parsed <= maximum else None


def _provider_usage(response: Any) -> dict[str, int]:
    """Normalize provider usage without copying arbitrary response fields."""

    if not isinstance(response, dict):
        return {}
    raw = response.get("usage")
    if not isinstance(raw, dict):
        return {}
    aliases = {
        "input_tokens": ("input_tokens", "prompt_tokens"),
        "output_tokens": ("output_tokens", "completion_tokens"),
        "total_tokens": ("total_tokens",),
    }
    usage: dict[str, int] = {}
    for target, keys in aliases.items():
        for key in keys:
            value = _bounded_nonnegative_int(raw.get(key))
            if value is not None:
                usage[target] = value
                break
    return usage


def _provider_turn_payload(
    provider: Any,
    *,
    turn_index: int,
    attempt_index: int,
    finalization_only: bool,
    tools_exposed: int,
    projection: Any = None,
) -> dict[str, Any]:
    """Build the allow-listed metadata shared by provider lifecycle events."""

    payload: dict[str, Any] = {
        "provider": _safe_provider_label(_provider_name(provider), fallback="provider_unknown"),
        "model": _provider_model_label(provider),
        "turn_index": max(1, int(turn_index)),
        "attempt_index": max(1, int(attempt_index)),
        "finalization_only": bool(finalization_only),
        "tools_exposed": max(0, min(int(tools_exposed), 256)),
    }
    snapshot = getattr(projection, "budget_snapshot", None)
    estimated = _bounded_nonnegative_int(getattr(snapshot, "estimated_input_tokens", None))
    budget = _bounded_nonnegative_int(getattr(snapshot, "hard_limit_tokens", None))
    if estimated is not None:
        payload["estimated_input_tokens"] = estimated
    if budget is not None:
        payload["hard_input_tokens"] = budget
    return payload


def _append_provider_turn_event(
    store: SQLiteStore | None,
    ctx: ToolContext,
    event_type: str,
    *,
    base: dict[str, Any],
    duration_ms: int | None = None,
    attempt_count: int | None = None,
    response: Any = None,
    error: ProviderError | None = None,
) -> None:
    """Append a safe provider lifecycle event without affecting control flow."""

    payload = dict(base)
    if duration_ms is not None:
        payload["duration_ms"] = max(0, min(int(duration_ms), 86_400_000))
    if attempt_count is not None:
        payload["attempt_count"] = max(1, min(int(attempt_count), 16))
    if event_type == EVENT_PROVIDER_TURN_COMPLETED:
        calls = response.get("tool_calls") if isinstance(response, dict) else None
        payload["response_kind"] = (
            "tool_calls" if isinstance(calls, list) and calls else
            "text" if isinstance(response, dict) and isinstance(response.get("content"), str) and response.get("content") else
            "empty"
        )
        payload["tool_call_count"] = min(len(calls), 256) if isinstance(calls, list) else 0
        usage = _provider_usage(response)
        if usage:
            payload["usage"] = usage
    elif event_type == EVENT_PROVIDER_TURN_FAILED:
        safe = error or ProviderError("provider_error")
        payload["error_code"] = safe.code
        payload["retryable"] = bool(safe.retryable)
        if safe.attempt:
            payload["attempt_count"] = min(max(0, int(safe.attempt)), 16)
    _append_event(store, ctx, event_type, actor=ACTOR_ORCH, source=ACTOR_ORCH, payload=payload)


def _append_fallback_audit(store: SQLiteStore | None, ctx: ToolContext, data: dict[str, Any]) -> bool:
    """Append only allow-listed fallback metadata before the next provider call."""

    if store is None:
        return False
    payload = {
        key: data.get(key)
        for key in ("from_provider", "to_provider", "reason_code", "attempt", "privacy_mode", "request_id")
        if data.get(key) is not None
    }
    try:
        store.append(Event(
            idea_id=ctx.idea,
            event_type=EVENT_FALLBACK,
            actor=ACTOR_ORCH,
            source=ACTOR_ORCH,
            correlation_id=getattr(ctx, "request_id", None),
            phase=_phase(store, ctx),
            payload=payload,
        ))
    except Exception:  # noqa: BLE001 - no audit means no fallback may proceed
        return False
    return True


def _compact_and_reassemble(
    store: SQLiteStore,
    ctx: ToolContext,
    *,
    user_text: str,
    system_msg: str,
    skills: list | None,
    previous_ephemeral: list[dict],
    tool_schemas: list[dict] | None = None,
) -> tuple[list[dict], Any] | None:
    """Compact once and rebuild a fresh projection for an overflow retry."""

    try:
        from . import compaction as compaction_module
    except ImportError:
        return None
    try:
        compaction_module.compact(
            store,
            ctx.idea,
            reason="token_pressure",
            summarizer=getattr(ctx, "compaction_summarizer", None),
        )
        messages, assembled = _assemble_initial_messages(
            store,
            ctx,
            user_text=user_text,
            system_msg=system_msg,
            skills=skills,
            tool_schemas=tool_schemas or [],
        )
        messages.extend(previous_ephemeral)
        return messages, assembled
    except Exception:
        return None


def _provider_name(provider: Any) -> str:
    value = getattr(provider, "provider", None)
    if value:
        return str(value).strip().lower()
    return provider.__class__.__name__.strip().lower()


def _safe_call_id(value: Any) -> str | None:
    """Return a provider call id only when it is safe and bounded.

    Provider ids are opaque display/trace identifiers.  Treat malformed ids
    as absent so a provider cannot inject control characters or an unbounded
    value into the assistant transcript, ledger, or UI projections.
    """

    if not isinstance(value, str):
        return None
    value = value.strip()
    if not 1 <= len(value) <= 128:
        return None
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        return None
    return value


def _stable_tool_call_id(
    call: dict[str, Any],
    ctx: ToolContext,
    *,
    ordinal: int,
    used: set[str],
) -> str:
    """Assign one unique id for a tool call within the current request.

    Live providers usually return an id.  Older/mock providers do not, and
    some providers have been observed to reuse an id across model turns.  A
    request-scoped, monotonically numbered fallback is deterministic and
    avoids making the UI pair same-name calls by FIFO.
    """

    supplied = _safe_call_id(call.get("id"))
    if supplied is not None and supplied not in used:
        used.add(supplied)
        return supplied

    request_id = _safe_call_id(getattr(ctx, "request_id", None))
    base = request_id or _safe_call_id(getattr(ctx, "idea", None)) or "agent"
    candidate_ordinal = max(1, int(ordinal))
    candidate = f"{base}:tool:{candidate_ordinal}"
    while candidate in used:
        candidate_ordinal += 1
        candidate = f"{base}:tool:{candidate_ordinal}"
    used.add(candidate)
    return candidate


def _assistant_tool_message(
    calls: list[dict],
    *,
    request_id: str | None = None,
    offset: int = 0,
) -> dict:
    encoded: list[dict] = []
    for index, call in enumerate(calls):
        if not isinstance(call, dict):
            call = {}
        arguments = call.get("arguments")
        if isinstance(arguments, str):
            argument_text = arguments
        else:
            argument_text = _dump(arguments if arguments is not None else {})
        encoded.append({
            "id": call.get("id")
            or (f"{request_id}:tool:{offset + index + 1}" if request_id else f"agent-tool-{index}"),
            "type": "function",
            "function": {
                "name": str(call.get("name") or ""),
                "arguments": argument_text,
            },
        })
    return {"role": "assistant", "content": None, "tool_calls": encoded}


def _tool_context_text(tool: Tool | None, result: dict) -> str:
    try:
        return tool.result_to_context(result) if tool else _stringify(result)
    except Exception as exc:  # noqa: BLE001
        return f"工具结果摘要失败：{exc}"


def _active_tools(tools: dict[str, Tool], ctx: ToolContext, skills: list | None) -> dict[str, Tool]:
    """Resolve the single active tool set used for schemas and execution."""

    active: dict[str, Tool] = {}
    for name, tool in tools.items():
        try:
            if tool.enabled(ctx):
                active[name] = tool
        except Exception:
            continue
    if skills:
        allowed: set[str] = set()
        for skill in skills:
            allowed.update(getattr(skill, "allowed_tools", ()) or ())
        if allowed:
            active = {
                name: tool
                for name, tool in active.items()
                if name in allowed or name == "ask_user"
            }
    return active


def _has_compactable_history(store: SQLiteStore, idea: str) -> bool:
    """Return whether a physical compaction attempt can make progress.

    This is intentionally conservative.  The compactor remains the authority
    on semantic safety; the loop only avoids creating a boundary for an empty
    ledger or a ledger containing no post-checkpoint events.
    """

    try:
        from .compaction import latest_valid_boundary

        events = list(store.scan(idea))
        boundary = latest_valid_boundary(events)
        lower = boundary.to_seq + 1 if boundary is not None else 1
        return any(
            int(event.seq or 0) >= lower
            and event.event_type not in {"compaction.boundary", "context.assembled"}
            for event in events
        )
    except Exception:  # noqa: BLE001 - no compaction candidate is safe by default
        return False


def _snapshot_is_soft_exceeded(projection: Any) -> bool:
    snapshot = getattr(projection, "budget_snapshot", None)
    return bool(getattr(snapshot, "soft_exceeded", False))


def run_loop(
    store: SQLiteStore,
    provider,
    tools: dict[str, Tool],
    ctx: ToolContext,
    *,
    user_text: str,
    system: str | None = None,
    max_steps: int = 12,
    max_tool_calls: int = 32,
    privacy_mode: str | None = None,
    skills: list | None = None,
    on_event=None,
    cancellation=None,
    cancel_token=None,
    cancel_event=None,
) -> LoopResult:
    """Run a bounded function-calling loop.

    ``max_steps`` bounds provider turns; ``max_tool_calls`` bounds individual
    calls even when one provider response contains a large batch.  Once a tool
    has run, the final provider turn is reserved for synthesising its results
    with no tool schemas.  This prevents a successful side effect from ending
    in a bare budget error before the user receives an answer.
    """

    token = (
        cancellation
        or cancel_token
        or cancel_event
        or getattr(ctx, "cancellation", None)
        or getattr(ctx, "cancel_token", None)
        or getattr(ctx, "cancellation_token", None)
    )
    if token is not None:
        # ToolContext aliases are normalized by its __post_init__, but callers
        # may pass an older context-like object.  Set all supported names so
        # handlers and custom tools observe the same token.
        for attr in ("cancellation", "cancel_token", "cancellation_token"):
            try:
                setattr(ctx, attr, token)
            except Exception:  # noqa: BLE001 - a read-only context can still be polled below
                pass
    if is_cancel_requested(token):
        return _finish(
            store,
            ctx,
            reply=f"（本轮已取消：{cancellation_reason(token)}。）",
            reason="cancelled",
        )

    effective_mode = privacy_mode if privacy_mode is not None else getattr(ctx, "privacy_mode", LOCAL_STRICT)
    try:
        effective_mode = normalize_mode(effective_mode)
    except Exception:  # noqa: BLE001 - unknown mode is a hard stop
        return _finish(store, ctx, reply="隐私模式无效，已拒绝本轮。", reason="privacy_denied")

    provider_name = _provider_name(provider)
    _provider_set_privacy(provider, effective_mode)
    _provider_set_request_id(provider, getattr(ctx, "request_id", None))
    if not remote_llm_allowed(effective_mode, provider):
        return _finish(
            store,
            ctx,
            reply="当前隐私策略不允许调用该模型。",
            reason="privacy_denied",
        )
    if provider_is_remote(provider):
        try:
            configured_live = getattr(ctx, "live_provider_enabled", None)
            if configured_live is None:
                from ..config import live_provider_enabled

                configured_live = live_provider_enabled()
            if not configured_live:
                return _finish(
                    store,
                    ctx,
                    reply="live provider 未显式启用，本轮未调用网络。",
                    reason="provider_unavailable",
                )
        except Exception:  # noqa: BLE001 - configuration failures fail closed
            return _finish(
                store,
                ctx,
                reply="live provider 配置无效，本轮未调用网络。",
                reason="provider_unavailable",
            )
    if not hasattr(provider, "chat"):
        return _finish(store, ctx, reply="（当前 provider 不支持工具调用，仅能聊天。）", reason="provider_unsupported")

    set_fallback_callback = getattr(provider, "set_fallback_callback", None)
    if callable(set_fallback_callback):
        set_fallback_callback(lambda data: _append_fallback_audit(store, ctx, data))

    system_msg = system or (
        "你是 stata-agent，一个通用对话助手，配备了一些实证研究工具（Stata 回归、"
        "文献检索、写初稿等）。底层模型是 deepseek（可通过配置切换），不是 Claude/GPT/Anthropic，"
        "自我介绍不要声称是别家模型。"
        "你首先是个正常助手：闲聊、提问、解释概念都直接回答，不要强行往研究上带。"
        "只有当用户明确要做研究（验证假设、跑回归、写初稿）时才调用对应工具。"
        "缺关键信息（如数据文件路径）用 ask_user 问用户，不要猜。"
        "工具结果只当事实，不编造数字或引用。"
        "效率规则：run_stata 返回了结果（含机器层数值）就是成功，不要重跑相同命令；"
        "拿到的结果够用就推进下一步（write_draft 出稿或换稳健性变体）。"
        "回复风格：直接、简短、紧扣用户问题；不要主动罗列能力清单或反复自我介绍；"
        "只有用户明确问'你能做什么'才简要列举。空泛输入（你好/在吗）一句话回应即可。"
    )
    # Resolve the active tool set before assembly so schemas are included in
    # the same pre-I/O budget snapshot as messages.
    active = _active_tools(tools, ctx, skills)
    tool_schemas = [tool.as_openai() for tool in active.values()]

    # ContextAssembler is the sole owner of both initial and later request
    # projections.  The fallback retains the pre-V2 projection until the
    # context-core branch is merged, while the public API stays identical.
    try:
        messages, assembled = _assemble_initial_messages(
            store,
            ctx,
            user_text=user_text,
            system_msg=system_msg,
            skills=skills,
            tool_schemas=tool_schemas,
        )
    except Exception as exc:  # noqa: BLE001 - context errors are fail-closed
        if _is_context_budget_error(exc):
            return _budget_finish(
                store,
                ctx,
                reason="context_budget",
                limit=_budget_limit(ctx) or 0,
                tool_calls=0,
                message="（上下文超过本轮预算，未调用模型。请缩短输入或先完成当前步骤。）",
            )
        return _finish(store, ctx, reply="上下文组装失败，本轮未调用模型。", reason="context_error")
    initial_message_count = len(messages)

    enforcer = ToolEnforcer(active)
    tool_calls = 0
    recent_stata: list[str] = []
    overflow_retry_used = False
    compaction_attempted = False
    retry_same_step = False
    physical_attempts = 0
    used_tool_call_ids: set[str] = set()

    try:
        step_limit = max(0, int(max_steps))
        call_limit = max(0, int(max_tool_calls))
    except (TypeError, ValueError):
        return _finish(store, ctx, reply="预算参数无效，已拒绝本轮。", reason="budget_invalid")
    if step_limit == 0:
        return _budget_finish(
            store,
            ctx,
            reason="max_steps",
            limit=step_limit,
            tool_calls=tool_calls,
            message="（达到本轮步数上限，已停下。可继续追问。）",
        )

    for _step in range(step_limit):
        finalization_only = _step == step_limit - 1 and tool_calls > 0
        if not retry_same_step:
            physical_attempts = 0
            _provider_begin_step(provider)
        retry_same_step = False
        if is_cancel_requested(token):
            return _finish(
                store,
                ctx,
                reply=f"（本轮已取消：{cancellation_reason(token)}。）",
                tool_calls=tool_calls,
                reason="cancelled",
            )
        content, calls = None, None
        turn_started: float | None = None
        turn_base: dict[str, Any] | None = None
        try:
            # Keep the unbounded in-memory transcript for a possible overflow
            # retry; every actual attempt is projected by the same assembler
            # entry point, including the first request.
            bounded = _bounded_messages(
                messages,
                ctx,
                user_text=user_text,
                tool_schemas=tool_schemas,
                store=store,
                return_projection=True,
            )
            prompt_source, projection = bounded

            # Soft pressure is a deterministic pre-I/O signal.  Physical
            # compaction is shared with overflow recovery and is attempted at
            # most once for this run; the compactor itself validates the safe
            # semantic cut.
            if (
                _snapshot_is_soft_exceeded(projection)
                and not compaction_attempted
                and _has_compactable_history(store, ctx.idea)
            ):
                compaction_attempted = True
                previous_ephemeral = messages[initial_message_count:]
                rebuilt = _compact_and_reassemble(
                    store,
                    ctx,
                    user_text=user_text,
                    system_msg=system_msg,
                    skills=skills,
                    previous_ephemeral=previous_ephemeral,
                    tool_schemas=tool_schemas,
                )
                if rebuilt is not None:
                    messages, assembled = rebuilt
                    initial_message_count = len(messages) - len(previous_ephemeral)
                    bounded = _bounded_messages(
                        messages,
                        ctx,
                        user_text=user_text,
                        tool_schemas=tool_schemas,
                        store=store,
                        return_projection=True,
                    )
                    prompt_source, projection = bounded

            snapshot = getattr(projection, "budget_snapshot", None)
            if getattr(snapshot, "overflow", False):
                _raise_context_budget(
                    "complete provider request exceeds hard context budget before provider I/O"
                )
            prompt_messages = sanitize_messages(prompt_source, mode=effective_mode, provider=provider)
            available_tool_schemas = [] if finalization_only else tool_schemas
            if callable(getattr(provider, "begin_step", None)):
                # ProviderRouter freezes one privacy projection for all of its
                # candidates.  A plain provider is sanitized here, including
                # its tool schemas, before every physical attempt.
                provider_tool_schemas = available_tool_schemas
            else:
                provider_tool_schemas = sanitize_tool_schemas(
                    available_tool_schemas,
                    mode=effective_mode,
                    provider=provider,
                )
        except Exception as exc:  # noqa: BLE001 - budget failures must stop before I/O
            if _is_context_budget_error(exc):
                return _budget_finish(
                    store,
                    ctx,
                    reason="context_budget",
                    limit=_budget_limit(ctx) or 0,
                    tool_calls=tool_calls,
                    message="（工具结果使上下文超过本轮预算，已安全停下。请继续追问。）",
                )
            return _finish(store, ctx, reply="上下文组装失败，本轮未调用模型。", tool_calls=tool_calls,
                           reason="context_error")
        try:
            raise_if_cancelled(token)
            stream_had_delta = False
            before_attempts = _provider_attempts(provider)
            if before_attempts is not None and before_attempts >= 2:
                raise ProviderError(
                    "provider_unavailable",
                    provider=provider_name,
                    attempt=before_attempts,
                    request_id=getattr(ctx, "request_id", None),
                )
            if before_attempts is None and physical_attempts >= 2:
                raise ProviderError(
                    "provider_unavailable",
                    provider=provider_name,
                    attempt=physical_attempts,
                    request_id=getattr(ctx, "request_id", None),
                )
            if before_attempts is None:
                physical_attempts += 1
                attempt_index = physical_attempts
            else:
                # ProviderRouter owns its physical attempt counter.  The
                # outer loop still exposes the next 1-based attempt without
                # probing the provider a second time.
                attempt_index = max(1, before_attempts + 1)
            turn_started = time.perf_counter()
            turn_base = _provider_turn_payload(
                provider,
                turn_index=_step + 1,
                attempt_index=attempt_index,
                finalization_only=finalization_only,
                tools_exposed=len(provider_tool_schemas),
                projection=projection,
            )
            _append_provider_turn_event(
                store,
                ctx,
                EVENT_PROVIDER_TURN_STARTED,
                base=turn_base,
            )
            response_for_event: dict[str, Any] = {}
            if on_event and hasattr(provider, "stream_chat"):
                stream_done = False
                for event in provider.stream_chat(prompt_messages, tools=provider_tool_schemas):
                    raise_if_cancelled(token)
                    if not isinstance(event, dict):
                        raise RuntimeError("provider stream event 不是 object")
                    event_type = event.get("type")
                    if event_type == "text_delta":
                        stream_had_delta = True
                        if on_event:
                            on_event({"type": "text_delta", "text": event.get("text") or ""})
                    elif event_type == "done":
                        stream_done = True
                        content, calls = event.get("content"), event.get("tool_calls")
                    elif event_type == "error":
                        raise RuntimeError(str(event.get("message") or "provider stream error"))
                if not stream_done:
                    raise RuntimeError("provider stream 异常 EOF：缺少 done")
                response_for_event = {"content": content, "tool_calls": calls}
            else:
                response = provider.chat(prompt_messages, tools=provider_tool_schemas)
                if not isinstance(response, dict):
                    raise RuntimeError("provider response 不是 object")
                content, calls = response.get("content"), response.get("tool_calls")
                response_for_event = response
            after_attempts = _provider_attempts(provider)
            if after_attempts is not None:
                physical_attempts = max(physical_attempts, after_attempts)
            raise_if_cancelled(token)
            _append_provider_turn_event(
                store,
                ctx,
                EVENT_PROVIDER_TURN_COMPLETED,
                base=turn_base,
                duration_ms=round((time.perf_counter() - turn_started) * 1000),
                attempt_count=physical_attempts,
                response=response_for_event,
            )
        except CancellationRequested:
            if turn_started is not None and turn_base is not None:
                _append_provider_turn_event(
                    store,
                    ctx,
                    EVENT_PROVIDER_TURN_FAILED,
                    base=turn_base,
                    duration_ms=round((time.perf_counter() - turn_started) * 1000),
                    attempt_count=physical_attempts,
                    error=ProviderError("provider_error", retryable=False),
                )
            return _finish(
                store,
                ctx,
                reply=f"（本轮已取消：{cancellation_reason(token)}。）",
                tool_calls=tool_calls,
                reason="cancelled",
            )
        except Exception as exc:  # noqa: BLE001 - provider failures are terminal and ledgered
            safe_error = provider_error_from_exception(
                exc,
                provider=provider_name,
                attempt=physical_attempts,
                request_id=getattr(ctx, "request_id", None),
            )
            if turn_started is not None and turn_base is not None:
                _append_provider_turn_event(
                    store,
                    ctx,
                    EVENT_PROVIDER_TURN_FAILED,
                    base=turn_base,
                    duration_ms=round((time.perf_counter() - turn_started) * 1000),
                    attempt_count=physical_attempts,
                    error=safe_error,
                )
            # A stream that emitted a visible delta cannot be replayed safely;
            # retrying it would duplicate UI output.  The same guard applies
            # to context overflow discovered after a partial stream.
            if (
                _is_provider_overflow(safe_error)
                and not stream_had_delta
                and not overflow_retry_used
                and physical_attempts < 2
            ):
                overflow_retry_used = True
                previous_ephemeral = messages[initial_message_count:]
                rebuilt = None
                if not compaction_attempted:
                    compaction_attempted = True
                    rebuilt = _compact_and_reassemble(
                        store,
                        ctx,
                        user_text=user_text,
                        system_msg=system_msg,
                        skills=skills,
                        previous_ephemeral=previous_ephemeral,
                        tool_schemas=tool_schemas,
                    )
                    if rebuilt is None:
                        # Provider-side under-counting can happen even when
                        # the ledger has no safe physical compaction range.
                        # The request was already locally within hard budget,
                        # so one bounded retry of the same complete projection
                        # is safe; a second provider overflow remains
                        # terminal below.
                        rebuilt = (messages, assembled)
                elif previous_ephemeral:
                    # A proactive compaction may already have consumed the
                    # physical boundary attempt before the provider rejects
                    # the request.  The provider retry is still bounded, but
                    # it must rebuild from the canonical baseline and append
                    # the complete in-memory tool turn; otherwise the retry
                    # path can silently reuse the over-sized request.
                    try:
                        baseline, baseline_assembled = _assemble_initial_messages(
                            store,
                            ctx,
                            user_text=user_text,
                            system_msg=system_msg,
                            skills=skills,
                            tool_schemas=tool_schemas,
                        )
                        baseline.extend(previous_ephemeral)
                        rebuilt = (baseline, baseline_assembled)
                    except Exception:
                        rebuilt = None
                else:
                    # An active pre-I/O compaction already produced the
                    # freshest baseline; one bounded retry is still allowed
                    # for providers that undercount the request locally.
                    rebuilt = (messages, assembled)
                if rebuilt is not None:
                    messages, assembled = rebuilt
                    initial_message_count = len(messages) - len(previous_ephemeral)
                    # The retry is still the same bounded provider step.  A
                    # second overflow is terminal by construction.
                    retry_same_step = True
                    continue
            if (
                safe_error.retryable
                and not stream_had_delta
                and physical_attempts < 2
            ):
                # Plain providers do not own routing; give them one bounded
                # transient retry.  ProviderRouter already consumed its
                # internal attempt budget, so this branch normally applies
                # only to legacy/custom providers.
                retry_same_step = True
                continue
            if _is_context_budget_error(exc) or safe_error.code == "context_overflow":
                return _budget_finish(
                    store,
                    ctx,
                    reason="context_budget",
                    limit=_budget_limit(ctx) or 0,
                    tool_calls=tool_calls,
                    message="（上下文超过本轮预算，未继续调用模型。）",
                )
            return _finish(
                store,
                ctx,
                reply=_provider_failure_message(safe_error),
                tool_calls=tool_calls,
                reason="provider_error",
            )

        if not calls:
            return _finish(store, ctx, reply=content or "", tool_calls=tool_calls, reason="model_stop")
        if not isinstance(calls, list):
            return _finish(store, ctx, reply="模型返回的工具调用格式无效。", tool_calls=tool_calls, reason="invalid_tool_calls")
        if finalization_only:
            return _budget_finish(
                store,
                ctx,
                reason="max_steps",
                limit=step_limit,
                tool_calls=tool_calls,
                message="（已达到本轮模型回合上限；最后一步未完成结果总结。）",
            )

        remaining = call_limit - tool_calls
        if remaining <= 0:
            return _budget_finish(
                store,
                ctx,
                reason="tool_calls",
                limit=call_limit,
                tool_calls=tool_calls,
                message="（达到本轮工具调用上限，已停下。可继续追问。）",
            )
        selected_raw = calls[:remaining]
        selected: list[dict[str, Any]] = []
        for index, raw_call in enumerate(selected_raw):
            call = dict(raw_call) if isinstance(raw_call, dict) else {}
            call["id"] = _stable_tool_call_id(
                call,
                ctx,
                ordinal=tool_calls + index + 1,
                used=used_tool_call_ids,
            )
            selected.append(call)
        messages.append(_assistant_tool_message(selected))
        for _index, raw_call in enumerate(selected):
            if is_cancel_requested(token):
                return _finish(
                    store,
                    ctx,
                    reply=f"（本轮已取消：{cancellation_reason(token)}。）",
                    tool_calls=tool_calls,
                    reason="cancelled",
                )
            call = raw_call if isinstance(raw_call, dict) else {}
            name = str(call.get("name") or "")
            raw_args = call.get("arguments") if "arguments" in call else {}
            args, _arg_error = normalize_arguments(raw_args)
            if name == "run_stata" and args is not None:
                signature = _dump(args.get("code") or "")
                if signature and len(recent_stata) >= 2 and recent_stata[-1] == signature and recent_stata[-2] == signature:
                    return _finish(
                        store,
                        ctx,
                        reply="主回归已运行，结果已记录。请继续：write_draft 出初稿，或换稳健性变体（不要重跑相同命令）。",
                        tool_calls=tool_calls,
                        reason="duplicate_run_blocked",
                    )
                if signature:
                    recent_stata.append(signature)

            tool = active.get(name)
            call_id = str(call["id"])
            if on_event:
                on_event(
                    {
                        "type": "tool_started",
                        "name": name,
                        "call_id": call_id,
                        "tool_id": call_id,
                    }
                )
            _append_event(
                store,
                ctx,
                EVENT_TOOL_INVOKED,
                actor=ACTOR_ORCH,
                source=ACTOR_ORCH,
                payload={
                    "tool": name,
                    "args": raw_args,
                    "allowed": tool is not None,
                    "call_id": call_id,
                    "tool_id": call_id,
                },
            )
            result = enforcer.execute(name, raw_args, ctx)
            tool_calls += 1
            if on_event:
                on_event(
                    {
                        "type": "tool_completed",
                        "name": name,
                        "ok": bool(result.get("ok")),
                        "call_id": call_id,
                        "tool_id": call_id,
                    }
                )
            _append_event(
                store,
                ctx,
                EVENT_TOOL_DONE,
                actor=ACTOR_ORCH,
                source=ACTOR_ORCH,
                payload={
                    "tool": name,
                    "ok": bool(result.get("ok")),
                    "result": result,
                    "call_id": call_id,
                    "tool_id": call_id,
                },
            )
            context_text = _tool_context_text(tool, result)
            messages.append({
                "role": "tool",
                "tool_call_id": call_id,
                "content": context_text,
            })
            if is_cancel_requested(token):
                result_error = (result.get("error") or {}) if isinstance(result, dict) else {}
                cancel_reason = (
                    "cancel_requested"
                    if result_error.get("type") in {"uncertain", "cancel_requested"}
                    else "cancelled"
                )
                return _finish(
                    store,
                    ctx,
                    reply=f"（本轮已取消：{cancellation_reason(token)}。）",
                    tool_calls=tool_calls,
                    reason=cancel_reason,
                )
            ask = (result.get("data") or {}).get("ask") if isinstance(result, dict) else None
            if ask:
                return _finish(store, ctx, ask=str(ask), tool_calls=tool_calls, reason="ask_user")

        if len(calls) > len(selected):
            return _budget_finish(
                store,
                ctx,
                reason="tool_calls",
                limit=call_limit,
                tool_calls=tool_calls,
                message="（单次模型响应包含过多工具调用，已按预算截断。）",
            )

    return _budget_finish(
        store,
        ctx,
        reason="max_steps",
        limit=step_limit,
        tool_calls=tool_calls,
        message="（达到本轮步数上限，已停下。可继续追问。）",
    )


def _stringify(result: dict) -> str:
    return _dump(result)


def _dump(obj) -> str:
    import json as _json

    return _json.dumps(obj, ensure_ascii=False)
