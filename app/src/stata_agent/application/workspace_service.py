"""Framework-neutral workspace application service.

The FastAPI adapter historically owned workspace identifiers, path
canonicalisation, and the small workspace registry.  This module keeps those
rules in one transport-independent use-case boundary.  Persistence is
intentionally expressed as a narrow protocol so the application layer can be
tested with an in-memory fake and can use the SQLite memory repository
without importing that concrete adapter.
"""

from __future__ import annotations

import hashlib
import re
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


DEFAULT_WORKSPACE_ID = "ui"
DEFAULT_WORKSPACE_NAME = "未命名研究"
MAX_WORKSPACE_ID_LENGTH = 64
MAX_WORKSPACE_NAME_LENGTH = 120

_WORKSPACE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

RecordMapping = Mapping[str, Any]


@runtime_checkable
class WorkspaceRepository(Protocol):
    """Persistence surface required by :class:`WorkspaceService`.

    ``SQLiteMemoryRepository`` satisfies this protocol structurally.  Keeping
    the protocol here prevents the application layer from depending on a
    storage implementation or on the FastAPI module.
    """

    def register_workspace(
        self,
        workspace_id: str,
        *,
        root: str | Path | None = None,
        name: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        now: int | None = None,
    ) -> RecordMapping:
        ...

    def get_workspace(self, workspace_id: str) -> RecordMapping | None:
        ...

    def list_workspaces(self) -> Sequence[RecordMapping]:
        ...


class WorkspaceValidationError(ValueError):
    """Raised when an identifier, name, or request field is invalid."""


class WorkspaceConflictError(ValueError):
    """Raised when a requested workspace identity conflicts with a registry row."""


class WorkspaceNotFoundError(LookupError):
    """Raised when a valid workspace identifier is not registered."""


