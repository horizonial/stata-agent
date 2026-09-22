"""Output-token budget policy contract."""

from stata_research_agent.application.output_budget import OutputTokenBudgetPolicy


def test_budget_exposes_the_entire_provider_output_window() -> None:
    decision = OutputTokenBudgetPolicy().decide(
        remaining_turn_tokens=64_000,
        provider_max_output_tokens=8_192,
    )

    assert decision.admitted_tokens == 8_192
    assert decision.constrained_by_turn_budget is False


def test_explicit_turn_budget_is_the_only_smaller_ceiling() -> None:
    decision = OutputTokenBudgetPolicy().decide(
        remaining_turn_tokens=1_000,
        provider_max_output_tokens=8_192,
    )

    assert decision.admitted_tokens == 1_000
    assert decision.constrained_by_turn_budget is True
