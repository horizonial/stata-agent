"""意图识别先于路由：普通聊天不被迫进研究流程。"""

from __future__ import annotations

from stata_agent import ui


class FakeChat:
    def __init__(self, out: str):
        self.out = out

    def chat(self, messages):
        return self.out


def test_classifier_respects_llm_intent():
    assert ui._classify_intent(FakeChat("research"), "随便一句", []) == "research"
    assert ui._classify_intent(FakeChat("chat"), "is collecting data always good", []) == "chat"


def test_classifier_fallback_only_without_llm():
    class NoChat:
        pass

    # 无 LLM 时才退回关键词（提到"研究/回归"算 research，普通闲聊不算）
    assert ui._classify_intent(NoChat(), "用双重差分跑回归", []) == "research"
    assert ui._classify_intent(NoChat(), "is data good", []) == "chat"