@dataclass(frozen=True, slots=True)
class WorkspaceRecord:
    """Stable application-level representation of one workspace."""

    id: str
    workspace_id: str
    root: Path
    name: str
    created_at: int
    updated_at: int
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly copy for a transport adapter."""

        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "root": str(self.root),
            "name": self.name,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class CreateWorkspaceRequest:
    """Input for creating or idempotently retrieving a workspace."""

    name: str = "新研究工作区"
    id: str | None = None
    root: str | Path | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    now: int | None = None


def _normalise_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


class WorkspaceService:
    """Own workspace identity and registry use cases without a web framework."""

    def __init__(
        self,
        repository: WorkspaceRepository,
        *,
        root_base: str | Path | None = None,
        default_id: str = DEFAULT_WORKSPACE_ID,
        default_name: str = DEFAULT_WORKSPACE_NAME,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self._repository = repository
        self._default_id = self.validate_id(default_id, default=default_id)
        self._default_name = self.validate_name(default_name)
        self._root_base = self._canonical_path(root_base or Path.cwd())
        # Registry timestamps are part of the existing UI contract and use
        # Unix milliseconds (event timestamps use the same display scale).
        self._clock = clock or (lambda: int(time.time() * 1000))

    @property
    def default_id(self) -> str:
        """The legacy-compatible user-facing default workspace identifier."""

        return self._default_id

    @property
    def default_name(self) -> str:
        """Display name used when the compatibility workspace is bootstrapped."""

        return self._default_name

    @classmethod
    def validate_id(cls, value: str | None = None, *, default: str = DEFAULT_WORKSPACE_ID) -> str:
        """Validate a user-facing workspace identifier.

        Empty values follow the old UI behaviour and resolve to the default
        workspace.  Creation treats an explicitly blank request id as
        omitted, allowing the name-based slug path to run instead.
        """

        raw = str(value or default).strip()
        if not _WORKSPACE_ID_RE.fullmatch(raw):
            raise WorkspaceValidationError(
                "workspace id must start with a letter or number and contain only letters, "
                "numbers, underscores, or hyphens (maximum 64 characters)"
            )
        return raw

    @classmethod
    def validate_name(cls, value: Any) -> str:
        """Normalize and validate a display name using the UI's 120-char limit."""

        name = _normalise_text(value)
        if not name:
            raise WorkspaceValidationError("workspace name must not be blank")
        if len(name) > MAX_WORKSPACE_NAME_LENGTH:
            raise WorkspaceValidationError(
                f"workspace name must not exceed {MAX_WORKSPACE_NAME_LENGTH} characters"
            )
        return name

    @staticmethod
    def _canonical_path(value: str | Path) -> Path:
        """Canonicalize an existing or not-yet-created local path.

        ``strict=False`` is important for a workspace selected before its
        directory is created (and for tests using synthetic Windows paths).
        ``Path.resolve`` can still fail for malformed symlink loops, in which
        case ``absolute`` preserves a safe, normalized fallback.
        """

        path = Path(value).expanduser()
        try:
            return path.resolve(strict=False)
        except (OSError, RuntimeError):
            return Path.absolute(path)

    def canonical_root(
        self,
        root: str | Path | None = None,
        *,
        workspace_id: str | None = None,
    ) -> Path:
        """Return the canonical root used by workspace identity.

        If no explicit root is supplied, the compatibility layout is
        ``root_base / user-facing-id``.  The layout mirrors the old UI while
        keeping the base directory injectable for CLI, desktop, and tests.
        """

        if root is None or (isinstance(root, str) and not root.strip()):
            identifier = self.validate_id(workspace_id, default=self._default_id)
            return self._canonical_path(self._root_base / identifier)
        return self._canonical_path(root)

    def workspace_id(self, root: str | Path | None = None, *, workspace_id: str | None = None) -> str:
        """Derive the deterministic opaque ID for a canonical workspace root."""

        canonical = self.canonical_root(root, workspace_id=workspace_id)
        digest = hashlib.sha256(str(canonical).encode("utf-8")).hexdigest()
        return f"sha256:{digest}"

    # Descriptive alias for callers that want to emphasize that the argument
    # is a path rather than the user-facing registry slug.
    workspace_id_for_root = workspace_id

    @staticmethod
    def _workspace_slug(name: str) -> str:
        base = re.sub(r"[^A-Za-z0-9_-]+", "-", name.lower()).strip("-_")[:48]
        return base or f"workspace-{uuid.uuid4().hex[:8]}"

    def _now(self) -> int:
        return _safe_int(self._clock(), int(time.time() * 1000))

    @staticmethod
    def _metadata(row: RecordMapping) -> dict[str, Any]:
        value = row.get("metadata")
        return dict(value) if isinstance(value, Mapping) else {}

    def _record_id(self, row: RecordMapping, *, fallback: str | None = None) -> str:
        metadata = self._metadata(row)
        candidate = row.get("id") or metadata.get("id") or metadata.get("slug") or fallback
        if candidate is None:
            candidate = row.get("workspace_id") or self._default_id
        try:
            return self.validate_id(str(candidate), default=self._default_id)
        except WorkspaceValidationError:
            return str(candidate).strip() or self._default_id

    def _record_from_row(
        self,
        row: RecordMapping,
        *,
        fallback_id: str | None = None,
        fallback_root: Path | None = None,
        fallback_name: str | None = None,
        fallback_metadata: Mapping[str, Any] | None = None,
    ) -> WorkspaceRecord:
        if not isinstance(row, Mapping):
            raise WorkspaceValidationError("workspace repository returned a non-mapping row")
        workspace_id = str(row.get("workspace_id") or "").strip()
        if not workspace_id:
            raise WorkspaceValidationError("workspace repository row has no workspace_id")
        identifier = self._record_id(row, fallback=fallback_id)
        root_value = row.get("root")
        root = (
            fallback_root
            if root_value is None and fallback_root is not None
            else self.canonical_root(root_value, workspace_id=identifier)
        )
        raw_name = row.get("name")
        if raw_name is None:
            name = _normalise_text(fallback_name or identifier)
        else:
            name = _normalise_text(raw_name)
        metadata = dict(fallback_metadata or {})
        metadata.update(self._metadata(row))
        metadata.setdefault("id", identifier)
        created_at = _safe_int(row.get("created_at"), 0)
        updated_at = _safe_int(row.get("updated_at"), created_at)
        return WorkspaceRecord(
            id=identifier,
            workspace_id=workspace_id,
            root=root,
            name=name,
            created_at=created_at,
            updated_at=updated_at,
            metadata=metadata,
        )

    def _rows(self) -> list[WorkspaceRecord]:
        return [self._record_from_row(row) for row in self._repository.list_workspaces()]

    def _find_by_id(self, identifier: str, rows: list[WorkspaceRecord] | None = None) -> WorkspaceRecord | None:
        for record in rows if rows is not None else self._rows():
            if record.id == identifier:
                return record
        return None

    @staticmethod
    def _find_by_root(root: Path, rows: list[WorkspaceRecord]) -> WorkspaceRecord | None:
        for record in rows:
            if record.root == root:
                return record
        return None

    def _ensure_default(self, rows: list[WorkspaceRecord] | None = None) -> WorkspaceRecord:
        current = rows if rows is not None else self._rows()
        existing = self._find_by_id(self._default_id, current)
        if existing is not None:
            return existing

        root = self.canonical_root(workspace_id=self._default_id)
        existing = self._find_by_root(root, current)
        if existing is not None:
            return existing

        workspace_id = self.workspace_id(root)
        row = self._repository.register_workspace(
            workspace_id,
            root=str(root),
            name=self._default_name,
            metadata={"id": self._default_id},
            now=self._now(),
        )
        return self._record_from_row(
            row,
            fallback_id=self._default_id,
            fallback_root=root,
            fallback_name=self._default_name,
            fallback_metadata={"id": self._default_id},
        )

    def list(self) -> list[WorkspaceRecord]:
        """List registered workspaces, bootstrapping the legacy default row."""

        rows = self._rows()
        self._ensure_default(rows)
        listed = self._rows()
        listed.sort(key=lambda record: (record.id != self._default_id, -record.updated_at, record.id))
        return listed

    list_workspaces = list

    def get(self, value: str | None = None) -> WorkspaceRecord | None:
        """Get a workspace by user-facing id, returning ``None`` if absent."""

        identifier = self.validate_id(value, default=self._default_id)
        rows = self._rows()
        if identifier == self._default_id:
            self._ensure_default(rows)
            rows = self._rows()
        return self._find_by_id(identifier, rows)

    get_workspace = get

    def resolve(self, value: str | None = None) -> WorkspaceRecord:
        """Resolve a workspace or raise a framework-neutral not-found error."""

        identifier = self.validate_id(value, default=self._default_id)
        record = self.get(identifier)
        if record is None:
            raise WorkspaceNotFoundError(f"workspace {identifier!r} is not registered")
        return record

    resolve_workspace = resolve

    def create(self, request: CreateWorkspaceRequest | None = None, **kwargs: Any) -> WorkspaceRecord:
        """Create a workspace, with canonical-root idempotency.

        Passing an already registered root returns its existing record.  An
        explicit id that is already used for a different root is a conflict;
        name-derived ids receive the same ``-2``/``-3`` suffix treatment as
        the legacy UI.
        """

        if request is None:
            request = CreateWorkspaceRequest(**kwargs)
        elif kwargs:
            raise TypeError("create accepts either a request object or keyword fields, not both")
        name = self.validate_name(request.name)
        requested_id = str(request.id).strip() if request.id is not None else ""
        explicit_id = bool(requested_id)
        identifier = self.validate_id(requested_id, default=self._default_id) if explicit_id else self._workspace_slug(name)

        root = self.canonical_root(request.root, workspace_id=identifier)
        workspace_id = self.workspace_id(root)
        rows = self._rows()

        by_root = self._find_by_root(root, rows)
        if by_root is not None:
            return by_root

        by_id = self._find_by_id(identifier, rows)
        if by_id is not None:
            if explicit_id:
                raise WorkspaceConflictError(f"workspace id {identifier!r} is already registered")
            suffix = 2
            stem = identifier
            while self._find_by_id(identifier, rows) is not None:
                suffix_text = f"-{suffix}"
                identifier = f"{stem[: MAX_WORKSPACE_ID_LENGTH - len(suffix_text)]}{suffix_text}"
                if request.root is None:
                    root = self.canonical_root(workspace_id=identifier)
                    workspace_id = self.workspace_id(root)
                suffix += 1
                if suffix > 10_000:
                    raise WorkspaceConflictError("could not allocate a unique workspace id")

        metadata = dict(request.metadata)
        metadata["id"] = identifier
        row = self._repository.register_workspace(
            workspace_id,
            root=str(root),
            name=name,
            metadata=metadata,
            now=request.now if request.now is not None else self._now(),
        )
        return self._record_from_row(
            row,
            fallback_id=identifier,
            fallback_root=root,
            fallback_name=name,
            fallback_metadata=metadata,
        )

    create_workspace = create

    def touch_name(self, value: str | None, text: Any) -> WorkspaceRecord:
        """Set the initial display name after the first useful user message.

        Existing non-placeholder names are preserved, matching the old UI's
        ``_touch_workspace_name`` behaviour.  Blank text is a no-op.
        """

        record = self.resolve(value)
        clean = _normalise_text(text)[:MAX_WORKSPACE_NAME_LENGTH]
        if not clean or (record.name and record.name != self._default_name):
            return record
        metadata = dict(record.metadata)
        metadata["id"] = record.id
        row = self._repository.register_workspace(
            record.workspace_id,
            root=str(record.root),
            name=clean,
            metadata=metadata,
            now=self._now(),
        )
        return self._record_from_row(
            row,
            fallback_id=record.id,
            fallback_root=record.root,
            fallback_name=clean,
            fallback_metadata=metadata,
        )


__all__ = [
    "CreateWorkspaceRequest",
    "DEFAULT_WORKSPACE_ID",
    "DEFAULT_WORKSPACE_NAME",
    "MAX_WORKSPACE_ID_LENGTH",
    "MAX_WORKSPACE_NAME_LENGTH",
    "WorkspaceConflictError",
    "WorkspaceNotFoundError",
    "WorkspaceRecord",
    "WorkspaceRepository",
    "WorkspaceService",
    "WorkspaceValidationError",
]
