"""Real canonical-RAG Product Evaluation adapter and deterministic report grader."""

from __future__ import annotations

import json
import shutil
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from typing import Any, Protocol

from stata_research_agent.application.control import CreateWorkspaceCommand
from stata_research_agent.application.knowledge_retrieval import (
    ContinueKnowledgeRetrievalCommand,
    CorpusRole,
    KnowledgeRetrievalHit,
    RetrievalMode,
    StartKnowledgeRetrievalCommand,
)
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
from stata_research_agent.application.rag_evaluation import (
    RagAcceptanceThresholds,
    RagGoldCase,
    RagTraceGoldCase,
    RagTraceHop,
    evaluate_rag,
    evaluate_retrieval_trace,
)
from stata_research_agent.application.workspace_service import WorkspaceControlService
from stata_research_agent.domain.identifiers import CommandId, WorkspaceId
from stata_research_agent.interfaces.knowledge_runtime import (
    StataHelpIndexService,
    WorkspaceKnowledgeIndexService,
)
from stata_research_agent.persistence.control_store import SqliteControlStore
from stata_research_agent.persistence.knowledge_store import SqliteKnowledgeRepository
from stata_research_agent.persistence.workspace import WorkspaceDatabase
from stata_research_agent.runtime.uuid_identity import UuidIdentityGenerator


class _TraceReadableConnection(Protocol):
    def execute(self, statement: str) -> Any: ...


