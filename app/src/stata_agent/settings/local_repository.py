"""Revisioned, non-secret local settings repository.

The settings repository deliberately has a much smaller responsibility than
the research ledger.  It stores an allow-listed JSON object, protects
compare-and-write with a side-car lock, and never attempts to migrate or
partially accept an invalid document.  The application service owns the
catalog and user-facing validation; this module repeats the structural checks
needed to keep direct repository callers safe.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SETTINGS_SCHEMA = "stata-agent.settings.v1"
SETTINGS_SCHEMA_PREFIX = "stata-agent.settings.v"
DEFAULT_MAX_BYTES = 64 * 1024
DEFAULT_MAX_DEPTH = 4
DEFAULT_MAX_STRING_LENGTH = 4096

# Duplicated deliberately at the persistence boundary to avoid importing the
# application layer (which owns the richer catalog and imports this module).
# A direct repository caller therefore cannot smuggle an arbitrary dotted key
# into the user settings file.  Secret keys are absent by construction.
PERSISTABLE_SETTING_KEYS = frozenset(
    {
        "provider.primary",
        "provider.model",
        "provider.live_enabled",
        "provider.base_url",
        "provider.deepseek.base_url",
        "provider.qwen.base_url",
        "privacy.mode",
        "executor.kind",
        "stata.mcp_dir",
        "library.root",
        "skills.root",
        "attachments.default_role",
        "agent.default_mode",
        "agent.interactive_max_steps",
        "agent.goal_max_steps",
        "agent.max_tool_calls",
        "compaction.summary_mode",
        "memory.extraction_mode",
        "context.max_input_tokens",
        "context.reserve_output_tokens",
        "context.recent_tail_tokens",
        "context.memory_tokens",
        "ui.theme",
        "ui.language",
        "ui.port",
        "storage.database",
        "storage.workspaces",
        "storage.attachments",
    }
)


class SettingsRepositoryError(RuntimeError):
    """Base error raised by the local settings repository."""

    code = "settings_repository_error"


class SettingsRevisionConflict(SettingsRepositoryError):
    """The caller attempted to update a stale settings revision."""

    code = "settings_revision_conflict"

    def __init__(self, expected: int, actual: int) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__("settings revision conflict")


class SettingsDocumentError(SettingsRepositoryError, ValueError):
    """A settings document is malformed, unsupported, or unsafe to persist."""

    code = "settings_invalid_document"

    def __init__(self, message: str = "invalid settings document", *, code: str | None = None) -> None:
        if code is not None:
            self.code = code
        # Do not include arbitrary JSON values in the error.  A settings file
        # can be user supplied and may accidentally contain a secret marker.
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class RepositoryHealth:
    """Safe health projection for the last repository read."""

    status: str = "ready"
    code: str = "ok"
    message: str = ""
    checked_at: int = 0

    @property
    def ok(self) -> bool:
        return self.status == "ready"

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "code": self.code,
            "message": self.message,
            "checked_at": self.checked_at,
        }


def _now_ms() -> int:
    return int(time.time() * 1000)


def _safe_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SettingsDocumentError(f"invalid settings {field}")
    return value


def _validate_json_value(value: Any, *, depth: int = 0) -> None:
    """Reject non-JSON values and intentionally hostile nesting/size."""

    if depth > DEFAULT_MAX_DEPTH:
        raise SettingsDocumentError("settings value nesting is too deep")
    if value is None or isinstance(value, (str, bool, int)):
        if isinstance(value, str) and len(value) > DEFAULT_MAX_STRING_LENGTH:
            raise SettingsDocumentError("settings value is too long")
        return
    # Floats are not part of the V1 catalog.  Explicitly reject them rather
    # than allowing NaN/Infinity through a permissive JSON encoder.
    if isinstance(value, float):
        raise SettingsDocumentError("settings value must not be a floating point number")
    if isinstance(value, list):
        if len(value) > 128:
            raise SettingsDocumentError("settings list is too long")
        for item in value:
            _validate_json_value(item, depth=depth + 1)
        return
    if isinstance(value, Mapping):
        if len(value) > 128:
            raise SettingsDocumentError("settings object has too many keys")
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > 256:
                raise SettingsDocumentError("settings object key is invalid")
            _validate_json_value(item, depth=depth + 1)
        return
    raise SettingsDocumentError("settings value is not JSON compatible")


@dataclass(frozen=True, slots=True)
class SettingsDocument:
    """On-disk document contract.

    ``values`` contains only user-owned non-secret overrides.  It is copied
    on construction so callers cannot mutate the object after validation.
    """

    schema: str = SETTINGS_SCHEMA
    revision: int = 0
    updated_at: int = 0
    values: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.schema != SETTINGS_SCHEMA:
            raise SettingsDocumentError("unsupported settings schema", code="settings_future_schema")
        revision = _safe_int(self.revision, field="revision")
        updated_at = _safe_int(self.updated_at, field="updated_at")
        raw_values = {} if self.values is None else self.values
        if not isinstance(raw_values, Mapping):
            raise SettingsDocumentError("settings values must be an object")
        copied = dict(raw_values)
        for key, value in copied.items():
            if not isinstance(key, str) or not key or len(key) > 256:
                raise SettingsDocumentError("settings key is invalid")
            _validate_json_value(value)
        object.__setattr__(self, "revision", revision)
        object.__setattr__(self, "updated_at", updated_at)
        object.__setattr__(self, "values", copied)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "revision": self.revision,
            "updated_at": self.updated_at,
            "values": dict(self.values or {}),
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "SettingsDocument":
        if not isinstance(raw, Mapping):
            raise SettingsDocumentError("settings document must be an object")
        schema = raw.get("schema")
        if schema != SETTINGS_SCHEMA:
            if isinstance(schema, str) and schema.startswith(SETTINGS_SCHEMA_PREFIX):
                raise SettingsDocumentError("unsupported settings schema", code="settings_future_schema")
            raise SettingsDocumentError("unsupported settings schema", code="settings_invalid_document")
        required = {"schema", "revision", "updated_at", "values"}
        if set(raw) != required:
            raise SettingsDocumentError("settings document fields are invalid")
        return cls(
            schema=SETTINGS_SCHEMA,
            revision=raw["revision"],
            updated_at=raw["updated_at"],
            values=raw["values"],
        )


class SettingsFileLock:
    """Small cross-process lock abstraction for a settings side-car file.

    Windows uses ``msvcrt.locking`` and POSIX uses ``fcntl.flock``.  The lock
    file is intentionally separate from ``settings.json`` so an interrupted
    atomic replace cannot invalidate the lock inode.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._handle: Any = None

    def __enter__(self) -> "SettingsFileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a+b")
        try:
            if sys.platform.startswith("win"):
                import msvcrt

                self._handle.seek(0)
                # Ensure the byte exists before locking it.  msvcrt locking
                # is process-wide and blocks until the byte is available.
                self._handle.write(b"0")
                self._handle.flush()
                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX)
        except Exception:
            self._handle.close()
            self._handle = None
            raise
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._handle is None:
            return
        try:
            if sys.platform.startswith("win"):
                import msvcrt

                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None


