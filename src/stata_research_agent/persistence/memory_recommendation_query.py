"""Recommendation-style retrieval over authoritative Project Memory metadata.

The query never rewrites or summarizes Memory.  It produces ranked references to exact
current revisions using lexical, scope, authority, temporal, source-link, and supersession
signals.  The caller decides whether to open the immutable filesystem payload.
"""

from __future__ import annotations

import math
import re
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass

_LATIN_TOKEN = re.compile(r"[A-Za-z0-9_.-]{2,}", re.UNICODE)
_HAN_SEQUENCE = re.compile(r"[\u3400-\u9fff]+", re.UNICODE)
_LATIN_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "how",
    "in",
    "into",
    "is",
    "it",
    "of",
    "on",
    "or",
    "our",
    "should",
    "that",
    "the",
    "this",
    "to",
    "we",
    "with",
}


def memory_tokens(value: str) -> set[str]:
    tokens = {
        normalized
        for token in _LATIN_TOKEN.findall(value)
        if (normalized := token.casefold()) not in _LATIN_STOPWORDS
    }
    for sequence in _HAN_SEQUENCE.findall(value):
        tokens.update(sequence)
        tokens.update(sequence[index : index + 2] for index in range(len(sequence) - 1))
    return tokens


@dataclass(frozen=True, slots=True)
class RankedMemoryCandidate:
    row: sqlite3.Row
    score: float
    reasons: tuple[str, ...]
    source_rows: tuple[sqlite3.Row, ...]


