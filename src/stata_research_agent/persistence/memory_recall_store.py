"""SQLite adapter for scoped, progressive Project Memory recall."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable, Mapping

from stata_research_agent.application.ports.identity import IdentityGenerator

from .filesystem_memory import FilesystemMemoryStore

_TOKEN = re.compile(r"[\w\u3400-\u9fff]+", re.UNICODE)


def _tokens(value: str) -> set[str]:
    return {token.casefold() for token in _TOKEN.findall(value) if len(token) > 1}


class SqliteMemoryRecallRepository:
    def __init__(
        self,
        connection: sqlite3.Connection,
        files: FilesystemMemoryStore,
        identities: IdentityGenerator,
    ) -> None:
        self._connection = connection
        self._files = files
        self._identities = identities

    def reconcile_external_edits(self) -> None:
        self._files.reconcile_external_edits(self._connection, self._identities)

    def search(
        self,
        arguments: Mapping[str, object],
        *,
        research_path_id: str,
    ) -> list[dict[str, object]]:
        query = str(arguments.get("query", "")).strip()
        if not query:
            raise ValueError("query must not be empty")
        limit = max(1, min(int(str(arguments.get("limit", 8))), 24))
        include_archived = bool(arguments.get("include_archived", False))
        query_tokens = _tokens(query)
        rows = self._rows(research_path_id, include_archived=include_archived)
        ranked: list[tuple[int, int, sqlite3.Row]] = []
        for row in rows:
            overlap = len(query_tokens.intersection(_tokens(f"{row['title']} {row['content']}")))
            if overlap == 0:
                continue
            scope_boost = 1 if str(row["scope_kind"]) == "research_path" else 0
            ranked.append((overlap, scope_boost, row))
        ranked.sort(
            key=lambda item: (item[0], item[1], int(item[2]["created_revision"])),
            reverse=True,
        )
        return [self._payload(row, include_content=False) for _, _, row in ranked[:limit]]

    def open(
        self,
        arguments: Mapping[str, object],
        *,
        research_path_id: str,
    ) -> list[dict[str, object]]:
        raw_ids = arguments.get("memory_item_ids")
        if not isinstance(raw_ids, list) or not raw_ids:
            raise ValueError("memory_item_ids must be a non-empty array")
        if len(raw_ids) > 24:
            raise ValueError("at most 24 Memory items can be opened at once")
        include_archived = bool(arguments.get("include_archived", False))
        by_id = {
            str(row["memory_item_id"]): row
            for row in self._rows(research_path_id, include_archived=include_archived)
        }
        hits: list[dict[str, object]] = []
        for raw_id in raw_ids:
            memory_item_id = str(raw_id)
            row = by_id.get(memory_item_id)
            if row is None:
                raise ValueError(
                    f"Memory item is unavailable, out of scope, or archived: {memory_item_id}"
                )
            hits.append(self._payload(row, include_content=True))
        return hits

    def _rows(self, research_path_id: str, *, include_archived: bool) -> Iterable[sqlite3.Row]:
        workspace_id = str(
            self._connection.execute(
                "SELECT workspace_id FROM workspace_identity WHERE singleton_id = 1"
            ).fetchone()[0]
        )
        return self._connection.execute(
            """
            SELECT item.memory_item_id, item.scope_kind, item.scope_object_id,
                   item.memory_kind, state.current_revision_id AS memory_revision_id,
                   state.lifecycle, revision.title, revision.content,
                   revision.origin_kind, revision.created_revision,
                   retention.access_tier, retention.pinned,
                   retention.superseded_by_memory_item_id,
                   payload.relative_path
            FROM memory_items AS item
            JOIN memory_current_states AS state USING (memory_item_id)
            JOIN memory_revisions AS revision
              ON revision.memory_revision_id = state.current_revision_id
            JOIN memory_retention_states AS retention USING (memory_item_id)
            LEFT JOIN memory_payload_files AS payload
              ON payload.memory_revision_id = revision.memory_revision_id
            WHERE state.lifecycle = 'active'
              AND (? = 1 OR retention.access_tier != 'archived')
              AND (? = 1 OR retention.superseded_by_memory_item_id IS NULL)
              AND (
                    (item.scope_kind = 'workspace' AND item.scope_object_id = ?)
                    OR
                    (item.scope_kind = 'research_path' AND item.scope_object_id = ?)
                  )
            ORDER BY revision.created_revision DESC
            """,
            (int(include_archived), int(include_archived), workspace_id, research_path_id),
        ).fetchall()

    @staticmethod
    def _payload(row: sqlite3.Row, *, include_content: bool) -> dict[str, object]:
        content = str(row["content"])
        return {
            "memory_item_id": str(row["memory_item_id"]),
            "memory_revision_id": str(row["memory_revision_id"]),
            "created_revision": int(row["created_revision"]),
            "scope_kind": str(row["scope_kind"]),
            "scope_object_id": str(row["scope_object_id"]),
            "memory_kind": str(row["memory_kind"]),
            "title": str(row["title"]),
            "content": content if include_content else None,
            "excerpt": content[:280],
            "access_tier": str(row["access_tier"]),
            "pinned": bool(row["pinned"]),
            "origin_kind": str(row["origin_kind"]),
            "memory_file": row["relative_path"],
        }
