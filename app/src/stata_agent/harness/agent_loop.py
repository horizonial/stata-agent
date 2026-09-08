"""通用 agent loop（agent-tool-routing.md §2）：LLM + function calling，自主多步。

替换 research_turn：聊天是默认（模型不调工具直接回文本）；要做事就调工具。
护栏在工具执行前：validator（名字/参数在注册表）→ policy（隐私/写类允许）。
每步落 events（agent_step / tool.call / tool.result），Trace 可回放。

返回：最终文本回复（模型自判停）或 ask_user 的问题。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..events.schema import (
    EVENT_AGENT_STEP,
    EVENT_TOOL_INVOKED,
    EVENT_TOOL_DONE,
    ACTOR_AGENT,
    ACTOR_ORCH,
    Event,
)
from ..storage.sqlite_store import SQLiteStore
from ..toolkit import Tool, ToolContext


@dataclass
class LoopResult:
    reply: str | None
    ask: str | None = None
    tool_calls: int = 0

    def __post_init__(self):
        if self.reply is None and self.ask is None:
            self.reply = "（这轮没有产出，请再具体一点。）"


class Guard:
    """工具执行前护栏：validator（未知工具拒绝，防现造工具）。

    更细的 policy/隐私/审批在工具 handler 内部与后续层判定；本层先保证"只能调注册表里的工具"。
    """

    def __init__(self, tools: dict[str, Tool]):
        self.tools = tools

    def validate(self, name: str, arguments: dict) -> str | None:
        """返回 None=通过；否则错误字符串。"""
        if name not in self.tools:
            return f"未知工具 {name!r}（只能调用已注册工具，不许现造）"
        return None


def _history_messages(store: SQLiteStore, idea: str, limit: int = 16) -> list[dict]:
    """把账本投影成 OpenAI 消息（历史）。只取 user/assistant 文本，工具结果不塞历史。"""
    out: list[dict] = []
    for e in store.scan(idea):
        p = e.payload or {}
        if e.event_type == "user.message":
            out.append({"role": "user", "content": str(p.get("text") or "")})
        elif e.event_type == EVENT_AGENT_STEP and not p.get("tool_call"):
            content = str(p.get("ask") or p.get("reply") or p.get("decision_summary") or "")
            if content:
                out.append({"role": "assistant", "content": content})
    return out[-limit:]


def run_loop(
    store: SQLiteStore,
    provider,
    tools: dict[str, Tool],
    ctx: ToolContext,
    *,
    user_text: str,
    system: str | None = None,
    max_steps: int = 12,
    privacy_mode: str = "local_strict",
    skills: list | None = None,
    on_event=None,
) -> LoopResult:
    """通用循环。provider.chat(messages, tools=[...]) 返回 {content, tool_calls}。

    skills：匹配到的方法论（Skill，决策层）。全文注入 system，allowed_tools 约束工具池。
    on_event：可选回调 callable(dict)，流式接收统一 AgentEvent：
      {"type":"text_delta","text":...} / {"type":"tool_started","name":...} / {"type":"tool_completed","name":...}
    """
    if not hasattr(provider, "chat"):
        return LoopResult(reply="（当前 provider 不支持工具调用，仅能聊天。）")

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
    # 决策层注入 Skill 方法论（全文）
    if skills:
        skill_text = "\n\n".join(
            f"【当前任务方法论：{sk.slug}】\n{sk.body}" for sk in skills)
        system_msg = system_msg + "\n\n" + skill_text

    messages: list[dict] = [{"role": "system", "content": system_msg}]
    messages.extend(_history_messages(store, ctx.idea))
    messages.append({"role": "user", "content": user_text})

    # 动态暴露：enabled(ctx) 为真，且被当前 Skill 允许（若 Skill 声明了 allowed_tools）
    active = {name: t for name, t in tools.items() if t.enabled(ctx)}
    if skills:
        allowed = set()
        for sk in skills:
            allowed.update(sk.allowed_tools)
        if allowed:
            active = {name: t for name, t in active.items() if name in allowed or name == "ask_user"}
    tool_schemas = [t.as_openai() for t in active.values()]
    guard = Guard(active)
    tool_calls = 0
    recent_stata: list[str] = []  # 防循环：记录最近 run_stata 的 code

    for _step in range(max_steps):
        content, calls = None, None
        if on_event and hasattr(provider, "stream_chat"):
            for ev in provider.stream_chat(messages, tools=tool_schemas):
                if ev["type"] == "text_delta":
                    on_event({"type": "text_delta", "text": ev["text"]})
                elif ev["type"] == "done":
                    content, calls = ev["content"], ev["tool_calls"]
        else:
            resp = provider.chat(messages, tools=tool_schemas)
            content, calls = resp.get("content"), resp.get("tool_calls")

        if not calls:
            # 最终文本回复
            store.append(Event(idea_id=ctx.idea, event_type=EVENT_AGENT_STEP, actor=ACTOR_AGENT,
                               source=ACTOR_AGENT, phase=ctx.store.project(ctx.idea).phase,
                               payload={"reply": content or ""}))
            return LoopResult(reply=content or "", tool_calls=tool_calls)

        # 有工具调用：逐条护栏 + 执行 + 回填
        # assistant 消息的 tool_calls 必须用 OpenAI 格式（arguments 是 JSON 字符串）
        assistant_msg = {
            "role": "assistant",
            "content": content,
            "tool_calls": [
                {"id": c["id"], "type": "function",
                 "function": {"name": c["name"], "arguments": _dump(c.get("arguments") or {})}}
                for c in calls
            ],
        }
        messages.append(assistant_msg)
        for call in calls:
            name, args = call["name"], call.get("arguments") or {}
            # 防循环护栏：连续 3 次相同 run_stata 代码 → 中断（确定性，不靠模型自觉）
            if name == "run_stata":
                sig = _dump(args.get("code") or "")
                if sig and len(recent_stata) >= 2 and recent_stata[-1] == sig and recent_stata[-2] == sig:
                    store.append(Event(idea_id=ctx.idea, event_type=EVENT_AGENT_STEP, actor=ACTOR_AGENT,
                                       source=ACTOR_AGENT, phase=ctx.store.project(ctx.idea).phase,
                                       payload={"reply": "该回归已运行过且结果已签入证据链，无需重跑。"}))
                    return LoopResult(
                        reply="主回归已运行，结果已签入证据链。请继续：write_draft 出初稿，或换稳健性变体（不要重跑相同命令）。",
                        tool_calls=tool_calls)
                recent_stata.append(sig)
            tool = active.get(name)
            err = guard.validate(name, args)
            if on_event:
                on_event({"type": "tool_started", "name": name})
            store.append(Event(idea_id=ctx.idea, event_type=EVENT_TOOL_INVOKED, actor=ACTOR_ORCH,
                               source=ACTOR_ORCH, phase=ctx.store.project(ctx.idea).phase,
                               payload={"tool": name, "args": args, "allowed": err is None}))
            if err:
                result = {"ok": False, "error": {"type": "guard", "message": err}}
            else:
                try:
                    result = tool.handler(args, ctx)
                except Exception as e:  # noqa: BLE001
                    result = {"ok": False, "error": {"type": "tool_error", "message": str(e)[:200]}}
            tool_calls += 1
            if on_event:
                on_event({"type": "tool_completed", "name": name, "ok": bool(result.get("ok"))})
            store.append(Event(idea_id=ctx.idea, event_type=EVENT_TOOL_DONE, actor=ACTOR_ORCH,
                               source=ACTOR_ORCH, phase=ctx.store.project(ctx.idea).phase,
                               payload={"tool": name, "ok": bool(result.get("ok")), "result": result}))
            # 结果进上下文：用 result_to_context（摘要/截断），不整段塞
            context_text = tool.result_to_context(result) if tool else _stringify(result)
            messages.append({"role": "tool", "tool_call_id": call.get("id"),
                             "content": context_text})
            # ask_user：停，把问题交给用户
            if (result.get("data") or {}).get("ask"):
                ask = result["data"]["ask"]
                store.append(Event(idea_id=ctx.idea, event_type=EVENT_AGENT_STEP, actor=ACTOR_AGENT,
                                   source=ACTOR_AGENT, phase=ctx.store.project(ctx.idea).phase,
                                   payload={"ask": ask}))
                return LoopResult(reply=None, ask=ask, tool_calls=tool_calls)

    return LoopResult(reply="（达到本轮步数上限，已停下。可继续追问。）", tool_calls=tool_calls)


def _stringify(result: dict) -> str:
    return _dump(result)


def _dump(obj) -> str:
    import json as _json

    return _json.dumps(obj, ensure_ascii=False)