class LocalSettingsRepository:
    """Persist non-secret settings under the current user's local profile."""

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        clock: Any | None = None,
        lock_factory: Any = SettingsFileLock,
    ) -> None:
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
            raise ValueError("max_bytes must be a positive integer")
        self.path = Path(path) if path is not None else self.default_path()
        self.max_bytes = max_bytes
        self._clock = clock or _now_ms
        self._lock_factory = lock_factory
        self._health = RepositoryHealth(checked_at=self._clock())

    @staticmethod
    def default_path() -> Path:
        """Resolve the documented Windows path with a portable test fallback."""

        local_appdata = os.environ.get("LOCALAPPDATA", "").strip()
        if local_appdata:
            return Path(local_appdata) / "StataAgent" / "settings.json"
        if os.name == "nt":
            return Path.home() / "AppData" / "Local" / "StataAgent" / "settings.json"
        return Path.home() / ".local" / "share" / "StataAgent" / "settings.json"

    @property
    def lock_path(self) -> Path:
        return self.path.with_name(f".{self.path.name}.lock")

    @property
    def health(self) -> RepositoryHealth:
        return self._health

    @contextmanager
    def lock(self) -> Iterator[SettingsFileLock]:
        """Expose the lock context for tests and narrow adapters."""

        with self._lock_factory(self.lock_path) as lock:
            yield lock

    def _safe_document(self, *, code: str, message: str) -> SettingsDocument:
        self._health = RepositoryHealth(
            status="error",
            code=code,
            message=message,
            checked_at=self._clock(),
        )
        return SettingsDocument(updated_at=0, revision=0, values={})

    def _read_unlocked(self) -> SettingsDocument:
        if not self.path.exists():
            self._health = RepositoryHealth(status="ready", code="missing", message="", checked_at=self._clock())
            return SettingsDocument(updated_at=0, revision=0, values={})
        try:
            if self.path.is_symlink():
                return self._safe_document(code="settings_unsafe_path", message="settings file path is unsafe")
            size = self.path.stat().st_size
            if size > self.max_bytes:
                return self._safe_document(code="settings_oversized", message="settings file exceeds the size limit")
            raw_bytes = self.path.read_bytes()
            if len(raw_bytes) > self.max_bytes:
                return self._safe_document(code="settings_oversized", message="settings file exceeds the size limit")
            decoded = json.loads(raw_bytes.decode("utf-8"))
            document = SettingsDocument.from_mapping(decoded)
            # Structural validation is repeated on reads so unknown or
            # secret-looking values in a hand-edited file are ignored as a
            # whole rather than partially accepted.
            self._validate_values(document.values or {})
        except SettingsDocumentError as exc:
            return self._safe_document(code=exc.code, message=str(exc))
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            return self._safe_document(code="settings_corrupt", message="settings file is unreadable")
        self._health = RepositoryHealth(status="ready", code="ok", message="", checked_at=self._clock())
        return document

    def load(self) -> SettingsDocument:
        """Read the current document, failing closed on invalid input.

        Invalid, future, and oversized files remain untouched.  A safe empty
        document is returned so callers can continue in default mode while the
        health projection explains why saved overrides were ignored.
        """

        with self.lock():
            return self._read_unlocked()

    read = load

    def _validate_values(self, values: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(values, Mapping):
            raise SettingsDocumentError("settings values must be an object")
        copied = dict(values)
        for key, value in copied.items():
            if not isinstance(key, str) or not key:
                raise SettingsDocumentError("settings key is invalid")
            if key not in PERSISTABLE_SETTING_KEYS:
                raise SettingsDocumentError("settings key is not allow-listed")
            _validate_json_value(value)
        return copied

    def _encode(self, document: SettingsDocument) -> bytes:
        values = self._validate_values(document.values or {})
        normalized = SettingsDocument(
            schema=SETTINGS_SCHEMA,
            revision=document.revision,
            updated_at=document.updated_at,
            values=values,
        )
        encoded = json.dumps(
            normalized.as_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if len(encoded) > self.max_bytes:
            raise SettingsDocumentError("settings document exceeds the size limit", code="settings_oversized")
        return encoded

    def _atomic_replace(self, encoded: bytes) -> None:
        parent = self.path.parent
        parent.mkdir(parents=True, exist_ok=True)
        temporary: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                dir=parent,
                delete=False,
            ) as handle:
                temporary = handle.name
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            temporary = None
            # Directory fsync is available on POSIX.  Windows has no portable
            # equivalent; the atomic replace still provides the required
            # all-or-nothing file visibility there.
            if hasattr(os, "O_DIRECTORY"):
                try:
                    directory_fd = os.open(parent, os.O_DIRECTORY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                except OSError:
                    pass
        finally:
            if temporary is not None:
                try:
                    Path(temporary).unlink(missing_ok=True)
                except OSError:
                    pass

    def compare_and_write(
        self,
        expected_revision: int,
        values: Mapping[str, Any],
        *,
        updated_at: int | None = None,
    ) -> SettingsDocument:
        """Atomically replace values if ``expected_revision`` is current."""

        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 0:
            raise SettingsDocumentError("settings expected revision is invalid", code="settings_invalid_value")
        normalized_values = self._validate_values(values)
        with self.lock():
            current = self._read_unlocked()
            # Do not overwrite a corrupt/future/oversized file through a
            # seemingly valid patch.  The operator must repair/remove it
            # deliberately, while this process remains fail-closed.
            if not self._health.ok:
                raise SettingsRepositoryError("settings repository is unhealthy")
            if current.revision != expected_revision:
                raise SettingsRevisionConflict(expected_revision, current.revision)
            timestamp = self._clock() if updated_at is None else updated_at
            document = SettingsDocument(
                schema=SETTINGS_SCHEMA,
                revision=current.revision + 1,
                updated_at=timestamp,
                values=normalized_values,
            )
            encoded = self._encode(document)
            try:
                self._atomic_replace(encoded)
            except (OSError, ValueError) as exc:
                self._health = RepositoryHealth(
                    status="error",
                    code="settings_write_failed",
                    message="settings could not be written",
                    checked_at=self._clock(),
                )
                raise SettingsRepositoryError("settings could not be written") from exc
            self._health = RepositoryHealth(status="ready", code="ok", message="", checked_at=self._clock())
            return document

    write = compare_and_write
    save = compare_and_write


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
]
