"""Run deterministic RAG retrieval evaluation against one real Workspace ledger."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from stata_research_agent.application.knowledge_retrieval import (
    CorpusRole,
    KnowledgeRetrievalHit,
)
from stata_research_agent.application.rag_evaluation import (
    RagAcceptanceThresholds,
    RagGoldCase,
    evaluate_rag,
)
from stata_research_agent.domain.identifiers import WorkspaceId
from stata_research_agent.persistence.knowledge_store import SqliteKnowledgeRepository
from stata_research_agent.persistence.workspace import WorkspaceDatabase


def _required_string(raw: dict[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"gold case requires non-empty {key}")
    return value.strip()


def _string_tuple(raw: dict[str, Any], key: str) -> tuple[str, ...]:
    value = raw.get(key, [])
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"gold case {key} must be an array of strings")
    return tuple(item for item in value if item.strip())


def load_gold_cases(path: Path) -> tuple[RagGoldCase, ...]:
    payload: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("cases"), list):
        raise ValueError("gold file must be an object with a cases array")
    if payload.get("schema_version") != "rag-gold-v1":
        raise ValueError("gold file requires schema_version rag-gold-v1")
    cases: list[RagGoldCase] = []
    for item in payload["cases"]:
        if not isinstance(item, dict):
            raise ValueError("every gold case must be an object")
        raw_roles = item.get("corpus_roles")
        if not isinstance(raw_roles, list) or not raw_roles:
            raise ValueError("gold case requires corpus_roles")
        raw_groups = item.get("evidence_groups", [])
        if not isinstance(raw_groups, list) or any(
            not isinstance(group, list)
            or any(not isinstance(marker, str) for marker in group)
            for group in raw_groups
        ):
            raise ValueError("evidence_groups must be arrays of marker strings")
        cases.append(
            RagGoldCase(
                _required_string(item, "case_id"),
                _required_string(item, "query"),
                tuple(CorpusRole(str(role)) for role in raw_roles),
                _string_tuple(item, "expected_source_locators"),
                _string_tuple(item, "expected_content_markers"),
                tuple(tuple(str(marker) for marker in group) for group in raw_groups),
                int(item.get("k", 6)),
            )
        )
    return tuple(cases)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate canonical RAG retrieval against a versioned gold set."
    )
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument("--gold", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--min-recall", type=float, default=0.80)
    parser.add_argument("--min-precision", type=float, default=0.10)
    parser.add_argument("--min-mrr", type=float, default=0.50)
    parser.add_argument("--min-group-coverage", type=float, default=0.80)
    arguments = parser.parse_args()

    database = WorkspaceDatabase(
        arguments.workspace_root.resolve(), WorkspaceId(arguments.workspace_id)
    )
    connection = database.open(writable=False)
    try:
        repository = SqliteKnowledgeRepository(connection)

        def retrieve(
            query: str, roles: tuple[CorpusRole, ...], limit: int
        ) -> tuple[KnowledgeRetrievalHit, ...]:
            return tuple(
                KnowledgeRetrievalHit(
                    hit.node_id,
                    hit.source_revision_id,
                    hit.parse_revision_id,
                    hit.source_locator,
                    hit.corpus_role,
                    hit.node_kind,
                    hit.page_start,
                    hit.page_end,
                    hit.section_title,
                    hit.content,
                    hit.lexical_rank,
                    hit.fused_score,
                )
                for hit in repository.search_canonical(
                    query, corpus_roles=roles, limit=limit
                )
            )

        report = evaluate_rag(load_gold_cases(arguments.gold), retrieve)
    finally:
        connection.close()
    thresholds = RagAcceptanceThresholds(
        arguments.min_recall,
        arguments.min_precision,
        arguments.min_mrr,
        arguments.min_group_coverage,
    )
    accepted = report.accepts(thresholds)
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "workspace_id": arguments.workspace_id,
        "gold_file": str(arguments.gold.resolve()),
        "report": asdict(report),
        "acceptance_thresholds": asdict(thresholds),
        "accepted": accepted,
    }
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    if arguments.output is not None:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if accepted else 2


if __name__ == "__main__":
    raise SystemExit(main())
