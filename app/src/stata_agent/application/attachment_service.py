"""Framework-neutral, workspace-scoped PDF attachment intake.

This module owns the boundary between untrusted bytes and the existing
SQLite ledger/storage.  It deliberately does not import FastAPI, provider
code, or a task queue.  The SQLite store supplies the only durable metadata
operations; PDF bytes and extracted text remain in a private filesystem root.
"""

from __future__ import annotations

import builtins
import hashlib
import multiprocessing
import os
import re
import secrets
import time
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, BinaryIO, Protocol

from ..events.schema import (
    ACTOR_SYSTEM,
    EVENT_ARTIFACT,
    EVENT_ATTACHMENT_FAILED,
    EVENT_ATTACHMENT_INTAKE_REQUESTED,
    EVENT_ATTACHMENT_QUARANTINED,
    EVENT_ATTACHMENT_REJECTED,
    Event,
)
from ..storage.store import (
    ATTACHMENT_FAILED,
    ATTACHMENT_PENDING,
    ATTACHMENT_QUARANTINED,
    ATTACHMENT_READY,
    ATTACHMENT_REJECTED,
    AttachmentRecord,
    AttachmentStore,
)

PDF_MAGIC = b"%PDF-"
ALLOWED_MEDIA_TYPES = frozenset({"", "application/pdf", "application/octet-stream"})
SOURCE_ROLE_STYLE = "style_only"
SOURCE_ROLE_CITABLE = "citable_evidence"
ATTACHMENT_SOURCE_ROLES = frozenset({SOURCE_ROLE_STYLE, SOURCE_ROLE_CITABLE})


class AttachmentStatus(str, Enum):
    PENDING = "pending"
    READY = "ready"
    QUARANTINED = "quarantined"
    REJECTED = "rejected"
    FAILED = "failed"

ERROR_FILE_TOO_LARGE = "file_too_large"
ERROR_WORKSPACE_QUOTA = "workspace_quota_exceeded"
ERROR_READY_QUOTA = "ready_quota_exceeded"
ERROR_EMPTY_FILE = "empty_file"
ERROR_DECLARED_SIZE_MISMATCH = "declared_size_mismatch"
ERROR_EXTENSION_MISMATCH = "extension_mismatch"
ERROR_MIME_NOT_ALLOWED = "mime_not_allowed"
ERROR_MAGIC_MISMATCH = "magic_mismatch"
ERROR_DUPLICATE_CONTENT = "duplicate_content"
ERROR_ENCRYPTED_PDF = "encrypted_pdf"
ERROR_SCANNED_PDF = "scanned_pdf"
ERROR_EMPTY_PDF = "empty_pdf"
ERROR_ACTIVE_CONTENT = "active_content"
ERROR_EMBEDDED_CONTENT = "embedded_content"
ERROR_PARSER_TIMEOUT = "parser_timeout"
ERROR_PARSER_CRASHED = "parser_crashed"
ERROR_PARSER_RESULT_LIMIT = "parser_result_limit"
ERROR_MISSING_STAGING = "missing_staging"
ERROR_STALE_STAGING = "stale_staging"
ERROR_ARTIFACT_MISSING = "artifact_missing"
ERROR_HASH_MISMATCH = "hash_mismatch"
ERROR_QUARANTINE_EXPIRED = "quarantine_expired"
ERROR_INTERNAL = "internal_failure"

_QUARANTINE_ERRORS = frozenset(
    {
        ERROR_ENCRYPTED_PDF,
        ERROR_SCANNED_PDF,
        ERROR_EMPTY_PDF,
        ERROR_ACTIVE_CONTENT,
        ERROR_EMBEDDED_CONTENT,
        ERROR_PARSER_TIMEOUT,
        ERROR_PARSER_CRASHED,
        ERROR_PARSER_RESULT_LIMIT,
    }
)
_RESERVED_WINDOWS_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
)
_ERROR_RE = re.compile(r"[^a-z0-9_]+")
_STABLE_ERROR_CODES = frozenset(
    {
        ERROR_FILE_TOO_LARGE, ERROR_WORKSPACE_QUOTA, ERROR_READY_QUOTA, ERROR_EMPTY_FILE,
        ERROR_DECLARED_SIZE_MISMATCH, ERROR_EXTENSION_MISMATCH, ERROR_MIME_NOT_ALLOWED,
        ERROR_MAGIC_MISMATCH, ERROR_DUPLICATE_CONTENT, ERROR_ENCRYPTED_PDF, ERROR_SCANNED_PDF,
        ERROR_EMPTY_PDF, ERROR_ACTIVE_CONTENT, ERROR_EMBEDDED_CONTENT, ERROR_PARSER_TIMEOUT,
        ERROR_PARSER_CRASHED, ERROR_PARSER_RESULT_LIMIT, ERROR_MISSING_STAGING, ERROR_STALE_STAGING,
        ERROR_ARTIFACT_MISSING, ERROR_HASH_MISMATCH, ERROR_QUARANTINE_EXPIRED, ERROR_INTERNAL,
        "attachment_reference_limit",
    }
)


class AttachmentError(RuntimeError):
    """Stable attachment service error base."""


class AttachmentValidationError(AttachmentError, ValueError):
    """Request metadata is invalid."""


class AttachmentLimitError(AttachmentValidationError):
    """A file, workspace, or bounded collection exceeds its limit."""


AttachmentQuotaError = AttachmentLimitError


class AttachmentUnsupportedError(AttachmentValidationError):
    """The declared or detected content is not an accepted PDF."""


class AttachmentConflictError(AttachmentError, ValueError):
    """The same idempotency key/content is in a conflicting state."""


class AttachmentParserError(AttachmentError):
    """Parser isolation failed; the caller receives a stable error code."""


