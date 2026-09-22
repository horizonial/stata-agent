"""Filesystem-backed, human-readable Project Memory persistence adapter.

SQLite remains the revision/source ledger.  Immutable revision files and editable current
files make Memory transparent to users and available to progressive Agent tools.  The index
is a rebuildable projection; an external edit becomes a new imported Memory revision instead
of overwriting history.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from stata_research_agent.application.memory import (
    MemoryLifecycle,
    MemoryOriginKind,
    MemorySource,
    MemorySourceRole,
    ReviseMemoryCommand,
)
from stata_research_agent.application.memory_service import MemoryService
from stata_research_agent.application.ports.identity import IdentityGenerator
from stata_research_agent.domain.identifiers import CommandId, MemoryItemId
from stata_research_agent.persistence.memory_store import SqliteMemoryRepository


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _payload_fingerprint(title: str, content: str) -> str:
    return hashlib.sha256(f"{title}\0{content}".encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class ExternalMemoryEdit:
    memory_item_id: str
    base_memory_revision_id: str
    expected_pointer_revision: int
    relative_path: str
    title: str
    content: str
    file_sha256: str


@dataclass(frozen=True, slots=True)
class MemoryFilesystemSyncReport:
    revision_files_written: int
    current_files_written: int
    external_edits: tuple[ExternalMemoryEdit, ...]
    invalid_files: tuple[str, ...]


class FilesystemMemoryStore:
    """Materialize and reconcile one Workspace's transparent Memory directory."""

    def __init__(self, workspace_root: Path) -> None:
        self._workspace_root = workspace_root.resolve()
        self.root = (self._workspace_root / ".stata-agent" / "memory").resolve()
        if not self.root.is_relative_to(self._workspace_root):
            raise ValueError("Memory root escapes the Workspace")

    def synchronize(self, connection: sqlite3.Connection) -> MemoryFilesystemSyncReport:
        self.root.mkdir(parents=True, exist_ok=True)
        revision_writes = 0
        current_writes = 0
        external: list[ExternalMemoryEdit] = []
        invalid: list[str] = []
        rows = connection.execute(
            """
            SELECT item.memory_item_id, item.scope_kind, item.scope_object_id,
                   item.memory_kind, revision.memory_revision_id,
                   revision.revision_number, revision.title, revision.content,
                   revision.content_sha256, revision.origin_kind,
                   revision.created_revision, state.current_revision_id,
                   state.pointer_revision, state.lifecycle,
                   retention.access_tier, retention.pinned,
                   retention.retention_revision,
                   retention.superseded_by_memory_item_id
            FROM memory_items AS item
            JOIN memory_revisions AS revision USING (memory_item_id)
            JOIN memory_current_states AS state USING (memory_item_id)
            JOIN memory_retention_states AS retention USING (memory_item_id)
            ORDER BY item.memory_item_id, revision.revision_number
            """
        ).fetchall()
        current_rows: dict[str, sqlite3.Row] = {}
        for row in rows:
            revision_path = self._revision_path(str(row["memory_revision_id"]))
            revision_payload = self._render(row, editable=False)
            if not revision_path.exists():
                self._atomic_write(revision_path, revision_payload)
                revision_writes += 1
            elif revision_path.read_bytes() != revision_payload:
                raise ValueError(f"immutable Memory revision file is corrupt: {revision_path.name}")
            self._record_payload_file(connection, row, revision_path, revision_payload)
            if str(row["memory_revision_id"]) == str(row["current_revision_id"]):
                current_rows[str(row["memory_item_id"])] = row

        for item_id, row in current_rows.items():
            current_path = self._current_path(row)
            expected_payload = self._render(row, editable=True)
            if not current_path.exists():
                self._atomic_write(current_path, expected_payload)
                current_writes += 1
                continue
            parsed = self._parse_editable(current_path)
            if parsed is None or parsed[0] != item_id:
                invalid.append(current_path.relative_to(self._workspace_root).as_posix())
                continue
            _, base_revision_id, title, content = parsed
            expected_fingerprint = _payload_fingerprint(str(row["title"]), str(row["content"]))
            actual_fingerprint = _payload_fingerprint(title, content)
            if actual_fingerprint == expected_fingerprint:
                if current_path.read_bytes() != expected_payload:
                    self._atomic_write(current_path, expected_payload)
                    current_writes += 1
                continue
            if base_revision_id != str(row["current_revision_id"]):
                invalid.append(current_path.relative_to(self._workspace_root).as_posix())
                continue
            raw = current_path.read_bytes()
            external.append(
                ExternalMemoryEdit(
                    item_id,
                    base_revision_id,
                    int(row["pointer_revision"]),
                    current_path.relative_to(self._workspace_root).as_posix(),
                    title,
                    content,
                    _sha256_bytes(raw),
                )
            )

        self._write_indexes(tuple(current_rows.values()))
        return MemoryFilesystemSyncReport(
            revision_writes,
            current_writes,
            tuple(external),
            tuple(invalid),
        )

    def reconcile_external_edits(
        self,
        connection: sqlite3.Connection,
        identities: IdentityGenerator,
    ) -> MemoryFilesystemSyncReport:
        report = self.synchronize(connection)
        service = MemoryService(SqliteMemoryRepository(connection), identities)
        for edit in report.external_edits:
            service.revise(
                ReviseMemoryCommand(
                    identities.new(CommandId),
                    MemoryItemId(edit.memory_item_id),
                    edit.expected_pointer_revision,
                    edit.title,
                    edit.content,
                    MemoryOriginKind.IMPORTED,
                    (
                        MemorySource(
                            "memory_file",
                            edit.relative_path,
                            edit.file_sha256,
                            MemorySourceRole.IMPORT,
                        ),
                    ),
                    MemoryLifecycle.ACTIVE,
                )
            )
        return self.synchronize(connection) if report.external_edits else report

    def current_relative_path(self, row: sqlite3.Row) -> str:
        return self._current_path(row).relative_to(self._workspace_root).as_posix()

    def _revision_path(self, revision_id: str) -> Path:
        if not revision_id.startswith("memoryrev_") or any(
            char in revision_id for char in ("/", "\\", ".")
        ):
            raise ValueError("invalid Memory Revision identity")
        return self._contained(self.root / "revisions" / f"{revision_id}.md")

    def _current_path(self, row: sqlite3.Row) -> Path:
        item_id = str(row["memory_item_id"])
        if str(row["scope_kind"]) == "research_path":
            target = self.root / "items" / "paths" / str(row["scope_object_id"])
        else:
            target = self.root / "items" / "workspace"
        return self._contained(target / f"{item_id}.md")

    def _contained(self, path: Path) -> Path:
        resolved = path.resolve()
        if not resolved.is_relative_to(self.root):
            raise ValueError("Memory path escapes the managed root")
        return resolved

    @staticmethod
    def _render(row: sqlite3.Row, *, editable: bool) -> bytes:
        lines = [
            "---",
            f"memory_id: {row['memory_item_id']}",
            f"revision_id: {row['memory_revision_id']}",
            f"revision_number: {row['revision_number']}",
            f"scope: {row['scope_kind']}:{row['scope_object_id']}",
            f"kind: {row['memory_kind']}",
            f"content_sha256: {row['content_sha256']}",
            f"editable: {str(editable).lower()}",
        ]
        if editable:
            lines.extend(
                (
                    f"lifecycle: {row['lifecycle']}",
                    f"access_tier: {row['access_tier']}",
                    f"pinned: {str(bool(row['pinned'])).lower()}",
                )
            )
        lines.extend(
            (
                "---",
                "",
                f"# {row['title']}",
                "",
                str(row["content"]).rstrip(),
                "",
            )
        )
        return "\n".join(lines).encode("utf-8")

    @staticmethod
    def _parse_editable(path: Path) -> tuple[str, str, str, str] | None:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return None
        if not text.startswith("---\n") or "\n---\n" not in text[4:]:
            return None
        metadata_text, body = text[4:].split("\n---\n", 1)
        metadata: dict[str, str] = {}
        for line in metadata_text.splitlines():
            key, separator, value = line.partition(":")
            if separator:
                metadata[key.strip()] = value.strip()
        body = body.lstrip("\n")
        first, separator, remainder = body.partition("\n")
        if not first.startswith("# ") or not separator:
            return None
        title = first[2:].strip()
        content = remainder.lstrip("\n").rstrip()
        memory_id = metadata.get("memory_id", "")
        revision_id = metadata.get("revision_id", "")
        if not memory_id.startswith("memoryitem_") or not revision_id.startswith("memoryrev_"):
            return None
        if not title or not content:
            return None
        return memory_id, revision_id, title, content

    def _record_payload_file(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        path: Path,
        payload: bytes,
    ) -> None:
        connection.execute(
            """
            INSERT INTO memory_payload_files(
                memory_revision_id, relative_path, payload_sha256, size_bytes,
                availability, observed_at, source_revision
            ) VALUES (?, ?, ?, ?, 'available', ?, ?)
            ON CONFLICT(memory_revision_id) DO UPDATE SET
                relative_path = excluded.relative_path,
                payload_sha256 = excluded.payload_sha256,
                size_bytes = excluded.size_bytes,
                availability = excluded.availability,
                observed_at = excluded.observed_at,
                source_revision = excluded.source_revision
            """,
            (
                str(row["memory_revision_id"]),
                path.relative_to(self._workspace_root).as_posix(),
                _sha256_bytes(payload),
                len(payload),
                datetime.now(UTC).isoformat(),
                int(row["created_revision"]),
            ),
        )

    def _write_indexes(self, rows: tuple[sqlite3.Row, ...]) -> None:
        active = [
            row
            for row in rows
            if str(row["lifecycle"]) == "active"
            and row["superseded_by_memory_item_id"] is None
            and str(row["access_tier"]) != "archived"
        ]
        active.sort(
            key=lambda row: (
                {"hot": 0, "warm": 1, "cold": 2}.get(str(row["access_tier"]), 3),
                0 if bool(row["pinned"]) else 1,
                -int(row["created_revision"]),
            )
        )
        index_lines = [
            "# Project Memory",
            "",
            (
                "Generated routing index. Current Workspace state and research Evidence "
                "take precedence."
            ),
            "",
        ]
        for row in active:
            relative = self._current_path(row).relative_to(self.root).as_posix()
            index_lines.append(
                f"- [{row['access_tier']}] [{row['title']}]({relative}) "
                f"`{row['memory_kind']}` `{row['memory_revision_id']}`"
            )
        self._atomic_write(
            self.root / "MEMORY.md", ("\n".join(index_lines).rstrip() + "\n").encode("utf-8")
        )
        summary_lines = [
            "# Memory Summary",
            "",
            "Navigation only; not Evidence or current Research State.",
            "",
        ]
        for row in (item for item in active if str(item["access_tier"]) == "hot"):
            line = f"- [{row['memory_kind']}] {row['title']}: {row['content']}"
            candidate = "\n".join([*summary_lines, line]).encode("utf-8")
            if len(candidate) > 4096:
                break
            summary_lines.append(line)
        self._atomic_write(
            self.root / "memory_summary.md",
            ("\n".join(summary_lines).rstrip() + "\n").encode("utf-8"),
        )

    @staticmethod
    def _atomic_write(path: Path, payload: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()
