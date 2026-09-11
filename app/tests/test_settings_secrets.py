"""SecretStore lifecycle, precedence and fail-closed tests."""

from __future__ import annotations

import json

import pytest

import stata_agent.settings.secret_store as secret_store
from stata_agent.settings.secret_store import (
    InMemorySecretStore,
    SecureStoreUnavailable,
    SecretStatus,
    SecretStoreValidationError,
    WindowsCredentialSecretStore,
    credential_target,
    resolve_secret_status,
    resolve_secret_value,
)


def test_fixed_provider_targets_are_closed_and_not_enumerable() -> None:
    assert credential_target("deepseek") == "StataAgent/provider/deepseek/default"
    assert credential_target("QWEN") == "StataAgent/provider/qwen/default"
    store = InMemorySecretStore()
    assert not hasattr(store, "enumerate")
    with pytest.raises(SecretStoreValidationError) as raised:
        credential_target("unrelated-app")
    assert raised.value.code == "settings_invalid_value"


def test_in_memory_secret_lifecycle_returns_only_masked_status() -> None:
    marker = "SECRET_MARKER_DO_NOT_LEAK"
    store = InMemorySecretStore()

    absent = store.status("deepseek")
    assert absent == SecretStatus("deepseek", False, "none", True)
    added = store.set("deepseek", f"  {marker}  ")
    assert added == SecretStatus("deepseek", True, "credential", True)
    assert store.get("deepseek") == marker

    replaced = store.replace("deepseek", "replacement-marker")
    assert replaced.as_dict() == {
        "provider": "deepseek",
        "configured": True,
        "source": "credential",
        "editable": True,
    }
    assert store.get("deepseek") == "replacement-marker"
    deleted = store.delete("deepseek")
    assert deleted == SecretStatus("deepseek", False, "none", True)
    assert store.get("deepseek") is None

    # Public status/repr projections never contain secret content or derived
    # metadata such as length/prefix.
    rendered = json.dumps(added.as_dict(), ensure_ascii=False) + repr(store)
    assert marker not in rendered
    assert "SECRET_MARKER" not in rendered


@pytest.mark.parametrize("operation", ["set", "replace"])
def test_in_memory_rejects_blank_non_string_and_oversized_secrets(operation: str) -> None:
    store = InMemorySecretStore()
    method = getattr(store, operation)
    for value in ("", "  \t", None, 123, "x" * (secret_store.MAX_SECRET_BYTES + 1)):
        with pytest.raises(SecretStoreValidationError):
            method("deepseek", value)
    assert store.status("deepseek") == SecretStatus("deepseek", False, "none", True)


def test_secret_precedence_is_dotenv_then_credential_then_environment() -> None:
    store = InMemorySecretStore({"deepseek": "credential-secret"})
    dotenv = {"DEEPSEEK_API_KEY": "dotenv-secret"}

    assert resolve_secret_value("deepseek", store, dotenv=dotenv, environ={}) == "credential-secret"
    assert resolve_secret_status("deepseek", store, dotenv=dotenv, environ={}).as_dict() == {
        "provider": "deepseek",
        "configured": True,
        "source": "credential",
        "editable": True,
    }

    store.delete("deepseek")
    assert resolve_secret_value("deepseek", store, dotenv=dotenv, environ={}) == "dotenv-secret"
    assert resolve_secret_status("deepseek", store, dotenv=dotenv, environ={}).source == "dotenv"

    explicit = {"DEEPSEEK_API_KEY": "environment-secret"}
    store.set("deepseek", "credential-secret")
    assert resolve_secret_value("deepseek", store, dotenv=dotenv, environ=explicit) == "environment-secret"
    status = resolve_secret_status("deepseek", store, dotenv=dotenv, environ=explicit)
    assert status.as_dict() == {
        "provider": "deepseek",
        "configured": True,
        "source": "environment",
        "editable": False,
    }


def test_catalog_provider_secret_lifecycle_is_not_limited_to_legacy_names() -> None:
    store = InMemorySecretStore()
    added = store.set("openai", "openai-secret")
    assert added.as_dict() == {
        "provider": "openai",
        "configured": True,
        "source": "credential",
        "editable": True,
    }
    assert resolve_secret_value("openai", store, environ={}) == "openai-secret"
    assert resolve_secret_status("openai", store, environ={}).source == "credential"
    assert credential_target("openai") == "StataAgent/provider/openai/default"


def test_environment_override_works_without_touching_unavailable_store() -> None:
    class Unavailable:
        def get(self, provider: str) -> str | None:
            raise AssertionError(f"store must not be read for {provider}")

        def status(self, provider: str) -> SecretStatus:
            raise AssertionError(f"store must not be read for {provider}")

    store = Unavailable()
    environment = {"DASHSCOPE_API_KEY": "env-only-secret"}
    assert resolve_secret_value("qwen", store, environ=environment) == "env-only-secret"
    assert resolve_secret_status("qwen", store, environ=environment).source == "environment"


def test_windows_store_fails_closed_when_credential_manager_is_unavailable(monkeypatch) -> None:
    # Force the platform branch even when this test is run on Windows.  The
    # adapter must never silently switch to a plaintext file/dict fallback.
    monkeypatch.setattr(secret_store.os, "name", "posix")
    store = WindowsCredentialSecretStore()
    assert store.available is False
    for operation, args in (
        (store.get, ("deepseek",)),
        (store.status, ("deepseek",)),
        (store.set, ("deepseek", "secret")),
        (store.replace, ("deepseek", "secret")),
        (store.delete, ("deepseek",)),
    ):
        with pytest.raises(SecureStoreUnavailable) as raised:
            operation(*args)
        assert raised.value.code == "secure_store_unavailable"


def test_windows_store_rejects_unknown_provider_before_platform_access(monkeypatch) -> None:
    monkeypatch.setattr(secret_store.os, "name", "posix")
    store = WindowsCredentialSecretStore()
    with pytest.raises(SecretStoreValidationError):
        store.set("other-app", "secret")
