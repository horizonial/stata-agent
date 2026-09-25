"""Build one versioned dense index inside an existing Workspace database."""

from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from stata_research_agent.application.dense_retrieval import DenseKnowledgeIndexService
from stata_research_agent.application.knowledge_retrieval import CorpusRole
from stata_research_agent.domain.identifiers import CommandId, WorkspaceId
from stata_research_agent.interfaces.sentence_transformer_embedding import (
    SentenceTransformerEmbeddingGateway,
)
from stata_research_agent.persistence.dense_knowledge_store import (
    SqliteDenseKnowledgeIndexRepository,
)
from stata_research_agent.persistence.workspace import WorkspaceDatabase


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument("--embedding-cache", type=Path, required=True)
    parser.add_argument("--model-name", default="intfloat/multilingual-e5-small")
    parser.add_argument(
        "--model-revision",
        default="614241f622f53c4eeff9890bdc4f31cfecc418b3",
    )
    parser.add_argument("--query-prefix", default="query: ")
    parser.add_argument("--document-prefix", default="passage: ")
    parser.add_argument("--profile-family", default="e5-prefix-v1")
    parser.add_argument("--trust-remote-code", action="store_true")
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--max-sequence-length", type=int)
    parser.add_argument("--report", type=Path, required=True)
    arguments = parser.parse_args()

    report_path = arguments.report.resolve()
    if report_path.exists():
        raise ValueError("dense-index report already exists; run outputs are immutable")
    report_path.parent.mkdir(parents=True, exist_ok=True)

    database = WorkspaceDatabase(
        arguments.workspace_root.resolve(), WorkspaceId(arguments.workspace_id)
    )
    connection = database.open(writable=True)
    started = time.perf_counter()
    try:
        gateway = SentenceTransformerEmbeddingGateway(
            model_name=arguments.model_name,
            model_revision=arguments.model_revision,
            query_prefix=arguments.query_prefix,
            document_prefix=arguments.document_prefix,
            profile_family=arguments.profile_family,
            trust_remote_code=arguments.trust_remote_code,
            cache_folder=arguments.embedding_cache.resolve(),
            device=arguments.device,
            batch_size=arguments.batch_size,
            max_sequence_length=arguments.max_sequence_length,
        )
        service = DenseKnowledgeIndexService(
            SqliteDenseKnowledgeIndexRepository(connection), gateway
        )
        index_id = service.ensure_current(
            CommandId(
                "cmd_corporate_finance_dense_index_"
                + gateway.profile.profile_revision.encode("utf-8").hex()[:24]
            ),
            (CorpusRole.LITERATURE_EVIDENCE,),
        )
    finally:
        connection.close()
    elapsed = time.perf_counter() - started
    report = {
        "schema_version": "stata-research-agent/dense-index-build/v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "workspace_root": str(arguments.workspace_root.resolve()),
        "workspace_id": arguments.workspace_id,
        "index_id": index_id,
        "embedding_profile": asdict(gateway.profile),
        "device": arguments.device,
        "batch_size": arguments.batch_size,
        "elapsed_seconds": elapsed,
        "process_id": os.getpid(),
    }
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
