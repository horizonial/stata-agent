"""Dynamic, model-aware budgeting for one model-visible Step context.

The policy does not compress or summarize content.  It computes how much room remains
for exact Context Items after the provider's output allowance and the actual fixed input
prefix (system prompt, main Skill, Tool schemas, and runtime envelope) are accounted for.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .model_gateway import ContextBuildDecisionCandidate
from .turn_driver import TurnDriverModelConfig


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _estimate_tokens(value: Mapping[str, Any]) -> int:
    payload = _canonical_json(value).encode("utf-8")
    return max(1, (len(payload) + 3) // 4)


@dataclass(frozen=True, slots=True)
class ContextBudgetAllocation:
    policy_revision: str
    context_window_tokens: int
    reserved_output_tokens: int
    hard_input_token_limit: int
    fixed_input_tokens: int
    safety_reserve_tokens: int
    context_item_budget_tokens: int

    def as_build_decision(self) -> ContextBuildDecisionCandidate:
        return ContextBuildDecisionCandidate(
            "included",
            "context_budget_policy",
            self.policy_revision,
            "dynamic_model_budget",
            {
                "context_window_tokens": self.context_window_tokens,
                "reserved_output_tokens": self.reserved_output_tokens,
                "hard_input_token_limit": self.hard_input_token_limit,
                "fixed_input_tokens": self.fixed_input_tokens,
                "safety_reserve_tokens": self.safety_reserve_tokens,
                "context_item_budget_tokens": self.context_item_budget_tokens,
                "lossy_compression_enabled": False,
            },
        )


class DynamicContextBudgetPolicy:
    """Allocate a per-Step budget from the selected model and actual fixed prefix."""

    def __init__(self, policy_revision: str = "dynamic-context-budget-v1") -> None:
        if not policy_revision.strip():
            raise ValueError("Context budget policy revision is required")
        self.policy_revision = policy_revision

    def allocate(
        self,
        model: TurnDriverModelConfig,
        *,
        remaining_step_budget: int,
        remaining_tool_budget: int,
    ) -> ContextBudgetAllocation:
        hard_input_limit = model.context_window_tokens - model.max_output_tokens
        if hard_input_limit < 1:
            raise ValueError("model output reserve exhausts the context window")

        # This mirrors the fixed portion of ModelGatewayService._normalized_input.
        # Context Item wrapper cost is estimated by ContextCompiler per item and the
        # configured runtime reserve absorbs tokenizer/provider estimation variance.
        fixed_input = {
            "system": {
                "revision": model.system_prompt_revision,
                "content": model.system_prompt,
            },
            "main_skill": {
                "name": model.main_skill_name,
                "revision": model.main_skill_revision,
                "content": model.main_skill_content,
            },
            "context": [],
            "tools": {
                "catalog_revision": model.tool_catalog_revision,
                "schemas": [dict(schema) for schema in model.tool_schemas],
            },
            "runtime": {
                "remaining_step_budget": remaining_step_budget,
                "remaining_tool_budget": remaining_tool_budget,
            },
        }
        fixed_tokens = _estimate_tokens(fixed_input)
        item_budget = hard_input_limit - fixed_tokens - model.reserved_runtime_tokens
        if item_budget < 1:
            raise ValueError(
                "system prompt, Skill, Tool schemas, and runtime reserve exhaust model input"
            )
        return ContextBudgetAllocation(
            self.policy_revision,
            model.context_window_tokens,
            model.max_output_tokens,
            hard_input_limit,
            fixed_tokens,
            model.reserved_runtime_tokens,
            item_budget,
        )
