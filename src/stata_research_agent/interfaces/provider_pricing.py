"""Load optional, versioned provider price projections from local configuration."""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from stata_research_agent.application.runtime_usage import (
    ProviderPrice,
    ProviderPricingCatalog,
)


def load_provider_pricing_catalog(path: Path) -> ProviderPricingCatalog:
    if not path.is_file():
        return ProviderPricingCatalog.unconfigured()
    try:
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("schema_version") != "1":
            raise ValueError("provider pricing schema_version must be '1'")
        revision = payload["pricing_revision"]
        currency = payload["currency"]
        raw_prices = payload["profiles"]
        if not isinstance(revision, str) or not isinstance(currency, str):
            raise ValueError("provider pricing revision and currency must be strings")
        if not isinstance(raw_prices, list):
            raise ValueError("provider pricing profiles must be a list")
        prices: list[ProviderPrice] = []
        for raw in raw_prices:
            if not isinstance(raw, dict):
                raise ValueError("provider pricing profile must be an object")
            cached = raw.get("cached_input_per_million")
            prices.append(
                ProviderPrice(
                    provider_profile=str(raw["provider_profile"]),
                    input_per_million=Decimal(str(raw["input_per_million"])),
                    output_per_million=Decimal(str(raw["output_per_million"])),
                    cached_input_per_million=None
                    if cached is None
                    else Decimal(str(cached)),
                )
            )
        return ProviderPricingCatalog(revision, currency.upper(), tuple(prices))
    except (KeyError, TypeError, InvalidOperation, json.JSONDecodeError) as error:
        raise ValueError("provider pricing configuration is invalid") from error
