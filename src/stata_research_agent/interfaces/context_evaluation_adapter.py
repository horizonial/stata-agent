"""Intrinsic Product Evaluation adapter for deterministic Context compilation."""

from __future__ import annotations

import json
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from typing import Any, Protocol

from stata_research_agent.application.context_compiler import (
    ContextAuthorityReader,
    ContextBudgetExceeded,
    ContextCompiler,
    ContextPrivacyBoundary,
    ContextSourceCandidate,
)
from stata_research_agent.application.control import CreateWorkspaceCommand
from stata_research_agent.application.model_gateway import ContextItemCandidate
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
from stata_research_agent.application.workspace_service import WorkspaceControlService
from stata_research_agent.domain.identifiers import CommandId, TurnId, WorkspaceId
from stata_research_agent.persistence.control_store import SqliteControlStore
from stata_research_agent.persistence.workspace import WorkspaceDatabase
from stata_research_agent.runtime.uuid_identity import UuidIdentityGenerator


class _TraceReadableConnection(Protocol):
    def execute(self, statement: str) -> Any: ...


class _FixedContextAuthority(ContextAuthorityReader):
    def __init__(self, candidates: tuple[ContextSourceCandidate, ...]) -> None:
        self._candidates = candidates

    def collect(self, turn_id: TurnId) -> tuple[ContextSourceCandidate, ...]:
        del turn_id
        return self._candidates


def _item(
    kind: str,
    object_type: str,
    object_id: str,
    revision: str,
    transmission: str,
    content: str,
) -> ContextItemCandidate:
    return ContextItemCandidate(
        kind,
        object_type,
        object_id,
        revision,
        transmission,
        content,
    )


