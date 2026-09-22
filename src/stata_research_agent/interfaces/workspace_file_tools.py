"""Read-only, traceable Workspace file discovery for the production Agent."""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Any

from stata_research_agent.application.broker_execution import (
    BeginBrokerExecutionCommand,
    CompleteBrokerExecutionCommand,
)
from stata_research_agent.application.broker_execution_service import BrokerExecutionService
from stata_research_agent.application.ports.identity import IdentityGenerator
from stata_research_agent.application.tool_broker import (
    ResourceClaimTemplate,
    ToolContractDefinition,
)
from stata_research_agent.application.turn_driver import (
    ToolExecutionRequest,
    ToolExecutionResult,
)
from stata_research_agent.domain.identifiers import CommandId

_PRIVATE_PARTS = frozenset({".stata-agent", ".runtime"})
_PRIVATE_FILE_NAMES = frozenset(
    {
        "workspace.sqlite3",
        "workspace.sqlite3-journal",
        "workspace.sqlite3-shm",
        "workspace.sqlite3-wal",
    }
)
_TEXT_SUFFIXES = frozenset(
    {
        ".ado",
        ".csv",
        ".do",
        ".json",
        ".log",
        ".md",
        ".smcl",
        ".tex",
        ".txt",
        ".yaml",
        ".yml",
    }
)


def workspace_list_files_contract() -> ToolContractDefinition:
    return ToolContractDefinition(
        "workspace.list_files",
        "1.0.0",
        (
            "List user-visible files in the current research Workspace. Use this to discover "
            "replication do-files, documentation, data, and supporting materials."
        ),
        "artifact.verify",
        {
            "type": "object",
            "properties": {
                "relative_directory": {"type": "string", "default": "."},
                "recursive": {"type": "boolean", "default": True},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 200},
            },
            "additionalProperties": False,
        },
        {"type": "object"},
        "local_runtime",
        "pure_read",
        "parallel_safe",
        "replay_safe",
        "never",
        "not_interruptible",
        30,
        60,
        500_000,
        (ResourceClaimTemplate("workspace-files:current", "read"),),
    )


def workspace_read_text_contract() -> ToolContractDefinition:
    return ToolContractDefinition(
        "workspace.read_text",
        "1.0.0",
        (
            "Read a bounded line range from a user-visible text file in the current research "
            "Workspace. Suitable for Stata do/ado files, logs, CSV excerpts, and documentation."
        ),
        "artifact.verify",
        {
            "type": "object",
            "properties": {
                "relative_path": {"type": "string", "minLength": 1},
                "start_line": {"type": "integer", "minimum": 1, "default": 1},
                "max_lines": {"type": "integer", "minimum": 1, "maximum": 400, "default": 200},
            },
            "required": ["relative_path"],
            "additionalProperties": False,
        },
        {"type": "object"},
        "local_runtime",
        "pure_read",
        "parallel_safe",
        "replay_safe",
        "never",
        "not_interruptible",
        30,
        60,
        500_000,
        (ResourceClaimTemplate("workspace-files:current", "read"),),
    )


