"""Intrinsic Product Evaluation for recommendation-style Project Memory recall."""

from __future__ import annotations

import json
import time
from hashlib import sha256
from pathlib import Path
from typing import Any, Protocol

from stata_research_agent.application.control import (
    CompleteTurnCommand,
    CreateWorkspaceCommand,
    SubmitMessageCommand,
    SubmitMessageResult,
)
from stata_research_agent.application.dense_retrieval import EmbeddingProfile
from stata_research_agent.application.memory import (
    CreateMemoryCommand,
    MemoryKind,
    MemoryOriginKind,
    MemoryScopeKind,
    MemorySource,
    MemorySourceRole,
    SupersedeMemoryCommand,
)
from stata_research_agent.application.memory_service import MemoryService
from stata_research_agent.application.product_evaluation import (
    EvaluationLevel,
    EvaluationPayloadReference,
    EvaluationScenario,
    EvaluationScore,
    GraderKind,
    GraderSpec,
    ProductEvaluationError,
    ScoreVerdict,
    SubsystemEvaluationMode,
    TrialBundle,
)
from stata_research_agent.application.research_state import CreateResearchPathBranchCommand
from stata_research_agent.application.research_state_service import ResearchStateService
from stata_research_agent.application.workspace_service import WorkspaceControlService
from stata_research_agent.domain.identifiers import CommandId, WorkspaceId
from stata_research_agent.domain.status import TurnStatus
from stata_research_agent.persistence.control_store import SqliteControlStore
from stata_research_agent.persistence.filesystem_memory import FilesystemMemoryStore
from stata_research_agent.persistence.memory_recall_store import SqliteMemoryRecallRepository
from stata_research_agent.persistence.memory_store import SqliteMemoryRepository
from stata_research_agent.persistence.research_state_store import SqliteResearchStateRepository
from stata_research_agent.persistence.workspace import WorkspaceDatabase
from stata_research_agent.runtime.uuid_identity import UuidIdentityGenerator


class _TraceReadableConnection(Protocol):
    def execute(self, statement: str) -> Any: ...


