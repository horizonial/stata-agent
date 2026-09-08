"""ActionProposal 与文本/JSON 之间的编解码（LLM 结构化输出的一层）。

职责：把模型自由文本收敛成合法 ActionProposal；坏输出在此被拦（schema 只保形状）。
"""

from __future__ import annotations

import json
import re

from ..domain.action import ActionProposal


class StructuredOutputError(Exception):
    """重试后仍无法解析为合法结构化输出。"""


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def parse_proposal(text: str) -> ActionProposal:
    """把模型输出解析成 ActionProposal；解析/校验失败抛 StructuredOutputError。"""
    cleaned = _strip_fences(text)
    try:
        obj = json.loads(cleaned)
    except json.JSONDecodeError as e:  # noqa: F841
        raise StructuredOutputError("模型输出不是合法 JSON") from e
    try:
        return ActionProposal.model_validate(obj)
    except Exception as e:
        raise StructuredOutputError(f"模型输出不符合 ActionProposal schema: {e}") from e


def proposal_prompt(context: str) -> tuple[str, str]:
    """构造一次 propose 的系统/用户提示（含 'json'，兼容 deepseek json_object）。"""
    system = (
        "你是实证研究 agent 的『提议层』：只负责把当前研究状态折叠成下一步候选动作，"
        "不执行、不签发证据、不提交阶段迁移。禁止输出 act_type 为 "
        "mark_done / delete_file / sign_claim / phase.transition。"
    )
    user = (
        "以下 json 是当前上下文。请只输出一个 json 对象（不要解释、不要代码块外的文字），"
        "形如："
        '{"decision_summary":"为什么这么想(一句)","acts":[{"act_type":"inspect_data",'
        '"target":{},"reason":"为什么做这个"}],"ask_user":null|"要问学者的话",'
        '"stop_reason":null|"done"|"need_input"}。'
        f"可用的 act_type：propose_spec/request_run/interpret/select_candidate/ask_user/"
        f"read_artifact/rag_search/inspect_data/request_robustness_variant/draft_section。\n\n"
        f"<context>\n{context}\n</context>"
    )
    return system, user