def workspace_list_artifacts_contract() -> ToolContractDefinition:
    return ToolContractDefinition(
        "research.list_artifacts",
        "1.0.0",
        (
            "List authoritative Artifact Ledger entries, including Stata-captured graphs, "
            "tables, logs, datasets, and delivered documents. Use this to verify managed "
            "outputs; workspace.list_files intentionally cannot see private managed storage."
        ),
        "artifact.verify",
        {
            "type": "object",
            "properties": {
                "artifact_kind": {
                    "type": "string",
                    "enum": [
                        "dataset",
                        "code",
                        "log",
                        "table",
                        "document",
                        "diagnostic",
                    ],
                },
                "media_type": {"type": "string", "minLength": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
            },
            "additionalProperties": False,
        },
        {"type": "object"},
        "local_runtime",
        "pure_read",
        "parallel_safe",
        "replay_safe",
        "never",
        "not_interruptible",
        30,
        60,
        500_000,
        (ResourceClaimTemplate("artifact-ledger:current", "read"),),
    )


class ArtifactLedgerQueryExecutor:
    """Expose authoritative Artifact identities without leaking managed filesystem paths."""

    def __init__(
        self,
        connection: Any,
        bridge: BrokerExecutionService,
        identities: IdentityGenerator,
    ) -> None:
        self._connection = connection
        self._bridge = bridge
        self._identities = identities

    async def execute(self, request: ToolExecutionRequest) -> ToolExecutionResult:
        handle = self._bridge.begin(
            BeginBrokerExecutionCommand(
                self._identities.new(CommandId),
                request.turn_id,
                request.tool_call_id,
                request.operation_id,
            )
        )
        try:
            payload = self._list(request.arguments)
            success = True
            summary = "Artifact Ledger query completed"
        except Exception as error:
            payload = {"error_type": type(error).__name__, "message": str(error)}
            success = False
            summary = "Artifact Ledger query failed"
        outcome = self._bridge.complete(
            CompleteBrokerExecutionCommand(
                self._identities.new(CommandId), handle, success, summary, payload
            )
        )
        payload["status"] = outcome.status
        return ToolExecutionResult(
            success,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        )

    def _list(self, arguments: Any) -> dict[str, object]:
        limit = int(arguments.get("limit", 50))
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        clauses: list[str] = []
        parameters: list[object] = []
        artifact_kind = arguments.get("artifact_kind")
        if artifact_kind is not None:
            clauses.append("artifact.artifact_kind = ?")
            parameters.append(str(artifact_kind))
        media_type = arguments.get("media_type")
        if media_type is not None:
            clauses.append("artifact.media_type = ?")
            parameters.append(str(media_type))
        where = "" if not clauses else "WHERE " + " AND ".join(clauses)
        parameters.append(limit)
        rows = self._connection.execute(
            f"""
            SELECT artifact.artifact_id, artifact.artifact_kind, artifact.media_type,
                   artifact.size_bytes, artifact.content_hash, artifact.created_revision,
                   state.availability, state.verified_at,
                   attempt.operation_id, manifest.output_slot, manifest.producer_locator
            FROM artifacts AS artifact
            JOIN artifact_states AS state USING (artifact_id)
            JOIN operation_attempts AS attempt
              ON attempt.operation_attempt_id = artifact.producer_attempt_id
            LEFT JOIN artifact_candidate_sources AS source USING (artifact_id)
            LEFT JOIN completion_manifest_artifacts AS manifest
              USING (artifact_candidate_id)
            {where}
            ORDER BY artifact.created_revision DESC
            LIMIT ?
            """,
            tuple(parameters),
        ).fetchall()
        return {
            "artifacts": [
                {
                    "artifact_id": str(row["artifact_id"]),
                    "artifact_kind": str(row["artifact_kind"]),
                    "media_type": str(row["media_type"]),
                    "size_bytes": int(row["size_bytes"]),
                    "sha256": str(row["content_hash"]),
                    "availability": str(row["availability"]),
                    "verified_at": str(row["verified_at"]),
                    "producer_operation_id": str(row["operation_id"]),
                    "output_slot": (
                        None if row["output_slot"] is None else str(row["output_slot"])
                    ),
                    "producer_locator": (
                        None
                        if row["producer_locator"] is None
                        else str(row["producer_locator"])
                    ),
                    "created_revision": int(row["created_revision"]),
                }
                for row in rows
            ],
            "limit": limit,
            "truncated": len(rows) >= limit,
        }


class WorkspaceFileExecutor:
    """Resolve Workspace paths without exposing private storage or arbitrary host paths."""

    def __init__(
        self,
        workspace_root: Path,
        bridge: BrokerExecutionService,
        identities: IdentityGenerator,
        *,
        action: str,
    ) -> None:
        if action not in {"list", "read"}:
            raise ValueError("unknown Workspace file action")
        self._root = workspace_root.resolve()
        self._bridge = bridge
        self._identities = identities
        self._action = action

    async def execute(self, request: ToolExecutionRequest) -> ToolExecutionResult:
        handle = self._bridge.begin(
            BeginBrokerExecutionCommand(
                self._identities.new(CommandId),
                request.turn_id,
                request.tool_call_id,
                request.operation_id,
            )
        )
        try:
            payload = self._list(request.arguments) if self._action == "list" else self._read(
                request.arguments
            )
            success = True
            summary = f"Workspace file {self._action} completed"
        except Exception as error:
            payload = {"error_type": type(error).__name__, "message": str(error)}
            success = False
            summary = f"Workspace file {self._action} failed"
        outcome = self._bridge.complete(
            CompleteBrokerExecutionCommand(
                self._identities.new(CommandId), handle, success, summary, payload
            )
        )
        payload["status"] = outcome.status
        return ToolExecutionResult(
            success,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        )

    def _list(self, arguments: Any) -> dict[str, object]:
        relative_directory = str(arguments.get("relative_directory", ".")).strip() or "."
        directory = self._resolve(relative_directory, require_file=False)
        if not directory.is_dir():
            raise ValueError("Workspace directory does not exist")
        recursive = bool(arguments.get("recursive", True))
        limit = int(arguments.get("limit", 200))
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        iterator = directory.rglob("*") if recursive else directory.glob("*")
        files: list[dict[str, object]] = []
        for path in iterator:
            if len(files) >= limit:
                break
            try:
                resolved = path.resolve(strict=True)
                relative = resolved.relative_to(self._root)
                if (
                    path.is_symlink()
                    or any(part.casefold() in _PRIVATE_PARTS for part in relative.parts)
                    or relative.name.casefold() in _PRIVATE_FILE_NAMES
                    or not resolved.is_file()
                ):
                    continue
                observed = resolved.stat()
            except (FileNotFoundError, OSError, RuntimeError, ValueError):
                continue
            files.append(
                {
                    "relative_path": relative.as_posix(),
                    "size_bytes": int(observed.st_size),
                    "text_readable": resolved.suffix.casefold() in _TEXT_SUFFIXES,
                }
            )
        files.sort(key=lambda item: str(item["relative_path"]).casefold())
        return {
            "relative_directory": directory.relative_to(self._root).as_posix() or ".",
            "recursive": recursive,
            "limit": limit,
            "truncated": len(files) >= limit,
            "files": files,
        }

    def _read(self, arguments: Any) -> dict[str, object]:
        relative_path = str(arguments["relative_path"]).strip()
        path = self._resolve(relative_path, require_file=True)
        if path.suffix.casefold() not in _TEXT_SUFFIXES:
            raise ValueError("Workspace file type is not approved for text reading")
        if path.stat().st_size > 8_000_000:
            raise ValueError("Workspace text file exceeds the bounded read limit")
        start_line = int(arguments.get("start_line", 1))
        max_lines = int(arguments.get("max_lines", 200))
        if start_line < 1 or not 1 <= max_lines <= 400:
            raise ValueError("invalid Workspace text line range")
        raw = path.read_bytes()
        if b"\x00" in raw:
            raise ValueError("Workspace file is binary")
        text, encoding = self._decode(raw)
        lines = text.splitlines()
        selected = lines[start_line - 1 : start_line - 1 + max_lines]
        content = "\n".join(selected)
        if len(content) > 200_000:
            content = content[:200_000]
        return {
            "relative_path": path.relative_to(self._root).as_posix(),
            "sha256": sha256(raw).hexdigest(),
            "encoding": encoding,
            "total_lines": len(lines),
            "start_line": start_line,
            "end_line": start_line + len(selected) - 1,
            "has_more": start_line - 1 + len(selected) < len(lines),
            "content": content,
        }

    def _resolve(self, relative_path: str, *, require_file: bool) -> Path:
        raw = Path(relative_path)
        if raw.is_absolute() or ".." in raw.parts or not relative_path.strip():
            raise ValueError("Workspace path is unsafe")
        if any(part.casefold() in _PRIVATE_PARTS for part in raw.parts):
            raise ValueError("private Workspace storage is not readable")
        if raw.name.casefold() in _PRIVATE_FILE_NAMES:
            raise ValueError("private Workspace storage is not readable")
        candidate = self._root / raw
        try:
            if candidate.is_symlink():
                raise ValueError("Workspace path aliases are not readable")
            target = candidate.resolve(strict=True)
        except (FileNotFoundError, OSError, RuntimeError) as error:
            raise ValueError("Workspace path is unavailable") from error
        if not target.is_relative_to(self._root) or target.is_symlink():
            raise ValueError("Workspace path escaped the Workspace")
        if require_file and not target.is_file():
            raise ValueError("Workspace path is not a file")
        return target

    @staticmethod
    def _decode(raw: bytes) -> tuple[str, str]:
        for encoding in ("utf-8-sig", "utf-8", "cp1252"):
            try:
                return raw.decode(encoding), encoding
            except UnicodeDecodeError:
                continue
        raise ValueError("Workspace text encoding is unsupported")
