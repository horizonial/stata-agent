"""M5-01b versioned Provider credential lifecycle and recovery."""

from __future__ import annotations

import os
import secrets
from pathlib import Path

import pytest

from stata_research_agent.application.model_configuration import (
    WorkspaceModelConfigurationService,
)
from stata_research_agent.application.provider_credentials import (
    CredentialUnavailableError,
    ProviderCredentialService,
)
from stata_research_agent.interfaces.windows_credential_store import (
    WindowsCredentialStore,
)
from stata_research_agent.persistence.global_credentials import (
    GlobalCredentialDatabase,
    SqliteProviderCredentialRepository,
)


class MemorySecretStore:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def write(self, target_name: str, secret: str) -> None:
        self.values[target_name] = secret

    def read(self, target_name: str) -> str:
        try:
            return self.values[target_name]
        except KeyError as error:
            raise CredentialUnavailableError("credential is unavailable") from error

    def contains(self, target_name: str) -> bool:
        return target_name in self.values

    def delete(self, target_name: str) -> None:
        self.values.pop(target_name, None)


def service(tmp_path: Path) -> tuple[ProviderCredentialService, MemorySecretStore, object]:
    connection = GlobalCredentialDatabase(tmp_path / "global-control.sqlite3").open()
    secrets_store = MemorySecretStore()
    return (
        ProviderCredentialService(SqliteProviderCredentialRepository(connection), secrets_store),
        secrets_store,
        connection,
    )


def test_create_rotate_and_gateway_resolution_use_exact_active_version(tmp_path: Path) -> None:
    credentials, secret_store, connection = service(tmp_path)
    first_secret = "sk-first-canary-do-not-persist"
    created = credentials.create_profile(
        provider_kind="deepseek",
        endpoint="https://api.deepseek.com/chat/completions",
        secret=first_secret,
        account_label="Primary",
    )
    first_ref = connection.execute(
        """
        SELECT credential_ref FROM provider_credential_versions
        WHERE credential_version_id = ?
        """,
        (created.credential_version_id,),
    ).fetchone()[0]
    resolved = credentials.resolve_for_transport(
        str(first_ref),
        provider_profile_id=created.provider_profile_id,
        endpoint="https://api.deepseek.com/chat/completions",
    )
    assert resolved.secret == first_secret
    assert first_secret not in "\n".join(connection.iterdump())
    assert first_secret.encode() not in (tmp_path / "global-control.sqlite3").read_bytes()
    assert first_secret not in repr(resolved)

    second_secret = "sk-second-canary-do-not-persist"
    rotated = credentials.rotate(created.provider_profile_id, secret=second_secret)
    second_ref = connection.execute(
        """
        SELECT credential_ref FROM provider_credential_versions
        WHERE credential_version_id = ?
        """,
        (rotated.credential_version_id,),
    ).fetchone()[0]
    with pytest.raises(CredentialUnavailableError):
        credentials.resolve_for_transport(
            str(first_ref),
            provider_profile_id=created.provider_profile_id,
            endpoint="https://api.deepseek.com/chat/completions",
        )
    latest = credentials.resolve_for_transport(
        str(second_ref),
        provider_profile_id=created.provider_profile_id,
        endpoint="https://api.deepseek.com/chat/completions",
    )
    assert latest.secret == second_secret
    assert (
        connection.execute(
            """
        SELECT state FROM provider_credential_versions
        WHERE credential_version_id = ?
        """,
            (created.credential_version_id,),
        ).fetchone()[0]
        == "retired"
    )
    assert len(secret_store.values) == 2
    with pytest.raises(CredentialUnavailableError, match="binding"):
        credentials.resolve_for_transport(
            str(second_ref),
            provider_profile_id="provider_wrong",
            endpoint="https://api.deepseek.com/chat/completions",
        )
    deleted = credentials.delete_profile(created.provider_profile_id)
    assert deleted.status == "deleted"
    assert secret_store.values == {}
    assert (
        connection.execute(
            "SELECT status FROM provider_profiles WHERE provider_profile_id = ?",
            (created.provider_profile_id,),
        ).fetchone()[0]
        == "deleted"
    )
    connection.close()


