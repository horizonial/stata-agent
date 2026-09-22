"""Read model for reviewable Memory-to-Skill evolution candidates."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SkillEvolutionCandidateSnapshot:
    candidate_id: str
    skill_name: str
    proposed_version: str
    description: str
    instruction_body: str
    rationale: str
    policy_revision: str
    validation_status: str
    validation_findings: tuple[str, ...]
    lifecycle: str
    pointer_revision: int
    source_memory_item_ids: tuple[str, ...]
    created_revision: int
    updated_revision: int
    relative_skill_path: str | None


@dataclass(frozen=True, slots=True)
class SkillVersionSnapshot:
    skill_version_id: str
    version_label: str
    content_sha256: str
    source_candidate_id: str | None
    predecessor_skill_version_id: str | None
    created_revision: int
    outcome_observation_count: int


@dataclass(frozen=True, slots=True)
class SkillAdoptionSnapshot:
    skill_name: str
    lifecycle: str
    current_skill_version_id: str | None
    pointer_revision: int
    updated_revision: int
    versions: tuple[SkillVersionSnapshot, ...]


class SqliteSkillEvolutionQuery:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def index(self) -> tuple[int, tuple[SkillEvolutionCandidateSnapshot, ...]]:
        rows = self._connection.execute(
            """
            SELECT candidate.*, state.lifecycle, state.pointer_revision,
                   state.updated_revision, manifest.relative_skill_path
            FROM skill_evolution_candidates AS candidate
            JOIN skill_evolution_current_states AS state
              USING (skill_evolution_candidate_id)
            LEFT JOIN skill_activation_manifests AS manifest
              USING (skill_evolution_candidate_id)
            ORDER BY candidate.created_revision DESC,
                     candidate.skill_evolution_candidate_id
            """
        ).fetchall()
        items: list[SkillEvolutionCandidateSnapshot] = []
        for row in rows:
            sources = tuple(
                str(source[0])
                for source in self._connection.execute(
                    """
                    SELECT memory_item_id FROM skill_evolution_candidate_sources
                    WHERE skill_evolution_candidate_id = ? ORDER BY memory_item_id
                    """,
                    (str(row["skill_evolution_candidate_id"]),),
                ).fetchall()
            )
            findings = json.loads(str(row["validation_findings_json"]))
            items.append(
                SkillEvolutionCandidateSnapshot(
                    str(row["skill_evolution_candidate_id"]),
                    str(row["skill_name"]),
                    str(row["proposed_version"]),
                    str(row["description"]),
                    str(row["instruction_body"]),
                    str(row["rationale"]),
                    str(row["policy_revision"]),
                    str(row["validation_status"]),
                    tuple(str(item) for item in findings),
                    str(row["lifecycle"]),
                    int(row["pointer_revision"]),
                    sources,
                    int(row["created_revision"]),
                    int(row["updated_revision"]),
                    None
                    if row["relative_skill_path"] is None
                    else str(row["relative_skill_path"]),
                )
            )
        revision = int(
            self._connection.execute(
                "SELECT COALESCE(MAX(workspace_revision), 0) FROM workspace_commits"
            ).fetchone()[0]
        )
        return revision, tuple(items)

    def adoptions(self) -> tuple[SkillAdoptionSnapshot, ...]:
        adoptions = self._connection.execute(
            """
            SELECT skill_name, lifecycle, current_skill_version_id,
                   pointer_revision, updated_revision
            FROM skill_adoptions ORDER BY skill_name
            """
        ).fetchall()
        result: list[SkillAdoptionSnapshot] = []
        for adoption in adoptions:
            versions = self._connection.execute(
                """
                SELECT version.skill_version_id, version.version_label,
                       version.content_sha256, version.source_candidate_id,
                       version.predecessor_skill_version_id, version.created_revision,
                       count(observation.context_item_id) AS outcome_observation_count
                FROM skill_versions AS version
                LEFT JOIN skill_context_uses AS use
                  ON use.skill_version_id = version.skill_version_id
                LEFT JOIN skill_outcome_observations AS observation
                  USING (context_item_id)
                WHERE version.skill_name = ?
                GROUP BY version.skill_version_id
                ORDER BY version.created_revision DESC, version.skill_version_id
                """,
                (str(adoption["skill_name"]),),
            ).fetchall()
            result.append(
                SkillAdoptionSnapshot(
                    str(adoption["skill_name"]),
                    str(adoption["lifecycle"]),
                    None
                    if adoption["current_skill_version_id"] is None
                    else str(adoption["current_skill_version_id"]),
                    int(adoption["pointer_revision"]),
                    int(adoption["updated_revision"]),
                    tuple(
                        SkillVersionSnapshot(
                            str(version["skill_version_id"]),
                            str(version["version_label"]),
                            str(version["content_sha256"]),
                            None
                            if version["source_candidate_id"] is None
                            else str(version["source_candidate_id"]),
                            None
                            if version["predecessor_skill_version_id"] is None
                            else str(version["predecessor_skill_version_id"]),
                            int(version["created_revision"]),
                            int(version["outcome_observation_count"]),
                        )
                        for version in versions
                    ),
                )
            )
        return tuple(result)
