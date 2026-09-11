"""Provider catalog and adapters for the application composition boundary."""

from .catalog import (
    MODEL_IDS,
    PROVIDER_DEFINITIONS,
    PROVIDER_IDS,
    ProviderDefinition,
    default_profile,
    model_profile,
    provider_definition,
    provider_definitions,
    provider_public_catalog,
)
from .openai_compatible import OpenAICompatibleProvider

__all__ = [
    "MODEL_IDS",
    "PROVIDER_DEFINITIONS",
    "PROVIDER_IDS",
    "OpenAICompatibleProvider",
    "ProviderDefinition",
    "default_profile",
    "model_profile",
    "provider_definition",
    "provider_definitions",
    "provider_public_catalog",
]