def test_workspace_model_selection_tracks_profile_not_secret_version(tmp_path: Path) -> None:
    credentials, _, connection = service(tmp_path)
    created = credentials.create_profile(
        provider_kind="deepseek",
        endpoint="https://api.deepseek.com/chat/completions",
        secret="sk-workspace-model-first",
    )
    models = WorkspaceModelConfigurationService(SqliteProviderCredentialRepository(connection))
    selected = models.select(
        workspace_id="ws_model",
        provider_profile_id=created.provider_profile_id,
        model_name="deepseek-chat",
        reasoning_effort="medium",
    )
    assert selected.configuration_revision == 1
    assert selected.model_name == "deepseek-chat"
    first_ref = selected.credential_ref

    credentials.rotate(created.provider_profile_id, secret="sk-workspace-model-second")
    resolved = models.resolve("ws_model")
    assert resolved.configuration_revision == 1
    assert resolved.credential_ref != first_ref
    assert resolved.provider_profile_id == created.provider_profile_id
    connection.close()


def test_recovery_cleans_orphan_and_marks_missing_active_secret_unavailable(
    tmp_path: Path,
) -> None:
    credentials, secret_store, connection = service(tmp_path)
    repository = SqliteProviderCredentialRepository(connection)
    allocation = repository.allocate_profile(
        provider_profile_id="provider_orphan",
        provider_kind="deepseek",
        endpoint="https://api.deepseek.com/chat/completions",
        account_label=None,
        credential_version_id="credentialversion_orphan",
        credential_ref="credential://provider_orphan/credentialversion_orphan",
        target_name=(
            "StataResearchAgent/provider/deepseek/provider_orphan/credentialversion_orphan"
        ),
    )
    secret_store.write(allocation.target_name, "orphan-secret")
    recovered = credentials.recover()
    assert recovered[0].status == "orphan_cleaned"
    assert not secret_store.contains(allocation.target_name)

    active = credentials.create_profile(
        provider_kind="deepseek",
        endpoint="https://api.deepseek.com/chat/completions",
        secret="active-secret",
    )
    row = connection.execute(
        """
        SELECT credential_ref, target_name FROM provider_credential_versions
        WHERE credential_version_id = ?
        """,
        (active.credential_version_id,),
    ).fetchone()
    secret_store.delete(str(row["target_name"]))
    unavailable = credentials.recover()
    assert any(item.status == "credential_unavailable" for item in unavailable)
    assert (
        connection.execute(
            "SELECT status FROM provider_profiles WHERE provider_profile_id = ?",
            (active.provider_profile_id,),
        ).fetchone()[0]
        == "credential_unavailable"
    )
    with pytest.raises(CredentialUnavailableError):
        credentials.resolve_for_transport(
            str(row["credential_ref"]),
            provider_profile_id=active.provider_profile_id,
            endpoint="https://api.deepseek.com/chat/completions",
        )

    pending = credentials.create_profile(
        provider_kind="deepseek",
        endpoint="https://api.deepseek.com/chat/completions",
        secret="delete-pending-secret",
    )
    staged = repository.stage_profile_delete(pending.provider_profile_id)
    assert staged and secret_store.contains(staged[0].target_name)
    resumed_delete = credentials.recover()
    assert any(item.status == "deleted" for item in resumed_delete)
    assert not secret_store.contains(staged[0].target_name)
    connection.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows-only secret store")
def test_windows_credential_manager_roundtrip_uses_namespaced_generic_target() -> None:
    store = WindowsCredentialStore()
    target = f"StataResearchAgent/provider/test/provider_test/{secrets.token_hex(16)}"
    secret = "sk-windows-roundtrip-" + secrets.token_urlsafe(24)
    try:
        store.write(target, secret)
        assert store.contains(target)
        assert store.read(target) == secret
    finally:
        store.delete(target)
    assert not store.contains(target)
