"""安全的 provider credential 存储边界。

This module deliberately keeps credentials separate from the settings JSON,
SQLite ledger, diagnostics and browser projections.  ``SecretStore`` is the
small adapter used by the application composition boundary; it is not a
general credential enumeration API.

The production implementation uses the Windows Credential Manager generic
credential APIs directly through :mod:`ctypes`.  On non-Windows systems, or
when those APIs cannot be loaded, every credential-manager operation fails
closed with ``secure_store_unavailable``.  The in-memory implementation is
for tests and offline composition only.
"""

from __future__ import annotations

import ctypes
import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

from ..providers.catalog import PROVIDER_DEFINITIONS

# These mappings are derived from the reviewed provider catalog.  The set is
# still closed: callers cannot enumerate or overwrite unrelated Windows
# credentials, and adding a provider requires an explicit catalog entry.
SUPPORTED_SECRET_PROVIDERS = frozenset(item.provider_id for item in PROVIDER_DEFINITIONS)
PROVIDER_ENV_VARS: Mapping[str, str] = {
    item.provider_id: item.api_key_env for item in PROVIDER_DEFINITIONS
}
PROVIDER_TARGETS: Mapping[str, str] = {
    item.provider_id: item.credential_target for item in PROVIDER_DEFINITIONS
}

SECRET_SOURCES = frozenset({"none", "dotenv", "credential", "environment"})
SecretSource = Literal["none", "dotenv", "credential", "environment"]

# Credential Manager generic blobs are small.  Keeping an application bound
# here also prevents accidental use as a large arbitrary secret store.  The
# Windows API has a lower platform limit for some credential types; failures
# from the API are still converted to the same safe unavailable error.
MAX_SECRET_BYTES = 4096


class SecretStoreError(RuntimeError):
    """Base error with a stable, non-sensitive public code."""

    code = "secure_store_unavailable"

    def __init__(self, code: str | None = None) -> None:
        normalized = str(code or self.code).strip().lower()
        if normalized not in {"secure_store_unavailable", "settings_invalid_value"}:
            normalized = self.code
        self.code = normalized
        super().__init__(_SAFE_ERROR_MESSAGES[normalized])


class SecureStoreUnavailable(SecretStoreError):
    """Credential persistence is unavailable and must not fall back to a file."""

    code = "secure_store_unavailable"


# A spelling that is natural at call sites and useful for compatibility with
# callers that name the adapter itself ``SecretStore``.
SecretStoreUnavailable = SecureStoreUnavailable


class SecretStoreValidationError(SecretStoreError, ValueError):
    """The provider or secret value is outside the closed contract."""

    code = "settings_invalid_value"


_SAFE_ERROR_MESSAGES = {
    "secure_store_unavailable": "安全凭据存储不可用，未保存密钥。",
    "settings_invalid_value": "凭据设置无效。",
}


@dataclass(frozen=True, slots=True)
class SecretStatus:
    """Masked credential state safe for HTTP/UI/diagnostic projections.

    This object intentionally has no secret-derived metadata (length, prefix,
    timestamps, hashes or target internals).  ``source='none'`` means that no
    configured credential was found.  ``editable`` is false for an explicit
    environment override and true for a user-managed credential slot.
    """

    provider: str
    configured: bool
    source: SecretSource
    editable: bool

    def __post_init__(self) -> None:
        provider = _normalize_provider(self.provider)
        source = str(self.source).strip().lower()
        if source not in SECRET_SOURCES:
            raise SecretStoreValidationError()
        if not isinstance(self.configured, bool) or not isinstance(self.editable, bool):
            raise SecretStoreValidationError()
        if source == "none" and self.configured:
            raise SecretStoreValidationError()
        if source in {"dotenv", "credential", "environment"} and not self.configured:
            raise SecretStoreValidationError()
        if source == "environment" and self.editable:
            raise SecretStoreValidationError()
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "source", source)

    def as_dict(self) -> dict[str, object]:
        """Return the complete allowed public projection, never a secret."""

        return {
            "provider": self.provider,
            "configured": self.configured,
            "source": self.source,
            "editable": self.editable,
        }

    # ``to_dict`` is a harmless compatibility alias used by other project
    # contracts.  It has exactly the same allow-listed output.
    to_dict = as_dict