class RagIntrinsicEvaluationAdapter:
    scenario_id = "agent.rag.corpus-isolation"

    def __init__(self, project_root: Path) -> None:
        self._project_root = project_root.resolve()

    def execute(
        self,
        scenario: EvaluationScenario,
        *,
        trial_id: str,
        trial_root: Path,
    ) -> TrialBundle:
        self._validate_scenario(scenario)
        if trial_root.exists() and any(trial_root.iterdir()):
            raise ProductEvaluationError("evaluation Trial bundle directory is not empty")
        trial_root.mkdir(parents=True, exist_ok=True)
        fixtures = self._verified_fixtures(scenario)
        workspace_root = trial_root / "workspace"
        database = WorkspaceDatabase(
            workspace_root,
            WorkspaceId(f"ws_eval_{trial_id.replace('-', '_')}"),
        )
        database.create()
        literature = workspace_root / "literature"
        style = workspace_root / "style-references"
        help_root = trial_root / "stata-help"
        literature.mkdir()
        style.mkdir()
        help_root.mkdir()
        shutil.copy2(fixtures["rag_literature"], literature / "design.md")
        shutil.copy2(fixtures["rag_style"], style / "selected-paper.md")
        shutil.copy2(fixtures["rag_stata_help"], help_root / "regress.sthlp")

        connection = database.open(writable=True)
        trace_path = trial_root / "trace.jsonl"
        report_path = trial_root / "rag-report.json"
        artifact_manifest_path = trial_root / "artifact-manifest.json"
        try:
            identities = UuidIdentityGenerator()
            WorkspaceControlService(SqliteControlStore(connection), identities).create_workspace(
                CreateWorkspaceCommand(
                    CommandId(f"cmd_{trial_id}_workspace"), database.workspace_id
                )
            )
            repository = SqliteKnowledgeRepository(connection)
            WorkspaceKnowledgeIndexService(
                repository,
                workspace_root,
                identities,
            ).synchronize()
            StataHelpIndexService(
                repository,
                (("base", help_root),),
                identities,
            ).synchronize()
            cases = self._load_cases(fixtures["rag_gold_dataset"])

            def retrieve(
                query: str,
                roles: tuple[CorpusRole, ...],
                limit: int,
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
                        hit.dense_rank,
                        hit.dense_score,
                    )
                    for hit in repository.search_canonical(
                        query,
                        corpus_roles=roles,
                        limit=limit,
                    )
                )

            report = evaluate_rag(cases, retrieve)
            first_hop = repository.retrieve(
                StartKnowledgeRetrievalCommand(
                    identities.new(CommandId),
                    "parallel trends identifying assumption",
                    "Identify the assumption before examining its diagnostic.",
                    (CorpusRole.LITERATURE_EVIDENCE,),
                    RetrievalMode.MULTI_HOP,
                    3,
                )
            )
            second_hop = repository.continue_retrieval(
                ContinueKnowledgeRetrievalCommand(
                    identities.new(CommandId),
                    first_hop.retrieval_session_id,
                    "event-study leads pre-trend violations",
                    "Which diagnostic can reveal violations?",
                    3,
                    True,
                )
            )
            trace_score = evaluate_retrieval_trace(
                RagTraceGoldCase(
                    "literature-identification-multihop",
                    (CorpusRole.LITERATURE_EVIDENCE,),
                    (("parallel trends",), ("event-study leads", "pre-trend")),
                    minimum_hops=2,
                    maximum_hops=2,
                    minimum_novel_node_ratio=0.25,
                ),
                (
                    RagTraceHop(
                        "Identify the assumption.",
                        first_hop.hits,
                        first_hop.stop_reason,
                    ),
                    RagTraceHop(
                        "Identify the diagnostic.",
                        second_hop.hits,
                        second_hop.stop_reason,
                    ),
                ),
            )
            no_answer_hits = repository.search_canonical(
                "lunar cheese placebo galaxy",
                corpus_roles=(CorpusRole.LITERATURE_EVIDENCE,),
                limit=6,
            )
            report_payload = {
                "schema_version": "stata-research-agent.rag-evaluation-report/v1",
                **asdict(report),
                "multihop_trace": asdict(trace_score),
                "no_answer_hit_count": len(no_answer_hits),
                "no_answer_response": (
                    "NO_SUPPORTED_ANSWER" if not no_answer_hits else "EVIDENCE_FOUND"
                ),
            }
            encoded_report = self._json(report_payload).encode("utf-8")
            report_path.write_bytes(encoded_report)
            observations = {"rag.report_persisted"}
            thresholds = RagAcceptanceThresholds()
            if report.accepts(thresholds):
                observations.add("rag.retrieval_thresholds_met")
            if report.total_role_leaks == 0:
                observations.add("rag.corpus_roles_isolated")
            else:
                observations.add("rag.corpus_role_leak")
            if trace_score.passed:
                observations.add("rag.multihop_trace_valid")
            if not no_answer_hits:
                observations.add("rag.no_answer_abstained")
            else:
                observations.add("rag.no_answer_fabricated")
            self._export_trace(connection, trace_path)
            artifact_manifest_path.write_text(
                self._json(
                    {
                        "schema_version": "stata-research-agent.eval-artifacts/v1",
                        "artifacts": [
                            {"role": "rag_report", "locator": "rag-report.json"},
                            {"role": "trace", "locator": "trace.jsonl"},
                            {
                                "role": "workspace_database",
                                "locator": "workspace/workspace.sqlite3",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
                newline="\n",
            )
            return TrialBundle(
                trial_id,
                scenario.scenario_id,
                scenario.revision,
                scenario.scenario_sha256,
                workspace_root,
                database.database_path,
                trace_path,
                artifact_manifest_path,
                tuple(sorted(observations)),
                ("evaluation_payload:rag-report",),
                (
                    EvaluationPayloadReference(
                        "rag-report",
                        "application/json",
                        report_path,
                        sha256(encoded_report).hexdigest(),
                    ),
                ),
            )
        finally:
            connection.close()

    def _verified_fixtures(self, scenario: EvaluationScenario) -> dict[str, Path]:
        fixtures: dict[str, Path] = {}
        for fixture in scenario.fixtures:
            path = (self._project_root / fixture.locator).resolve()
            if not path.is_relative_to(self._project_root) or not path.is_file():
                raise ProductEvaluationError("RAG fixture is unavailable")
            if sha256(path.read_bytes()).hexdigest() != fixture.sha256:
                raise ProductEvaluationError("RAG fixture digest mismatch")
            fixtures[fixture.role] = path
        required = {
            "rag_gold_dataset",
            "rag_literature",
            "rag_style",
            "rag_stata_help",
        }
        if not required <= fixtures.keys():
            raise ProductEvaluationError("RAG scenario is missing a required fixture role")
        return fixtures

    @staticmethod
    def _load_cases(path: Path) -> tuple[RagGoldCase, ...]:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if raw.get("schema_version") != "rag-gold-v1" or not isinstance(
            raw.get("cases"), list
        ):
            raise ProductEvaluationError("RAG gold fixture schema is invalid")
        return tuple(
            RagGoldCase(
                str(item["case_id"]),
                str(item["query"]),
                tuple(CorpusRole(role) for role in item["corpus_roles"]),
                tuple(item.get("expected_source_locators", ())),
                tuple(item.get("expected_content_markers", ())),
                tuple(tuple(group) for group in item.get("evidence_groups", ())),
                int(item.get("k", 6)),
            )
            for item in raw["cases"]
        )

    @staticmethod
    def _validate_scenario(scenario: EvaluationScenario) -> None:
        if scenario.scenario_id != RagIntrinsicEvaluationAdapter.scenario_id:
            raise ProductEvaluationError("RAG adapter received an unsupported scenario")
        if (
            scenario.level is not EvaluationLevel.SUBSYSTEM
            or scenario.subsystem_mode is not SubsystemEvaluationMode.INTRINSIC
            or "rag" not in scenario.subsystems
        ):
            raise ProductEvaluationError("RAG adapter requires an intrinsic RAG scenario")

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


class RagRetrievalReportGrader:
    grader_id = "rag.retrieval_report"
    revision = "rag-report/v1"
    kind = GraderKind.OUTCOME

    def grade(
        self,
        scenario: EvaluationScenario,
        spec: GraderSpec,
        bundle: TrialBundle,
    ) -> EvaluationScore:
        del scenario
        payload = next(
            (item for item in bundle.evaluation_payloads if item.payload_id == "rag-report"),
            None,
        )
        if payload is None or not payload.path.is_file():
            raise ProductEvaluationError("RAG report payload is unavailable")
        content = payload.path.read_bytes()
        if sha256(content).hexdigest() != payload.sha256:
            raise ProductEvaluationError("RAG report payload digest mismatch")
        report = json.loads(content)
        config = json.loads(spec.config_json)
        passed = (
            report["macro_recall_at_k"] >= config["minimum_macro_recall_at_k"]
            and report["macro_precision_at_k"]
            >= config["minimum_macro_precision_at_k"]
            and report["mean_reciprocal_rank"]
            >= config["minimum_mean_reciprocal_rank"]
            and report["macro_evidence_group_coverage"]
            >= config["minimum_evidence_group_coverage"]
            and report["total_role_leaks"] <= config["maximum_role_leaks"]
            and report["multihop_trace"]["passed"] is True
            and report["no_answer_hit_count"] == 0
            and report["no_answer_response"] == "NO_SUPPORTED_ANSWER"
        )
        return EvaluationScore(
            self.grader_id,
            self.revision,
            self.kind,
            spec.role,
            ScoreVerdict.PASS if passed else ScoreVerdict.FAIL,
            passed,
            json.dumps(
                {
                    "macro_recall_at_k": report["macro_recall_at_k"],
                    "macro_precision_at_k": report["macro_precision_at_k"],
                    "mean_reciprocal_rank": report["mean_reciprocal_rank"],
                    "total_role_leaks": report["total_role_leaks"],
                    "multihop_passed": report["multihop_trace"]["passed"],
                    "no_answer_hit_count": report["no_answer_hit_count"],
                },
                sort_keys=True,
            ),
            ("evaluation_payload:rag-report",),
        )
