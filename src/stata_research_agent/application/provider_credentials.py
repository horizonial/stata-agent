"""Provider credential lifecycle contracts without secret persistence."""

from __future__ import annotations

import hmac
import secrets
from dataclasses import dataclass
from typing import Protocol


class CredentialLifecycleError(RuntimeError):
    """Safe credential lifecycle failure with no secret-bearing detail."""


class CredentialUnavailableError(CredentialLifecycleError):
    pass


@dataclass(frozen=True, slots=True)
class ProviderCredentialReceipt:
    provider_profile_id: str
    credential_version_id: str
    status: str


@dataclass(frozen=True, slots=True)
class ProviderProfileDeleteReceipt:
    provider_profile_id: str
    status: str


@dataclass(frozen=True, slots=True)
class ProviderProfileSummary:
    """Secret-free Provider profile metadata safe for authenticated settings UI."""

    provider_profile_id: str
    provider_kind: str
    endpoint: str
    account_label: str | None
    status: str
    credential_version_id: str | None


@dataclass(frozen=True, slots=True)
class ProviderCredentialAllocation:
    provider_profile_id: str
    provider_kind: str
    endpoint: str
    credential_version_id: str
    credential_ref: str
    target_name: str


@dataclass(frozen=True, slots=True, repr=False)
class ResolvedProviderCredential:
    provider_profile_id: str
    credential_version_id: str
    endpoint: str
    secret: str

    def __repr__(self) -> str:
        return (
            "ResolvedProviderCredential("
            f"provider_profile_id={self.provider_profile_id!r}, "
            f"credential_version_id={self.credential_version_id!r}, "
            f"endpoint={self.endpoint!r}, secret='<redacted>')"
        )


class ProviderSecretStore(Protocol):
    def write(self, target_name: str, secret: str) -> None: ...

    def read(self, target_name: str) -> str: ...

    def contains(self, target_name: str) -> bool: ...

    def delete(self, target_name: str) -> None: ...


class ProviderCredentialRepository(Protocol):
    def list_profiles(self) -> tuple[ProviderProfileSummary, ...]: ...

    def allocate_profile(
        self,
        *,
        provider_profile_id: str,
        provider_kind: str,
        endpoint: str,
        account_label: str | None,
        credential_version_id: str,
        credential_ref: str,
        target_name: str,
    ) -> ProviderCredentialAllocation: ...

    def allocate_rotation(
        self,
        *,
        provider_profile_id: str,
        credential_version_id: str,
        credential_ref: str,
    ) -> ProviderCredentialAllocation: ...

    def adopt(self, credential_version_id: str) -> ProviderCredentialReceipt: ...

    def resolve_active(self, credential_ref: str) -> ProviderCredentialAllocation: ...

    def mark_unavailable(self, provider_profile_id: str) -> None: ...

    def recovery_candidates(
        self,
    ) -> tuple[tuple[ProviderCredentialAllocation, str], ...]: ...

    def mark_orphan_cleaned(self, credential_version_id: str) -> None: ...

    def stage_profile_delete(
        self, provider_profile_id: str
    ) -> tuple[ProviderCredentialAllocation, ...]: ...

    def mark_deleted(self, credential_version_id: str) -> None: ...