@runtime_checkable
class SecretStore(Protocol):
    """Closed credential lifecycle used by provider composition.

    ``get``/``read_secret`` is an internal composition operation: only the
    provider factory may consume its return value.  It must never be passed to
    a response, event, logger or serializer.  All lifecycle mutation methods
    return :class:`SecretStatus`, not the stored value.
    """

    def get(self, provider: str) -> str | None:
        """Read one fixed provider secret for an in-process provider factory."""

    def set(self, provider: str, secret: str) -> SecretStatus:
        """Create or update the fixed provider credential slot."""

    def replace(self, provider: str, secret: str) -> SecretStatus:
        """Replace the fixed provider credential slot."""

    def delete(self, provider: str) -> SecretStatus:
        """Delete one fixed provider credential slot."""

    def status(self, provider: str) -> SecretStatus:
        """Return a masked status projection for one provider."""


def _normalize_provider(provider: str) -> str:
    if not isinstance(provider, str):
        raise SecretStoreValidationError()
    normalized = provider.strip().lower()
    if normalized not in SUPPORTED_SECRET_PROVIDERS:
        raise SecretStoreValidationError()
    return normalized


def credential_target(provider: str) -> str:
    """Return the fixed target for a supported provider.

    There is deliberately no public ``target`` argument on the store methods;
    this function is the only target mapping exposed to composition/tests.
    """

    return PROVIDER_TARGETS[_normalize_provider(provider)]


def provider_environment_key(provider: str) -> str:
    """Return the compatibility environment variable for a provider."""

    return PROVIDER_ENV_VARS[_normalize_provider(provider)]


def _validate_secret(secret: str) -> str:
    if not isinstance(secret, str):
        raise SecretStoreValidationError()
    value = secret.strip()
    if not value:
        raise SecretStoreValidationError()
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as error:
        del error
        raise SecretStoreValidationError() from None
    if size > MAX_SECRET_BYTES:
        raise SecretStoreValidationError()
    return value


def _configured_value(value: object) -> str | None:
    """Normalize compatibility values without ever exposing them in errors."""

    if not isinstance(value, str):
        return None
    candidate = value.strip()
    return candidate or None


class InMemorySecretStore:
    """Thread-safe test double with the same closed provider surface.

    It is intentionally ephemeral.  It must not be used as a production
    fallback when Windows Credential Manager is unavailable.
    """

    def __init__(self, initial: Mapping[str, str] | None = None) -> None:
        self._lock = threading.RLock()
        self._secrets: dict[str, str] = {}
        if initial is not None:
            for provider, secret in initial.items():
                normalized = _normalize_provider(provider)
                self._secrets[normalized] = _validate_secret(secret)

    def __repr__(self) -> str:
        with self._lock:
            configured = sum(1 for _ in self._secrets)
        return f"InMemorySecretStore(configured={configured})"

    def get(self, provider: str) -> str | None:
        normalized = _normalize_provider(provider)
        with self._lock:
            return self._secrets.get(normalized)

    # Explicit alias for composition code that wants to make the secret
    # boundary visible.  It has no public serialization/projection method.
    read_secret = get

    def set(self, provider: str, secret: str) -> SecretStatus:
        return self._put(provider, secret)

    def replace(self, provider: str, secret: str) -> SecretStatus:
        return self._put(provider, secret)

    def _put(self, provider: str, secret: str) -> SecretStatus:
        normalized = _normalize_provider(provider)
        value = _validate_secret(secret)
        with self._lock:
            self._secrets[normalized] = value
        return self.status(normalized)

    def delete(self, provider: str) -> SecretStatus:
        normalized = _normalize_provider(provider)
        with self._lock:
            self._secrets.pop(normalized, None)
        return self.status(normalized)

    def status(self, provider: str) -> SecretStatus:
        normalized = _normalize_provider(provider)
        with self._lock:
            configured = normalized in self._secrets
        return SecretStatus(
            provider=normalized,
            configured=configured,
            source="credential" if configured else "none",
            editable=True,
        )


# Windows Credential Manager constants and structures.  They are defined at
# module import time because ctypes.Structure declarations are platform
# neutral; loading Advapi32 is deferred to the adapter constructor.
_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2
_ERROR_FILE_NOT_FOUND = 2
_ERROR_NOT_FOUND = 1168


class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", ctypes.c_ulong), ("dwHighDateTime", ctypes.c_ulong)]


class _CREDENTIAL_ATTRIBUTEW(ctypes.Structure):
    _fields_ = [
        ("Keyword", ctypes.c_wchar_p),
        ("Flags", ctypes.c_ulong),
        ("ValueSize", ctypes.c_ulong),
        ("Value", ctypes.POINTER(ctypes.c_ubyte)),
    ]