@dataclass(frozen=True, slots=True)
class AttachmentLimits:
    """V1 bounded attachment and parser policy."""

    max_file_bytes: int = 25 * 1024 * 1024
    max_references_per_turn: int = 8
    max_retained_bytes: int = 512 * 1024 * 1024
    max_ready_attachments: int = 128
    max_filename_chars: int = 180
    max_pages: int = 64
    max_chunks: int = 10_000
    max_extracted_chars: int = 2_000_000
    parser_timeout_seconds: float = 20.0
    staging_grace_seconds: int = 3600
    quarantine_ttl_seconds: int = 7 * 24 * 3600
    reconcile_batch: int = 100
    max_cache_bytes: int = 8 * 1024 * 1024

    def __post_init__(self) -> None:
        integer_limits = (
            "max_file_bytes", "max_references_per_turn", "max_retained_bytes", "max_ready_attachments",
            "max_filename_chars", "max_pages", "max_chunks", "max_extracted_chars",
            "staging_grace_seconds", "quarantine_ttl_seconds", "reconcile_batch", "max_cache_bytes",
        )
        for name in integer_limits:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if isinstance(self.parser_timeout_seconds, bool) or float(self.parser_timeout_seconds) <= 0:
            raise ValueError("parser_timeout_seconds must be positive")


@dataclass(frozen=True, slots=True)
class ParseOutcome:
    """Bounded scalar parser result; no text is returned across the process."""

    detected_format: str = "pdf"
    parser_version: int = 1
    page_count: int = 0
    chunk_count: int = 0
    extracted_chars: int = 0
    scanned_suspect: bool = False
    error_code: str | None = None


class AttachmentParser(Protocol):
    def parse(
        self,
        path: str | Path,
        *,
        attachment_id: str,
        source_role: str,
        max_pages: int,
        max_chunks: int,
        max_chars: int,
    ) -> ParseOutcome | Mapping[str, object] | tuple[object, Mapping[str, object]]:
        ...


def _stable_error(value: object, default: str = ERROR_INTERNAL) -> str:
    if not isinstance(value, str):
        return default
    result = _ERROR_RE.sub("_", value.strip().lower()).strip("_")[:64]
    return result if result in _STABLE_ERROR_CODES else default


def _as_nonnegative_int(value: object, default: int = 0) -> int:
    """Coerce untrusted parser metadata to one bounded non-negative integer."""

    if isinstance(value, bool):
        return default
    if not isinstance(value, (str, bytes, bytearray, int, float)):
        return default
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return default


def validate_display_filename(filename: str, *, max_chars: int = 180) -> str:
    """Validate a display-only PDF name without ever using it as a path."""

    if not isinstance(filename, str):
        raise AttachmentValidationError("filename must be text")
    if not filename or filename in {".", ".."} or len(filename) > max_chars:
        raise AttachmentValidationError("invalid display filename")
    if filename != filename.strip() or not filename.lower().endswith(".pdf"):
        raise AttachmentValidationError("filename must be a .pdf name")
    if any(ord(char) < 32 or ord(char) == 127 for char in filename):
        raise AttachmentValidationError("filename contains a control character")
    if any(char in filename for char in ("/", "\\", ":")):
        raise AttachmentValidationError("filename contains a path separator")
    stem = filename.rsplit(".", 1)[0].upper()
    if stem in _RESERVED_WINDOWS_NAMES:
        raise AttachmentValidationError("filename uses a reserved device name")
    return filename


def validate_workspace_identity(workspace_id: str) -> str:
    if not isinstance(workspace_id, str) or not workspace_id.strip() or len(workspace_id) > 512:
        raise AttachmentValidationError("workspace_id is invalid")
    if any(ord(char) < 32 or ord(char) == 127 for char in workspace_id):
        raise AttachmentValidationError("workspace_id contains a control character")
    return workspace_id.strip()


