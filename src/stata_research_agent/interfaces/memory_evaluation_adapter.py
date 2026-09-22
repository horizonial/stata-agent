"""Real intrinsic Product Evaluation adapter for Project Memory correction semantics."""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Any, Protocol

from stata_research_agent.application.control import (
    CompleteTurnCommand,
    CreateWorkspaceCommand,
    SubmitMessageCommand,
    SubmitMessageResult,
)
from stata_research_agent.application.memory import (
    CreateMemoryCommand,
    MemoryKind,
    MemoryOriginKind,
    MemorySource,
    MemorySourceRole,
    RetractMemoryCommand,
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
from stata_research_agent.application.workspace_service import WorkspaceControlService
from stata_research_agent.domain.identifiers import CommandId, WorkspaceId
from stata_research_agent.domain.status import TurnStatus
from stata_research_agent.persistence.control_store import SqliteControlStore
from stata_research_agent.persistence.memory_query import MemoryItemSnapshot, SqliteMemoryQuery
from stata_research_agent.persistence.memory_store import SqliteMemoryRepository
from stata_research_agent.persistence.workspace import WorkspaceDatabase
from stata_research_agent.runtime.uuid_identity import UuidIdentityGenerator


class _TraceReadableConnection(Protocol):
    def execute(self, statement: str) -> Any: ...


class MemoryCorrectionEvaluationAdapter:
    """Exercise the authoritative Memory store instead of scoring hand-written predictions."""

    scenario_id = "agent.memory.correction"

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
        workspace_root = trial_root / "workspace"
        workspace_id = WorkspaceId(f"ws_eval_{trial_id.replace('-', '_')}")
        database = WorkspaceDatabase(workspace_root, workspace_id)
        database.create()
        connection = database.open(writable=True)
        trace_path = trial_root / "trace.jsonl"
        report_path = trial_root / "memory-report.json"
        artifact_manifest_path = trial_root / "artifact-manifest.json"
        try:
            identities = UuidIdentityGenerator()
            control = WorkspaceControlService(SqliteControlStore(connection), identities)
            control.create_workspace(
                CreateWorkspaceCommand(CommandId(f"cmd_{trial_id}_workspace"), workspace_id)
            )
            initial = control.submit_message(
                SubmitMessageCommand(
                    CommandId(f"cmd_{trial_id}_initial"),
                    scenario.initial_user_message,
                )
            )
            memory = MemoryService(SqliteMemoryRepository(connection), identities)
            original = memory.create(
                CreateMemoryCommand(
                    CommandId(f"cmd_{trial_id}_old_memory"),
                    MemoryKind.RESEARCH_DECISION,
                    "Outcome transformation",
                    "Use log price as the primary outcome.",
                    MemoryOriginKind.EXPLICIT_USER,
                    (self._user_source(initial),),
                )
            )
            control.complete_turn(
                CompleteTurnCommand(
                    CommandId(f"cmd_{trial_id}_complete_initial"),
                    initial.turn_id,
                    TurnStatus.SUCCEEDED,
                )
            )
            correction_text = next(
                event.content
                for event in scenario.interaction
                if event.event_kind == "user_message"
            )
            correction = control.submit_message(
                SubmitMessageCommand(
                    CommandId(f"cmd_{trial_id}_correction"),
                    correction_text,
                )
            )
            memory.retract(
                RetractMemoryCommand(
                    CommandId(f"cmd_{trial_id}_retract"),
                    original.memory_item_id,
                    original.pointer_revision,
                    "The user replaced the outcome transformation decision.",
                )
            )
            memory.create(
                CreateMemoryCommand(
                    CommandId(f"cmd_{trial_id}_new_memory"),
                    MemoryKind.RESEARCH_DECISION,
                    "Outcome transformation",
                    "Keep price in levels as the primary outcome.",
                    MemoryOriginKind.EXPLICIT_USER,
                    (self._user_source(correction),),
                )
            )

            snapshot = SqliteMemoryQuery(connection).index()
            observations = self._observations(
                snapshot.items,
                original.memory_item_id.value,
                correction.message_id.value,
            )
            checks = {
                "current_decision_correct": "memory.current_decision_correct" in observations,
                "stale_decision_inactive": "memory.stale_decision_inactive" in observations,
                "source_message_linked": "memory.source_message_linked" in observations,
                "no_assistant_invention_active": (
                    "memory.assistant_invention_active" not in observations
                ),
                "no_stale_decision_reactivated": (
                    "memory.stale_decision_reactivated" not in observations
                ),
            }
            active_decisions = tuple(
                item
                for item in snapshot.items
                if item.kind == MemoryKind.RESEARCH_DECISION.value and item.lifecycle == "active"
            )
            report_payload = {
                "schema_version": "stata-research-agent.memory-evaluation-report/v1",
                "checks": checks,
                "metrics": {
                    "activation_precision": (
                        1.0
                        if checks["current_decision_correct"] and len(active_decisions) == 1
                        else 0.0
                    ),
                    "source_accuracy": 1.0 if checks["source_message_linked"] else 0.0,
                    "stale_suppression_rate": (1.0 if checks["stale_decision_inactive"] else 0.0),
                    "false_active_memory_count": sum(
                        1
                        for key in (
                            "memory.assistant_invention_active",
                            "memory.stale_decision_reactivated",
                        )
                        if key in observations
                    ),
                },
                "counts": {
                    "memory_items": len(snapshot.items),
                    "active_research_decisions": len(active_decisions),
                },
            }
            encoded_report = self._json(report_payload).encode("utf-8")
            report_path.write_bytes(encoded_report)
            self._export_trace(connection, trace_path)
            artifact_manifest_path.write_text(
                json.dumps(
                    {
                        "schema_version": "stata-research-agent.eval-artifacts/v1",
                        "artifacts": [
                            {
                                "role": "workspace_database",
                                "locator": "workspace/workspace.sqlite3",
                            },
                            {"role": "memory_report", "locator": "memory-report.json"},
                            {"role": "trace", "locator": "trace.jsonl"},
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
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
                artifact_manifest_path,
                tuple(sorted(observations)),
                (
                    f"memory_item:{original.memory_item_id.value}",
                    f"message:{correction.message_id.value}",
                ),
                (
                    EvaluationPayloadReference(
                        "memory-report",
                        "application/json",
                        report_path,
                        sha256(encoded_report).hexdigest(),
                    ),
                ),
            )
        finally:
            connection.close()

    @staticmethod
    def _validate_scenario(scenario: EvaluationScenario) -> None:
        if scenario.scenario_id != MemoryCorrectionEvaluationAdapter.scenario_id:
            raise ProductEvaluationError("Memory adapter received an unsupported scenario")
        if (
            scenario.level is not EvaluationLevel.SUBSYSTEM
            or scenario.subsystem_mode is not SubsystemEvaluationMode.INTRINSIC
            or "memory" not in scenario.subsystems
        ):
            raise ProductEvaluationError("Memory correction requires an intrinsic Memory scenario")
        if not any(event.event_kind == "user_message" for event in scenario.interaction):
            raise ProductEvaluationError("Memory correction scenario has no correction message")

    @staticmethod
    def _user_source(message: SubmitMessageResult) -> MemorySource:
        return MemorySource(
            "message",
            message.message_id.value,
            str(message.commit_revision.value),
            MemorySourceRole.USER_STATEMENT,
        )

    @staticmethod
    def _observations(
        items: tuple[MemoryItemSnapshot, ...],
        original_memory_item_id: str,
        correction_message_id: str,
    ) -> set[str]:
        observations: set[str] = set()
        original = next(item for item in items if item.memory_item_id == original_memory_item_id)
        active_decisions = tuple(
            item
            for item in items
            if item.kind == MemoryKind.RESEARCH_DECISION.value and item.lifecycle == "active"
        )
        current = next(
            (
                item
                for item in active_decisions
                if "price" in item.content.casefold() and "levels" in item.content.casefold()
            ),
            None,
        )
        if current is not None:
            observations.add("memory.current_decision_correct")
            if any(
                source.object_type == "message"
                and source.object_id == correction_message_id
                and source.role == MemorySourceRole.USER_STATEMENT.value
                for source in current.sources
            ):
                observations.add("memory.source_message_linked")
        if original.lifecycle == "retracted":
            observations.add("memory.stale_decision_inactive")
        else:
            observations.add("memory.stale_decision_reactivated")
        if any("assistant" in item.origin for item in active_decisions):
            observations.add("memory.assistant_invention_active")
        return observations

    @staticmethod
    def _export_trace(connection: _TraceReadableConnection, path: Path) -> None:
        rows = connection.execute(
            """
            SELECT journal_entry_id, workspace_revision, ordinal, event_type,
                   object_type, object_id, payload_json
            FROM journal_entries
            ORDER BY workspace_revision, ordinal
            """
        ).fetchall()
        payload = "".join(
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
        )
        path.write_text(payload, encoding="utf-8", newline="\n")

    @staticmethod
    def _json(value: object) -> str:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )


class MemoryIntrinsicQualityGrader:
    """Grade Memory quality from the persisted report, not adapter self-assertion."""

    grader_id = "memory.intrinsic_quality"
    revision = "memory-intrinsic/v1"
    kind = GraderKind.OUTCOME

    def grade(
        self,
        scenario: EvaluationScenario,
        spec: GraderSpec,
        bundle: TrialBundle,
    ) -> EvaluationScore:
        del scenario
        payload = next(
            (item for item in bundle.evaluation_payloads if item.payload_id == "memory-report"),
            None,
        )
        if payload is None or not payload.path.is_file():
            raise ProductEvaluationError("Memory report payload is unavailable")
        content = payload.path.read_bytes()
        if sha256(content).hexdigest() != payload.sha256:
            raise ProductEvaluationError("Memory report payload digest mismatch")
        report = json.loads(content)
        config = json.loads(spec.config_json)
        metrics = report.get("metrics", {})
        checks = report.get("checks", {})
        passed = bool(
            metrics.get("activation_precision", 0.0) >= config["minimum_activation_precision"]
            and metrics.get("source_accuracy", 0.0) >= config["minimum_source_accuracy"]
            and checks
            and all(value is True for value in checks.values())
        )
        return EvaluationScore(
            self.grader_id,
            self.revision,
            self.kind,
            spec.role,
            ScoreVerdict.PASS if passed else ScoreVerdict.FAIL,
            metrics.get("activation_precision"),
            json.dumps(
                {"metrics": metrics, "checks": checks},
                ensure_ascii=False,
                sort_keys=True,
            ),
            ("evaluation_payload:memory-report",),
        )
