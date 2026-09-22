"""Turn-level runtime usage projections built from authoritative execution facts.

Usage reports are operational projections.  They explain what the runtime consumed without
turning price estimates into research evidence or pretending that an unconfigured price is zero.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

UsageQuality = Literal["exact", "estimated", "unknown"]


@dataclass(frozen=True, slots=True)
class ProviderPrice:
    provider_profile: str
    input_per_million: Decimal
    output_per_million: Decimal
    cached_input_per_million: Decimal | None = None

    def __post_init__(self) -> None:
        if not self.provider_profile.strip():
            raise ValueError("provider price profile cannot be empty")
        for value in (
            self.input_per_million,
            self.output_per_million,
            self.cached_input_per_million,
        ):
            if value is not None and value < 0:
                raise ValueError("provider prices cannot be negative")


@dataclass(frozen=True, slots=True)
class ProviderPricingCatalog:
    pricing_revision: str
    currency: str
    prices: tuple[ProviderPrice, ...]

    def __post_init__(self) -> None:
        if not self.pricing_revision.strip():
            raise ValueError("pricing revision cannot be empty")
        if not self.currency.strip():
            raise ValueError("pricing currency cannot be empty")
        profiles = tuple(price.provider_profile for price in self.prices)
        if len(profiles) != len(set(profiles)):
            raise ValueError("provider price profiles must be unique")

    @classmethod
    def unconfigured(cls) -> ProviderPricingCatalog:
        return cls("unconfigured", "USD", ())

    def price_for(self, provider_profile: str) -> ProviderPrice | None:
        return next(
            (price for price in self.prices if price.provider_profile == provider_profile),
            None,
        )


@dataclass(frozen=True, slots=True)
class ProviderAttemptUsage:
    provider_attempt_id: str
    model_invocation_id: str
    step_id: str
    step_ordinal: int
    attempt_ordinal: int
    provider_profile: str
    provider_kind: str
    model_name: str
    state: str
    usage_quality: UsageQuality
    input_tokens: int | None
    output_tokens: int | None
    cached_input_tokens: int | None
    uncached_input_tokens: int | None
    is_retry: bool
    is_fallback: bool
    observed_duration_seconds: float | None
    error_code: str | None


@dataclass(frozen=True, slots=True)
class ToolOperationUsage:
    operation_id: str
    tool_call_id: str | None
    tool_name: str | None
    operation_kind: str
    status: str
    attempt_count: int
    observed_duration_seconds: float | None


@dataclass(frozen=True, slots=True)
class CountBudgetUsage:
    policy_revision: str
    max_steps: int
    used_steps: int
    remaining_steps: int
    max_tool_admissions: int
    used_tool_admissions: int
    remaining_tool_admissions: int
    max_provider_attempts: int
    used_provider_attempts: int
    remaining_provider_attempts: int


@dataclass(frozen=True, slots=True)
class RuntimeBudgetUsage:
    policy_revision: str
    max_wall_clock_seconds: float
    consumed_wall_clock_seconds: float
    remaining_wall_clock_seconds: float
    segment_count: int


@dataclass(frozen=True, slots=True)
class MonetaryUsageEstimate:
    pricing_revision: str
    currency: str
    quality: UsageQuality
    amount: Decimal | None
    known_subtotal: Decimal
    known_cached_savings: Decimal
    priced_attempt_count: int
    unpriced_attempt_count: int
    explanation: str | None


@dataclass(frozen=True, slots=True)
class TurnUsageSnapshot:
    authoritative_revision: int
    turn_id: str
    turn_status: str
    step_count: int
    provider_attempts: tuple[ProviderAttemptUsage, ...]
    tool_operations: tuple[ToolOperationUsage, ...]
    input_tokens_observed: int
    output_tokens_observed: int
    cached_input_tokens_observed: int
    uncached_input_tokens_observed: int
    unknown_usage_attempt_count: int
    token_quality: UsageQuality
    retry_count: int
    fallback_count: int
    provider_duration_observed_seconds: float
    tool_duration_observed_seconds: float
    count_budget: CountBudgetUsage | None
    runtime_budget: RuntimeBudgetUsage | None
    monetary_estimate: MonetaryUsageEstimate


def estimate_provider_cost(
    attempts: tuple[ProviderAttemptUsage, ...], catalog: ProviderPricingCatalog
) -> MonetaryUsageEstimate:
    """Estimate cost only when the catalog and the provider's usage report support it."""

    million = Decimal(1_000_000)
    known_subtotal = Decimal(0)
    known_cached_savings = Decimal(0)
    qualities: list[UsageQuality] = []
    priced = 0
    unpriced = 0

    for attempt in attempts:
        price = catalog.price_for(attempt.provider_profile)
        if (
            price is None
            or attempt.usage_quality == "unknown"
            or attempt.input_tokens is None
            or attempt.output_tokens is None
        ):
            unpriced += 1
            continue
        cached = attempt.cached_input_tokens or 0
        uncached = attempt.uncached_input_tokens
        if uncached is None:
            uncached = attempt.input_tokens - cached
        if uncached < 0 or cached > 0 and price.cached_input_per_million is None:
            unpriced += 1
            continue
        cached_rate = price.cached_input_per_million or price.input_per_million
        amount = (
            Decimal(uncached) * price.input_per_million
            + Decimal(cached) * cached_rate
            + Decimal(attempt.output_tokens) * price.output_per_million
        ) / million
        savings = Decimal(cached) * (price.input_per_million - cached_rate) / million
        known_subtotal += amount
        known_cached_savings += savings
        qualities.append(attempt.usage_quality)
        priced += 1

    if not attempts:
        return MonetaryUsageEstimate(
            catalog.pricing_revision,
            catalog.currency,
            "exact",
            Decimal(0),
            Decimal(0),
            Decimal(0),
            0,
            0,
            None,
        )
    if unpriced:
        explanation = (
            "One or more attempts lack compatible pricing or reliable token usage; "
            "known_subtotal is not the complete Turn cost."
        )
        return MonetaryUsageEstimate(
            catalog.pricing_revision,
            catalog.currency,
            "unknown",
            None,
            known_subtotal,
            known_cached_savings,
            priced,
            unpriced,
            explanation,
        )
    quality: UsageQuality = "estimated" if "estimated" in qualities else "exact"
    return MonetaryUsageEstimate(
        catalog.pricing_revision,
        catalog.currency,
        quality,
        known_subtotal,
        known_subtotal,
        known_cached_savings,
        priced,
        0,
        None,
    )