def _is_reparse_or_link(path: Path) -> bool:
    if path.is_symlink():
        return True
    checker = getattr(path, "is_junction", None)
    if callable(checker) and checker():
        return True
    try:
        attrs = path.stat(follow_symlinks=False).st_file_attributes
    except (AttributeError, OSError):
        return False
    return bool(attrs & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT


def _ensure_real_directory(path: Path) -> None:
    if path.exists():
        if _is_reparse_or_link(path) or not path.is_dir():
            raise AttachmentValidationError("attachment root contains a link or non-directory")
        return
    path.mkdir()
    if _is_reparse_or_link(path) or not path.is_dir():
        raise AttachmentValidationError("attachment directory is not private")


@dataclass(frozen=True, slots=True)
class AttachmentPaths:
    """Private path derivation from opaque IDs and generated content hashes."""

    root: Path
    workspace_id: str

    def __post_init__(self) -> None:
        # Keep the lexical root so ``prepare`` can reject a configured
        # symlink/junction instead of silently resolving through it first.
        object.__setattr__(self, "root", Path(os.path.abspath(str(Path(self.root).expanduser()))))
        object.__setattr__(self, "workspace_id", validate_workspace_identity(self.workspace_id))

    @property
    def workspace_token(self) -> str:
        return hashlib.sha256(self.workspace_id.encode("utf-8")).hexdigest()

    @property
    def workspace_root(self) -> Path:
        return self.root / "workspaces" / self.workspace_token

    @property
    def staging_root(self) -> Path:
        return self.workspace_root / "staging"

    @property
    def quarantine_root(self) -> Path:
        return self.workspace_root / "quarantine"

    @property
    def objects_root(self) -> Path:
        return self.workspace_root / "objects"

    @property
    def derived_root(self) -> Path:
        return self.workspace_root / "derived"

    def prepare(self) -> None:
        # Check existing ancestors as well as managed descendants.  A managed
        # symlink/junction is rejected before any write is attempted.
        for ancestor in (self.root, *self.root.parents):
            if ancestor.exists() and _is_reparse_or_link(ancestor):
                raise AttachmentValidationError("attachment root contains a link or reparse point")
        if not self.root.exists():
            missing: list[Path] = []
            cursor = self.root
            while not cursor.exists():
                missing.append(cursor)
                cursor = cursor.parent
            if _is_reparse_or_link(cursor):
                raise AttachmentValidationError("attachment root parent contains a link")
            for item in reversed(missing):
                _ensure_real_directory(item)
        elif _is_reparse_or_link(self.root) or not self.root.is_dir():
            raise AttachmentValidationError("attachment root is not a private directory")
        for directory in (
            self.root / "workspaces", self.workspace_root, self.staging_root,
            self.quarantine_root, self.objects_root, self.derived_root,
        ):
            _ensure_real_directory(directory)

    def _safe(self, path: Path) -> Path:
        candidate = path.resolve(strict=False)
        base = self.workspace_root.resolve(strict=False)
        try:
            candidate.relative_to(base)
        except ValueError as exc:
            raise AttachmentValidationError("attachment path escaped workspace root") from exc
        return candidate

    def staging_path(self, attachment_id: str) -> Path:
        return self._safe(self.staging_root / f"{attachment_id}.part")

    def quarantine_path(self, attachment_id: str) -> Path:
        return self._safe(self.quarantine_root / f"{attachment_id}.pdf")

    def object_path(self, digest: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise AttachmentValidationError("invalid generated object digest")
        return self._safe(self.objects_root / digest[:2] / f"{digest}.pdf")

    def object_key(self, digest: str) -> str:
        # Relative POSIX spelling is persisted; it is not an absolute path.
        return f"objects/{digest[:2]}/{digest}.pdf"


def _spawn_parser_worker(
    path: str,
    max_pages: int,
    max_chunks: int,
    max_chars: int,
    connection: Any,
) -> None:
    """Spawn target: imports the concrete PDF parser only in the child."""

    try:
        import fitz

        document = fitz.open(path)
        try:
            if bool(getattr(document, "needs_pass", False)):
                connection.send({"error_code": ERROR_ENCRYPTED_PDF, "parser_version": 1})
                return
            embedded = getattr(document, "embfile_names", lambda: ())()
            if embedded:
                connection.send({"error_code": ERROR_EMBEDDED_CONTENT, "parser_version": 1})
                return
            # Reject common executable/action dictionaries without returning
            # the PDF object's raw bytes or strings to the parent process.
            xref_length = min(4096, max(0, int(getattr(document, "xref_length", lambda: 0)())))
            active_markers = ("/JavaScript", "/JS", "/Launch", "/OpenAction", "/AA", "/SubmitForm", "/GoToR", "/RichMedia")
            for xref in range(1, xref_length):
                try:
                    object_text = document.xref_object(xref, compressed=False)
                except BaseException:
                    continue
                if isinstance(object_text, str) and any(marker in object_text for marker in active_markers):
                    connection.send({"error_code": ERROR_ACTIVE_CONTENT, "parser_version": 1})
                    return
            from ..rag.ingest import ingest_pdf

            chunks, meta = ingest_pdf(
                path,
                max_pages=max_pages,
                max_chunks=max_chunks,
            )
            pages = min(max_pages, max(0, int(meta.get("pages", 0))))
            chars = max(0, min(max_chars, int(meta.get("chars", 0))))
            count = max(0, min(max_chunks, len(chunks)))
            # The legacy ingest heuristic treats short one-page prose as
            # "suspect".  At the attachment boundary a non-empty extracted
            # text result is sufficient evidence of a text PDF; a scanned-only
            # document is represented by zero extracted characters/chunks.
            scanned = bool(meta.get("scanned_suspect", False)) and chars == 0
            if not chunks or chars == 0:
                code = ERROR_SCANNED_PDF if pages else ERROR_EMPTY_PDF
            else:
                code = None
            connection.send(
                {
                    "detected_format": "pdf",
                    "parser_version": 1,
                    "page_count": pages,
                    "chunk_count": count,
                    "extracted_chars": chars,
                    "scanned_suspect": scanned,
                    "error_code": code,
                }
            )
        finally:
            document.close()
    except BaseException:
        # Raw parser exceptions never cross the process boundary.
        try:
            connection.send({"error_code": ERROR_PARSER_CRASHED, "parser_version": 1})
        except BaseException:
            pass
    finally:
        try:
            connection.close()
        except BaseException:
            pass


class SpawnPdfParser:
    """One-file, spawn-isolated PyMuPDF parser with a fixed wall-clock bound."""

    def __init__(self, *, timeout_seconds: float = 20.0) -> None:
        self.timeout_seconds = float(timeout_seconds)

    def parse(
        self,
        path: str | Path,
        *,
        attachment_id: str,
        source_role: str,
        max_pages: int,
        max_chunks: int,
        max_chars: int,
    ) -> ParseOutcome:
        del attachment_id, source_role  # protocol metadata; worker receives no raw content
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe(duplex=False)
        process = context.Process(
            target=_spawn_parser_worker,
            args=(str(path), int(max_pages), int(max_chunks), int(max_chars), child),
        )
        try:
            process.start()
            child.close()
            process.join(self.timeout_seconds)
            if process.is_alive():
                process.terminate()
                process.join(2.0)
                if process.is_alive() and hasattr(process, "kill"):
                    process.kill()
                    process.join(1.0)
                return ParseOutcome(error_code=ERROR_PARSER_TIMEOUT)
            payload = parent.recv() if parent.poll(0.25) else None
            if not isinstance(payload, Mapping):
                return ParseOutcome(error_code=ERROR_PARSER_CRASHED)
            return _coerce_parse_outcome(payload)
        except (OSError, EOFError, BrokenPipeError):
            if process.is_alive():
                process.terminate()
                process.join(1.0)
            return ParseOutcome(error_code=ERROR_PARSER_CRASHED)
        finally:
            try:
                parent.close()
            except OSError:
                pass


def _coerce_parse_outcome(value: object) -> ParseOutcome:
    if isinstance(value, ParseOutcome):
        return value
    metadata: Mapping[str, object]
    chunks: object = None
    if isinstance(value, tuple) and len(value) == 2 and isinstance(value[1], Mapping):
        chunks, metadata = value
    elif isinstance(value, Mapping):
        metadata = value
    else:
        metadata = {}
    if chunks is not None and "chunk_count" not in metadata:
        try:
            metadata = {**metadata, "chunk_count": len(chunks)}  # type: ignore[arg-type]
        except TypeError:
            pass
    indicated_error = metadata.get("error_code")
    if not indicated_error:
        if metadata.get("encrypted") or metadata.get("needs_pass"):
            indicated_error = ERROR_ENCRYPTED_PDF
        elif metadata.get("active_content") or metadata.get("active"):
            indicated_error = ERROR_ACTIVE_CONTENT
        elif metadata.get("embedded_content") or metadata.get("embedded"):
            indicated_error = ERROR_EMBEDDED_CONTENT
    return ParseOutcome(
        detected_format=str(metadata.get("detected_format", "pdf")),
        parser_version=_as_nonnegative_int(metadata.get("parser_version", metadata.get("parser_ver", 1))),
        page_count=_as_nonnegative_int(metadata.get("page_count", metadata.get("pages", 0))),
        chunk_count=_as_nonnegative_int(metadata.get("chunk_count", metadata.get("chunks", 0))),
        extracted_chars=_as_nonnegative_int(metadata.get("extracted_chars", metadata.get("chars", 0))),
        scanned_suspect=bool(metadata.get("scanned_suspect", False)),
        error_code=_stable_error(indicated_error, ERROR_PARSER_CRASHED) if indicated_error else None,
    )


class AttachmentService:
    """Durable intake/reconciliation over an existing ``SQLiteStore``."""

    def __init__(
        self,
        store: AttachmentStore,
        attachment_root: str | Path,
        *,
        parser: AttachmentParser | None = None,
        limits: AttachmentLimits | None = None,
        clock: Any | None = None,
    ) -> None:
        self.store = store
        self.root = Path(attachment_root)
        self.limits = limits or AttachmentLimits()
        self.parser = parser or SpawnPdfParser(timeout_seconds=self.limits.parser_timeout_seconds)
        self._clock = clock or time.time

    def _now(self, value: int | None = None) -> int:
        return int(self._clock()) if value is None else int(value)

    @staticmethod
    def _media_type(value: str | None) -> str:
        if value is None:
            return ""
        if not isinstance(value, str) or len(value) > 128:
            raise AttachmentValidationError("declared media type is invalid")
        return value.split(";", 1)[0].strip().lower()

    def _event(
        self,
        event_type: str,
        *,
        idea_id: str,
        payload: Mapping[str, object],
        attachment_id: str,
        state_version: int,
    ) -> Event:
        fingerprint = hashlib.sha256(
            f"attachment|{event_type}|{attachment_id}|{state_version}|{payload.get('sha256') or ''}".encode("utf-8")
        ).hexdigest()
        return Event(
            idea_id=idea_id,
            event_type=event_type,
            actor=ACTOR_SYSTEM,
            source=ACTOR_SYSTEM,
            payload=dict(payload),
            fingerprint=fingerprint,
        )

    def _payload(
        self,
        record: AttachmentRecord,
        *,
        status: str,
        error_code: str | None = None,
        storage_key: str | None = None,
        parser_version: int | None = None,
        page_count: int = 0,
        chunk_count: int = 0,
        extracted_chars: int = 0,
        scanned_suspect: bool = False,
        byte_size: int | None = None,
        sha256: str | None = None,
        event_sha256: str | None = None,
        detected_format: str | None = None,
    ) -> dict[str, object]:
        event_digest = event_sha256 if event_sha256 is not None else sha256
        payload: dict[str, object] = {
            "attachment_id": record.attachment_id,
            "workspace_id": record.workspace_id,
            "idea_id": record.idea_id,
            "detected_format": detected_format or record.detected_format,
            "byte_size": record.byte_size if byte_size is None else byte_size,
            "sha256": record.sha256 if event_digest is None else event_digest,
            "source_role": record.source_role,
            "status": status,
            "parser_version": record.parser_version if parser_version is None else parser_version,
            "page_count": page_count,
            "chunk_count": chunk_count,
            "extracted_chars": extracted_chars,
            "scanned_suspect": scanned_suspect,
        }
        if error_code:
            payload["error_code"] = _stable_error(error_code)
        if storage_key is not None:
            payload["storage_key"] = storage_key
        return payload

    def _transition(
        self,
        record: AttachmentRecord,
        *,
        status: str,
        updates: Mapping[str, object],
        error_code: str | None = None,
        storage_key: str | None = None,
        parser_version: int | None = None,
        page_count: int = 0,
        chunk_count: int = 0,
        extracted_chars: int = 0,
        scanned_suspect: bool = False,
        byte_size: int | None = None,
        sha256: str | None = None,
        event_sha256: str | None = None,
        detected_format: str | None = None,
        now: int | None = None,
    ) -> AttachmentRecord:
        event_type = {
            ATTACHMENT_READY: EVENT_ARTIFACT,
            ATTACHMENT_QUARANTINED: EVENT_ATTACHMENT_QUARANTINED,
            ATTACHMENT_REJECTED: EVENT_ATTACHMENT_REJECTED,
            ATTACHMENT_FAILED: EVENT_ATTACHMENT_FAILED,
        }[status]
        next_values = dict(updates)
        next_values.setdefault("byte_size", record.byte_size if byte_size is None else byte_size)
        next_values.setdefault("sha256", record.sha256 if sha256 is None else sha256)
        next_values.setdefault("detected_format", record.detected_format if detected_format is None else detected_format)
        next_values.setdefault("parser_version", parser_version if parser_version is not None else record.parser_version)
        next_values.setdefault("page_count", page_count)
        next_values.setdefault("chunk_count", chunk_count)
        next_values.setdefault("extracted_chars", extracted_chars)
        next_values.setdefault("scanned_suspect", scanned_suspect)
        next_values.setdefault("error_code", _stable_error(error_code) if error_code else None)
        next_values.setdefault("ready_at", self._now(now) if status == ATTACHMENT_READY else None)
        next_values.setdefault("expires_at", self._now(now) + self.limits.quarantine_ttl_seconds if status == ATTACHMENT_QUARANTINED else None)
        parser_value = next_values.get("parser_version")
        selected_parser_version = (
            parser_value if isinstance(parser_value, int) and not isinstance(parser_value, bool) else None
        )
        page_value = _as_nonnegative_int(next_values.get("page_count"))
        chunk_value = _as_nonnegative_int(next_values.get("chunk_count"))
        chars_value = _as_nonnegative_int(next_values.get("extracted_chars"))
        size_value = _as_nonnegative_int(next_values.get("byte_size"))
        digest_value = next_values.get("sha256")
        selected_digest = digest_value if isinstance(digest_value, str) else None
        payload = self._payload(
            record,
            status=status,
            error_code=error_code,
            storage_key=storage_key,
            parser_version=selected_parser_version,
            page_count=page_value,
            chunk_count=chunk_value,
            extracted_chars=chars_value,
            scanned_suspect=bool(next_values.get("scanned_suspect", False)),
            byte_size=size_value,
            sha256=selected_digest,
            event_sha256=event_sha256,
            detected_format=str(next_values.get("detected_format") or record.detected_format),
        )
        event = self._event(
            event_type,
            idea_id=record.idea_id,
            payload=payload,
            attachment_id=record.attachment_id,
            state_version=record.state_version,
        )
        return self.store.transition_attachment(
            record.attachment_id,
            workspace_id=record.workspace_id,
            expected_state_version=record.state_version,
            status=status,
            event=event,
            updates=next_values,
            now=self._now(now),
        )

    @staticmethod
    def _iter_blocks(body: bytes | bytearray | memoryview | BinaryIO | Iterable[bytes]) -> Iterable[bytes]:
        if isinstance(body, (bytes, bytearray, memoryview)):
            yield bytes(body)
            return
        reader = getattr(body, "read", None)
        if callable(reader):
            while True:
                block = reader(64 * 1024)
                if not block:
                    break
                yield bytes(block)
            return
        try:
            iterator = iter(body)
        except TypeError as exc:
            raise AttachmentValidationError("body must be bytes or a byte stream") from exc
        for block in iterator:
            if not isinstance(block, (bytes, bytearray, memoryview)):
                raise AttachmentValidationError("body blocks must be bytes")
            if block:
                yield bytes(block)

    def _write_staging(self, path: Path, body: object) -> tuple[int, str, bytes]:
        digest = hashlib.sha256()
        size = 0
        prefix = bytearray()
        try:
            with path.open("xb") as stream:
                for block in self._iter_blocks(body):  # type: ignore[arg-type]
                    if size + len(block) > self.limits.max_file_bytes:
                        raise AttachmentLimitError(ERROR_FILE_TOO_LARGE)
                    stream.write(block)
                    digest.update(block)
                    size += len(block)
                    if len(prefix) < len(PDF_MAGIC):
                        prefix.extend(block[: len(PDF_MAGIC) - len(prefix)])
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            try:
                path.unlink()
            except OSError:
                pass
            raise
        return size, digest.hexdigest(), bytes(prefix)

    @staticmethod
    def _unlink_exact(path: Path) -> None:
        try:
            if path.exists() and not _is_reparse_or_link(path):
                path.unlink()
        except FileNotFoundError:
            pass

    def _promote(self, paths: AttachmentPaths, staging: Path, digest: str, size: int) -> str:
        object_path = paths.object_path(digest)
        _ensure_real_directory(object_path.parent)
        if object_path.exists():
            if _is_reparse_or_link(object_path) or not object_path.is_file():
                raise AttachmentParserError(ERROR_HASH_MISMATCH)
            if object_path.stat().st_size != size or _hash_file(object_path) != digest:
                raise AttachmentParserError(ERROR_HASH_MISMATCH)
            self._unlink_exact(staging)
        else:
            os.replace(staging, object_path)
            try:
                descriptor = os.open(object_path.parent, os.O_RDONLY)
            except OSError:
                descriptor = None
            if descriptor is not None:
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        return paths.object_key(digest)

    def _reject(
        self,
        record: AttachmentRecord,
        paths: AttachmentPaths,
        code: str,
        *,
        size: int,
        digest: str | None,
        detected: str = "unknown",
        event_digest: str | None = None,
    ) -> AttachmentRecord:
        self._unlink_exact(paths.staging_path(record.attachment_id))
        return self._transition(
            record,
            status=ATTACHMENT_REJECTED,
            error_code=code,
            updates={"byte_size": min(size, self.limits.max_file_bytes), "sha256": digest, "detected_format": detected},
            byte_size=min(size, self.limits.max_file_bytes),
            sha256=digest,
            event_sha256=event_digest,
            detected_format=detected,
        )

    def _quarantine(
        self,
        record: AttachmentRecord,
        paths: AttachmentPaths,
        code: str,
        *,
        size: int,
        digest: str,
        outcome: ParseOutcome | None = None,
    ) -> AttachmentRecord:
        staging = paths.staging_path(record.attachment_id)
        quarantine = paths.quarantine_path(record.attachment_id)
        if staging.exists():
            os.replace(staging, quarantine)
        result = outcome or ParseOutcome(error_code=code)
        return self._transition(
            record,
            status=ATTACHMENT_QUARANTINED,
            error_code=code,
            updates={
                "byte_size": size,
                "sha256": digest,
                "detected_format": "pdf",
                "parser_version": result.parser_version,
                "page_count": min(result.page_count, self.limits.max_pages),
                "chunk_count": min(result.chunk_count, self.limits.max_chunks),
                "extracted_chars": min(result.extracted_chars, self.limits.max_extracted_chars),
                "scanned_suspect": result.scanned_suspect,
            },
            byte_size=size,
            sha256=digest,
            detected_format="pdf",
            parser_version=result.parser_version,
            page_count=min(result.page_count, self.limits.max_pages),
            chunk_count=min(result.chunk_count, self.limits.max_chunks),
            extracted_chars=min(result.extracted_chars, self.limits.max_extracted_chars),
            scanned_suspect=result.scanned_suspect,
        )

    def _fail_best_effort(
        self,
        record: AttachmentRecord,
        paths: AttachmentPaths,
        code: str = ERROR_INTERNAL,
        *,
        size: int | None = None,
        digest: str | None = None,
        detected: str | None = None,
    ) -> AttachmentRecord:
        self._unlink_exact(paths.staging_path(record.attachment_id))
        try:
            updates: dict[str, object] = {"error_code": code}
            if size is not None:
                updates["byte_size"] = min(size, self.limits.max_file_bytes)
            if digest is not None:
                updates["sha256"] = digest
            if detected is not None:
                updates["detected_format"] = detected
            return self._transition(
                record,
                status=ATTACHMENT_FAILED,
                error_code=code,
                updates=updates,
                byte_size=size,
                sha256=digest,
                detected_format=detected,
            )
        except Exception:
            current = self.store.get_attachment(record.attachment_id, workspace_id=record.workspace_id)
            return current or record

    def intake(
        self,
        workspace_id: str,
        filename: str,
        body: bytes | bytearray | memoryview | BinaryIO | Iterable[bytes],
        *,
        idea_id: str | None = None,
        declared_media_type: str | None = None,
        source_role: str = SOURCE_ROLE_STYLE,
        declared_size: int | None = None,
        idempotency_key: str | None = None,
    ) -> AttachmentRecord:
        """Stream one upload through pending -> terminal state.

        Repeated ``idempotency_key`` or same-workspace content returns the
        existing record after the transient pending row is closed as a safe
        duplicate.  The method performs no provider, network, or task-queue
        calls.
        """

        workspace_id = validate_workspace_identity(workspace_id)
        idea_id = validate_workspace_identity(idea_id or workspace_id)
        display_name = validate_display_filename(filename, max_chars=self.limits.max_filename_chars)
        media_type = self._media_type(declared_media_type)
        if media_type not in ALLOWED_MEDIA_TYPES:
            raise AttachmentUnsupportedError(ERROR_MIME_NOT_ALLOWED)
        if source_role not in ATTACHMENT_SOURCE_ROLES:
            raise AttachmentValidationError("source_role is invalid")
        if declared_size is not None:
            if isinstance(declared_size, bool) or not isinstance(declared_size, int) or declared_size < 0:
                raise AttachmentValidationError("declared_size is invalid")
            if declared_size > self.limits.max_file_bytes:
                raise AttachmentLimitError(ERROR_FILE_TOO_LARGE)
        if idempotency_key is not None:
            if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 256:
                raise AttachmentValidationError("idempotency_key is invalid")
            existing = self.store.get_attachment_by_idempotency(idempotency_key, workspace_id=workspace_id)
            if existing is not None:
                return existing
        retained, ready_count = self.store.attachment_usage(workspace_id=workspace_id)
        if retained >= self.limits.max_retained_bytes or ready_count >= self.limits.max_ready_attachments:
            raise AttachmentLimitError(ERROR_WORKSPACE_QUOTA if retained >= self.limits.max_retained_bytes else ERROR_READY_QUOTA)

        attachment_id = uuid.uuid4().hex
        idem = idempotency_key or secrets.token_urlsafe(18)
        paths = AttachmentPaths(self.root, workspace_id)
        paths.prepare()
        now = self._now()
        requested_payload = {
            "attachment_id": attachment_id,
            "workspace_id": workspace_id,
            "idea_id": idea_id,
            "detected_format": "unknown",
            "byte_size": 0,
            "sha256": None,
            "source_role": source_role,
            "status": ATTACHMENT_PENDING,
            "parser_version": None,
            "page_count": 0,
            "chunk_count": 0,
            "extracted_chars": 0,
            "scanned_suspect": False,
        }
        requested = self._event(
            EVENT_ATTACHMENT_INTAKE_REQUESTED,
            idea_id=idea_id,
            payload=requested_payload,
            attachment_id=attachment_id,
            state_version=0,
        )
        pending = self.store.begin_attachment(
            {
                "attachment_id": attachment_id,
                "workspace_id": workspace_id,
                "idea_id": idea_id,
                "display_name": display_name,
                "declared_media_type": media_type,
                "detected_format": "unknown",
                "source_role": source_role,
                "status": ATTACHMENT_PENDING,
                "byte_size": 0,
                "sha256": None,
                "storage_key": None,
                "parser_version": None,
                "page_count": 0,
                "chunk_count": 0,
                "extracted_chars": 0,
                "scanned_suspect": False,
                "error_code": None,
                "created_at": now,
                "updated_at": now,
                "ready_at": None,
                "expires_at": None,
                "idempotency_key": idem,
            },
            event=requested,
            now=now,
        )
        if pending.attachment_id != attachment_id or pending.status != ATTACHMENT_PENDING:
            return pending
        staging = paths.staging_path(attachment_id)
        size = 0
        digest: str | None = None
        try:
            size, digest, prefix = self._write_staging(staging, body)
            if declared_size is not None and declared_size != size:
                return self._reject(pending, paths, ERROR_DECLARED_SIZE_MISMATCH, size=size, digest=digest)
            if size == 0:
                return self._reject(pending, paths, ERROR_EMPTY_FILE, size=size, digest=digest)
            if size > self.limits.max_file_bytes:
                return self._reject(pending, paths, ERROR_FILE_TOO_LARGE, size=size, digest=digest)
            if not display_name.lower().endswith(".pdf"):
                return self._reject(pending, paths, ERROR_EXTENSION_MISMATCH, size=size, digest=digest)
            if prefix != PDF_MAGIC:
                return self._reject(pending, paths, ERROR_MAGIC_MISMATCH, size=size, digest=digest)
            if retained + size > self.limits.max_retained_bytes:
                return self._reject(pending, paths, ERROR_WORKSPACE_QUOTA, size=size, digest=digest, detected="pdf")
            existing = self.store.get_attachment_by_hash(digest, workspace_id=workspace_id)
            if existing is not None:
                # Keep the unique workspace/hash index owned by the canonical
                # existing row.  The transient duplicate is rejected without
                # retaining the duplicate digest in SQLite.
                duplicate = self._reject(pending, paths, ERROR_DUPLICATE_CONTENT, size=size, digest=None, event_digest=digest, detected="pdf")
                del duplicate
                return existing
            try:
                outcome = _coerce_parse_outcome(
                    self.parser.parse(
                        staging,
                        attachment_id=attachment_id,
                        source_role=source_role,
                        max_pages=self.limits.max_pages,
                        max_chunks=self.limits.max_chunks,
                        max_chars=self.limits.max_extracted_chars,
                    )
                )
            except Exception:
                # A parser crash is an unsafe document outcome, not a
                # successful ingest and not a raw exception disclosure.
                return self._quarantine(
                    pending,
                    paths,
                    ERROR_PARSER_CRASHED,
                    size=size,
                    digest=digest,
                    outcome=ParseOutcome(error_code=ERROR_PARSER_CRASHED),
                )
            if outcome.error_code in _QUARANTINE_ERRORS:
                return self._quarantine(pending, paths, outcome.error_code, size=size, digest=digest, outcome=outcome)
            if outcome.error_code:
                return self._fail_best_effort(pending, paths, outcome.error_code, size=size, digest=digest, detected="pdf")
            if outcome.page_count > self.limits.max_pages or outcome.chunk_count > self.limits.max_chunks or outcome.extracted_chars > self.limits.max_extracted_chars:
                return self._quarantine(pending, paths, ERROR_PARSER_RESULT_LIMIT, size=size, digest=digest, outcome=outcome)
            if outcome.scanned_suspect or outcome.extracted_chars <= 0 or outcome.chunk_count <= 0:
                return self._quarantine(pending, paths, ERROR_SCANNED_PDF if outcome.page_count else ERROR_EMPTY_PDF, size=size, digest=digest, outcome=outcome)
            storage_key = self._promote(paths, staging, digest, size)
            return self._transition(
                pending,
                status=ATTACHMENT_READY,
                updates={
                    "byte_size": size,
                    "sha256": digest,
                    "storage_key": storage_key,
                    "detected_format": "pdf",
                    "parser_version": outcome.parser_version,
                    "page_count": outcome.page_count,
                    "chunk_count": outcome.chunk_count,
                    "extracted_chars": outcome.extracted_chars,
                    "scanned_suspect": outcome.scanned_suspect,
                    "error_code": None,
                },
                storage_key=storage_key,
                parser_version=outcome.parser_version,
                page_count=outcome.page_count,
                chunk_count=outcome.chunk_count,
                extracted_chars=outcome.extracted_chars,
                scanned_suspect=outcome.scanned_suspect,
                byte_size=size,
                sha256=digest,
                detected_format="pdf",
            )
        except AttachmentLimitError as exc:
            return self._reject(pending, paths, _stable_error(str(exc), ERROR_FILE_TOO_LARGE), size=size, digest=digest)
        except (AttachmentValidationError, AttachmentUnsupportedError):
            raise
        except Exception:
            return self._fail_best_effort(pending, paths, size=size, digest=digest, detected="pdf" if digest else None)

    ingest = intake
    upload = intake

    def get(self, workspace_id: str, attachment_id: str) -> AttachmentRecord | None:
        return self.store.get_attachment(attachment_id, workspace_id=validate_workspace_identity(workspace_id))

    def list(self, workspace_id: str, *, limit: int = 100) -> builtins.list[AttachmentRecord]:
        return self.store.list_attachments(workspace_id=validate_workspace_identity(workspace_id), limit=limit)

    def ready(self, workspace_id: str, *, limit: int = 128) -> builtins.list[AttachmentRecord]:
        return self.store.list_attachments(
            workspace_id=validate_workspace_identity(workspace_id),
            limit=min(limit, self.limits.max_ready_attachments),
            statuses={ATTACHMENT_READY},
        )

    def resolve_ready(self, workspace_id: str, attachment_ids: Iterable[str]) -> builtins.list[AttachmentRecord]:
        """Resolve a bounded, same-workspace collection for a chat turn."""

        try:
            raw_ids = list(attachment_ids)
        except TypeError as exc:
            raise AttachmentValidationError("attachment_ids must be iterable") from exc
        if len(raw_ids) > self.limits.max_references_per_turn:
            raise AttachmentLimitError("attachment_reference_limit")
        unique: list[str] = []
        seen: set[str] = set()
        for attachment_id in raw_ids:
            if not isinstance(attachment_id, str) or not attachment_id or len(attachment_id) > 256:
                raise AttachmentValidationError("attachment_id is invalid")
            if attachment_id in seen:
                continue
            seen.add(attachment_id)
            unique.append(attachment_id)
        selected: list[AttachmentRecord] = []
        for attachment_id in unique:
            record = self.get(workspace_id, attachment_id)
            if record is None or record.status != ATTACHMENT_READY:
                # Unknown, foreign, and non-ready IDs intentionally share one
                # stable conflict class; this prevents enumeration.
                raise AttachmentConflictError("attachment reference is unavailable")
            selected.append(record)
        return selected

    resolve_attachment_ids = resolve_ready

    def path_for_attachment(self, record: AttachmentRecord, *, require_ready: bool = True) -> Path:
        """Derive a private canonical path from the record, never its filename."""

        if require_ready and record.status != ATTACHMENT_READY:
            raise AttachmentConflictError("attachment is not ready")
        if not record.sha256:
            raise AttachmentConflictError("attachment has no canonical digest")
        current = self.get(record.workspace_id, record.attachment_id)
        if current is None or current.sha256 != record.sha256 or current.status != record.status:
            raise AttachmentConflictError("attachment metadata changed")
        paths = AttachmentPaths(self.root, record.workspace_id)
        paths.prepare()
        return paths.object_path(record.sha256)

    attachment_path = path_for_attachment

    def reconcile(self, workspace_id: str, *, now: int | None = None, limit: int | None = None) -> builtins.list[AttachmentRecord]:
        """Reconcile one bounded workspace page without provider/task calls."""

        workspace_id = validate_workspace_identity(workspace_id)
        paths = AttachmentPaths(self.root, workspace_id)
        paths.prepare()
        timestamp = self._now(now)
        if limit is None:
            page = self.limits.reconcile_batch
        elif isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= self.limits.reconcile_batch:
            raise AttachmentValidationError("reconcile limit is outside bounds")
        else:
            page = limit
        candidates = self.store.list_attachment_candidates(workspace_id=workspace_id, limit=page)
        changed: list[AttachmentRecord] = []
        for record in candidates:
            try:
                if record.status == ATTACHMENT_PENDING:
                    staging = paths.staging_path(record.attachment_id)
                    object_path = paths.object_path(record.sha256) if record.sha256 else None
                    if object_path is not None and object_path.exists() and record.sha256 and object_path.is_file() and _hash_file(object_path) == record.sha256:
                        changed.append(
                            self._transition(
                                record,
                                status=ATTACHMENT_READY,
                                updates={"storage_key": paths.object_key(record.sha256), "detected_format": "pdf"},
                                storage_key=paths.object_key(record.sha256),
                                page_count=record.page_count,
                                chunk_count=record.chunk_count,
                                extracted_chars=record.extracted_chars,
                                parser_version=record.parser_version,
                                byte_size=record.byte_size,
                                sha256=record.sha256,
                                detected_format="pdf",
                            )
                        )
                    elif not staging.exists() or timestamp - max(record.updated_at, record.created_at) >= self.limits.staging_grace_seconds:
                        self._unlink_exact(staging)
                        changed.append(self._transition(record, status=ATTACHMENT_FAILED, error_code=ERROR_MISSING_STAGING if not staging.exists() else ERROR_STALE_STAGING, updates={"error_code": ERROR_MISSING_STAGING if not staging.exists() else ERROR_STALE_STAGING}))
                elif record.status == ATTACHMENT_READY:
                    if not record.sha256 or not record.storage_key:
                        changed.append(self._transition(record, status=ATTACHMENT_FAILED, error_code=ERROR_ARTIFACT_MISSING, updates={"error_code": ERROR_ARTIFACT_MISSING}))
                    else:
                        object_path = paths.object_path(record.sha256)
                        if not object_path.exists():
                            changed.append(self._transition(record, status=ATTACHMENT_FAILED, error_code=ERROR_ARTIFACT_MISSING, updates={"error_code": ERROR_ARTIFACT_MISSING}))
                        elif not object_path.is_file() or _hash_file(object_path) != record.sha256:
                            changed.append(self._transition(record, status=ATTACHMENT_FAILED, error_code=ERROR_HASH_MISMATCH, updates={"error_code": ERROR_HASH_MISMATCH}))
                elif record.status == ATTACHMENT_QUARANTINED:
                    quarantine = paths.quarantine_path(record.attachment_id)
                    if (record.expires_at is not None and timestamp >= record.expires_at) or not quarantine.exists():
                        self._unlink_exact(quarantine)
                        changed.append(self._transition(record, status=ATTACHMENT_REJECTED, error_code=ERROR_QUARANTINE_EXPIRED, updates={"error_code": ERROR_QUARANTINE_EXPIRED, "expires_at": None}))
            except Exception:
                # A concurrent writer or a malformed external object is left
                # for the next bounded pass; no broad cleanup is attempted.
                continue
        self._cleanup_orphans(paths, timestamp, page)
        return changed

    reconcile_workspace = reconcile

    def _cleanup_orphans(self, paths: AttachmentPaths, now: int, limit: int) -> int:
        deleted = 0
        inspected = 0
        if not paths.objects_root.exists():
            return 0
        for prefix in sorted(paths.objects_root.iterdir()):
            if inspected >= limit:
                break
            if not prefix.is_dir() or _is_reparse_or_link(prefix) or not re.fullmatch(r"[0-9a-f]{2}", prefix.name):
                continue
            for item in sorted(prefix.iterdir()):
                if inspected >= limit:
                    break
                inspected += 1
                if not item.is_file() or _is_reparse_or_link(item) or not re.fullmatch(r"[0-9a-f]{64}\.pdf", item.name):
                    continue
                try:
                    if now - int(item.stat().st_mtime) < self.limits.staging_grace_seconds:
                        continue
                    digest = item.stem
                    record = self.store.get_attachment_by_hash(digest, workspace_id=paths.workspace_id)
                    if record is None:
                        self._unlink_exact(item)
                        deleted += 1
                except OSError:
                    continue
        return deleted


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(64 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


__all__ = [
    "ALLOWED_MEDIA_TYPES", "ATTACHMENT_SOURCE_ROLES", "AttachmentConflictError", "AttachmentError",
    "AttachmentLimits", "AttachmentParser", "AttachmentParserError", "AttachmentPaths", "AttachmentQuotaError",
    "AttachmentRecord", "AttachmentService", "AttachmentStatus", "AttachmentUnsupportedError",
    "AttachmentValidationError", "ERROR_ACTIVE_CONTENT",
    "ERROR_ARTIFACT_MISSING", "ERROR_DUPLICATE_CONTENT", "ERROR_EMPTY_FILE", "ERROR_EMPTY_PDF",
    "ERROR_ENCRYPTED_PDF", "ERROR_EXTENSION_MISMATCH", "ERROR_FILE_TOO_LARGE", "ERROR_HASH_MISMATCH",
    "ERROR_MAGIC_MISMATCH", "ERROR_MIME_NOT_ALLOWED", "ERROR_MISSING_STAGING", "ERROR_PARSER_CRASHED",
    "ERROR_PARSER_RESULT_LIMIT", "ERROR_PARSER_TIMEOUT", "ERROR_QUARANTINE_EXPIRED", "ERROR_SCANNED_PDF",
    "ERROR_STALE_STAGING", "ERROR_WORKSPACE_QUOTA", "ParseOutcome", "PDF_MAGIC", "SOURCE_ROLE_CITABLE",
    "SOURCE_ROLE_STYLE", "SpawnPdfParser", "validate_display_filename", "validate_workspace_identity",
]
