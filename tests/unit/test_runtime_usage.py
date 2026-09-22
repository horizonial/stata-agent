from __future__ import annotations

import json
from decimal import Decimal

import pytest

from stata_research_agent.application.runtime_usage import (
    ProviderAttemptUsage,
    ProviderPrice,
    ProviderPricingCatalog,
    estimate_provider_cost,
)
from stata_research_agent.interfaces.provider_pricing import load_provider_pricing_catalog


def attempt(
    *, usage_quality: str = "exact", cached: int | None = 400
) -> ProviderAttemptUsage:
    return ProviderAttemptUsage(
        provider_attempt_id="providerattempt_one",
        model_invocation_id="modelinv_one",
        step_id="step_one",
        step_ordinal=1,
        attempt_ordinal=1,
        provider_profile="deepseek",
        provider_kind="openai-compatible",
        model_name="deepseek-chat",
        state="completed",
        usage_quality=usage_quality,  # type: ignore[arg-type]
        input_tokens=1000,
        output_tokens=100,
        cached_input_tokens=cached,
        uncached_input_tokens=None if cached is None else 1000 - cached,
        is_retry=False,
        is_fallback=False,
        observed_duration_seconds=1.25,
        error_code=None,
    )


def catalog() -> ProviderPricingCatalog:
    return ProviderPricingCatalog(
        "prices-2026-09",
        "USD",
        (ProviderPrice("deepseek", Decimal("1"), Decimal("2"), Decimal("0.25")),),
    )


def test_cost_estimate_accounts_for_cached_input_without_hiding_quality() -> None:
    estimate = estimate_provider_cost((attempt(),), catalog())

    assert estimate.quality == "exact"
    assert estimate.amount == Decimal("0.0009")
    assert estimate.known_cached_savings == Decimal("0.000300")
    assert estimate.unpriced_attempt_count == 0


def test_missing_price_is_unknown_instead_of_zero() -> None:
    estimate = estimate_provider_cost(
        (attempt(),), ProviderPricingCatalog("empty", "USD", ())
    )

    assert estimate.quality == "unknown"
    assert estimate.amount is None
    assert estimate.known_subtotal == 0
    assert estimate.unpriced_attempt_count == 1
    assert estimate.explanation is not None


def test_versioned_local_pricing_configuration_loads_and_rejects_duplicates(tmp_path) -> None:
    path = tmp_path / "provider-pricing.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "pricing_revision": "catalog-v7",
                "currency": "usd",
                "profiles": [
                    {
                        "provider_profile": "deepseek",
                        "input_per_million": "0.27",
                        "output_per_million": "1.10",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_provider_pricing_catalog(path)
    assert loaded.pricing_revision == "catalog-v7"
    assert loaded.currency == "USD"
    assert loaded.price_for("deepseek") is not None

    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["profiles"].append(payload["profiles"][0])
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="unique"):
        load_provider_pricing_catalog(path)
