"""通用 agent loop（LLM + function calling，自主多步）。

This is the single tool-routing path used by the UI.  The model may propose a
tool, but :class:`ToolEnforcer` is the only component allowed to validate or
execute it.  Every normal, failed, or budget-limited termination is recorded
as an ``agent_step`` so the loop is replayable from the ledger.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..events.schema import (
    ACTOR_AGENT,
    ACTOR_ORCH,
    EVENT_AGENT_STEP,
    EVENT_BUDGET,
    EVENT_TOOL_DONE,
    EVENT_TOOL_INVOKED,
    Event,
)
from ..privacy.modes import (
    LOCAL_STRICT,
    normalize_mode,
    remote_llm_allowed,
    sanitize_messages,
)
from ..storage.sqlite_store import SQLiteStore
from ..toolkit import Tool, ToolContext
from .tool_enforcer import ToolEnforcer, normalize_arguments


@dataclass
class LoopResult:
    reply: str | None
    ask: str | None = None
    tool_calls: int = 0

    def __post_init__(self):
        if self.reply is None and self.ask is None:
            self.reply = "（这轮没有产出，请再具体一点。）"


class Guard:
    """Backward-compatible facade for callers that imported the old Guard.

    New code should use :class:`ToolEnforcer` directly.  Keeping this facade
    avoids breaking integrations while ensuring they get the same checks.
    """

    def __init__(self, tools: dict[str, Tool]):
        self.enforcer = ToolEnforcer(tools)

    def validate(self, name: str, arguments: Any, ctx: ToolContext | None = None) -> str | None:
        return self.enforcer.validate(name, arguments, ctx)


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
    _append_event(store, ctx, EVENT_AGENT_STEP, actor=ACTOR_AGENT, source=ACTOR_AGENT, payload=payload)
    return LoopResult(reply=reply, ask=ask, tool_calls=tool_calls)


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


def _provider_name(provider: Any) -> str:
    value = getattr(provider, "provider", None)
    if value:
        return str(value).strip().lower()
    return provider.__class__.__name__.strip().lower()


def _assistant_tool_message(calls: list[dict]) -> dict:
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
            "id": call.get("id") or f"agent-tool-{index}",
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
) -> LoopResult:
    """Run a bounded function-calling loop.

    ``max_steps`` bounds provider turns; ``max_tool_calls`` bounds individual
    calls even when one provider response contains a large batch.  The latter
    is intentionally separate so a single malformed response cannot evade the
    budget by packing 100 calls into one step.
    """

    effective_mode = privacy_mode if privacy_mode is not None else getattr(ctx, "privacy_mode", LOCAL_STRICT)
    try:
        effective_mode = normalize_mode(effective_mode)
    except Exception as exc:  # noqa: BLE001 - unknown mode is a hard stop
        return _finish(store, ctx, reply=f"隐私模式无效，已拒绝本轮：{exc}", reason="privacy_denied")

    provider_name = _provider_name(provider)
    if not remote_llm_allowed(effective_mode, provider_name):
        return _finish(
            store,
            ctx,
            reply="当前 local_strict 不允许把研究内容发送到远端模型。",
            reason="privacy_denied",
        )
    if not hasattr(provider, "chat"):
        return _finish(store, ctx, reply="（当前 provider 不支持工具调用，仅能聊天。）", reason="provider_unsupported")

    system_msg = system or (
        "你是 stata-agent，一个实证研究 agent。你由本项目自主搭建，"
        "底层模型是 deepseek（可通过配置切换），不是 Claude、GPT 或其它厂商模型，"
        "自我介绍时不要声称自己是 Claude/Anthropic。"
        "你可以在回文本和调用工具之间自主选择：普通问题直接回答；"
        "要做研究/查文献/写稿/记约定就调对应工具；"
        "缺关键信息（如数据文件路径）时用 ask_user 问用户，不要猜。"
        "工具结果只当事实，不编造数字或引用。"
        "效率规则：run_stata 一旦返回了结果（含机器层数值），就是成功，"
        "不要重跑相同命令；拿到的结果够用就推进下一步（如 write_draft 出稿或换稳健性变体）。"
    )
    if skills:
        skill_text = "\n\n".join(f"【当前任务方法论：{sk.slug}】\n{sk.body}" for sk in skills)
        system_msg += "\n\n" + skill_text
    memory_lines = _memory_context(ctx)
    if memory_lines:
        system_msg += "\n\n[bounded research memory; constraints, not evidence]\n" + "\n".join(memory_lines)

    messages: list[dict] = [{"role": "system", "content": system_msg}]
    messages.extend(_history_messages(store, ctx.idea))
    if not _current_user_is_recorded(store, ctx.idea, user_text):
        messages.append({"role": "user", "content": user_text})

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
            active = {name: tool for name, tool in active.items() if name in allowed or name == "ask_user"}
    tool_schemas = [tool.as_openai() for tool in active.values()]
    enforcer = ToolEnforcer(active)
    tool_calls = 0
    recent_stata: list[str] = []

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
        content, calls = None, None
        prompt_messages = sanitize_messages(messages, mode=effective_mode, provider=provider_name)
        try:
            if on_event and hasattr(provider, "stream_chat"):
                stream_done = False
                for event in provider.stream_chat(prompt_messages, tools=tool_schemas):
                    if not isinstance(event, dict):
                        raise RuntimeError("provider stream event 不是 object")
                    event_type = event.get("type")
                    if event_type == "text_delta":
                        if on_event:
                            on_event({"type": "text_delta", "text": event.get("text") or ""})
                    elif event_type == "done":
                        stream_done = True
                        content, calls = event.get("content"), event.get("tool_calls")
                    elif event_type == "error":
                        raise RuntimeError(str(event.get("message") or "provider stream error"))
                if not stream_done:
                    raise RuntimeError("provider stream 异常 EOF：缺少 done")
            else:
                response = provider.chat(prompt_messages, tools=tool_schemas)
                if not isinstance(response, dict):
                    raise RuntimeError("provider response 不是 object")
                content, calls = response.get("content"), response.get("tool_calls")
        except Exception as exc:  # noqa: BLE001 - provider failures are terminal and ledgered
            return _finish(store, ctx, reply=f"模型调用失败，本轮未完成：{str(exc)[:200]}", reason="provider_error")

        if not calls:
            return _finish(store, ctx, reply=content or "", tool_calls=tool_calls, reason="model_stop")
        if not isinstance(calls, list):
            return _finish(store, ctx, reply="模型返回的工具调用格式无效。", tool_calls=tool_calls, reason="invalid_tool_calls")

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
        selected = calls[:remaining]
        messages.append(_assistant_tool_message(selected))
        for index, raw_call in enumerate(selected):
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
                        reply="主回归已运行，结果已签入证据链。请继续：write_draft 出初稿，或换稳健性变体（不要重跑相同命令）。",
                        tool_calls=tool_calls,
                        reason="duplicate_run_blocked",
                    )
                if signature:
                    recent_stata.append(signature)

            tool = active.get(name)
            if on_event:
                on_event({"type": "tool_started", "name": name})
            _append_event(
                store,
                ctx,
                EVENT_TOOL_INVOKED,
                actor=ACTOR_ORCH,
                source=ACTOR_ORCH,
                payload={"tool": name, "args": raw_args, "allowed": tool is not None},
            )
            result = enforcer.execute(name, raw_args, ctx)
            tool_calls += 1
            if on_event:
                on_event({"type": "tool_completed", "name": name, "ok": bool(result.get("ok"))})
            _append_event(
                store,
                ctx,
                EVENT_TOOL_DONE,
                actor=ACTOR_ORCH,
                source=ACTOR_ORCH,
                payload={"tool": name, "ok": bool(result.get("ok")), "result": result},
            )
            context_text = _tool_context_text(tool, result)
            messages.append({
                "role": "tool",
                "tool_call_id": call.get("id") or f"agent-tool-{index}",
                "content": context_text,
            })
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
