"""chat_structured：把一次模型调用收敛成合法结构（schema 校验 + 自动重试）。

SPEC §4.2：坏输出不进系统；provider 只管文本，这里管"形状正确"。
事实正确（来源）由 EvidenceBundle/validator 管（切片 3+）。
"""

from __future__ import annotations

from typing import Callable

from ..domain.action import ActionProposal
from .codec import StructuredOutputError, parse_proposal

Chat = Callable[[list[dict]], str]  # (messages) -> text


def chat_proposal(
    chat: Chat,
    context: str,
    *,
    max_retries: int = 2,
    probe_system: str | None = None,
) -> ActionProposal:
    """用给定 chat 调用，把上下文换成 ActionProposal；失败自动重试带提示。"""
    from .codec import proposal_prompt

    sys_msg, user_msg = proposal_prompt(context)
    if probe_system:
        sys_msg = probe_system
    messages: list[dict] = [
        {"role": "system", "content": sys_msg},
        {"role": "user", "content": user_msg},
    ]
    last_err: Exception | None = None
    for attempt in range(max_retries + 1):
        text = chat(messages)
        try:
            return parse_proposal(text)
        except StructuredOutputError as e:
            last_err = e
            if attempt < max_retries:
                messages.append({"role": "assistant", "content": text})
                messages.append({
                    "role": "user",
                    "content": f"上一轮输出无效（{e}）。请只输出符合格式的 json。",
                })
    raise StructuredOutputError(f"重试 {max_retries} 次后仍解析失败: {last_err}") from last_err
