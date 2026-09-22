"""Windows Credential Manager Generic Credential adapter."""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

from stata_research_agent.application.provider_credentials import (
    CredentialLifecycleError,
    CredentialUnavailableError,
)

_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2
_ERROR_NOT_FOUND = 1168
_MAX_CREDENTIAL_BYTES = 2560


class _CredentialW(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


class WindowsCredentialStore:
    """Persist secrets for the current Windows user without a plaintext fallback."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise CredentialLifecycleError("Windows Credential Manager requires Windows")
        self._advapi = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        self._advapi.CredWriteW.argtypes = [ctypes.POINTER(_CredentialW), wintypes.DWORD]
        self._advapi.CredWriteW.restype = wintypes.BOOL
        self._advapi.CredReadW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.POINTER(_CredentialW)),
        ]
        self._advapi.CredReadW.restype = wintypes.BOOL
        self._advapi.CredDeleteW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
        ]
        self._advapi.CredDeleteW.restype = wintypes.BOOL
        self._advapi.CredFree.argtypes = [ctypes.c_void_p]
        self._advapi.CredFree.restype = None

    def write(self, target_name: str, secret: str) -> None:
        self._validate_target(target_name)
        encoded = bytearray(secret.encode("utf-8"))
        if not encoded or len(encoded) > _MAX_CREDENTIAL_BYTES:
            self._zero(encoded)
            raise CredentialLifecycleError("credential size is invalid")
        blob = (ctypes.c_ubyte * len(encoded)).from_buffer(encoded)
        credential = _CredentialW()
        credential.Type = _CRED_TYPE_GENERIC
        credential.TargetName = target_name
        credential.CredentialBlobSize = len(encoded)
        credential.CredentialBlob = ctypes.cast(blob, ctypes.POINTER(ctypes.c_ubyte))
        credential.Persist = _CRED_PERSIST_LOCAL_MACHINE
        credential.UserName = "Stata Research Agent"
        try:
            if not self._advapi.CredWriteW(ctypes.byref(credential), 0):
                raise CredentialLifecycleError("Credential Manager write failed")
        finally:
            self._zero(encoded)

    def read(self, target_name: str) -> str:
        self._validate_target(target_name)
        pointer = ctypes.POINTER(_CredentialW)()
        if not self._advapi.CredReadW(
            target_name,
            _CRED_TYPE_GENERIC,
            0,
            ctypes.byref(pointer),
        ):
            if ctypes.get_last_error() == _ERROR_NOT_FOUND:
                raise CredentialUnavailableError("credential is unavailable")
            raise CredentialLifecycleError("Credential Manager read failed")
        try:
            credential = pointer.contents
            raw = bytearray(
                ctypes.string_at(
                    credential.CredentialBlob,
                    credential.CredentialBlobSize,
                )
            )
            try:
                return raw.decode("utf-8", errors="strict")
            except UnicodeDecodeError as error:
                raise CredentialUnavailableError("credential payload is invalid") from error
            finally:
                self._zero(raw)
        finally:
            self._advapi.CredFree(pointer)

    def contains(self, target_name: str) -> bool:
        self._validate_target(target_name)
        pointer = ctypes.POINTER(_CredentialW)()
        if self._advapi.CredReadW(
            target_name,
            _CRED_TYPE_GENERIC,
            0,
            ctypes.byref(pointer),
        ):
            self._advapi.CredFree(pointer)
            return True
        if ctypes.get_last_error() == _ERROR_NOT_FOUND:
            return False
        raise CredentialLifecycleError("Credential Manager availability check failed")

    def delete(self, target_name: str) -> None:
        self._validate_target(target_name)
        if self._advapi.CredDeleteW(target_name, _CRED_TYPE_GENERIC, 0):
            return
        if ctypes.get_last_error() != _ERROR_NOT_FOUND:
            raise CredentialLifecycleError("Credential Manager delete failed")

    @staticmethod
    def _validate_target(target_name: str) -> None:
        if not target_name.startswith("StataResearchAgent/provider/"):
            raise CredentialLifecycleError("credential target namespace is invalid")

    @staticmethod
    def _zero(value: bytearray) -> None:
        for index in range(len(value)):
            value[index] = 0
