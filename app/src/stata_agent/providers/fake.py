"""FakeChat：给 chat_structured / codec 用的假模型（离线契约测试）。"""

from __future__ import annotations

from typing import Callable


def FakeChat(responses: list[str]) -> Callable[[list[dict]], str]:
    """返回一个 chat 函数：按序返回脚本文本；用尽抛 StopIteration。"""
    it = iter(responses)
    calls: list[int] = []

    def _chat(messages: list[dict]) -> str:
        calls.append(len(messages))
        return next(it)  # 用尽即抛，测试里能察觉

    _chat.calls = calls  # type: ignore[attr-defined]
    return _chat
