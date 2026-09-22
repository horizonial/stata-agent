"""SQLite authority for review, approval, and activation of evolved Skills."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from typing import Any, cast

from stata_research_agent.application.skill_evolution import (
    ApproveSkillEvolutionCommand,
    DeactivatedSkillReceipt,
    DeactivateSkillCommand,
    PublishedSkillReceipt,
    RejectSkillEvolutionCommand,
    RollbackSkillCommand,
    SkillActivationPlan,
    SkillAdoptionOutcome,
    SkillDeactivationPlan,
    SkillEvolutionOutcome,
    SkillVersionAdoptionPlan,
)
from stata_research_agent.domain.identifiers import (
    CommandId,
    SkillActivationManifestId,
    SkillAdoptionHistoryId,
    SkillEvolutionCandidateId,
    SkillEvolutionStateHistoryId,
    SkillPublicationManifestId,
    SkillVersionId,
)
from stata_research_agent.domain.revisions import WorkspaceRevision

from .atomic_commit import (
    AtomicCommitService,
    JournalDraft,
    MutationPayload,
    OutboxDraft,
    canonical_json,
)
from .errors import CommandConflictError


class SqliteSkillEvolutionRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._commits = AtomicCommitService(connection)

    def rollback_replay(self, command: RollbackSkillCommand) -> SkillAdoptionOutcome | None:
        return self._replayed_adoption(
            command.command_id,
            "skill.adoption.rollback",
            {
                "skill_name": command.skill_name,
                "target_skill_version_id": command.target_skill_version_id.value,
                "expected_pointer_revision": command.expected_pointer_revision,
                "reason": command.reason,
            },
        )

    def deactivation_replay(
        self, command: DeactivateSkillCommand
    ) -> SkillAdoptionOutcome | None:
        current = self._connection.execute(
            """
            SELECT current_skill_version_id FROM skill_adoptions WHERE skill_name = ?
            """,
            (command.skill_name,),
        ).fetchone()
        receipt = self._connection.execute(
            """
            SELECT response_json FROM command_receipts WHERE command_id = ?
            """,
            (command.command_id.value,),
        ).fetchone()
        if receipt is not None:
            response = json.loads(str(receipt["response_json"]))
            version_id = response.get("deactivated_skill_version_id")
        else:
            version_id = None if current is None else current["current_skill_version_id"]
        return self._replayed_adoption(
            command.command_id,
            "skill.adoption.deactivate",
            {
                "skill_name": command.skill_name,
                "current_skill_version_id": version_id,
                "expected_pointer_revision": command.expected_pointer_revision,
                "reason": command.reason,
            },
        )

    def approve(
        self,
        command: ApproveSkillEvolutionCommand,
        state_history_id: SkillEvolutionStateHistoryId,
    ) -> SkillActivationPlan:
        current = self._candidate(self._connection, command.candidate_id)
        command_exists = self._connection.execute(
            "SELECT 1 FROM command_receipts WHERE command_id = ?",
            (command.command_id.value,),
        ).fetchone()
        if str(current["lifecycle"]) == "approved" and command_exists is None:
            if int(current["pointer_revision"]) != command.expected_pointer_revision:
                raise ValueError("Skill candidate pointer revision changed")
            if str(current["validation_status"]) != "passed":
                raise ValueError("blocked Skill candidate cannot be approved")
            self._require_current_active_sources(self._connection, command.candidate_id)
            return SkillActivationPlan(
                command.candidate_id,
                str(current["skill_name"]),
                str(current["proposed_version"]),
                str(current["skill_markdown"]),
                str(current["skill_sha256"]),
                int(current["pointer_revision"]),
                WorkspaceRevision(int(current["updated_revision"])),
                True,
            )
        request = {
            "skill_evolution_candidate_id": command.candidate_id.value,
            "expected_pointer_revision": command.expected_pointer_revision,
        }

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            candidate = self._candidate(connection, command.candidate_id)
            if str(candidate["validation_status"]) != "passed":
                raise ValueError("blocked Skill candidate cannot be approved")
            if str(candidate["lifecycle"]) != "proposed":
                raise ValueError("only a proposed Skill candidate can be approved")
            if int(candidate["pointer_revision"]) != command.expected_pointer_revision:
                raise ValueError("Skill candidate pointer revision changed")
            self._require_current_active_sources(connection, command.candidate_id)
            pointer_revision = command.expected_pointer_revision + 1
            cursor = connection.execute(
                """
                UPDATE skill_evolution_current_states
                SET lifecycle = 'approved', pointer_revision = ?, updated_revision = ?
                WHERE skill_evolution_candidate_id = ? AND pointer_revision = ?
                  AND lifecycle = 'proposed'
                """,
                (
                    pointer_revision,
                    revision.value,
                    command.candidate_id.value,
                    command.expected_pointer_revision,
                ),
            )
            if cursor.rowcount != 1:
                raise sqlite3.IntegrityError("Skill candidate changed during approval")
            connection.execute(
                """
                INSERT INTO skill_evolution_state_history
                VALUES (?, ?, 'approved', 'user_approved', ?)
                """,
                (state_history_id.value, command.candidate_id.value, revision.value),
            )
            response = self._plan_response(candidate, pointer_revision)
            return MutationPayload(
                response,
                (
                    JournalDraft(
                        "skill.evolution_approved",
                        "skill_evolution_candidate",
                        command.candidate_id.value,
                        response,
                    ),
                ),
                (OutboxDraft("skill.evolution_changed", response),),
            )

        receipt = self._commits.commit_mutation(
            command_id=command.command_id,
            command_type="skill.evolution.approve",
            request=request,
            mutation=mutate,
        )
        response = receipt.response
        return SkillActivationPlan(
            command.candidate_id,
            str(response["skill_name"]),
            str(response["proposed_version"]),
            str(response["skill_markdown"]),
            str(response["skill_sha256"]),
            int(response["pointer_revision"]),
            receipt.commit_revision,
            receipt.replayed,
        )

    def finalize_activation(
        self,
        *,
        command_id: CommandId,
        plan: SkillActivationPlan,
        receipt: PublishedSkillReceipt,
        state_history_id: SkillEvolutionStateHistoryId,
        manifest_id: SkillActivationManifestId,
        version_id: SkillVersionId,
        adoption_history_id: SkillAdoptionHistoryId,
        publication_manifest_id: SkillPublicationManifestId,
    ) -> SkillEvolutionOutcome:
        existing = self._connection.execute(
            """
            SELECT state.lifecycle, state.pointer_revision, state.updated_revision,
                   manifest.skill_activation_manifest_id, manifest.installed_sha256,
                   manifest.relative_skill_path
            FROM skill_evolution_current_states AS state
            LEFT JOIN skill_activation_manifests AS manifest
              USING (skill_evolution_candidate_id)
            WHERE state.skill_evolution_candidate_id = ?
            """,
            (plan.candidate_id.value,),
        ).fetchone()
        if existing is not None and str(existing["lifecycle"]) == "activated":
            if (
                str(existing["installed_sha256"]) != plan.skill_sha256
                or str(existing["relative_skill_path"]) != receipt.relative_skill_path
            ):
                raise ValueError("activated Skill manifest conflicts with publisher receipt")
            return SkillEvolutionOutcome(
                plan.candidate_id,
                "activated",
                int(existing["pointer_revision"]),
                WorkspaceRevision(int(existing["updated_revision"])),
                True,
                SkillActivationManifestId(str(existing["skill_activation_manifest_id"])),
                self._version_id_for_candidate(plan.candidate_id),
                self._adoption_pointer_revision(str(plan.skill_name)),
            )
        if receipt.installed_sha256 != plan.skill_sha256:
            raise ValueError("published Skill hash does not match approved candidate")
        expected_path = f"skills/{plan.skill_name}/SKILL.md"
        if receipt.relative_skill_path != expected_path:
            raise ValueError("published Skill path does not match approved candidate")
        request = {
            "skill_evolution_candidate_id": plan.candidate_id.value,
            "approved_pointer_revision": plan.pointer_revision,
            "relative_skill_path": receipt.relative_skill_path,
            "installed_sha256": receipt.installed_sha256,
        }

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            candidate = self._candidate(connection, plan.candidate_id)
            if str(candidate["lifecycle"]) != "approved":
                raise ValueError("Skill candidate is not approved")
            if int(candidate["pointer_revision"]) != plan.pointer_revision:
                raise ValueError("Skill candidate pointer revision changed")
            self._require_current_active_sources(connection, plan.candidate_id)
            pointer_revision = plan.pointer_revision + 1
            connection.execute(
                """
                INSERT INTO skill_activation_manifests VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    manifest_id.value,
                    plan.candidate_id.value,
                    receipt.relative_skill_path,
                    receipt.installed_sha256,
                    receipt.prior_sha256,
                    receipt.activation_kind,
                    revision.value,
                ),
            )
            cursor = connection.execute(
                """
                UPDATE skill_evolution_current_states
                SET lifecycle = 'activated', pointer_revision = ?, updated_revision = ?
                WHERE skill_evolution_candidate_id = ? AND pointer_revision = ?
                  AND lifecycle = 'approved'
                """,
                (
                    pointer_revision,
                    revision.value,
                    plan.candidate_id.value,
                    plan.pointer_revision,
                ),
            )
            if cursor.rowcount != 1:
                raise sqlite3.IntegrityError("Skill candidate changed during activation")
            connection.execute(
                """
                INSERT INTO skill_evolution_state_history
                VALUES (?, ?, 'activated', 'user_approved_installation', ?)
                """,
                (state_history_id.value, plan.candidate_id.value, revision.value),
            )
            previous = connection.execute(
                """
                SELECT current_skill_version_id, pointer_revision
                FROM skill_adoptions WHERE skill_name = ?
                """,
                (plan.skill_name,),
            ).fetchone()
            predecessor_version_id = (
                None if previous is None else previous["current_skill_version_id"]
            )
            if previous is not None and predecessor_version_id is None:
                historical = connection.execute(
                    """
                    SELECT target_skill_version_id FROM skill_adoption_history
                    WHERE skill_name = ? AND target_skill_version_id IS NOT NULL
                    ORDER BY created_revision DESC LIMIT 1
                    """,
                    (plan.skill_name,),
                ).fetchone()
                predecessor_version_id = (
                    None if historical is None else historical["target_skill_version_id"]
                )
            connection.execute(
                """
                INSERT INTO skill_versions(
                    skill_version_id, skill_name, version_label, content_sha256,
                    skill_markdown, source_candidate_id,
                    predecessor_skill_version_id, created_revision
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    version_id.value,
                    plan.skill_name,
                    plan.proposed_version,
                    plan.skill_sha256,
                    plan.skill_markdown,
                    plan.candidate_id.value,
                    predecessor_version_id,
                    revision.value,
                ),
            )
            adoption_pointer = 1 if previous is None else int(previous["pointer_revision"]) + 1
            connection.execute(
                """
                INSERT INTO skill_adoptions(
                    skill_name, current_skill_version_id, lifecycle,
                    pointer_revision, updated_revision
                ) VALUES (?, ?, 'active', ?, ?)
                ON CONFLICT(skill_name) DO UPDATE SET
                    current_skill_version_id = excluded.current_skill_version_id,
                    lifecycle = 'active',
                    pointer_revision = excluded.pointer_revision,
                    updated_revision = excluded.updated_revision
                """,
                (plan.skill_name, version_id.value, adoption_pointer, revision.value),
            )
            connection.execute(
                """
                INSERT INTO skill_adoption_history VALUES (?, ?, ?, 'activate', ?, ?, ?)
                """,
                (
                    adoption_history_id.value,
                    plan.skill_name,
                    version_id.value,
                    "user_approved_candidate",
                    adoption_pointer,
                    revision.value,
                ),
            )
            connection.execute(
                """
                INSERT INTO skill_publication_manifests VALUES (
                    ?, ?, ?, 'activate', ?, ?, ?, ?, ?
                )
                """,
                (
                    publication_manifest_id.value,
                    plan.skill_name,
                    version_id.value,
                    receipt.relative_skill_path,
                    receipt.installed_sha256,
                    receipt.prior_sha256,
                    receipt.activation_kind,
                    revision.value,
                ),
            )
            response = {
                "skill_evolution_candidate_id": plan.candidate_id.value,
                "lifecycle": "activated",
                "pointer_revision": pointer_revision,
                "skill_activation_manifest_id": manifest_id.value,
                "relative_skill_path": receipt.relative_skill_path,
                "skill_version_id": version_id.value,
                "adoption_pointer_revision": adoption_pointer,
            }
            return MutationPayload(
                response,
                (
                    JournalDraft(
                        "skill.evolution_activated",
                        "skill_evolution_candidate",
                        plan.candidate_id.value,
                        response,
                    ),
                ),
                (OutboxDraft("skill.evolution_changed", response),),
            )

        committed = self._commits.commit_mutation(
            command_id=command_id,
            command_type="skill.evolution.finalize_activation",
            request=request,
            mutation=mutate,
        )
        return SkillEvolutionOutcome(
            plan.candidate_id,
            "activated",
            int(committed.response["pointer_revision"]),
            committed.commit_revision,
            committed.replayed,
            SkillActivationManifestId(str(committed.response["skill_activation_manifest_id"])),
            SkillVersionId(str(committed.response["skill_version_id"])),
            int(committed.response["adoption_pointer_revision"]),
        )

    def reject(
        self,
        command: RejectSkillEvolutionCommand,
        state_history_id: SkillEvolutionStateHistoryId,
    ) -> SkillEvolutionOutcome:
        request = {
            "skill_evolution_candidate_id": command.candidate_id.value,
            "expected_pointer_revision": command.expected_pointer_revision,
            "reason": command.reason,
        }

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            candidate = self._candidate(connection, command.candidate_id)
            if str(candidate["lifecycle"]) != "proposed":
                raise ValueError("only a proposed Skill candidate can be rejected")
            if int(candidate["pointer_revision"]) != command.expected_pointer_revision:
                raise ValueError("Skill candidate pointer revision changed")
            pointer_revision = command.expected_pointer_revision + 1
            connection.execute(
                """
                UPDATE skill_evolution_current_states
                SET lifecycle = 'rejected', pointer_revision = ?, updated_revision = ?
                WHERE skill_evolution_candidate_id = ? AND pointer_revision = ?
                """,
                (
                    pointer_revision,
                    revision.value,
                    command.candidate_id.value,
                    command.expected_pointer_revision,
                ),
            )
            connection.execute(
                """
                INSERT INTO skill_evolution_state_history
                VALUES (?, ?, 'rejected', ?, ?)
                """,
                (
                    state_history_id.value,
                    command.candidate_id.value,
                    command.reason,
                    revision.value,
                ),
            )
            response = {
                "skill_evolution_candidate_id": command.candidate_id.value,
                "lifecycle": "rejected",
                "pointer_revision": pointer_revision,
            }
            return MutationPayload(
                response,
                (
                    JournalDraft(
                        "skill.evolution_rejected",
                        "skill_evolution_candidate",
                        command.candidate_id.value,
                        response,
                    ),
                ),
                (OutboxDraft("skill.evolution_changed", response),),
            )

        receipt = self._commits.commit_mutation(
            command_id=command.command_id,
            command_type="skill.evolution.reject",
            request=request,
            mutation=mutate,
        )
        return SkillEvolutionOutcome(
            command.candidate_id,
            "rejected",
            int(receipt.response["pointer_revision"]),
            receipt.commit_revision,
            receipt.replayed,
        )

    def prepare_rollback(self, command: RollbackSkillCommand) -> SkillVersionAdoptionPlan:
        adoption = self._connection.execute(
            """
            SELECT lifecycle, current_skill_version_id, pointer_revision
            FROM skill_adoptions WHERE skill_name = ?
            """,
            (command.skill_name,),
        ).fetchone()
        if adoption is None:
            raise ValueError("Skill adoption does not exist")
        if int(adoption["pointer_revision"]) != command.expected_pointer_revision:
            raise ValueError("Skill adoption pointer revision changed")
        if (
            str(adoption["lifecycle"]) == "active"
            and str(adoption["current_skill_version_id"])
            == command.target_skill_version_id.value
        ):
            raise ValueError("target Skill version is already current")
        target = self._connection.execute(
            """
            SELECT version_label, skill_markdown, content_sha256
            FROM skill_versions
            WHERE skill_version_id = ? AND skill_name = ?
            """,
            (command.target_skill_version_id.value, command.skill_name),
        ).fetchone()
        if target is None:
            raise ValueError("target Skill version does not belong to this Skill")
        return SkillVersionAdoptionPlan(
            command.command_id,
            command.skill_name,
            command.target_skill_version_id,
            str(target["version_label"]),
            str(target["skill_markdown"]),
            str(target["content_sha256"]),
            command.expected_pointer_revision,
            command.reason,
        )

    def finalize_rollback(
        self,
        *,
        plan: SkillVersionAdoptionPlan,
        receipt: PublishedSkillReceipt,
        history_id: SkillAdoptionHistoryId,
        publication_manifest_id: SkillPublicationManifestId,
    ) -> SkillAdoptionOutcome:
        if receipt.installed_sha256 != plan.skill_sha256:
            raise ValueError("published Skill hash does not match rollback target")
        request = {
            "skill_name": plan.skill_name,
            "target_skill_version_id": plan.target_skill_version_id.value,
            "expected_pointer_revision": plan.expected_pointer_revision,
            "reason": plan.reason,
        }

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            current = connection.execute(
                """
                SELECT lifecycle, current_skill_version_id, pointer_revision
                FROM skill_adoptions WHERE skill_name = ?
                """,
                (plan.skill_name,),
            ).fetchone()
            if current is None:
                raise ValueError("Skill adoption does not exist")
            if int(current["pointer_revision"]) != plan.expected_pointer_revision:
                raise ValueError("Skill adoption pointer revision changed")
            target = connection.execute(
                """
                SELECT content_sha256 FROM skill_versions
                WHERE skill_version_id = ? AND skill_name = ?
                """,
                (plan.target_skill_version_id.value, plan.skill_name),
            ).fetchone()
            if target is None or str(target["content_sha256"]) != receipt.installed_sha256:
                raise ValueError("rollback target version identity changed")
            pointer_revision = plan.expected_pointer_revision + 1
            connection.execute(
                """
                UPDATE skill_adoptions
                SET current_skill_version_id = ?, lifecycle = 'active',
                    pointer_revision = ?, updated_revision = ?
                WHERE skill_name = ? AND pointer_revision = ?
                """,
                (
                    plan.target_skill_version_id.value,
                    pointer_revision,
                    revision.value,
                    plan.skill_name,
                    plan.expected_pointer_revision,
                ),
            )
            connection.execute(
                "INSERT INTO skill_adoption_history VALUES (?, ?, ?, 'rollback', ?, ?, ?)",
                (
                    history_id.value,
                    plan.skill_name,
                    plan.target_skill_version_id.value,
                    plan.reason,
                    pointer_revision,
                    revision.value,
                ),
            )
            connection.execute(
                """
                INSERT INTO skill_publication_manifests VALUES (
                    ?, ?, ?, 'rollback', ?, ?, ?, ?, ?
                )
                """,
                (
                    publication_manifest_id.value,
                    plan.skill_name,
                    plan.target_skill_version_id.value,
                    receipt.relative_skill_path,
                    receipt.installed_sha256,
                    receipt.prior_sha256,
                    receipt.activation_kind,
                    revision.value,
                ),
            )
            response = {
                "skill_name": plan.skill_name,
                "lifecycle": "active",
                "current_skill_version_id": plan.target_skill_version_id.value,
                "pointer_revision": pointer_revision,
                "skill_publication_manifest_id": publication_manifest_id.value,
            }
            return MutationPayload(
                response,
                (
                    JournalDraft(
                        "skill.adoption_rolled_back",
                        "skill_adoption",
                        plan.skill_name,
                        response,
                    ),
                ),
                (OutboxDraft("skill.adoption_changed", response),),
            )

        committed = self._commits.commit_mutation(
            command_id=plan.command_id,
            command_type="skill.adoption.rollback",
            request=request,
            mutation=mutate,
        )
        return self._adoption_outcome(
            committed.response, committed.commit_revision, committed.replayed
        )

    def prepare_deactivation(self, command: DeactivateSkillCommand) -> SkillDeactivationPlan:
        row = self._connection.execute(
            """
            SELECT adoption.lifecycle, adoption.current_skill_version_id,
                   adoption.pointer_revision, version.content_sha256
            FROM skill_adoptions AS adoption
            LEFT JOIN skill_versions AS version
              ON version.skill_version_id = adoption.current_skill_version_id
            WHERE adoption.skill_name = ?
            """,
            (command.skill_name,),
        ).fetchone()
        if row is None or str(row["lifecycle"]) != "active":
            raise ValueError("Skill is not currently active")
        if int(row["pointer_revision"]) != command.expected_pointer_revision:
            raise ValueError("Skill adoption pointer revision changed")
        return SkillDeactivationPlan(
            command.command_id,
            command.skill_name,
            SkillVersionId(str(row["current_skill_version_id"])),
            str(row["content_sha256"]),
            command.expected_pointer_revision,
            command.reason,
        )

    def finalize_deactivation(
        self,
        *,
        plan: SkillDeactivationPlan,
        receipt: DeactivatedSkillReceipt,
        history_id: SkillAdoptionHistoryId,
        publication_manifest_id: SkillPublicationManifestId,
    ) -> SkillAdoptionOutcome:
        if receipt.prior_sha256 != plan.current_sha256:
            raise ValueError("deactivated Skill hash does not match current version")
        request = {
            "skill_name": plan.skill_name,
            "current_skill_version_id": plan.current_skill_version_id.value,
            "expected_pointer_revision": plan.expected_pointer_revision,
            "reason": plan.reason,
        }

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            current = connection.execute(
                """
                SELECT lifecycle, current_skill_version_id, pointer_revision
                FROM skill_adoptions WHERE skill_name = ?
                """,
                (plan.skill_name,),
            ).fetchone()
            if (
                current is None
                or str(current["lifecycle"]) != "active"
                or str(current["current_skill_version_id"]) != plan.current_skill_version_id.value
                or int(current["pointer_revision"]) != plan.expected_pointer_revision
            ):
                raise ValueError("Skill adoption changed before deactivation")
            pointer_revision = plan.expected_pointer_revision + 1
            connection.execute(
                """
                UPDATE skill_adoptions
                SET current_skill_version_id = NULL, lifecycle = 'deactivated',
                    pointer_revision = ?, updated_revision = ?
                WHERE skill_name = ? AND pointer_revision = ?
                """,
                (
                    pointer_revision,
                    revision.value,
                    plan.skill_name,
                    plan.expected_pointer_revision,
                ),
            )
            connection.execute(
                "INSERT INTO skill_adoption_history VALUES (?, ?, NULL, 'deactivate', ?, ?, ?)",
                (
                    history_id.value,
                    plan.skill_name,
                    plan.reason,
                    pointer_revision,
                    revision.value,
                ),
            )
            connection.execute(
                """
                INSERT INTO skill_publication_manifests VALUES (
                    ?, ?, NULL, 'deactivate', ?, NULL, ?, ?, ?
                )
                """,
                (
                    publication_manifest_id.value,
                    plan.skill_name,
                    receipt.relative_skill_path,
                    receipt.prior_sha256,
                    receipt.publication_result,
                    revision.value,
                ),
            )
            response = {
                "skill_name": plan.skill_name,
                "lifecycle": "deactivated",
                "current_skill_version_id": None,
                "deactivated_skill_version_id": plan.current_skill_version_id.value,
                "pointer_revision": pointer_revision,
                "skill_publication_manifest_id": publication_manifest_id.value,
            }
            return MutationPayload(
                response,
                (
                    JournalDraft(
                        "skill.adoption_deactivated",
                        "skill_adoption",
                        plan.skill_name,
                        response,
                    ),
                ),
                (OutboxDraft("skill.adoption_changed", response),),
            )

        committed = self._commits.commit_mutation(
            command_id=plan.command_id,
            command_type="skill.adoption.deactivate",
            request=request,
            mutation=mutate,
        )
        return self._adoption_outcome(
            committed.response, committed.commit_revision, committed.replayed
        )

    @staticmethod
    def _adoption_outcome(
        response: Mapping[str, Any], commit_revision: WorkspaceRevision, replayed: bool
    ) -> SkillAdoptionOutcome:
        raw_version = response["current_skill_version_id"]
        return SkillAdoptionOutcome(
            str(response["skill_name"]),
            str(response["lifecycle"]),
            None if raw_version is None else SkillVersionId(str(raw_version)),
            int(response["pointer_revision"]),
            commit_revision,
            replayed,
            SkillPublicationManifestId(str(response["skill_publication_manifest_id"])),
        )

    def _replayed_adoption(
        self,
        command_id: CommandId,
        command_type: str,
        request: Mapping[str, Any],
    ) -> SkillAdoptionOutcome | None:
        row = self._connection.execute(
            """
            SELECT command_type, request_hash, response_json, commit_revision
            FROM command_receipts WHERE command_id = ?
            """,
            (command_id.value,),
        ).fetchone()
        if row is None:
            return None
        expected_hash = hashlib.sha256(
            canonical_json({"command_type": command_type, "request": request}).encode("utf-8")
        ).hexdigest()
        if str(row["command_type"]) != command_type or str(row["request_hash"]) != expected_hash:
            raise CommandConflictError(
                f"command_id {command_id.value} was reused with different content"
            )
        response = json.loads(str(row["response_json"]))
        if not isinstance(response, dict):
            raise ValueError("Skill adoption command receipt is malformed")
        return self._adoption_outcome(
            response,
            WorkspaceRevision(int(row["commit_revision"])),
            True,
        )

    @staticmethod
    def _candidate(
        connection: sqlite3.Connection, candidate_id: SkillEvolutionCandidateId
    ) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT candidate.*, state.lifecycle, state.pointer_revision,
                   state.updated_revision
            FROM skill_evolution_candidates AS candidate
            JOIN skill_evolution_current_states AS state
              USING (skill_evolution_candidate_id)
            WHERE candidate.skill_evolution_candidate_id = ?
            """,
            (candidate_id.value,),
        ).fetchone()
        if row is None:
            raise ValueError("Skill evolution candidate does not exist")
        return cast(sqlite3.Row, row)

    @staticmethod
    def _require_current_active_sources(
        connection: sqlite3.Connection, candidate_id: SkillEvolutionCandidateId
    ) -> None:
        invalid = connection.execute(
            """
            SELECT source.memory_item_id
            FROM skill_evolution_candidate_sources AS source
            JOIN memory_current_states AS state USING (memory_item_id)
            JOIN memory_retention_states AS retention USING (memory_item_id)
            WHERE source.skill_evolution_candidate_id = ?
              AND (state.lifecycle != 'active'
                   OR state.current_revision_id != source.memory_revision_id
                   OR retention.access_tier = 'archived'
                   OR retention.superseded_by_memory_item_id IS NOT NULL)
            LIMIT 1
            """,
            (candidate_id.value,),
        ).fetchone()
        count = int(
            connection.execute(
                """
                SELECT count(*) FROM skill_evolution_candidate_sources
                WHERE skill_evolution_candidate_id = ?
                """,
                (candidate_id.value,),
            ).fetchone()[0]
        )
        if invalid is not None or count < 2:
            raise ValueError("Skill candidate Memory sources are no longer current and active")

    def _version_id_for_candidate(
        self, candidate_id: SkillEvolutionCandidateId
    ) -> SkillVersionId | None:
        row = self._connection.execute(
            "SELECT skill_version_id FROM skill_versions WHERE source_candidate_id = ?",
            (candidate_id.value,),
        ).fetchone()
        return None if row is None else SkillVersionId(str(row[0]))

    def _adoption_pointer_revision(self, skill_name: str) -> int | None:
        row = self._connection.execute(
            "SELECT pointer_revision FROM skill_adoptions WHERE skill_name = ?",
            (skill_name,),
        ).fetchone()
        return None if row is None else int(row[0])

    @staticmethod
    def _plan_response(candidate: sqlite3.Row, pointer_revision: int) -> dict[str, object]:
        return {
            "skill_evolution_candidate_id": str(candidate["skill_evolution_candidate_id"]),
            "skill_name": str(candidate["skill_name"]),
            "proposed_version": str(candidate["proposed_version"]),
            "skill_markdown": str(candidate["skill_markdown"]),
            "skill_sha256": str(candidate["skill_sha256"]),
            "pointer_revision": pointer_revision,
        }
