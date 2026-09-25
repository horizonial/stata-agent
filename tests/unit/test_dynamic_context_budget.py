from stata_research_agent.application.context_budget import DynamicContextBudgetPolicy
from stata_research_agent.application.turn_driver import TurnDriverModelConfig


def _model(*, context_window: int, system_prompt: str = "system") -> TurnDriverModelConfig:
    return TurnDriverModelConfig(
        "system-v1",
        system_prompt,
        "research",
        "research-v1",
        "main skill",
        "tools-v1",
        ({"name": "memory.search", "input_schema": {"type": "object"}},),
        "permissions-v1",
        {"workspace_read": True},
        "model-v1",
        "provider",
        "openai-compatible",
        "model",
        "https://provider.example/v1",
        "credential://provider",
        {},
        True,
        context_window,
        2_000,
        1_000,
    )


def test_context_budget_tracks_selected_model_and_actual_fixed_prefix() -> None:
    policy = DynamicContextBudgetPolicy()
    small = policy.allocate(
        _model(context_window=16_000), remaining_step_budget=20, remaining_tool_budget=30
    )
    large = policy.allocate(
        _model(context_window=128_000), remaining_step_budget=20, remaining_tool_budget=30
    )
    verbose = policy.allocate(
        _model(context_window=128_000, system_prompt="policy " * 4_000),
        remaining_step_budget=20,
        remaining_tool_budget=30,
    )

    assert small.hard_input_token_limit == 14_000
    assert large.context_item_budget_tokens > small.context_item_budget_tokens
    assert verbose.fixed_input_tokens > large.fixed_input_tokens
    assert verbose.context_item_budget_tokens < large.context_item_budget_tokens
    assert large.as_build_decision().detail["lossy_compression_enabled"] is False
