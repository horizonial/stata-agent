"""SQLite authority for user-reviewed evaluation-to-Skill change candidates."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Mapping, Sequence
from typing import Any, cast

from stata_research_agent.application.skill_change import (
    ActivateSkillChangeCommand,
    MaterializeSkillChangeCommand,
    RejectSkillChangeCommand,
    SkillChangeCandidateOutcome,
    SkillChangePublicationPlan,
)
from stata_research_agent.application.skill_evolution import PublishedSkillReceipt
from stata_research_agent.application.skill_evolution_policy import (
    validate_and_render_evaluation_change,
)
from stata_research_agent.domain.identifiers import (
    CommandId,
    SkillAdoptionHistoryId,
    SkillChangeCandidateId,
    SkillChangeStateHistoryId,
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

_DESCRIPTION_LINE = re.compile(r"^description:\s*(.+)$", re.MULTILINE)


class SqliteSkillChangeRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._commits = AtomicCommitService(connection)

    def materialization_replay(
        self, command: MaterializeSkillChangeCommand
    ) -> SkillChangeCandidateOutcome | None:
        receipt = self._command_receipt(command.command_id, "skill.change.materialize")
        if receipt is None:
            return None
        candidate = self._connection.execute(
            "SELECT * FROM skill_change_candidates WHERE source_proposal_id = ?",
            (command.proposal_id.value,),
        ).fetchone()
        if candidate is None:
            raise ValueError("Materialized Skill change candidate is missing")
        return self._replay_outcome(
            receipt,
            "skill.change.materialize",
            {
                "source_proposal_id": command.proposal_id.value,
                "skill_name": str(candidate["skill_name"]),
                "base_skill_version_id": str(candidate["base_skill_version_id"]),
                "merge_source_skill_version_id": candidate["merge_source_skill_version_id"],
                "skill_sha256": str(candidate["skill_sha256"]),
            },
        )

    def activation_replay(
        self, command: ActivateSkillChangeCommand
    ) -> SkillChangeCandidateOutcome | None:
        receipt = self._command_receipt(command.command_id, "skill.change.activate")
        if receipt is None:
            return None
        candidate = self._candidate(command.candidate_id)
        adoption = self._connection.execute(
            """
            SELECT MIN(history.pointer_revision) AS adopted_pointer
            FROM skill_version_change_sources AS source
            JOIN skill_adoption_history AS history
              ON history.target_skill_version_id = source.skill_version_id
             AND history.action_kind = 'activate'
            WHERE source.skill_change_candidate_id = ?
            """,
            (command.candidate_id.value,),
        ).fetchone()
        if adoption is None or adoption["adopted_pointer"] is None:
            raise ValueError("Activated Skill change adoption history is missing")
        return self._replay_outcome(
            receipt,
            "skill.change.activate",
            {
                "skill_change_candidate_id": command.candidate_id.value,
                "expected_candidate_pointer_revision": command.expected_pointer_revision,
                "expected_adoption_pointer_revision": int(adoption["adopted_pointer"]) - 1,
                "skill_sha256": str(candidate["skill_sha256"]),
            },
        )

    def rejection_replay(
        self, command: RejectSkillChangeCommand
    ) -> SkillChangeCandidateOutcome | None:
        receipt = self._command_receipt(command.command_id, "skill.change.reject")
        if receipt is None:
            return None
        return self._replay_outcome(
            receipt,
            "skill.change.reject",
            {
                "skill_change_candidate_id": command.candidate_id.value,
                "expected_pointer_revision": command.expected_pointer_revision,
                "reason": command.reason,
            },
        )

    def materialize(
        self,
        command: MaterializeSkillChangeCommand,
        candidate_id: SkillChangeCandidateId,
        history_id: SkillChangeStateHistoryId,
    ) -> SkillChangeCandidateOutcome:
        proposal = self._connection.execute(
            """
            SELECT proposal.*, run.skill_name
            FROM skill_improvement_proposals AS proposal
            JOIN skill_evaluation_runs AS run USING (skill_evaluation_run_id)
            WHERE proposal.skill_improvement_proposal_id = ?
            """,
            (command.proposal_id.value,),
        ).fetchone()
        if proposal is None:
            raise ValueError("Skill improvement proposal does not exist")
        change_kind = str(proposal["proposal_kind"])
        if change_kind not in {"revise", "merge"}:
            raise ValueError("only revise or merge proposals can become Skill change candidates")
        body = proposal["suggested_instruction_body"]
        if body is None or not str(body).strip():
            raise ValueError("Skill proposal has no complete replacement instructions")
        skill_name = str(proposal["skill_name"])
        base_id = SkillVersionId(str(proposal["target_skill_version_id"]))
        adoption = self._active_adoption(skill_name)
        if str(adoption["current_skill_version_id"]) != base_id.value:
            raise ValueError("Skill evaluation proposal is stale; evaluate the current version")
        base = self._version(base_id, skill_name)
        merge_source_id: SkillVersionId | None = None
        if change_kind == "merge":
            merge_name = str(proposal["merge_target_skill_name"])
            merge_adoption = self._active_adoption(merge_name)
            merge_source_id = SkillVersionId(str(merge_adoption["current_skill_version_id"]))
            if merge_source_id == base_id:
                raise ValueError("Skill cannot merge with itself")
        description = self._description(str(base["skill_markdown"]), skill_name)
        proposed_version = f"eval-{command.proposal_id.value.removeprefix('skillproposal_')[:20]}"
        validated = validate_and_render_evaluation_change(
            skill_name=skill_name,
            description=description,
            instruction_body=str(body),
            rationale=str(proposal["rationale"]),
            proposed_version=proposed_version,
        )
        request = {
            "source_proposal_id": command.proposal_id.value,
            "skill_name": skill_name,
            "base_skill_version_id": base_id.value,
            "merge_source_skill_version_id": (
                None if merge_source_id is None else merge_source_id.value
            ),
            "skill_sha256": validated.skill_sha256,
        }

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            current = connection.execute(
                """
                SELECT current_skill_version_id FROM skill_adoptions
                WHERE skill_name = ? AND lifecycle = 'active'
                """,
                (skill_name,),
            ).fetchone()
            if current is None or str(current[0]) != base_id.value:
                raise ValueError("Skill adoption changed before candidate materialization")
            if merge_source_id is not None:
                merge_current = connection.execute(
                    """
                    SELECT current_skill_version_id FROM skill_adoptions
                    WHERE skill_name = ? AND lifecycle = 'active'
                    """,
                    (str(proposal["merge_target_skill_name"]),),
                ).fetchone()
                if merge_current is None or str(merge_current[0]) != merge_source_id.value:
                    raise ValueError("merge source Skill changed before materialization")
            connection.execute(
                """
                INSERT INTO skill_change_candidates(
                    skill_change_candidate_id, source_proposal_id, change_kind,
                    skill_name, base_skill_version_id, merge_source_skill_version_id,
                    proposed_version, description, instruction_body, skill_markdown,
                    skill_sha256, rationale, policy_revision, validation_status,
                    validation_findings_json, created_revision
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate_id.value,
                    command.proposal_id.value,
                    change_kind,
                    skill_name,
                    base_id.value,
                    None if merge_source_id is None else merge_source_id.value,
                    validated.proposed_version,
                    validated.description,
                    validated.instruction_body,
                    validated.skill_markdown,
                    validated.skill_sha256,
                    validated.rationale,
                    validated.policy_revision,
                    validated.validation_status,
                    canonical_json(list(validated.validation_findings)),
                    revision.value,
                ),
            )
            connection.execute(
                "INSERT INTO skill_change_current_states VALUES (?, 'proposed', 1, ?)",
                (candidate_id.value, revision.value),
            )
            connection.execute(
                "INSERT INTO skill_change_state_history VALUES (?, ?, 'proposed', ?, ?)",
                (
                    history_id.value,
                    candidate_id.value,
                    "user_requested_materialization",
                    revision.value,
                ),
            )
            response = self._response(
                candidate_id,
                "proposed",
                1,
                skill_name,
                base_id,
                merge_source_id,
                validated.proposed_version,
                validated.validation_status,
                validated.validation_findings,
            )
            return MutationPayload(
                response,
                (
                    JournalDraft(
                        "skill.change_candidate_materialized",
                        "skill_change_candidate",
                        candidate_id.value,
                        response,
                    ),
                ),
                (OutboxDraft("skill.change_candidate_changed", response),),
            )

        receipt = self._commits.commit_mutation(
            command_id=command.command_id,
            command_type="skill.change.materialize",
            request=request,
            mutation=mutate,
        )
        return self._outcome(receipt.response, receipt.commit_revision, receipt.replayed)

    def prepare_activation(self, command: ActivateSkillChangeCommand) -> SkillChangePublicationPlan:
        row = self._candidate(command.candidate_id)
        if str(row["lifecycle"]) != "proposed":
            raise ValueError("only proposed Skill changes can be activated")
        if int(row["pointer_revision"]) != command.expected_pointer_revision:
            raise ValueError("Skill change pointer revision changed")
        if str(row["validation_status"]) != "passed":
            raise ValueError("blocked Skill change cannot be activated")
        adoption = self._active_adoption(str(row["skill_name"]))
        if str(adoption["current_skill_version_id"]) != str(row["base_skill_version_id"]):
            raise ValueError("Skill change base is no longer current")
        base = self._version(
            SkillVersionId(str(row["base_skill_version_id"])), str(row["skill_name"])
        )
        return SkillChangePublicationPlan(
            command.command_id,
            command.candidate_id,
            str(row["skill_name"]),
            SkillVersionId(str(row["base_skill_version_id"])),
            str(base["content_sha256"]),
            None
            if row["merge_source_skill_version_id"] is None
            else SkillVersionId(str(row["merge_source_skill_version_id"])),
            str(row["proposed_version"]),
            str(row["skill_markdown"]),
            str(row["skill_sha256"]),
            command.expected_pointer_revision,
            int(adoption["pointer_revision"]),
        )

    def finalize_activation(
        self,
        *,
        plan: SkillChangePublicationPlan,
        receipt: PublishedSkillReceipt,
        history_id: SkillChangeStateHistoryId,
        version_id: SkillVersionId,
        adoption_history_id: SkillAdoptionHistoryId,
        publication_manifest_id: SkillPublicationManifestId,
    ) -> SkillChangeCandidateOutcome:
        if receipt.installed_sha256 != plan.skill_sha256:
            raise ValueError("published Skill hash does not match change candidate")
        base = self._version(plan.base_skill_version_id, plan.skill_name)
        if receipt.prior_sha256 != str(base["content_sha256"]):
            raise ValueError("published Skill predecessor does not match the approved base")
        request = {
            "skill_change_candidate_id": plan.candidate_id.value,
            "expected_candidate_pointer_revision": plan.expected_candidate_pointer_revision,
            "expected_adoption_pointer_revision": plan.expected_adoption_pointer_revision,
            "skill_sha256": plan.skill_sha256,
        }

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            candidate = self._candidate(plan.candidate_id, connection=connection)
            adoption = connection.execute(
                """
                SELECT lifecycle, current_skill_version_id, pointer_revision
                FROM skill_adoptions WHERE skill_name = ?
                """,
                (plan.skill_name,),
            ).fetchone()
            if (
                str(candidate["lifecycle"]) != "proposed"
                or int(candidate["pointer_revision"]) != plan.expected_candidate_pointer_revision
                or adoption is None
                or str(adoption["lifecycle"]) != "active"
                or str(adoption["current_skill_version_id"]) != plan.base_skill_version_id.value
                or int(adoption["pointer_revision"]) != plan.expected_adoption_pointer_revision
            ):
                raise ValueError("Skill change or adoption changed before finalization")
            connection.execute(
                """
                INSERT INTO skill_versions(
                    skill_version_id, skill_name, version_label, content_sha256,
                    skill_markdown, source_candidate_id,
                    predecessor_skill_version_id, created_revision
                ) VALUES (?, ?, ?, ?, ?, NULL, ?, ?)
                """,
                (
                    version_id.value,
                    plan.skill_name,
                    plan.proposed_version,
                    plan.skill_sha256,
                    plan.skill_markdown,
                    plan.base_skill_version_id.value,
                    revision.value,
                ),
            )
            connection.execute(
                "INSERT INTO skill_version_change_sources VALUES (?, ?, ?)",
                (version_id.value, plan.candidate_id.value, revision.value),
            )
            candidate_pointer = plan.expected_candidate_pointer_revision + 1
            connection.execute(
                """
                UPDATE skill_change_current_states
                SET lifecycle = 'activated', pointer_revision = ?, updated_revision = ?
                WHERE skill_change_candidate_id = ? AND pointer_revision = ?
                """,
                (
                    candidate_pointer,
                    revision.value,
                    plan.candidate_id.value,
                    plan.expected_candidate_pointer_revision,
                ),
            )
            connection.execute(
                "INSERT INTO skill_change_state_history VALUES (?, ?, 'activated', ?, ?)",
                (history_id.value, plan.candidate_id.value, "user_approved_change", revision.value),
            )
            adoption_pointer = plan.expected_adoption_pointer_revision + 1
            connection.execute(
                """
                UPDATE skill_adoptions
                SET current_skill_version_id = ?, pointer_revision = ?, updated_revision = ?
                WHERE skill_name = ? AND pointer_revision = ?
                """,
                (
                    version_id.value,
                    adoption_pointer,
                    revision.value,
                    plan.skill_name,
                    plan.expected_adoption_pointer_revision,
                ),
            )
            connection.execute(
                "INSERT INTO skill_adoption_history VALUES (?, ?, ?, 'activate', ?, ?, ?)",
                (
                    adoption_history_id.value,
                    plan.skill_name,
                    version_id.value,
                    "user_approved_evaluation_change",
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
            response = self._response(
                plan.candidate_id,
                "activated",
                candidate_pointer,
                plan.skill_name,
                plan.base_skill_version_id,
                plan.merge_source_skill_version_id,
                plan.proposed_version,
                "passed",
                (),
                activated_skill_version_id=version_id,
            )
            response["adoption_pointer_revision"] = adoption_pointer
            return MutationPayload(
                response,
                (
                    JournalDraft(
                        "skill.change_candidate_activated",
                        "skill_change_candidate",
                        plan.candidate_id.value,
                        response,
                    ),
                ),
                (OutboxDraft("skill.adoption_changed", response),),
            )

        receipt_commit = self._commits.commit_mutation(
            command_id=plan.command_id,
            command_type="skill.change.activate",
            request=request,
            mutation=mutate,
        )
        return self._outcome(
            receipt_commit.response, receipt_commit.commit_revision, receipt_commit.replayed
        )

    def reject(
        self, command: RejectSkillChangeCommand, history_id: SkillChangeStateHistoryId
    ) -> SkillChangeCandidateOutcome:
        request = {
            "skill_change_candidate_id": command.candidate_id.value,
            "expected_pointer_revision": command.expected_pointer_revision,
            "reason": command.reason,
        }

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            candidate = self._candidate(command.candidate_id, connection=connection)
            if (
                str(candidate["lifecycle"]) != "proposed"
                or int(candidate["pointer_revision"]) != command.expected_pointer_revision
            ):
                raise ValueError("Skill change is no longer rejectable")
            pointer = command.expected_pointer_revision + 1
            connection.execute(
                """
                UPDATE skill_change_current_states
                SET lifecycle = 'rejected', pointer_revision = ?, updated_revision = ?
                WHERE skill_change_candidate_id = ? AND pointer_revision = ?
                """,
                (
                    pointer,
                    revision.value,
                    command.candidate_id.value,
                    command.expected_pointer_revision,
                ),
            )
            connection.execute(
                "INSERT INTO skill_change_state_history VALUES (?, ?, 'rejected', ?, ?)",
                (history_id.value, command.candidate_id.value, command.reason, revision.value),
            )
            response = self._response(
                command.candidate_id,
                "rejected",
                pointer,
                str(candidate["skill_name"]),
                SkillVersionId(str(candidate["base_skill_version_id"])),
                None
                if candidate["merge_source_skill_version_id"] is None
                else SkillVersionId(str(candidate["merge_source_skill_version_id"])),
                str(candidate["proposed_version"]),
                str(candidate["validation_status"]),
                tuple(json.loads(str(candidate["validation_findings_json"]))),
            )
            return MutationPayload(
                response,
                (
                    JournalDraft(
                        "skill.change_candidate_rejected",
                        "skill_change_candidate",
                        command.candidate_id.value,
                        response,
                    ),
                ),
                (OutboxDraft("skill.change_candidate_changed", response),),
            )

        receipt = self._commits.commit_mutation(
            command_id=command.command_id,
            command_type="skill.change.reject",
            request=request,
            mutation=mutate,
        )
        return self._outcome(receipt.response, receipt.commit_revision, receipt.replayed)

    def _active_adoption(self, skill_name: str) -> sqlite3.Row:
        row = self._connection.execute(
            """
            SELECT * FROM skill_adoptions
            WHERE skill_name = ? AND lifecycle = 'active'
            """,
            (skill_name,),
        ).fetchone()
        if row is None:
            raise ValueError("Skill is not currently active")
        return cast(sqlite3.Row, row)

    def _version(self, version_id: SkillVersionId, skill_name: str) -> sqlite3.Row:
        row = self._connection.execute(
            "SELECT * FROM skill_versions WHERE skill_version_id = ? AND skill_name = ?",
            (version_id.value, skill_name),
        ).fetchone()
        if row is None:
            raise ValueError("Skill version does not belong to the Skill")
        return cast(sqlite3.Row, row)

    def _candidate(
        self, candidate_id: SkillChangeCandidateId, *, connection: sqlite3.Connection | None = None
    ) -> sqlite3.Row:
        database = connection or self._connection
        row = database.execute(
            """
            SELECT candidate.*, state.lifecycle, state.pointer_revision
            FROM skill_change_candidates AS candidate
            JOIN skill_change_current_states AS state USING (skill_change_candidate_id)
            WHERE candidate.skill_change_candidate_id = ?
            """,
            (candidate_id.value,),
        ).fetchone()
        if row is None:
            raise ValueError("Skill change candidate does not exist")
        return cast(sqlite3.Row, row)

    def _command_receipt(
        self,
        command_id: CommandId,
        command_type: str,
    ) -> sqlite3.Row | None:
        row = self._connection.execute(
            """
            SELECT command_type, request_hash, response_json, commit_revision
            FROM command_receipts WHERE command_id = ?
            """,
            (command_id.value,),
        ).fetchone()
        if row is None:
            return None
        if str(row["command_type"]) != command_type:
            raise CommandConflictError(
                f"command_id {command_id.value} was reused with different content"
            )
        return cast(sqlite3.Row, row)

    def _replay_outcome(
        self,
        receipt: sqlite3.Row,
        command_type: str,
        request: Mapping[str, object],
    ) -> SkillChangeCandidateOutcome:
        request_hash = hashlib.sha256(
            canonical_json({"command_type": command_type, "request": request}).encode("utf-8")
        ).hexdigest()
        if str(receipt["request_hash"]) != request_hash:
            raise CommandConflictError("command_id was reused with different content")
        response = json.loads(str(receipt["response_json"]))
        if not isinstance(response, dict):
            raise ValueError("Skill change command receipt response is malformed")
        return self._outcome(
            response,
            WorkspaceRevision(int(receipt["commit_revision"])),
            True,
        )

    @staticmethod
    def _description(markdown: str, skill_name: str) -> str:
        match = _DESCRIPTION_LINE.search(markdown)
        if match is None:
            return f"Updated reusable guidance for {skill_name}"
        raw = match.group(1).strip()
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = raw
        description = " ".join(str(parsed).split())
        return description[:500] or f"Updated reusable guidance for {skill_name}"

    @staticmethod
    def _response(
        candidate_id: SkillChangeCandidateId,
        lifecycle: str,
        pointer_revision: int,
        skill_name: str,
        base_id: SkillVersionId,
        merge_source_id: SkillVersionId | None,
        proposed_version: str,
        validation_status: str,
        findings: tuple[str, ...],
        *,
        activated_skill_version_id: SkillVersionId | None = None,
    ) -> dict[str, object]:
        return {
            "skill_change_candidate_id": candidate_id.value,
            "lifecycle": lifecycle,
            "pointer_revision": pointer_revision,
            "skill_name": skill_name,
            "base_skill_version_id": base_id.value,
            "merge_source_skill_version_id": (
                None if merge_source_id is None else merge_source_id.value
            ),
            "proposed_version": proposed_version,
            "validation_status": validation_status,
            "validation_findings": list(findings),
            "activated_skill_version_id": (
                None if activated_skill_version_id is None else activated_skill_version_id.value
            ),
        }

    @staticmethod
    def _outcome(
        response: Mapping[str, Any], revision: WorkspaceRevision, replayed: bool
    ) -> SkillChangeCandidateOutcome:
        merge_source = response["merge_source_skill_version_id"]
        activated = response["activated_skill_version_id"]
        findings = response["validation_findings"]
        if not isinstance(findings, Sequence) or isinstance(findings, str):
            raise ValueError("Skill change validation findings are malformed")
        return SkillChangeCandidateOutcome(
            SkillChangeCandidateId(str(response["skill_change_candidate_id"])),
            str(response["lifecycle"]),
            int(str(response["pointer_revision"])),
            str(response["skill_name"]),
            SkillVersionId(str(response["base_skill_version_id"])),
            None if merge_source is None else SkillVersionId(str(merge_source)),
            str(response["proposed_version"]),
            str(response["validation_status"]),
            tuple(str(item) for item in findings),
            revision,
            replayed,
            None if activated is None else SkillVersionId(str(activated)),
        )
