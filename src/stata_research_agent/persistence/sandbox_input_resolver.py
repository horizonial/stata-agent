"""Resolve exact managed Artifact identities for staged sandbox input copies."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path


class SqliteSandboxArtifactPathResolver:
    """Fail closed unless the latest authoritative Artifact bytes still match."""

    def __init__(self, connection: sqlite3.Connection, workspace_root: Path) -> None:
        self._connection = connection
        self._workspace_root = workspace_root.resolve()
        self._objects_root = (self._workspace_root / ".stata-agent" / "objects").resolve()

    def __call__(self, artifact_id: str) -> Path:
        row = self._connection.execute(
            """
            SELECT a.size_bytes, a.content_hash, s.availability, l.managed_handle
            FROM artifacts AS a
            JOIN artifact_states AS s ON s.artifact_id = a.artifact_id
            JOIN artifact_locations AS l ON l.artifact_id = a.artifact_id
            WHERE a.artifact_id = ?
            """,
            (artifact_id,),
        ).fetchone()
        if row is None or str(row["availability"]) != "available":
            raise ValueError("sandbox input Artifact is unavailable")
        raw_handle = Path(str(row["managed_handle"]))
        if raw_handle.is_absolute() or ".." in raw_handle.parts:
            raise ValueError("sandbox input Artifact handle is unsafe")
        source = (self._workspace_root / raw_handle).resolve(strict=True)
        if not source.is_relative_to(self._objects_root) or not source.is_file():
            raise ValueError("sandbox input Artifact escaped the managed object store")
        size, digest = self._hash(source)
        if size != int(row["size_bytes"]) or digest != str(row["content_hash"]):
            raise ValueError("sandbox input Artifact failed fresh identity verification")
        return source

    @staticmethod
    def _hash(path: Path) -> tuple[int, str]:
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                size += len(chunk)
                digest.update(chunk)
        return size, digest.hexdigest()
