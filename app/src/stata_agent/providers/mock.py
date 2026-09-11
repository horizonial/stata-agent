"""确定性 mock provider：录 replay / 固定提议（SPEC §4.2 可测性 + DD-06 mock 替身）。

切片 1 用途：harness 确定性单测 + CLI demo。无网络、无随机。
"""

from __future__ import annotations

from ..domain.action import ActionProposal


class MockReplayProvider:
    """按脚本顺序回放一批 proposal；用尽抛 StopIteration（表示该停下问用户）。"""

    def __init__(self, proposals: list[ActionProposal]):
        self._proposals = list(proposals)
        self._i = 0

    def propose(self, context: str) -> ActionProposal:
        if self._i >= len(self._proposals):
            raise StopIteration("replay 用尽")
        prop = self._proposals[self._i]
        self._i += 1
        return prop

    def chat(self, messages: list[dict], **kwargs) -> dict:
        """Expose the modern chat surface without changing replay semantics.

        Legacy proposal actions are deliberately rendered as deterministic
        text instead of being reinterpreted as tool calls.  Tool execution is
        owned by the modern agent loop and requires its native tool schema.
        """
        del messages, kwargs
        return _proposal_as_chat_response(self.propose(""))

    @property
    def consumed(self) -> int:
        return self._i


class MockFixedProvider:
    """始终返回同一 proposal（契约测试/健康检查用，永不枯竭）。"""

    def __init__(self, proposal: ActionProposal):
        self._proposal = proposal

    def propose(self, context: str) -> ActionProposal:
        return self._proposal

    def chat(self, messages: list[dict], **kwargs) -> dict:
        """Return the fixed proposal through the modern chat protocol."""
        del messages, kwargs
        return _proposal_as_chat_response(self._proposal)


def _proposal_as_chat_response(proposal: ActionProposal) -> dict:
    """Render a legacy proposal safely for chat-only offline operation."""
    content = proposal.ask_user or proposal.decision_summary
    return {"content": content, "tool_calls": []}


def ask_question(question: str, reason: str = "") -> ActionProposal:
    return ActionProposal(decision_summary=reason, ask_user=question)


def run_estimate(spec_id: str, reason: str = "拟跑主回归") -> ActionProposal:
    from ..domain.action import Act

    return ActionProposal(
        decision_summary=reason,
        acts=[Act(act_type="request_run", target={"spec_id": spec_id}, reason=reason)],
    )
