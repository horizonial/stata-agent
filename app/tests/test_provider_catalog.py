from __future__ import annotations

import json

from stata_agent.application.settings import SettingsService
from stata_agent.providers.catalog import MODEL_IDS, provider_definitions, provider_public_catalog
from stata_agent.providers.openai_compatible import OpenAICompatibleProvider
from stata_agent.providers.registry import default_provider, live_available
from stata_agent.settings.local_repository import LocalSettingsRepository
from stata_agent.settings.secret_store import InMemorySecretStore


def test_catalog_is_data_driven_and_public_projection_is_safe() -> None:
    definitions = provider_definitions()
    assert len(definitions) >= 6
    assert len({item.provider_id for item in definitions}) == len(definitions)
    assert len(MODEL_IDS) == len(set(MODEL_IDS))
    projection = provider_public_catalog()
    assert {item["id"] for item in projection} == {item.provider_id for item in definitions}
    rendered = json.dumps(projection, ensure_ascii=False)
    assert "api_key_env" not in rendered
    assert "credential_target" not in rendered


def test_generic_provider_uses_profile_and_openai_compatible_wire_contract(monkeypatch) -> None:
    from stata_agent.providers.catalog import model_profile

    profile = model_profile("gpt-4o-mini")
    assert profile is not None
    provider = OpenAICompatibleProvider(
        provider_id="openai",
        api_key="test-key",
        base_url="https://api.openai.com/v1",
        profile=profile,
    )

    captured: dict[str, object] = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data.decode())
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    result = provider.chat([{"role": "user", "content": "hello"}])
    assert result["content"] == "ok"
    assert captured["url"] == "https://api.openai.com/v1/chat/completions"
    assert captured["body"]["model"] == "gpt-4o-mini"


def test_registry_constructs_non_legacy_provider_from_snapshot(tmp_path) -> None:
    secret_store = InMemorySecretStore({"openai": "openai-key"})
    service = SettingsService(
        LocalSettingsRepository(tmp_path / "settings.json"),
        environ={},
        dotenv={},
        secret_store=secret_store,
    )
    current = service.resolve()
    service.apply_patch(
        expected_revision=current.revision,
        changes={
            "provider.primary": "openai",
            "provider.model": "gpt-4o-mini",
            "provider.live_enabled": True,
            "privacy.mode": "approved_remote",
        },
    )
    effective = service.resolve()
    assert live_available(effective, secret_store=secret_store) is True
    router = default_provider(effective, secret_store=secret_store)
    assert router._providers[0].provider == "openai"
    assert router._providers[0].profile.model == "gpt-4o-mini"