class _CREDENTIALW(ctypes.Structure):
    _fields_ = [
        ("Flags", ctypes.c_ulong),
        ("Type", ctypes.c_ulong),
        ("TargetName", ctypes.c_wchar_p),
        ("Comment", ctypes.c_wchar_p),
        ("LastWritten", _FILETIME),
        ("CredentialBlobSize", ctypes.c_ulong),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", ctypes.c_ulong),
        ("AttributeCount", ctypes.c_ulong),
        ("Attributes", ctypes.POINTER(_CREDENTIAL_ATTRIBUTEW)),
        ("TargetAlias", ctypes.c_wchar_p),
        ("UserName", ctypes.c_wchar_p),
    ]


class WindowsCredentialSecretStore:
    """Credential Manager-backed store for supported provider credentials.

    Construction is side-effect free.  On non-Windows or when ``Advapi32``
    cannot be loaded, ``available`` is false and all persistence operations
    raise :class:`SecureStoreUnavailable`; no JSON/file fallback is attempted.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._api: Any | None = None
        if os.name != "nt":
            return
        try:
            api = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
            self._configure_api(api)
            self._api = api
        except Exception:
            # Do not retain a partially loaded backend or expose platform
            # loader/credential details to callers.
            self._api = None

    @property
    def available(self) -> bool:
        return self._api is not None

    def __repr__(self) -> str:
        return f"WindowsCredentialSecretStore(available={self.available})"

    @staticmethod
    def _configure_api(api: Any) -> None:
        api.CredReadW.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.POINTER(ctypes.POINTER(_CREDENTIALW)),
        ]
        api.CredReadW.restype = ctypes.c_bool
        api.CredWriteW.argtypes = [ctypes.POINTER(_CREDENTIALW), ctypes.c_ulong]
        api.CredWriteW.restype = ctypes.c_bool
        api.CredDeleteW.argtypes = [ctypes.c_wchar_p, ctypes.c_ulong, ctypes.c_ulong]
        api.CredDeleteW.restype = ctypes.c_bool
        api.CredFree.argtypes = [ctypes.c_void_p]
        api.CredFree.restype = None

    def _require_api(self) -> Any:
        if self._api is None:
            raise SecureStoreUnavailable()
        return self._api

    @staticmethod
    def _last_error() -> int:
        try:
            return int(ctypes.get_last_error())
        except Exception:
            return 0

    def get(self, provider: str) -> str | None:
        normalized = _normalize_provider(provider)
        target = credential_target(normalized)
        with self._lock:
            return self._read_target(target)

    read_secret = get

    def set(self, provider: str, secret: str) -> SecretStatus:
        return self._put(provider, secret)

    def replace(self, provider: str, secret: str) -> SecretStatus:
        return self._put(provider, secret)

    def _put(self, provider: str, secret: str) -> SecretStatus:
        normalized = _normalize_provider(provider)
        value = _validate_secret(secret)
        target = credential_target(normalized)
        with self._lock:
            api = self._require_api()
            encoded = bytearray(value.encode("utf-8"))
            try:
                blob = (ctypes.c_ubyte * len(encoded)).from_buffer(encoded)
                credential = _CREDENTIALW()
                credential.Type = _CRED_TYPE_GENERIC
                credential.TargetName = target
                credential.CredentialBlobSize = len(encoded)
                credential.CredentialBlob = ctypes.cast(blob, ctypes.POINTER(ctypes.c_ubyte))
                credential.Persist = _CRED_PERSIST_LOCAL_MACHINE
                credential.UserName = "StataAgent"
                if not api.CredWriteW(ctypes.byref(credential), 0):
                    raise SecureStoreUnavailable()
            except SecretStoreError:
                raise
            except Exception:
                raise SecureStoreUnavailable() from None
            finally:
                # Keep the mutable ctypes buffer from retaining the key after
                # the API call.  Python's immutable ``value`` is intentionally
                # not returned or serialized by this method.
                for index in range(len(encoded)):
                    encoded[index] = 0
        return SecretStatus(normalized, True, "credential", True)

    def delete(self, provider: str) -> SecretStatus:
        normalized = _normalize_provider(provider)
        target = credential_target(normalized)
        with self._lock:
            api = self._require_api()
            try:
                if not api.CredDeleteW(target, _CRED_TYPE_GENERIC, 0):
                    error = self._last_error()
                    if error not in {_ERROR_FILE_NOT_FOUND, _ERROR_NOT_FOUND}:
                        raise SecureStoreUnavailable()
            except SecretStoreError:
                raise
            except Exception:
                raise SecureStoreUnavailable() from None
        return SecretStatus(normalized, False, "none", True)

    def status(self, provider: str) -> SecretStatus:
        normalized = _normalize_provider(provider)
        target = credential_target(normalized)
        with self._lock:
            configured = self._read_target(target) is not None
        return SecretStatus(normalized, configured, "credential" if configured else "none", True)

    def _read_target(self, target: str) -> str | None:
        api = self._require_api()
        credential = ctypes.POINTER(_CREDENTIALW)()
        try:
            if not api.CredReadW(target, _CRED_TYPE_GENERIC, 0, ctypes.byref(credential)):
                error = self._last_error()
                if error in {_ERROR_FILE_NOT_FOUND, _ERROR_NOT_FOUND}:
                    return None
                raise SecureStoreUnavailable()
            if not credential:
                raise SecureStoreUnavailable()
            size = int(credential.contents.CredentialBlobSize)
            pointer = credential.contents.CredentialBlob
            if size <= 0 or not pointer:
                raise SecureStoreUnavailable()
            raw = bytearray(ctypes.string_at(pointer, size))
            try:
                return bytes(raw).decode("utf-8")
            except (UnicodeDecodeError, ValueError):
                raise SecureStoreUnavailable() from None
            finally:
                for index in range(len(raw)):
                    raw[index] = 0
        except SecretStoreError:
            raise
        except Exception:
            raise SecureStoreUnavailable() from None
        finally:
            if credential:
                try:
                    api.CredFree(credential)
                except Exception:
                    # A read error is already fail-closed; freeing is best
                    # effort and must never surface platform details.
                    pass


def resolve_secret_value(
    provider: str,
    store: SecretStore,
    *,
    dotenv: Mapping[str, object] | None = None,
    environ: Mapping[str, object] | None = None,
) -> str | None:
    """Resolve one secret using ``dotenv < credential < environment``.

    The returned value is for the in-process provider factory only.  This
    helper intentionally has no serializer or masked/public conversion.  An
    unavailable persistent store raises rather than writing or reading a
    plaintext replacement; explicit environment credentials are checked first
    so offline/legacy environment-only runs remain usable.
    """

    normalized = _normalize_provider(provider)
    env = os.environ if environ is None else environ
    env_value = _configured_value(env.get(PROVIDER_ENV_VARS[normalized]))
    if env_value is not None:
        return env_value

    try:
        credential = store.get(normalized)
    except SecureStoreUnavailable:
        # A missing credential manager must never create a file fallback, but
        # legacy dotenv credentials remain a valid compatibility source.
        credential = None
    if credential is not None:
        return _validate_secret(credential)

    if dotenv is not None:
        return _configured_value(dotenv.get(PROVIDER_ENV_VARS[normalized]))
    return None


def resolve_secret_status(
    provider: str,
    store: SecretStore,
    *,
    dotenv: Mapping[str, object] | None = None,
    environ: Mapping[str, object] | None = None,
) -> SecretStatus:
    """Resolve a safe status projection using the same source precedence."""

    normalized = _normalize_provider(provider)
    env = os.environ if environ is None else environ
    if _configured_value(env.get(PROVIDER_ENV_VARS[normalized])) is not None:
        return SecretStatus(normalized, True, "environment", False)

    try:
        status = store.status(normalized)
    except SecureStoreUnavailable:
        status = SecretStatus(normalized, False, "none", True)
    if status.configured:
        return SecretStatus(normalized, True, "credential", True)

    if dotenv is not None and _configured_value(dotenv.get(PROVIDER_ENV_VARS[normalized])) is not None:
        return SecretStatus(normalized, True, "dotenv", True)
    return SecretStatus(normalized, False, "none", True)


__all__ = [
    "InMemorySecretStore",
    "MAX_SECRET_BYTES",
    "PROVIDER_ENV_VARS",
    "PROVIDER_TARGETS",
    "SECRET_SOURCES",
    "SecretSource",
    "SecretStatus",
    "SecretStore",
    "SecretStoreError",
    "SecretStoreUnavailable",
    "SecretStoreValidationError",
    "SecureStoreUnavailable",
    "WindowsCredentialSecretStore",
    "credential_target",
    "provider_environment_key",
    "resolve_secret_status",
    "resolve_secret_value",
]
