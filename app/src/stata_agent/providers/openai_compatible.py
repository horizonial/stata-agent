"""Generic OpenAI-compatible chat provider.

DeepSeek and Qwen keep their historical adapter classes for compatibility;
this class supplies the same wire contract to the other explicitly registered
provider profiles.  It inherits the hardened parsing/streaming behavior from
``DeepSeekProvider`` but receives its provider id, endpoint and capability
profile from the closed provider catalog.
"""

from __future__ import annotations

from .capabilities import ModelCapabilityProfile
from .deepseek import DeepSeekProvider, MissingApiKey


class OpenAICompatibleProvider(DeepSeekProvider):
    """A catalog-backed provider using ``/chat/completions``."""

    def __init__(
        self,
        *,
        provider_id: str,
        api_key: str | None,
        base_url: str,
        profile: ModelCapabilityProfile,
        timeout: int = 90,
    ) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise MissingApiKey()
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError("provider base URL is required")
        if profile.provider != provider_id:
            raise ValueError("model profile does not belong to provider")
        self.provider = provider_id
        self.is_remote = True
        self._key = api_key.strip()
        self._base = base_url.rstrip("/")
        self.profile = profile
        self._timeout = timeout


__all__ = ["OpenAICompatibleProvider"]
