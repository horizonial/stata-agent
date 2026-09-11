"""Persistence and credential adapters for application settings.

The package keeps non-secret settings in the revisioned local repository and
routes credentials through the platform-backed :class:`SecretStore` contract.
"""

from .local_repository import (
    DEFAULT_MAX_BYTES,
    SETTINGS_SCHEMA,
    SETTINGS_SCHEMA_PREFIX,
    LocalSettingsRepository,
    PERSISTABLE_SETTING_KEYS,
    RepositoryHealth,
    SettingsDocument,
    SettingsDocumentError,
    SettingsFileLock,
    SettingsRepositoryError,
    SettingsRevisionConflict,
)
from .secret_store import (
    PROVIDER_TARGETS,
    InMemorySecretStore,
    SecretStatus,
    SecretStore,
    SecretStoreError,
    SecretStoreUnavailable,
    SecretStoreValidationError,
    SecureStoreUnavailable,
    WindowsCredentialSecretStore,
    credential_target,
    resolve_secret_value,
    resolve_secret_status,
)

__all__ = [
    "DEFAULT_MAX_BYTES",
    "LocalSettingsRepository",
    "PERSISTABLE_SETTING_KEYS",
    "RepositoryHealth",
    "SETTINGS_SCHEMA",
    "SETTINGS_SCHEMA_PREFIX",
    "SettingsDocument",
    "SettingsDocumentError",
    "SettingsFileLock",
    "SettingsRepositoryError",
    "SettingsRevisionConflict",
    "PROVIDER_TARGETS",
    "InMemorySecretStore",
    "SecretStatus",
    "SecretStore",
    "SecretStoreError",
    "SecretStoreUnavailable",
    "SecretStoreValidationError",
    "SecureStoreUnavailable",
    "WindowsCredentialSecretStore",
    "credential_target",
    "resolve_secret_value",
    "resolve_secret_status",
]
