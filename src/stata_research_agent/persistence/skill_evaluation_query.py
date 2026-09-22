"""Read model for independent Skill evaluations and advisory proposals."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any, cast

from stata_research_agent.application.skill_evaluation import (
    SkillEvaluationOutcome,
    SkillEvaluationVerdict,
    SkillImprovementKind,
    SkillImprovementProposal,
)
from stata_research_agent.domain.identifiers import (
    SkillEvaluationRunId,
    SkillImprovementProposalId,
    SkillVersionId,
)
from stata_research_agent.domain.revisions import WorkspaceRevision


@dataclass(frozen=True, slots=True)
class SkillEvaluationSnapshot:
    run_id: str
    status: str
    skill_name: str
    candidate_skill_version_id: str
    baseline_skill_version_id: str | None
    evaluation_kind: str
    policy_revision: str
    source_start_revision: int
    source_end_revision: int
    evidence: dict[str, Any]
    verdict: str | None
    rationale: str | None
    limitations: tuple[str, ...]
    proposals: tuple[SkillImprovementProposal, ...]
    created_revision: int
    updated_revision: int


class SqliteSkillEvaluationQuery:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def index(self, *, skill_name: str | None = None) -> tuple[SkillEvaluationSnapshot, ...]:
        where = "" if skill_name is None else "WHERE run.skill_name = ?"
        parameters: tuple[object, ...] = () if skill_name is None else (skill_name,)
        rows = self._connection.execute(
            f"""
            SELECT run.*, report.verdict, report.rationale, report.limitations_json
            FROM skill_evaluation_runs AS run
            LEFT JOIN skill_evaluation_reports AS report USING (skill_evaluation_run_id)
            {where}
            ORDER BY run.updated_revision DESC, run.skill_evaluation_run_id
            """,
            parameters,
        ).fetchall()
        return tuple(self._snapshot(row) for row in rows)

    def by_run(self, run_id: SkillEvaluationRunId) -> SkillEvaluationSnapshot:
        row = self._connection.execute(
            """
            SELECT run.*, report.verdict, report.rationale, report.limitations_json
            FROM skill_evaluation_runs AS run
            LEFT JOIN skill_evaluation_reports AS report USING (skill_evaluation_run_id)
            WHERE run.skill_evaluation_run_id = ?
            """,
            (run_id.value,),
        ).fetchone()
        if row is None:
            raise ValueError("Skill evaluation run does not exist")
        return self._snapshot(row)

    def outcome(self, run_id: SkillEvaluationRunId, *, replayed: bool) -> SkillEvaluationOutcome:
        item = self.by_run(run_id)
        return SkillEvaluationOutcome(
            SkillEvaluationRunId(item.run_id),
            item.status,
            item.skill_name,
            SkillVersionId(item.candidate_skill_version_id),
            None
            if item.baseline_skill_version_id is None
            else SkillVersionId(item.baseline_skill_version_id),
            item.evaluation_kind,
            cast(SkillEvaluationVerdict | None, item.verdict),
            item.rationale,
            item.limitations,
            item.proposals,
            WorkspaceRevision(item.updated_revision),
            replayed,
        )

    def _snapshot(self, row: sqlite3.Row) -> SkillEvaluationSnapshot:
        proposal_rows = self._connection.execute(
            """
            SELECT * FROM skill_improvement_proposals
            WHERE skill_evaluation_run_id = ?
            ORDER BY created_revision, skill_improvement_proposal_id
            """,
            (str(row["skill_evaluation_run_id"]),),
        ).fetchall()
        proposals = tuple(
            SkillImprovementProposal(
                SkillImprovementProposalId(str(item["skill_improvement_proposal_id"])),
                cast(SkillImprovementKind, str(item["proposal_kind"])),
                str(item["title"]),
                str(item["rationale"]),
                None
                if item["suggested_instruction_body"] is None
                else str(item["suggested_instruction_body"]),
                None
                if item["merge_target_skill_name"] is None
                else str(item["merge_target_skill_name"]),
            )
            for item in proposal_rows
        )
        raw_evidence = json.loads(str(row["evidence_json"]))
        if not isinstance(raw_evidence, dict):
            raise ValueError("Skill evaluation evidence is not an object")
        raw_limitations = (
            [] if row["limitations_json"] is None else json.loads(str(row["limitations_json"]))
        )
        return SkillEvaluationSnapshot(
            str(row["skill_evaluation_run_id"]),
            str(row["status"]),
            str(row["skill_name"]),
            str(row["candidate_skill_version_id"]),
            None
            if row["baseline_skill_version_id"] is None
            else str(row["baseline_skill_version_id"]),
            str(row["evaluation_kind"]),
            str(row["policy_revision"]),
            int(row["source_start_revision"]),
            int(row["source_end_revision"]),
            cast(dict[str, Any], raw_evidence),
            None if row["verdict"] is None else str(row["verdict"]),
            None if row["rationale"] is None else str(row["rationale"]),
            tuple(str(item) for item in raw_limitations),
            proposals,
            int(row["created_revision"]),
            int(row["updated_revision"]),
        )