class SqliteMemoryRecommendationQuery:
    """Rank Memory like a recommender while preserving exact revision identity."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def search(
        self,
        query: str,
        *,
        research_path_id: str,
        limit: int,
        include_archived: bool = False,
        hot_only: bool = False,
        semantic_scorer: Callable[
            [str, tuple[tuple[str, str], ...]], Mapping[str, float]
        ]
        | None = None,
    ) -> tuple[RankedMemoryCandidate, ...]:
        rows = tuple(
            self._rows(
                research_path_id,
                include_archived=include_archived,
                hot_only=hot_only,
            )
        )
        if not rows:
            return ()
        sources = self._sources(rows)
        semantic_scores = (
            {}
            if semantic_scorer is None
            else dict(
                semantic_scorer(
                    query,
                    tuple(
                        (
                            str(row["memory_item_id"]),
                            f"{row['title']}\n{row['content']}",
                        )
                        for row in rows
                    ),
                )
            )
        )
        query_tokens = memory_tokens(query)
        normalized_query = " ".join(query.casefold().split())
        scored: dict[str, tuple[float, list[str]]] = {}
        direct_scores: dict[str, float] = {}
        eligible_ids: set[str] = set()
        by_id = {str(row["memory_item_id"]): row for row in rows}

        for row in rows:
            item_id = str(row["memory_item_id"])
            text = f"{row['title']} {row['content']}"
            text_tokens = memory_tokens(text)
            overlap = len(query_tokens.intersection(text_tokens))
            coverage = overlap / max(1, len(query_tokens))
            score = 0.0
            reasons: list[str] = []
            if overlap:
                score += 9.0 * coverage + 1.5 * overlap
                reasons.append("lexical_overlap")
            if normalized_query and normalized_query in " ".join(text.casefold().split()):
                score += 5.0
                reasons.append("exact_phrase")
            semantic_score = max(0.0, min(1.0, float(semantic_scores.get(item_id, 0.0))))
            if semantic_score >= 0.2:
                score += semantic_score * 8.0
                reasons.append("semantic_similarity")

            source_overlap = 0
            for source in sources.get(item_id, ()):
                source_text = (
                    f"{source['source_object_type']} {source['source_object_id']} "
                    f"{source['source_object_revision']}"
                )
                source_overlap += len(query_tokens.intersection(memory_tokens(source_text)))
            if source_overlap:
                score += min(5.0, 2.0 * source_overlap)
                reasons.append("source_identity")

            intent_boost = self._intent_boost(query_tokens, str(row["memory_kind"]))
            if intent_boost:
                score += intent_boost
                reasons.append("memory_kind_intent")
            if str(row["scope_kind"]) == "research_path":
                score += 2.0
                reasons.append("active_research_path")
            if bool(row["pinned"]):
                score += 6.0
                reasons.append("pinned")
            origin_boost = {
                "explicit_user": 3.0,
                "confirmed": 2.5,
                "imported": 1.0,
                "inferred": 0.25,
            }.get(str(row["origin_kind"]), 0.0)
            score += origin_boost
            reasons.append(f"origin:{row['origin_kind']}")
            tier_boost = {"hot": 2.0, "warm": 1.0, "cold": 0.25, "archived": -2.0}.get(
                str(row["access_tier"]), 0.0
            )
            score += tier_boost
            if int(row["last_used_revision"] or 0) > 0:
                score += min(2.0, math.log2(int(row["last_used_revision"]) + 1) / 10)
                reasons.append("previously_useful")

            has_retrieval_signal = bool(
                overlap
                or semantic_score >= 0.2
                or source_overlap
                or intent_boost
                # Pinned Memory may be admitted proactively on the automatic hot path, but
                # an explicit search must still be allowed to say "no relevant Memory".
                or (hot_only and bool(row["pinned"]))
            )
            direct_scores[item_id] = score if has_retrieval_signal else 0.0
            if has_retrieval_signal:
                eligible_ids.add(item_id)
            scored[item_id] = (score, reasons)

        # Memories grounded in the same authoritative object are neighbors.  Expand from
        # directly matched seeds without inventing a new semantic fact.
        seed_ids = {item_id for item_id, score in direct_scores.items() if score > 0}
        seed_source_keys = {
            self._source_key(source)
            for item_id in seed_ids
            for source in sources.get(item_id, ())
        }
        for item_id, source_rows in sources.items():
            if item_id in seed_ids:
                continue
            shared = len(
                seed_source_keys.intersection(self._source_key(row) for row in source_rows)
            )
            if shared:
                score, reasons = scored[item_id]
                scored[item_id] = (score + min(4.0, 1.5 * shared), [*reasons, "shared_source"])
                eligible_ids.add(item_id)

        # A query may match an obsolete decision more strongly than its replacement.  Transfer
        # that signal to the current successor while retaining the old item only for deliberate
        # historical review.
        for row in rows:
            successor_id = row["superseded_by_memory_item_id"]
            if successor_id is None or str(successor_id) not in by_id:
                continue
            old_id = str(row["memory_item_id"])
            old_direct = direct_scores.get(old_id, 0.0)
            if old_direct <= 0:
                continue
            successor = str(successor_id)
            score, reasons = scored[successor]
            scored[successor] = (
                score + old_direct * 0.8,
                [*reasons, "superseded_match_forwarded"],
            )
            eligible_ids.add(successor)

        eligible: list[RankedMemoryCandidate] = []
        for row in rows:
            item_id = str(row["memory_item_id"])
            if not include_archived and row["superseded_by_memory_item_id"] is not None:
                continue
            score, reasons = scored[item_id]
            if item_id not in eligible_ids or score <= 0 or (
                not query_tokens and not bool(row["pinned"])
            ):
                continue
            eligible.append(
                RankedMemoryCandidate(
                    row,
                    score,
                    tuple(dict.fromkeys(reasons)),
                    tuple(sources.get(item_id, ())),
                )
            )
        return tuple(self._diversified(eligible, limit=limit))

    def _rows(
        self,
        research_path_id: str,
        *,
        include_archived: bool,
        hot_only: bool,
    ) -> tuple[sqlite3.Row, ...]:
        workspace_id = str(
            self._connection.execute(
                "SELECT workspace_id FROM workspace_identity WHERE singleton_id = 1"
            ).fetchone()[0]
        )
        return tuple(
            self._connection.execute(
                """
                SELECT item.memory_item_id, item.scope_kind, item.scope_object_id,
                       item.memory_kind, state.current_revision_id AS memory_revision_id,
                       state.pointer_revision, state.lifecycle,
                       revision.title, revision.content, revision.origin_kind,
                       revision.created_revision, retention.access_tier, retention.pinned,
                       retention.superseded_by_memory_item_id,
                       payload.relative_path, payload.payload_sha256, payload.size_bytes,
                       COALESCE(MAX(use.created_revision), 0) AS last_used_revision
                FROM memory_items AS item
                JOIN memory_current_states AS state USING (memory_item_id)
                JOIN memory_revisions AS revision
                  ON revision.memory_revision_id = state.current_revision_id
                JOIN memory_retention_states AS retention USING (memory_item_id)
                LEFT JOIN memory_payload_files AS payload
                  ON payload.memory_revision_id = revision.memory_revision_id
                LEFT JOIN memory_context_uses AS use USING (memory_item_id)
                WHERE state.lifecycle = 'active'
                  AND (
                        ? = 1
                        OR retention.access_tier != 'archived'
                        OR retention.superseded_by_memory_item_id IS NOT NULL
                      )
                  AND (
                        ? = 0
                        OR retention.access_tier = 'hot'
                        OR retention.superseded_by_memory_item_id IS NOT NULL
                      )
                  AND (
                        (item.scope_kind = 'workspace' AND item.scope_object_id = ?)
                        OR
                        (item.scope_kind = 'research_path' AND item.scope_object_id = ?)
                      )
                GROUP BY item.memory_item_id
                ORDER BY revision.created_revision DESC
                """,
                (int(include_archived), int(hot_only), workspace_id, research_path_id),
            ).fetchall()
        )

    def _sources(
        self, rows: tuple[sqlite3.Row, ...]
    ) -> dict[str, tuple[sqlite3.Row, ...]]:
        revision_to_item = {
            str(row["memory_revision_id"]): str(row["memory_item_id"]) for row in rows
        }
        placeholders = ",".join("?" for _ in revision_to_item)
        if not placeholders:
            return {}
        result: dict[str, list[sqlite3.Row]] = {
            item_id: [] for item_id in revision_to_item.values()
        }
        source_rows = self._connection.execute(
            f"""
            SELECT memory_revision_id, source_object_type, source_object_id,
                   source_object_revision, source_role
            FROM memory_revision_sources
            WHERE memory_revision_id IN ({placeholders})
            ORDER BY memory_revision_source_id
            """,
            tuple(revision_to_item),
        ).fetchall()
        for source in source_rows:
            result[revision_to_item[str(source["memory_revision_id"])]].append(source)
        return {key: tuple(value) for key, value in result.items()}

    @staticmethod
    def _source_key(row: sqlite3.Row) -> tuple[str, str]:
        return str(row["source_object_type"]), str(row["source_object_id"])

    @staticmethod
    def _intent_boost(query_tokens: set[str], memory_kind: str) -> float:
        groups = {
            "research_decision": {
                "why", "decision", "choose", "changed", "为什么", "决定", "选择", "改用",
            },
            "research_constraint": {
                "must", "constraint", "require", "不能", "必须", "约束", "要求",
            },
            "unresolved_question": {
                "unresolved", "pending", "question", "待定", "问题", "没有解决",
            },
            "user_preference": {"prefer", "preference", "style", "偏好", "习惯", "文风"},
            "project_procedure": {"how", "procedure", "process", "怎么", "流程", "步骤"},
        }
        return 3.0 if query_tokens.intersection(groups.get(memory_kind, set())) else 0.0

    @staticmethod
    def _diversified(
        candidates: list[RankedMemoryCandidate], *, limit: int
    ) -> list[RankedMemoryCandidate]:
        remaining = list(candidates)
        selected: list[RankedMemoryCandidate] = []
        while remaining and len(selected) < limit:
            def adjusted(candidate: RankedMemoryCandidate) -> tuple[float, int, str]:
                tokens = memory_tokens(f"{candidate.row['title']} {candidate.row['content']}")
                similarity = 0.0
                for prior in selected:
                    prior_tokens = memory_tokens(f"{prior.row['title']} {prior.row['content']}")
                    union = tokens.union(prior_tokens)
                    if union:
                        similarity = max(
                            similarity,
                            len(tokens.intersection(prior_tokens)) / len(union),
                        )
                return (
                    candidate.score - 3.0 * similarity,
                    int(candidate.row["created_revision"]),
                    str(candidate.row["memory_item_id"]),
                )

            winner = max(remaining, key=adjusted)
            selected.append(winner)
            remaining.remove(winner)
        return selected