class _EvaluationEmbeddingGateway:
    """Deterministic semantic channel; no provider or model is part of this L1 trial."""

    @property
    def profile(self) -> EmbeddingProfile:
        return EmbeddingProfile("memory-eval-v2", "fixture", "memory-semantic", 2)

    def embed_documents(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        return tuple(
            (1.0, 0.0) if "instrumental variable" in text.casefold() else (0.0, 0.0)
            for text in texts
        )

    def embed_query(self, text: str) -> tuple[float, ...]:
        return (1.0, 0.0) if "endogeneity" in text.casefold() else (0.0, 0.0)


class MemoryRetrievalEvaluationAdapter:
    """Run a checked-in Memory corpus through the production search/open adapters."""

    scenario_id = "agent.memory.retrieval"
    _project_root = Path(__file__).resolve().parents[3]

    def execute(
        self,
        scenario: EvaluationScenario,
        *,
        trial_id: str,
        trial_root: Path,
    ) -> TrialBundle:
        self._validate_scenario(scenario)
        fixture = self._load_fixture(scenario)
        if trial_root.exists() and any(trial_root.iterdir()):
            raise ProductEvaluationError("evaluation Trial bundle directory is not empty")
        trial_root.mkdir(parents=True, exist_ok=True)
        workspace_root = trial_root / "workspace"
        workspace_id = WorkspaceId(f"ws_memory_retrieval_{trial_id.replace('-', '_')}")
        database = WorkspaceDatabase(workspace_root, workspace_id)
        database.create()
        connection = database.open(writable=True)
        report_path = trial_root / "memory-retrieval-report.json"
        trace_path = trial_root / "trace.jsonl"
        manifest_path = trial_root / "artifact-manifest.json"
        try:
            identities = UuidIdentityGenerator()
            control = WorkspaceControlService(SqliteControlStore(connection), identities)
            initialized = control.create_workspace(
                CreateWorkspaceCommand(CommandId(f"cmd_{trial_id}_workspace"), workspace_id)
            )
            initial = control.submit_message(
                SubmitMessageCommand(
                    CommandId(f"cmd_{trial_id}_initial"),
                    scenario.initial_user_message,
                )
            )
            state = ResearchStateService(SqliteResearchStateRepository(connection), identities)
            branch_basis = int(
                connection.execute("SELECT MAX(workspace_revision) FROM workspace_commits")
                .fetchone()[0]
            )
            branch = state.create_path_branch(
                CreateResearchPathBranchCommand(
                    CommandId(f"cmd_{trial_id}_branch"),
                    initialized.main_path_id,
                    initial.turn_id,
                    "memory-evaluation-branch",
                    "Memory evaluation branch",
                    "Verify Research Path isolation in Memory retrieval.",
                    branch_basis,
                    branch_basis,
                )
            )
            control.complete_turn(
                CompleteTurnCommand(
                    CommandId(f"cmd_{trial_id}_complete_initial"),
                    initial.turn_id,
                    TurnStatus.SUCCEEDED,
                )
            )

            source_messages = self._source_messages(
                fixture,
                control=control,
                trial_id=trial_id,
            )
            memory = MemoryService(SqliteMemoryRepository(connection), identities)
            outcomes: dict[str, Any] = {}
            memory_specs = {
                str(item["key"]): item for item in self._object_list(fixture, "memories")
            }
            for key, item in memory_specs.items():
                scope = str(item["scope"])
                path_id = {
                    "main_path": initialized.main_path_id,
                    "branch_path": branch.research_path_id,
                }.get(scope)
                outcomes[key] = memory.create(
                    CreateMemoryCommand(
                        CommandId(f"cmd_{trial_id}_memory_{key}"),
                        MemoryKind(str(item["kind"])),
                        str(item["title"]),
                        str(item["content"]),
                        MemoryOriginKind.EXPLICIT_USER,
                        (self._user_source(source_messages[str(item["source_key"])]),),
                        (
                            MemoryScopeKind.WORKSPACE
                            if scope == "workspace"
                            else MemoryScopeKind.RESEARCH_PATH
                        ),
                        path_id,
                    )
                )
            for relation in self._object_list(fixture, "supersessions"):
                old_key = str(relation["old_key"])
                successor_key = str(relation["successor_key"])
                memory.supersede(
                    SupersedeMemoryCommand(
                        CommandId(f"cmd_{trial_id}_supersede_{old_key}"),
                        outcomes[old_key].memory_item_id,
                        outcomes[successor_key].memory_item_id,
                        1,
                        "Checked-in evaluation fixture supersedes the old decision.",
                    )
                )

            files = FilesystemMemoryStore(database.root)
            files.synchronize(connection)
            recall = SqliteMemoryRecallRepository(
                connection,
                files,
                identities,
                embedding_gateway=_EvaluationEmbeddingGateway(),
            )
            report_payload, observations = self._evaluate_cases(
                fixture,
                recall=recall,
                outcomes=outcomes,
                memory_specs=memory_specs,
                paths={
                    "main_path": initialized.main_path_id.value,
                    "branch_path": branch.research_path_id.value,
                },
            )
            tamper_rejected = self._tampered_open_is_rejected(
                connection,
                database.root,
                recall,
                outcomes["style_note"],
                initialized.main_path_id.value,
            )
            report_payload["metrics"]["tampered_open_rejection_rate"] = float(
                tamper_rejected
            )
            if tamper_rejected:
                observations.add("memory.retrieval.tampered_open_rejected")
            else:
                observations.add("memory.retrieval.tampered_open_accepted")
            encoded_report = self._json(report_payload).encode("utf-8")
            report_path.write_bytes(encoded_report)
            self._export_trace(connection, trace_path)
            manifest_path.write_text(
                self._json(
                    {
                        "schema_version": "stata-research-agent.eval-artifacts/v1",
                        "artifacts": [
                            {
                                "role": "workspace_database",
                                "locator": "workspace/workspace.sqlite3",
                            },
                            {
                                "role": "memory_retrieval_report",
                                "locator": "memory-retrieval-report.json",
                            },
                            {"role": "trace", "locator": "trace.jsonl"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            return TrialBundle(
                trial_id,
                scenario.scenario_id,
                scenario.revision,
                scenario.scenario_sha256,
                workspace_root,
                database.database_path,
                trace_path,
                manifest_path,
                tuple(sorted(observations)),
                tuple(
                    f"memory_item:{outcome.memory_item_id.value}"
                    for outcome in outcomes.values()
                ),
                (
                    EvaluationPayloadReference(
                        "memory-retrieval-report",
                        "application/json",
                        report_path,
                        sha256(encoded_report).hexdigest(),
                    ),
                ),
            )
        finally:
            connection.close()

    def _load_fixture(self, scenario: EvaluationScenario) -> dict[str, Any]:
        fixture = next(
            (item for item in scenario.fixtures if item.fixture_id == "memory-retrieval-core-v2"),
            None,
        )
        if fixture is None:
            raise ProductEvaluationError("Memory retrieval fixture is unavailable")
        path = self._project_root / fixture.locator
        try:
            content = path.read_bytes()
            payload = json.loads(content)
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ProductEvaluationError("Memory retrieval fixture is unreadable") from error
        if sha256(content).hexdigest() != fixture.sha256:
            raise ProductEvaluationError("Memory retrieval fixture digest mismatch")
        if not isinstance(payload, dict) or payload.get("schema_version") != (
            "memory-retrieval-benchmark/v2"
        ):
            raise ProductEvaluationError("unsupported Memory retrieval fixture")
        return payload

    @staticmethod
    def _source_messages(
        fixture: dict[str, Any],
        *,
        control: WorkspaceControlService,
        trial_id: str,
    ) -> dict[str, SubmitMessageResult]:
        source_keys = tuple(
            dict.fromkeys(
                str(item["source_key"])
                for item in MemoryRetrievalEvaluationAdapter._object_list(fixture, "memories")
            )
        )
        messages: dict[str, SubmitMessageResult] = {}
        for ordinal, key in enumerate(source_keys, start=1):
            message = control.submit_message(
                SubmitMessageCommand(
                    CommandId(f"cmd_{trial_id}_source_{ordinal}"),
                    f"Evaluation source statement for {key}.",
                )
            )
            control.complete_turn(
                CompleteTurnCommand(
                    CommandId(f"cmd_{trial_id}_complete_source_{ordinal}"),
                    message.turn_id,
                    TurnStatus.SUCCEEDED,
                )
            )
            messages[key] = message
        return messages

    @staticmethod
    def _evaluate_cases(
        fixture: dict[str, Any],
        *,
        recall: SqliteMemoryRecallRepository,
        outcomes: dict[str, Any],
        memory_specs: dict[str, dict[str, Any]],
        paths: dict[str, str],
    ) -> tuple[dict[str, object], set[str]]:
        id_to_key = {
            outcome.memory_item_id.value: key for key, outcome in outcomes.items()
        }
        case_reports: list[dict[str, object]] = []
        positive_recalls: list[float] = []
        recalls_by_cutoff: dict[int, list[float]] = {1: [], 3: [], 5: []}
        reciprocal_ranks: list[float] = []
        exact_open_successes = 0
        exact_open_total = 0
        silence_results: list[float] = []
        supersession_accuracy = 0.0
        cross_path_precision = 0.0
        hybrid_semantic_recall = 0.0
        lexical_semantic_recall = 0.0
        source_neighbor_recall = 0.0
        observations: set[str] = set()

        for case in MemoryRetrievalEvaluationAdapter._object_list(fixture, "cases"):
            relevant = tuple(str(key) for key in case["relevant_keys"])
            forbidden = tuple(str(key) for key in case["forbidden_keys"])
            limit = int(case["k"])
            started = time.perf_counter()
            hits = recall.search(
                {
                    "query": str(case["query"]),
                    "retrieval_mode": str(case["retrieval_mode"]),
                    "limit": limit,
                },
                research_path_id=paths[str(case["research_path"])],
            )
            latency_ms = round((time.perf_counter() - started) * 1000.0, 3)
            returned_keys = tuple(
                id_to_key[str(hit["memory_item_id"])]
                for hit in hits
                if str(hit["memory_item_id"]) in id_to_key
            )
            relevant_returned = tuple(key for key in returned_keys if key in relevant)
            forbidden_returned = tuple(key for key in returned_keys if key in forbidden)
            recall_at_k = (
                len(set(relevant_returned)) / len(relevant) if relevant else None
            )
            reciprocal_rank = 0.0
            if relevant:
                ranks = [
                    index
                    for index, key in enumerate(returned_keys, start=1)
                    if key in relevant
                ]
                reciprocal_rank = 0.0 if not ranks else 1.0 / min(ranks)
                positive_recalls.append(float(recall_at_k))
                for cutoff, values in recalls_by_cutoff.items():
                    values.append(
                        len(set(returned_keys[:cutoff]).intersection(relevant)) / len(relevant)
                    )
                reciprocal_ranks.append(reciprocal_rank)
            else:
                silence_results.append(1.0 if not hits else 0.0)

            opened_keys: list[str] = []
            for key in relevant_returned:
                exact_open_total += 1
                opened = recall.open(
                    {"memory_item_ids": [outcomes[key].memory_item_id.value]},
                    research_path_id=paths[str(case["research_path"])],
                )
                if (
                    len(opened) == 1
                    and opened[0]["memory_revision_id"]
                    == outcomes[key].memory_revision_id.value
                    and opened[0]["content"] == str(memory_specs[key]["content"])
                ):
                    exact_open_successes += 1
                    opened_keys.append(key)

            case_id = str(case["case_id"])
            reasons = {
                id_to_key[str(hit["memory_item_id"])]: tuple(hit["retrieval_reasons"])
                for hit in hits
                if str(hit["memory_item_id"]) in id_to_key
            }
            if case_id == "semantic-endogeneity":
                hybrid_semantic_recall = float(recall_at_k or 0.0)
                lexical_hits = recall.search(
                    {
                        "query": str(case["query"]),
                        "retrieval_mode": "lexical",
                        "limit": limit,
                    },
                    research_path_id=paths[str(case["research_path"])],
                )
                lexical_keys = {
                    id_to_key[str(hit["memory_item_id"])]
                    for hit in lexical_hits
                    if str(hit["memory_item_id"]) in id_to_key
                }
                lexical_semantic_recall = len(lexical_keys.intersection(relevant)) / len(relevant)
            elif case_id == "superseded-tail-rule":
                supersession_accuracy = float(
                    "current_tail_rule" in returned_keys
                    and "old_tail_rule" not in returned_keys
                    and "superseded_match_forwarded"
                    in reasons.get("current_tail_rule", ())
                )
            elif case_id == "cross-path-isolation":
                cross_path_precision = float(not forbidden_returned)
            elif case_id == "shared-source-expansion":
                source_neighbor_recall = float(
                    set(relevant).issubset(returned_keys)
                    and "shared_source" in reasons.get("sample_robustness", ())
                )

            case_reports.append(
                {
                    "case_id": case_id,
                    "retrieval_mode": str(case["retrieval_mode"]),
                    "query": str(case["query"]),
                    "relevant_keys": list(relevant),
                    "returned_keys": list(returned_keys),
                    "forbidden_returned": list(forbidden_returned),
                    "opened_keys": opened_keys,
                    "recall_at_k": recall_at_k,
                    "reciprocal_rank": reciprocal_rank if relevant else None,
                    "relevant_ranks": ranks if relevant else [],
                    "candidate_count": len(hits),
                    "latency_ms": latency_ms,
                    "retrieval_reasons": {key: list(value) for key, value in reasons.items()},
                }
            )

        metrics = {
            "recall_at_k": sum(positive_recalls) / len(positive_recalls),
            "recall_at_1": sum(recalls_by_cutoff[1]) / len(recalls_by_cutoff[1]),
            "recall_at_3": sum(recalls_by_cutoff[3]) / len(recalls_by_cutoff[3]),
            "recall_at_5": sum(recalls_by_cutoff[5]) / len(recalls_by_cutoff[5]),
            "mean_reciprocal_rank": sum(reciprocal_ranks) / len(reciprocal_ranks),
            "irrelevant_silence_rate": sum(silence_results) / len(silence_results),
            "supersession_forwarding_accuracy": supersession_accuracy,
            "cross_path_precision": cross_path_precision,
            "exact_open_accuracy": exact_open_successes / exact_open_total,
            "hybrid_semantic_recall": hybrid_semantic_recall,
            "lexical_semantic_recall": lexical_semantic_recall,
            "hybrid_semantic_gain": hybrid_semantic_recall - lexical_semantic_recall,
            "source_neighbor_recall": source_neighbor_recall,
            "mean_search_latency_ms": sum(
                float(case["latency_ms"]) for case in case_reports
            )
            / len(case_reports),
        }
        if metrics["recall_at_k"] == 1.0:
            observations.add("memory.retrieval.recall_complete")
        if metrics["irrelevant_silence_rate"] == 1.0:
            observations.add("memory.retrieval.irrelevant_silent")
        else:
            observations.add("memory.retrieval.fabricated_hit")
        if supersession_accuracy == 1.0:
            observations.add("memory.retrieval.supersession_forwarded")
        else:
            observations.add("memory.retrieval.stale_returned")
        if cross_path_precision == 1.0:
            observations.add("memory.retrieval.cross_path_isolated")
        else:
            observations.add("memory.retrieval.cross_path_leak")
        if metrics["exact_open_accuracy"] == 1.0:
            observations.add("memory.retrieval.exact_open_verified")
        if metrics["hybrid_semantic_gain"] > 0:
            observations.add("memory.retrieval.hybrid_semantic_gain")
        if source_neighbor_recall == 1.0:
            observations.add("memory.retrieval.source_neighbor_expanded")
        return (
            {
                "schema_version": "stata-research-agent.memory-retrieval-report/v2",
                "dataset_id": str(fixture["dataset_id"]),
                "metrics": metrics,
                "counts": {
                    "cases": len(case_reports),
                    "positive_cases": len(positive_recalls),
                    "exact_opens": exact_open_total,
                },
                "cases": case_reports,
            },
            observations,
        )

    @staticmethod
    def _user_source(message: SubmitMessageResult) -> MemorySource:
        return MemorySource(
            "message",
            message.message_id.value,
            str(message.commit_revision.value),
            MemorySourceRole.USER_STATEMENT,
        )

    @staticmethod
    def _tampered_open_is_rejected(
        connection: Any,
        workspace_root: Path,
        recall: SqliteMemoryRecallRepository,
        outcome: Any,
        research_path_id: str,
    ) -> bool:
        row = connection.execute(
            """
            SELECT relative_path FROM memory_payload_files
            WHERE memory_revision_id = ?
            """,
            (outcome.memory_revision_id.value,),
        ).fetchone()
        if row is None:
            return False
        path = (workspace_root / str(row["relative_path"])).resolve()
        if not path.is_relative_to((workspace_root / ".stata-agent" / "memory").resolve()):
            return False
        original = path.read_bytes()
        try:
            path.write_bytes(original + b"\n[tampered]")
            try:
                recall.open(
                    {"memory_item_ids": [outcome.memory_item_id.value]},
                    research_path_id=research_path_id,
                )
            except ValueError:
                return True
            return False
        finally:
            path.write_bytes(original)

    @staticmethod
    def _object_list(payload: dict[str, Any], key: str) -> tuple[dict[str, Any], ...]:
        value = payload.get(key)
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise ProductEvaluationError(f"Memory retrieval fixture {key} is invalid")
        return tuple(value)

    @staticmethod
    def _validate_scenario(scenario: EvaluationScenario) -> None:
        if scenario.scenario_id != MemoryRetrievalEvaluationAdapter.scenario_id:
            raise ProductEvaluationError("Memory retrieval adapter received another scenario")
        if (
            scenario.level is not EvaluationLevel.SUBSYSTEM
            or scenario.subsystem_mode is not SubsystemEvaluationMode.INTRINSIC
            or "memory" not in scenario.subsystems
        ):
            raise ProductEvaluationError("Memory retrieval requires an intrinsic Memory scenario")

    @staticmethod
    def _export_trace(connection: _TraceReadableConnection, path: Path) -> None:
        rows = connection.execute(
            """
            SELECT journal_entry_id, workspace_revision, ordinal, event_type,
                   object_type, object_id, payload_json
            FROM journal_entries ORDER BY workspace_revision, ordinal
            """
        ).fetchall()
        path.write_text(
            "".join(
                json.dumps(
                    {
                        "journal_entry_id": str(row["journal_entry_id"]),
                        "workspace_revision": int(row["workspace_revision"]),
                        "ordinal": int(row["ordinal"]),
                        "event_type": str(row["event_type"]),
                        "object_type": str(row["object_type"]),
                        "object_id": str(row["object_id"]),
                        "payload": json.loads(str(row["payload_json"])),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
                for row in rows
            ),
            encoding="utf-8",
            newline="\n",
        )

    @staticmethod
    def _json(value: object) -> str:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        ) + "\n"


class MemoryRetrievalQualityGrader:
    """Grade persisted retrieval metrics against the scenario's versioned policy."""

    grader_id = "memory.retrieval_quality"
    revision = "memory-retrieval/v2"
    kind = GraderKind.OUTCOME

    def grade(
        self,
        scenario: EvaluationScenario,
        spec: GraderSpec,
        bundle: TrialBundle,
    ) -> EvaluationScore:
        del scenario
        payload = next(
            (
                item
                for item in bundle.evaluation_payloads
                if item.payload_id == "memory-retrieval-report"
            ),
            None,
        )
        if payload is None or not payload.path.is_file():
            raise ProductEvaluationError("Memory retrieval report is unavailable")
        content = payload.path.read_bytes()
        if sha256(content).hexdigest() != payload.sha256:
            raise ProductEvaluationError("Memory retrieval report digest mismatch")
        report = json.loads(content)
        metrics = report.get("metrics", {})
        config = json.loads(spec.config_json)
        requirements = {
            "recall_at_k": "minimum_recall_at_k",
            "mean_reciprocal_rank": "minimum_mrr",
            "irrelevant_silence_rate": "minimum_irrelevant_silence_rate",
            "supersession_forwarding_accuracy": (
                "minimum_supersession_forwarding_accuracy"
            ),
            "cross_path_precision": "minimum_cross_path_precision",
            "exact_open_accuracy": "minimum_exact_open_accuracy",
            "hybrid_semantic_recall": "minimum_hybrid_semantic_recall",
            "source_neighbor_recall": "minimum_source_neighbor_recall",
            "tampered_open_rejection_rate": "minimum_tampered_open_rejection_rate",
        }
        passed = all(
            float(metrics.get(metric, 0.0)) >= float(config[threshold])
            for metric, threshold in requirements.items()
        )
        return EvaluationScore(
            self.grader_id,
            self.revision,
            self.kind,
            spec.role,
            ScoreVerdict.PASS if passed else ScoreVerdict.FAIL,
            float(metrics.get("recall_at_k", 0.0)),
            json.dumps(
                {"metrics": metrics, "requirements": requirements},
                ensure_ascii=False,
                sort_keys=True,
            ),
            ("evaluation_payload:memory-retrieval-report",),
        )
