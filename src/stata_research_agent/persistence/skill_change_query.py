"""Read model for human-reviewed evaluation-to-Skill change candidates."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SkillChangeCandidateSnapshot:
    candidate_id: str
    source_proposal_id: str
    change_kind: str
    skill_name: str
    base_skill_version_id: str
    merge_source_skill_version_id: str | None
    proposed_version: str
    description: str
    instruction_body: str
    rationale: str
    validation_status: str
    validation_findings: tuple[str, ...]
    lifecycle: str
    pointer_revision: int
    created_revision: int
    updated_revision: int
    activated_skill_version_id: str | None


class SqliteSkillChangeQuery:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def index(self) -> tuple[SkillChangeCandidateSnapshot, ...]:
        rows = self._connection.execute(
            """
            SELECT candidate.*, state.lifecycle, state.pointer_revision,
                   state.updated_revision, source.skill_version_id AS activated_skill_version_id
            FROM skill_change_candidates AS candidate
            JOIN skill_change_current_states AS state USING (skill_change_candidate_id)
            LEFT JOIN skill_version_change_sources AS source USING (skill_change_candidate_id)
            ORDER BY candidate.created_revision DESC, candidate.skill_change_candidate_id
            """
        ).fetchall()
        return tuple(
            SkillChangeCandidateSnapshot(
                str(row["skill_change_candidate_id"]),
                str(row["source_proposal_id"]),
                str(row["change_kind"]),
                str(row["skill_name"]),
                str(row["base_skill_version_id"]),
                None
                if row["merge_source_skill_version_id"] is None
                else str(row["merge_source_skill_version_id"]),
                str(row["proposed_version"]),
                str(row["description"]),
                str(row["instruction_body"]),
                str(row["rationale"]),
                str(row["validation_status"]),
                tuple(str(item) for item in json.loads(str(row["validation_findings_json"]))),
                str(row["lifecycle"]),
                int(row["pointer_revision"]),
                int(row["created_revision"]),
                int(row["updated_revision"]),
                None
                if row["activated_skill_version_id"] is None
                else str(row["activated_skill_version_id"]),
            )
            for row in rows
        )