class ProviderCredentialService:
    """Stage external secret writes before adopting an immutable credential version."""

    def __init__(
        self,
        repository: ProviderCredentialRepository,
        secret_store: ProviderSecretStore,
    ) -> None:
        self._repository = repository
        self._secret_store = secret_store

    def list_profiles(self) -> tuple[ProviderProfileSummary, ...]:
        return self._repository.list_profiles()

    def create_profile(
        self,
        *,
        provider_kind: str,
        endpoint: str,
        secret: str,
        account_label: str | None = None,
    ) -> ProviderCredentialReceipt:
        profile_id = f"provider_{secrets.token_hex(16)}"
        version_id = f"credentialversion_{secrets.token_hex(16)}"
        allocation = self._repository.allocate_profile(
            provider_profile_id=profile_id,
            provider_kind=provider_kind,
            endpoint=endpoint,
            account_label=account_label,
            credential_version_id=version_id,
            credential_ref=self._credential_ref(profile_id, version_id),
            target_name=self._target_name(provider_kind, profile_id, version_id),
        )
        return self._write_verify_adopt(allocation, secret)

    def rotate(self, provider_profile_id: str, *, secret: str) -> ProviderCredentialReceipt:
        version_id = f"credentialversion_{secrets.token_hex(16)}"
        allocation = self._repository.allocate_rotation(
            provider_profile_id=provider_profile_id,
            credential_version_id=version_id,
            credential_ref=self._credential_ref(provider_profile_id, version_id),
        )
        return self._write_verify_adopt(allocation, secret)

    def resolve_for_transport(
        self,
        credential_ref: str,
        *,
        provider_profile_id: str,
        endpoint: str,
    ) -> ResolvedProviderCredential:
        allocation = self._repository.resolve_active(credential_ref)
        if allocation.provider_profile_id != provider_profile_id or allocation.endpoint != endpoint:
            raise CredentialUnavailableError("credential binding rejected")
        try:
            secret = self._secret_store.read(allocation.target_name)
        except CredentialUnavailableError:
            self._repository.mark_unavailable(allocation.provider_profile_id)
            raise
        if not secret:
            self._repository.mark_unavailable(allocation.provider_profile_id)
            raise CredentialUnavailableError("credential is unavailable")
        return ResolvedProviderCredential(
            allocation.provider_profile_id,
            allocation.credential_version_id,
            allocation.endpoint,
            secret,
        )

    def recover(self) -> tuple[ProviderCredentialReceipt, ...]:
        receipts: list[ProviderCredentialReceipt] = []
        for allocation, state in self._repository.recovery_candidates():
            if state == "allocated":
                if self._secret_store.contains(allocation.target_name):
                    self._secret_store.delete(allocation.target_name)
                self._repository.mark_orphan_cleaned(allocation.credential_version_id)
                receipts.append(
                    ProviderCredentialReceipt(
                        allocation.provider_profile_id,
                        allocation.credential_version_id,
                        "orphan_cleaned",
                    )
                )
            elif state == "adopted" and not self._secret_store.contains(allocation.target_name):
                self._repository.mark_unavailable(allocation.provider_profile_id)
                receipts.append(
                    ProviderCredentialReceipt(
                        allocation.provider_profile_id,
                        allocation.credential_version_id,
                        "credential_unavailable",
                    )
                )
            elif state == "delete_pending":
                self._secret_store.delete(allocation.target_name)
                self._repository.mark_deleted(allocation.credential_version_id)
                receipts.append(
                    ProviderCredentialReceipt(
                        allocation.provider_profile_id,
                        allocation.credential_version_id,
                        "deleted",
                    )
                )
        return tuple(receipts)

    def delete_profile(self, provider_profile_id: str) -> ProviderProfileDeleteReceipt:
        allocations = self._repository.stage_profile_delete(provider_profile_id)
        for allocation in allocations:
            self._secret_store.delete(allocation.target_name)
            self._repository.mark_deleted(allocation.credential_version_id)
        return ProviderProfileDeleteReceipt(provider_profile_id, "deleted")

    def _write_verify_adopt(
        self, allocation: ProviderCredentialAllocation, secret: str
    ) -> ProviderCredentialReceipt:
        if not secret:
            raise CredentialLifecycleError("credential cannot be empty")
        self._secret_store.write(allocation.target_name, secret)
        try:
            observed = self._secret_store.read(allocation.target_name)
            if not hmac.compare_digest(observed, secret):
                raise CredentialLifecycleError("credential read-back verification failed")
            return self._repository.adopt(allocation.credential_version_id)
        except BaseException:
            # The allocated DB identity remains authoritative recovery input. Do not
            # move the active pointer and do not hide a possibly-created external secret.
            raise

    @staticmethod
    def _credential_ref(provider_profile_id: str, credential_version_id: str) -> str:
        return f"credential://{provider_profile_id}/{credential_version_id}"

    @staticmethod
    def _target_name(
        provider_kind: str, provider_profile_id: str, credential_version_id: str
    ) -> str:
        normalized_kind = "".join(
            character if character.isalnum() or character in {"-", "_"} else "_"
            for character in provider_kind.lower()
        )
        return (
            "StataResearchAgent/provider/"
            f"{normalized_kind}/{provider_profile_id}/{credential_version_id}"
        )
