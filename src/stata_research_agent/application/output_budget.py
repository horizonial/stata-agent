"""Versioned output-token budgeting for logical model invocations.

The budget is an admission decision, not a promise to truncate a valid answer.  A provider
reporting ``finish_reason=length`` has produced an incomplete candidate and that candidate
must never be admitted as an Assistant Output.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class OutputTokenBudgetDecision:
    policy_revision: str
    admitted_tokens: int
    remaining_turn_tokens: int
    provider_max_output_tokens: int
    constrained_by_turn_budget: bool


@dataclass(frozen=True, slots=True)
class OutputTokenBudgetPolicy:
    """Expose the entire available provider output window to an invocation.

    Content heuristics must not impose a smaller answer limit.  The only ceilings are the
    tokens left under an explicit Turn budget and the selected provider/model's advertised
    output limit.  If the Turn budget is the smaller value, callers can surface that fact and
    ask the user to extend it rather than pretending a truncated answer is complete.
    """

    policy_revision: str = "output-budget/v1"

    def decide(
        self,
        *,
        remaining_turn_tokens: int,
        provider_max_output_tokens: int,
    ) -> OutputTokenBudgetDecision:
        if remaining_turn_tokens < 0:
            raise ValueError("output budget inputs cannot be negative")
        if provider_max_output_tokens < 1:
            raise ValueError("provider_max_output_tokens must be positive")

        admitted = min(remaining_turn_tokens, provider_max_output_tokens)
        return OutputTokenBudgetDecision(
            self.policy_revision,
            admitted,
            remaining_turn_tokens,
            provider_max_output_tokens,
            remaining_turn_tokens < provider_max_output_tokens,
        )