class ContextCompilerEvaluationAdapter:
    scenario_id = "agent.context.compiler-boundaries"

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
        self._verify_fixture(scenario)
        if trial_root.exists() and any(trial_root.iterdir()):
            raise ProductEvaluationError("evaluation Trial bundle directory is not empty")
        trial_root.mkdir(parents=True, exist_ok=True)
        workspace_root = trial_root / "workspace"
        database = WorkspaceDatabase(
            workspace_root,
            WorkspaceId(f"ws_eval_{trial_id.replace('-', '_')}"),
        )
        database.create()
        connection = database.open(writable=True)
        trace_path = trial_root / "trace.jsonl"
        report_path = trial_root / "context-report.json"
        artifact_manifest_path = trial_root / "artifact-manifest.json"
        try:
            WorkspaceControlService(
                SqliteControlStore(connection), UuidIdentityGenerator()
            ).create_workspace(
                CreateWorkspaceCommand(
                    CommandId(f"cmd_{trial_id}_workspace"), database.workspace_id
                )
            )
            turn_id = TurnId(f"turn_{trial_id}")
            candidates = self._candidates()
            compiled = ContextCompiler(
                _FixedContextAuthority(candidates),
                input_token_budget=220,
                summary_token_target=64,
            ).compile(
                turn_id,
                static_items=(
                    _item(
                        "user_message",
                        "message",
                        "msg_context_user",
                        "1",
                        "remote_allowed",
                        scenario.initial_user_message,
                    ),
                ),
                remote_provider=True,
            )
            included = {item.source_object_id: item for item in compiled.items}
            decisions = {
                decision.source_object_id: decision for decision in compiled.decisions
            }
            checks = {
                "user_message_preserved": "msg_context_user" in included,
                "research_constraint_preserved": "memory_constraint" in included,
                "newest_duplicate_selected": (
                    included.get("memory_decision") is not None
                    and included["memory_decision"].source_revision == "2"
                ),
                "local_only_excluded_remote": (
                    "artifact_local" not in included
                    and decisions["artifact_local"].reason_code == "provider_policy"
                ),
                "decisions_cover_sources": len(decisions) == 6,
                "privacy_fail_closed": self._privacy_fails_closed(turn_id),
                "mandatory_budget_fail_closed": self._budget_fails_closed(turn_id),
            }
            observations = {
                f"context.{name}" for name, passed in checks.items() if passed
            }
            if "artifact_local" in included:
                observations.add("context.local_only_transmitted")
            if included.get("memory_decision", None) is not None and (
                included["memory_decision"].source_revision == "1"
            ):
                observations.add("context.stale_duplicate_selected")
            if not checks["research_constraint_preserved"]:
                observations.add("context.silent_mandatory_drop")
            report_payload = {
                "schema_version": "stata-research-agent.context-evaluation-report/v1",
                "checks": checks,
                "estimated_input_tokens": compiled.estimated_input_tokens,
                "included_items": [asdict(item) for item in compiled.items],
                "build_decisions": [asdict(decision) for decision in compiled.decisions],
            }
            encoded_report = self._json(report_payload).encode("utf-8")
            report_path.write_bytes(encoded_report)
            observations.add("context.report_persisted")
            self._export_trace(connection, trace_path)
            artifact_manifest_path.write_text(
                self._json(
                    {
                        "schema_version": "stata-research-agent.eval-artifacts/v1",
                        "artifacts": [
                            {"role": "context_report", "locator": "context-report.json"},
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
                ("evaluation_payload:context-report",),
                (
                    EvaluationPayloadReference(
                        "context-report",
                        "application/json",
                        report_path,
                        sha256(encoded_report).hexdigest(),
                    ),
                ),
            )
        finally:
            connection.close()

    @staticmethod
    def _candidates() -> tuple[ContextSourceCandidate, ...]:
        return (
            ContextSourceCandidate(
                _item(
                    "memory",
                    "memory_revision",
                    "memory_constraint",
                    "1",
                    "remote_allowed",
                    "Cluster standard errors by repair-record group.",
                ),
                0,
                "active_research_constraint",
                mandatory=True,
            ),
            ContextSourceCandidate(
                _item(
                    "memory",
                    "memory_revision",
                    "memory_decision",
                    "1",
                    "remote_allowed",
                    "Use log price as the outcome.",
                ),
                1,
                "older_memory",
                recency=1,
            ),
            ContextSourceCandidate(
                _item(
                    "memory",
                    "memory_revision",
                    "memory_decision",
                    "2",
                    "remote_allowed",
                    "Keep price in levels as the outcome.",
                ),
                1,
                "corrected_memory",
                recency=2,
            ),
            ContextSourceCandidate(
                _item(
                    "artifact_preview",
                    "artifact",
                    "artifact_local",
                    "1",
                    "local_only",
                    "Local-only raw rows must never leave this machine.",
                ),
                1,
                "local_data_preview",
            ),
            ContextSourceCandidate(
                _item(
                    "knowledge_chunk",
                    "knowledge_node",
                    "knowledge_long",
                    "1",
                    "remote_allowed",
                    "Parallel trends evidence. " * 80,
                ),
                2,
                "retrieval_support",
                summarizable=True,
            ),
            ContextSourceCandidate(
                _item(
                    "old_trace",
                    "journal_entry",
                    "journal_low_priority",
                    "1",
                    "remote_allowed",
                    "Low priority historical trace. " * 80,
                ),
                3,
                "historical_trace",
            ),
        )

    @staticmethod
    def _privacy_fails_closed(turn_id: TurnId) -> bool:
        compiler = ContextCompiler(_FixedContextAuthority(()), input_token_budget=256)
        try:
            compiler.compile(
                turn_id,
                transient_items=(
                    _item(
                        "tool_result",
                        "artifact",
                        "artifact_mandatory_local",
                        "1",
                        "local_only",
                        "Mandatory local-only result.",
                    ),
                ),
                remote_provider=True,
            )
        except ContextPrivacyBoundary:
            return True
        return False

    @staticmethod
    def _budget_fails_closed(turn_id: TurnId) -> bool:
        compiler = ContextCompiler(_FixedContextAuthority(()), input_token_budget=64)
        try:
            compiler.compile(
                turn_id,
                static_items=(
                    _item(
                        "user_message",
                        "message",
                        "msg_oversized",
                        "1",
                        "remote_allowed",
                        "Mandatory user instruction. " * 80,
                    ),
                ),
            )
        except ContextBudgetExceeded:
            return True
        return False

    @staticmethod
    def _validate_scenario(scenario: EvaluationScenario) -> None:
        if scenario.scenario_id != ContextCompilerEvaluationAdapter.scenario_id:
            raise ProductEvaluationError("Context adapter received an unsupported scenario")
        if (
            scenario.level is not EvaluationLevel.SUBSYSTEM
            or scenario.subsystem_mode is not SubsystemEvaluationMode.INTRINSIC
            or "context_compiler" not in scenario.subsystems
        ):
            raise ProductEvaluationError("Context adapter requires an intrinsic Context scenario")

    def _verify_fixture(self, scenario: EvaluationScenario) -> None:
        fixture = next(
            (
                item
                for item in scenario.fixtures
                if item.role == "context_gold_dataset"
            ),
            None,
        )
        if fixture is None:
            raise ProductEvaluationError("Context scenario is missing its gold fixture")
        path = (self._project_root / fixture.locator).resolve()
        if not path.is_relative_to(self._project_root) or not path.is_file():
            raise ProductEvaluationError("Context gold fixture is unavailable")
        if sha256(path.read_bytes()).hexdigest() != fixture.sha256:
            raise ProductEvaluationError("Context gold fixture digest mismatch")

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
                json.dumps(dict(row), ensure_ascii=False, sort_keys=True) + "\n"
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


class ContextCompilerReportGrader:
    grader_id = "context.compiler_report"
    revision = "context-compiler-report/v1"
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
                if item.payload_id == "context-report"
            ),
            None,
        )
        if payload is None or not payload.path.is_file():
            raise ProductEvaluationError("Context report payload is unavailable")
        content = payload.path.read_bytes()
        if sha256(content).hexdigest() != payload.sha256:
            raise ProductEvaluationError("Context report payload digest mismatch")
        report = json.loads(content)
        config = json.loads(spec.config_json)
        checks = report.get("checks", {})
        passed = (
            isinstance(checks, dict)
            and len(checks) == config["required_check_count"]
            and all(value is True for value in checks.values())
        )
        return EvaluationScore(
            self.grader_id,
            self.revision,
            self.kind,
            spec.role,
            ScoreVerdict.PASS if passed else ScoreVerdict.FAIL,
            passed,
            json.dumps(checks, sort_keys=True),
            ("evaluation_payload:context-report",),
        )
