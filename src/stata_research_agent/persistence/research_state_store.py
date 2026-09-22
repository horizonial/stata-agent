"""SQLite Research Path, Plan Revision, and adoption implementation."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Mapping
from typing import Any

from stata_research_agent.application.research_state import (
    AdoptPlanRevisionCommand,
    AdoptResearchBundleCommand,
    BranchSourceSlot,
    BranchSourceSnapshot,
    CreatePlanRevisionCommand,
    CreateResearchPathBranchCommand,
    CurrentPlanAdoption,
    PlanAdoptionOutcome,
    PlanCommandBinding,
    PlanRevisionIdentity,
    PlanRevisionOutcome,
    ResearchBundleAdoptionOutcome,
    ResearchPathBranchIdentity,
    ResearchPathBranchOutcome,
)
from stata_research_agent.domain.identifiers import (
    PlanId,
    PlanNodeId,
    PlanRevisionId,
    ResearchPathId,
)
from stata_research_agent.domain.revisions import WorkspaceRevision

from .atomic_commit import (
    AtomicCommitService,
    JournalDraft,
    MutationPayload,
    OutboxDraft,
    canonical_json,
)


class SqliteResearchStateRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._commits = AtomicCommitService(connection)

    def current_plan_adoption(
        self, research_path_id: ResearchPathId
    ) -> CurrentPlanAdoption | None:
        row = self._connection.execute(
            """
            SELECT revision.plan_id, adoption.target_plan_revision_id,
                   plan.canonical_key, adoption.pointer_revision
            FROM path_plan_adoptions AS adoption
            JOIN plan_revisions AS revision
              ON revision.plan_revision_id = adoption.target_plan_revision_id
            JOIN plans AS plan ON plan.plan_id = revision.plan_id
            WHERE adoption.research_path_id = ?
            """,
            (research_path_id.value,),
        ).fetchone()
        if row is None:
            return None
        return CurrentPlanAdoption(
            PlanId(str(row["plan_id"])),
            PlanRevisionId(str(row["target_plan_revision_id"])),
            str(row["canonical_key"]),
            int(row["pointer_revision"]),
        )

    def retained_plan_nodes(self, plan_id: PlanId) -> dict[str, PlanNodeId]:
        rows = self._connection.execute(
            "SELECT canonical_key, plan_node_id FROM plan_nodes WHERE plan_id = ?",
            (plan_id.value,),
        ).fetchall()
        return {
            str(row["canonical_key"]): PlanNodeId(str(row["plan_node_id"]))
            for row in rows
        }

    def current_plan_node_for_command(
        self, research_path_id: ResearchPathId, command: str
    ) -> PlanCommandBinding | None:
        row = self._connection.execute(
            """
            SELECT member.plan_revision_id, member.plan_node_id, node.canonical_key
            FROM path_plan_adoptions AS adoption
            JOIN plan_revision_nodes AS member
              ON member.plan_revision_id = adoption.target_plan_revision_id
            JOIN plan_nodes AS node ON node.plan_node_id = member.plan_node_id
            WHERE adoption.research_path_id = ?
              AND json_extract(member.specification_json, '$.command') = ?
            LIMIT 1
            """,
            (research_path_id.value, command),
        ).fetchone()
        if row is None:
            return None
        return PlanCommandBinding(
            PlanRevisionId(str(row["plan_revision_id"])),
            PlanNodeId(str(row["plan_node_id"])),
            str(row["canonical_key"]),
        )

    def current_plan_node_for_key(
        self, research_path_id: ResearchPathId, canonical_key: str
    ) -> PlanCommandBinding | None:
        row = self._connection.execute(
            """
            SELECT member.plan_revision_id, member.plan_node_id
            FROM path_plan_adoptions AS adoption
            JOIN plan_revision_nodes AS member
              ON member.plan_revision_id = adoption.target_plan_revision_id
            JOIN plan_nodes AS node ON node.plan_node_id = member.plan_node_id
            WHERE adoption.research_path_id = ? AND node.canonical_key = ?
            LIMIT 1
            """,
            (research_path_id.value, canonical_key),
        ).fetchone()
        if row is None:
            return None
        return PlanCommandBinding(
            PlanRevisionId(str(row["plan_revision_id"])),
            PlanNodeId(str(row["plan_node_id"])),
            canonical_key,
        )

    def current_plan_nodes(
        self, research_path_id: ResearchPathId
    ) -> tuple[PlanCommandBinding, ...]:
        rows = self._connection.execute(
            """
            SELECT member.plan_revision_id, member.plan_node_id, node.canonical_key
            FROM path_plan_adoptions AS adoption
            JOIN plan_revision_nodes AS member
              ON member.plan_revision_id = adoption.target_plan_revision_id
            JOIN plan_nodes AS node ON node.plan_node_id = member.plan_node_id
            WHERE adoption.research_path_id = ?
            ORDER BY member.ordinal
            """,
            (research_path_id.value,),
        ).fetchall()
        return tuple(
            PlanCommandBinding(
                PlanRevisionId(str(row["plan_revision_id"])),
                PlanNodeId(str(row["plan_node_id"])),
                str(row["canonical_key"]),
            )
            for row in rows
        )

    def current_plan_semantic_digest(self, research_path_id: ResearchPathId) -> str | None:
        row = self._connection.execute(
            """
            SELECT json_extract(revision.specification_json,
                                '$._adaptive_plan.semantic_digest') AS semantic_digest
            FROM path_plan_adoptions AS adoption
            JOIN plan_revisions AS revision
              ON revision.plan_revision_id = adoption.target_plan_revision_id
            WHERE adoption.research_path_id = ?
            """,
            (research_path_id.value,),
        ).fetchone()
        if row is None or row["semantic_digest"] is None:
            return None
        return str(row["semantic_digest"])

    def create_plan_revision(
        self,
        command: CreatePlanRevisionCommand,
        identity: PlanRevisionIdentity,
    ) -> PlanRevisionOutcome:
        node_payload = [
            {
                "canonical_key": node.canonical_key,
                "node_kind": node.node_kind,
                "specification": node.specification,
                "plan_node_id": node_id.value,
            }
            for node, node_id in zip(command.nodes, identity.node_ids, strict=True)
        ]
        content = {
            "summary": command.summary,
            "specification": command.specification,
            "nodes": node_payload,
            "dependencies": [
                {
                    "upstream": edge.upstream_node_key,
                    "downstream": edge.downstream_node_key,
                    "kind": edge.dependency_kind,
                }
                for edge in command.dependencies
            ],
        }
        content_sha256 = hashlib.sha256(canonical_json(content).encode()).hexdigest()
        request = {
            "created_by_turn_id": command.created_by_turn_id.value,
            "canonical_key": command.canonical_key,
            "plan_id": None if command.plan_id is None else command.plan_id.value,
            "parent_revision_ids": [revision.value for revision in command.parent_revision_ids],
            "content_sha256": content_sha256,
        }

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            self._assert_active_write_turn(connection, command.created_by_turn_id.value)
            if command.plan_id is None:
                connection.execute(
                    "INSERT INTO plans VALUES (?, ?, ?, ?)",
                    (
                        identity.plan_id.value,
                        command.canonical_key,
                        command.created_by_turn_id.value,
                        revision.value,
                    ),
                )
                revision_number = 1
            else:
                row = connection.execute(
                    "SELECT canonical_key FROM plans WHERE plan_id = ?",
                    (command.plan_id.value,),
                ).fetchone()
                if row is None:
                    raise ValueError(f"unknown Plan: {command.plan_id.value}")
                if str(row["canonical_key"]) != command.canonical_key:
                    raise ValueError("Plan canonical key is immutable")
                revision_number = int(
                    connection.execute(
                        """
                        SELECT COALESCE(MAX(revision_number), 0) + 1
                        FROM plan_revisions WHERE plan_id = ?
                        """,
                        (command.plan_id.value,),
                    ).fetchone()[0]
                )
                self._validate_plan_parents(connection, command)

            connection.execute(
                """
                INSERT INTO plan_revisions VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identity.plan_revision_id.value,
                    identity.plan_id.value,
                    revision_number,
                    command.summary,
                    canonical_json(command.specification),
                    content_sha256,
                    command.created_by_turn_id.value,
                    revision.value,
                ),
            )
            for ordinal, parent_id in enumerate(command.parent_revision_ids, start=1):
                connection.execute(
                    "INSERT INTO plan_revision_parents VALUES (?, ?, ?, ?)",
                    (
                        identity.plan_revision_id.value,
                        parent_id.value,
                        ordinal,
                        "primary" if ordinal == 1 else "merge",
                    ),
                )
            key_to_id: dict[str, str] = {}
            for ordinal, (node, node_id) in enumerate(
                zip(command.nodes, identity.node_ids, strict=True), start=1
            ):
                existing = connection.execute(
                    """
                    SELECT plan_id, canonical_key FROM plan_nodes
                    WHERE plan_node_id = ?
                    """,
                    (node_id.value,),
                ).fetchone()
                if node.retained_plan_node_id is None:
                    if existing is not None:
                        raise ValueError("new Plan Node identity already exists")
                    connection.execute(
                        "INSERT INTO plan_nodes VALUES (?, ?, ?, ?)",
                        (
                            node_id.value,
                            identity.plan_id.value,
                            node.canonical_key,
                            revision.value,
                        ),
                    )
                elif (
                    existing is None
                    or str(existing["plan_id"]) != identity.plan_id.value
                    or str(existing["canonical_key"]) != node.canonical_key
                ):
                    raise ValueError("retained Plan Node must keep its Plan and canonical meaning")
                connection.execute(
                    "INSERT INTO plan_revision_nodes VALUES (?, ?, ?, ?, ?)",
                    (
                        identity.plan_revision_id.value,
                        node_id.value,
                        ordinal,
                        node.node_kind,
                        canonical_json(node.specification),
                    ),
                )
                key_to_id[node.canonical_key] = node_id.value
            for edge in command.dependencies:
                connection.execute(
                    "INSERT INTO plan_node_dependencies VALUES (?, ?, ?, ?)",
                    (
                        identity.plan_revision_id.value,
                        key_to_id[edge.upstream_node_key],
                        key_to_id[edge.downstream_node_key],
                        edge.dependency_kind,
                    ),
                )
            response = {
                "plan_id": identity.plan_id.value,
                "plan_revision_id": identity.plan_revision_id.value,
                "revision_number": revision_number,
                "content_sha256": content_sha256,
            }
            return MutationPayload(
                response,
                (
                    JournalDraft(
                        "plan.revision_created",
                        "plan_revision",
                        identity.plan_revision_id.value,
                        response,
                    ),
                ),
                (OutboxDraft("research_state.changed", response),),
            )

        receipt = self._commits.commit_mutation(
            command_id=command.command_id,
            command_type="plan.revision.create",
            request=request,
            mutation=mutate,
        )
        return PlanRevisionOutcome(
            PlanId(str(receipt.response["plan_id"])),
            PlanRevisionId(str(receipt.response["plan_revision_id"])),
            int(receipt.response["revision_number"]),
            str(receipt.response["content_sha256"]),
            receipt.commit_revision,
            receipt.replayed,
        )

    def adopt_plan_revision(self, command: AdoptPlanRevisionCommand) -> PlanAdoptionOutcome:
        request = {
            "research_path_id": command.research_path_id.value,
            "target_plan_revision_id": command.target_plan_revision_id.value,
            "updated_by_turn_id": command.updated_by_turn_id.value,
            "expected_pointer_revision": command.expected_pointer_revision,
        }

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            self._assert_active_write_turn(
                connection,
                command.updated_by_turn_id.value,
                required_path_id=command.research_path_id.value,
            )
            if (
                connection.execute(
                    "SELECT 1 FROM plan_revisions WHERE plan_revision_id = ?",
                    (command.target_plan_revision_id.value,),
                ).fetchone()
                is None
            ):
                raise ValueError(f"unknown Plan Revision: {command.target_plan_revision_id.value}")
            current = connection.execute(
                """
                SELECT pointer_revision FROM path_plan_adoptions
                WHERE research_path_id = ?
                """,
                (command.research_path_id.value,),
            ).fetchone()
            current_revision = 0 if current is None else int(current["pointer_revision"])
            if current_revision != command.expected_pointer_revision:
                raise ValueError(
                    "stale Plan pointer: "
                    f"expected={command.expected_pointer_revision}, actual={current_revision}"
                )
            next_pointer = current_revision + 1
            connection.execute(
                "INSERT INTO path_plan_adoption_history VALUES (?, ?, ?, ?, ?)",
                (
                    command.research_path_id.value,
                    next_pointer,
                    command.target_plan_revision_id.value,
                    command.updated_by_turn_id.value,
                    revision.value,
                ),
            )
            connection.execute(
                """
                INSERT INTO path_plan_adoptions VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(research_path_id) DO UPDATE SET
                    target_plan_revision_id = excluded.target_plan_revision_id,
                    pointer_revision = excluded.pointer_revision,
                    updated_by_turn_id = excluded.updated_by_turn_id,
                    commit_revision = excluded.commit_revision
                """,
                (
                    command.research_path_id.value,
                    command.target_plan_revision_id.value,
                    next_pointer,
                    command.updated_by_turn_id.value,
                    revision.value,
                ),
            )
            response = {
                "research_path_id": command.research_path_id.value,
                "target_plan_revision_id": command.target_plan_revision_id.value,
                "pointer_revision": next_pointer,
            }
            return MutationPayload(
                response,
                (
                    JournalDraft(
                        "path.plan_adopted",
                        "research_path",
                        command.research_path_id.value,
                        response,
                    ),
                ),
                (OutboxDraft("research_state.changed", response),),
            )

        receipt = self._commits.commit_mutation(
            command_id=command.command_id,
            command_type="path.plan.adopt",
            request=request,
            mutation=mutate,
        )
        return PlanAdoptionOutcome(
            ResearchPathId(str(receipt.response["research_path_id"])),
            PlanRevisionId(str(receipt.response["target_plan_revision_id"])),
            int(receipt.response["pointer_revision"]),
            receipt.commit_revision,
            receipt.replayed,
        )

    def describe_branch_source(
        self, command: CreateResearchPathBranchCommand
    ) -> BranchSourceSnapshot:
        if (
            self._connection.execute(
                "SELECT 1 FROM research_paths WHERE research_path_id = ?",
                (command.source_research_path_id.value,),
            ).fetchone()
            is None
        ):
            raise ValueError(
                f"unknown source Research Path: {command.source_research_path_id.value}"
            )
        return BranchSourceSnapshot(
            self._source_slots(
                "path_data_slots",
                "path_data_slot_id",
                command.source_research_path_id.value,
            ),
            self._source_slots(
                "result_slots",
                "result_slot_id",
                command.source_research_path_id.value,
            ),
            self._source_slots(
                "document_slots",
                "document_slot_id",
                command.source_research_path_id.value,
            ),
        )

    def adopt_research_bundle(
        self, command: AdoptResearchBundleCommand
    ) -> ResearchBundleAdoptionOutcome:
        request = {
            "research_path_id": command.research_path_id.value,
            "updated_by_turn_id": command.updated_by_turn_id.value,
            "data_targets": [
                {
                    "slot_id": target.path_data_slot_id.value,
                    "data_version_id": target.data_version_id.value,
                    "verification_receipt_id": target.verification_receipt_id.value,
                    "expected_pointer_revision": target.expected_pointer_revision,
                }
                for target in command.data_targets
            ],
            "result_targets": [
                {
                    "slot_id": target.result_slot_id.value,
                    "result_id": target.result_id.value,
                    "expected_pointer_revision": target.expected_pointer_revision,
                }
                for target in command.result_targets
            ],
            "plan_revision_id": (
                None if command.plan_revision_id is None else command.plan_revision_id.value
            ),
            "expected_plan_pointer_revision": command.expected_plan_pointer_revision,
        }

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            self._assert_active_write_turn(
                connection,
                command.updated_by_turn_id.value,
                required_path_id=command.research_path_id.value,
            )
            proposed_data = self._validate_bundle_data(connection, command)
            self._validate_bundle_results(connection, command, proposed_data)
            data_revisions: dict[str, int] = {}
            for data_target in command.data_targets:
                next_pointer = data_target.expected_pointer_revision + 1
                connection.execute(
                    "INSERT INTO path_data_adoption_history VALUES (?, ?, ?, ?, ?)",
                    (
                        data_target.path_data_slot_id.value,
                        next_pointer,
                        data_target.data_version_id.value,
                        command.command_id.value,
                        revision.value,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO path_data_adoptions VALUES (?, ?, ?, ?)
                    ON CONFLICT(path_data_slot_id) DO UPDATE SET
                        target_data_version_id = excluded.target_data_version_id,
                        pointer_revision = excluded.pointer_revision,
                        commit_revision = excluded.commit_revision
                    """,
                    (
                        data_target.path_data_slot_id.value,
                        data_target.data_version_id.value,
                        next_pointer,
                        revision.value,
                    ),
                )
                data_revisions[data_target.path_data_slot_id.value] = next_pointer
            result_revisions: dict[str, int] = {}
            for result_target in command.result_targets:
                next_pointer = result_target.expected_pointer_revision + 1
                connection.execute(
                    "INSERT INTO path_result_adoption_history VALUES (?, ?, ?, ?, ?)",
                    (
                        result_target.result_slot_id.value,
                        next_pointer,
                        result_target.result_id.value,
                        command.updated_by_turn_id.value,
                        revision.value,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO path_result_adoptions VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(result_slot_id) DO UPDATE SET
                        target_result_id = excluded.target_result_id,
                        pointer_revision = excluded.pointer_revision,
                        updated_by_turn_id = excluded.updated_by_turn_id,
                        commit_revision = excluded.commit_revision
                    """,
                    (
                        result_target.result_slot_id.value,
                        result_target.result_id.value,
                        next_pointer,
                        command.updated_by_turn_id.value,
                        revision.value,
                    ),
                )
                result_revisions[result_target.result_slot_id.value] = next_pointer
            plan_pointer: int | None = None
            if command.plan_revision_id is not None:
                assert command.expected_plan_pointer_revision is not None
                plan_pointer = command.expected_plan_pointer_revision + 1
                connection.execute(
                    "INSERT INTO path_plan_adoption_history VALUES (?, ?, ?, ?, ?)",
                    (
                        command.research_path_id.value,
                        plan_pointer,
                        command.plan_revision_id.value,
                        command.updated_by_turn_id.value,
                        revision.value,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO path_plan_adoptions VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(research_path_id) DO UPDATE SET
                        target_plan_revision_id = excluded.target_plan_revision_id,
                        pointer_revision = excluded.pointer_revision,
                        updated_by_turn_id = excluded.updated_by_turn_id,
                        commit_revision = excluded.commit_revision
                    """,
                    (
                        command.research_path_id.value,
                        command.plan_revision_id.value,
                        plan_pointer,
                        command.updated_by_turn_id.value,
                        revision.value,
                    ),
                )
            response = {
                "research_path_id": command.research_path_id.value,
                "data_pointer_revisions": data_revisions,
                "result_pointer_revisions": result_revisions,
                "plan_pointer_revision": plan_pointer,
            }
            return MutationPayload(
                response,
                (
                    JournalDraft(
                        "research_path.bundle_adopted",
                        "research_path",
                        command.research_path_id.value,
                        response,
                    ),
                ),
                (OutboxDraft("research_state.changed", response),),
            )

        receipt = self._commits.commit_mutation(
            command_id=command.command_id,
            command_type="research_path.bundle_adopt",
            request=request,
            mutation=mutate,
        )
        raw_data = receipt.response["data_pointer_revisions"]
        raw_results = receipt.response["result_pointer_revisions"]
        if not isinstance(raw_data, Mapping) or not isinstance(raw_results, Mapping):
            raise ValueError("invalid Research Bundle receipt")
        return ResearchBundleAdoptionOutcome(
            ResearchPathId(str(receipt.response["research_path_id"])),
            {str(key): int(value) for key, value in raw_data.items()},
            {str(key): int(value) for key, value in raw_results.items()},
            (
                None
                if receipt.response["plan_pointer_revision"] is None
                else int(receipt.response["plan_pointer_revision"])
            ),
            receipt.commit_revision,
            receipt.replayed,
        )

    def create_path_branch(
        self,
        command: CreateResearchPathBranchCommand,
        identity: ResearchPathBranchIdentity,
    ) -> ResearchPathBranchOutcome:
        request = {
            "source_research_path_id": command.source_research_path_id.value,
            "created_by_turn_id": command.created_by_turn_id.value,
            "canonical_key": command.canonical_key,
            "display_name": command.display_name,
            "branch_reason": command.branch_reason,
            "source_workspace_revision": command.source_workspace_revision,
            "expected_workspace_revision": command.expected_workspace_revision,
        }

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            if revision.value - 1 != command.expected_workspace_revision:
                raise ValueError(
                    "stale Workspace revision for branch: "
                    f"expected={command.expected_workspace_revision}, "
                    f"actual={revision.value - 1}"
                )
            self._assert_active_write_turn(
                connection,
                command.created_by_turn_id.value,
                required_path_id=command.source_research_path_id.value,
            )
            source = self._snapshot_from_connection(
                connection, command.source_research_path_id.value
            )
            self._assert_branch_identities(source, identity)
            connection.execute(
                "INSERT INTO research_paths VALUES (?, ?, ?)",
                (identity.research_path_id.value, command.canonical_key, revision.value),
            )
            connection.execute(
                """
                INSERT INTO research_path_profiles VALUES (?, ?, 'path_branch', 'active', ?, ?)
                """,
                (
                    identity.research_path_id.value,
                    command.display_name,
                    command.created_by_turn_id.value,
                    revision.value,
                ),
            )
            connection.execute(
                "INSERT INTO research_path_parents VALUES (?, ?, ?, ?, ?, ?)",
                (
                    identity.research_path_id.value,
                    command.source_research_path_id.value,
                    command.source_workspace_revision,
                    command.branch_reason,
                    command.created_by_turn_id.value,
                    revision.value,
                ),
            )
            copied_data = self._clone_data_slots(connection, command, identity, source, revision)
            copied_results = self._clone_result_slots(
                connection, command, identity, source, revision
            )
            copied_documents = self._clone_document_slots(
                connection, command, identity, source, revision
            )
            copied_plan = self._clone_plan_adoption(connection, command, identity, revision)
            manifest = {
                "data_slots": copied_data,
                "result_slots": copied_results,
                "document_slots": copied_documents,
                "plan_adoption": copied_plan,
            }
            manifest_json = canonical_json(manifest)
            manifest_hash = hashlib.sha256(manifest_json.encode()).hexdigest()
            connection.execute(
                "INSERT INTO path_branch_manifests VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    identity.manifest_id.value,
                    identity.research_path_id.value,
                    command.source_research_path_id.value,
                    command.source_workspace_revision,
                    manifest_json,
                    manifest_hash,
                    revision.value,
                ),
            )
            response = {
                "research_path_id": identity.research_path_id.value,
                "parent_research_path_id": command.source_research_path_id.value,
                "source_workspace_revision": command.source_workspace_revision,
                "copied_data_adoptions": sum(
                    1 for item in copied_data if item["target_id"] is not None
                ),
                "copied_result_adoptions": sum(
                    1 for item in copied_results if item["target_id"] is not None
                ),
                "copied_document_adoptions": sum(
                    1 for item in copied_documents if item["target_id"] is not None
                ),
                "copied_plan_adoption": copied_plan is not None,
                "manifest_sha256": manifest_hash,
            }
            return MutationPayload(
                response,
                (
                    JournalDraft(
                        "research_path.branched",
                        "research_path",
                        identity.research_path_id.value,
                        response,
                    ),
                ),
                (OutboxDraft("research_state.changed", response),),
            )

        receipt = self._commits.commit_mutation(
            command_id=command.command_id,
            command_type="research_path.branch",
            request=request,
            mutation=mutate,
        )
        return ResearchPathBranchOutcome(
            ResearchPathId(str(receipt.response["research_path_id"])),
            ResearchPathId(str(receipt.response["parent_research_path_id"])),
            int(receipt.response["source_workspace_revision"]),
            int(receipt.response["copied_data_adoptions"]),
            int(receipt.response["copied_result_adoptions"]),
            int(receipt.response["copied_document_adoptions"]),
            bool(receipt.response["copied_plan_adoption"]),
            receipt.commit_revision,
            receipt.replayed,
        )

    def _source_slots(
        self, table: str, id_column: str, research_path_id: str
    ) -> tuple[BranchSourceSlot, ...]:
        return self._source_slots_from(self._connection, table, id_column, research_path_id)

    @staticmethod
    def _source_slots_from(
        connection: sqlite3.Connection,
        table: str,
        id_column: str,
        research_path_id: str,
    ) -> tuple[BranchSourceSlot, ...]:
        rows = connection.execute(
            f"""
            SELECT {id_column}, canonical_key FROM {table}
            WHERE research_path_id = ? ORDER BY canonical_key
            """,
            (research_path_id,),
        ).fetchall()
        return tuple(
            BranchSourceSlot(str(row[id_column]), str(row["canonical_key"])) for row in rows
        )

    def _snapshot_from_connection(
        self, connection: sqlite3.Connection, research_path_id: str
    ) -> BranchSourceSnapshot:
        return BranchSourceSnapshot(
            self._source_slots_from(
                connection, "path_data_slots", "path_data_slot_id", research_path_id
            ),
            self._source_slots_from(connection, "result_slots", "result_slot_id", research_path_id),
            self._source_slots_from(
                connection, "document_slots", "document_slot_id", research_path_id
            ),
        )

    @staticmethod
    def _assert_branch_identities(
        source: BranchSourceSnapshot, identity: ResearchPathBranchIdentity
    ) -> None:
        if (
            len(source.data_slots) != len(identity.data_slot_ids)
            or len(source.result_slots) != len(identity.result_slot_ids)
            or len(source.document_slots) != len(identity.document_slot_ids)
        ):
            raise ValueError("branch source slots changed before commit")

    def _clone_data_slots(
        self,
        connection: sqlite3.Connection,
        command: CreateResearchPathBranchCommand,
        identity: ResearchPathBranchIdentity,
        source: BranchSourceSnapshot,
        revision: WorkspaceRevision,
    ) -> list[dict[str, Any]]:
        copied: list[dict[str, Any]] = []
        for source_slot, target_slot_id in zip(
            source.data_slots, identity.data_slot_ids, strict=True
        ):
            row = connection.execute(
                """
                SELECT display_name, lifecycle FROM path_data_slots
                WHERE path_data_slot_id = ?
                """,
                (source_slot.slot_id,),
            ).fetchone()
            connection.execute(
                "INSERT INTO path_data_slots VALUES (?, ?, ?, ?, ?, ?)",
                (
                    target_slot_id.value,
                    identity.research_path_id.value,
                    source_slot.canonical_key,
                    str(row["display_name"]),
                    str(row["lifecycle"]),
                    revision.value,
                ),
            )
            connection.execute(
                "INSERT INTO path_data_slot_origins VALUES (?, ?, ?)",
                (
                    target_slot_id.value,
                    source_slot.slot_id,
                    command.source_workspace_revision,
                ),
            )
            adoption = connection.execute(
                """
                SELECT target_data_version_id FROM path_data_adoptions
                WHERE path_data_slot_id = ?
                """,
                (source_slot.slot_id,),
            ).fetchone()
            target_id = None if adoption is None else str(adoption[0])
            if target_id is not None:
                connection.execute(
                    "INSERT INTO path_data_adoption_history VALUES (?, 1, ?, ?, ?)",
                    (
                        target_slot_id.value,
                        target_id,
                        command.command_id.value,
                        revision.value,
                    ),
                )
                connection.execute(
                    "INSERT INTO path_data_adoptions VALUES (?, ?, 1, ?)",
                    (target_slot_id.value, target_id, revision.value),
                )
            copied.append(
                {
                    "source_slot_id": source_slot.slot_id,
                    "target_slot_id": target_slot_id.value,
                    "canonical_key": source_slot.canonical_key,
                    "target_id": target_id,
                }
            )
        return copied

    def _clone_result_slots(
        self,
        connection: sqlite3.Connection,
        command: CreateResearchPathBranchCommand,
        identity: ResearchPathBranchIdentity,
        source: BranchSourceSnapshot,
        revision: WorkspaceRevision,
    ) -> list[dict[str, Any]]:
        copied: list[dict[str, Any]] = []
        for source_slot, target_slot_id in zip(
            source.result_slots, identity.result_slot_ids, strict=True
        ):
            row = connection.execute(
                """
                SELECT display_name, lifecycle FROM result_slots
                WHERE result_slot_id = ?
                """,
                (source_slot.slot_id,),
            ).fetchone()
            connection.execute(
                "INSERT INTO result_slots VALUES (?, ?, ?, ?, ?, ?)",
                (
                    target_slot_id.value,
                    identity.research_path_id.value,
                    source_slot.canonical_key,
                    str(row["display_name"]),
                    str(row["lifecycle"]),
                    revision.value,
                ),
            )
            connection.execute(
                "INSERT INTO path_result_slot_origins VALUES (?, ?, ?)",
                (
                    target_slot_id.value,
                    source_slot.slot_id,
                    command.source_workspace_revision,
                ),
            )
            adoption = connection.execute(
                """
                SELECT target_result_id FROM path_result_adoptions
                WHERE result_slot_id = ?
                """,
                (source_slot.slot_id,),
            ).fetchone()
            target_id = None if adoption is None else str(adoption[0])
            if target_id is not None:
                connection.execute(
                    "INSERT INTO path_result_adoption_history VALUES (?, 1, ?, ?, ?)",
                    (
                        target_slot_id.value,
                        target_id,
                        command.created_by_turn_id.value,
                        revision.value,
                    ),
                )
                connection.execute(
                    "INSERT INTO path_result_adoptions VALUES (?, ?, 1, ?, ?)",
                    (
                        target_slot_id.value,
                        target_id,
                        command.created_by_turn_id.value,
                        revision.value,
                    ),
                )
            copied.append(
                {
                    "source_slot_id": source_slot.slot_id,
                    "target_slot_id": target_slot_id.value,
                    "canonical_key": source_slot.canonical_key,
                    "target_id": target_id,
                }
            )
        return copied

    def _clone_document_slots(
        self,
        connection: sqlite3.Connection,
        command: CreateResearchPathBranchCommand,
        identity: ResearchPathBranchIdentity,
        source: BranchSourceSnapshot,
        revision: WorkspaceRevision,
    ) -> list[dict[str, Any]]:
        copied: list[dict[str, Any]] = []
        for source_slot, target_slot_id in zip(
            source.document_slots, identity.document_slot_ids, strict=True
        ):
            row = connection.execute(
                """
                SELECT lifecycle FROM document_slots WHERE document_slot_id = ?
                """,
                (source_slot.slot_id,),
            ).fetchone()
            connection.execute(
                "INSERT INTO document_slots VALUES (?, ?, ?, ?, ?)",
                (
                    target_slot_id.value,
                    identity.research_path_id.value,
                    source_slot.canonical_key,
                    str(row["lifecycle"]),
                    revision.value,
                ),
            )
            connection.execute(
                "INSERT INTO path_document_slot_origins VALUES (?, ?, ?)",
                (
                    target_slot_id.value,
                    source_slot.slot_id,
                    command.source_workspace_revision,
                ),
            )
            adoption = connection.execute(
                """
                SELECT target_document_revision_id, delivery_gate_report_id
                FROM path_document_adoptions WHERE document_slot_id = ?
                """,
                (source_slot.slot_id,),
            ).fetchone()
            target_id = None if adoption is None else str(adoption[0])
            gate_id = None if adoption is None else adoption[1]
            if target_id is not None:
                connection.execute(
                    """
                    INSERT INTO path_document_adoption_history
                    VALUES (?, 1, ?, ?, ?, ?)
                    """,
                    (
                        target_slot_id.value,
                        target_id,
                        command.created_by_turn_id.value,
                        gate_id,
                        revision.value,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO path_document_adoptions
                    VALUES (?, ?, 1, ?, ?, ?)
                    """,
                    (
                        target_slot_id.value,
                        target_id,
                        command.created_by_turn_id.value,
                        gate_id,
                        revision.value,
                    ),
                )
            copied.append(
                {
                    "source_slot_id": source_slot.slot_id,
                    "target_slot_id": target_slot_id.value,
                    "canonical_key": source_slot.canonical_key,
                    "target_id": target_id,
                }
            )
        return copied

    @staticmethod
    def _clone_plan_adoption(
        connection: sqlite3.Connection,
        command: CreateResearchPathBranchCommand,
        identity: ResearchPathBranchIdentity,
        revision: WorkspaceRevision,
    ) -> Mapping[str, Any] | None:
        row = connection.execute(
            """
            SELECT target_plan_revision_id FROM path_plan_adoptions
            WHERE research_path_id = ?
            """,
            (command.source_research_path_id.value,),
        ).fetchone()
        if row is None:
            return None
        target = str(row["target_plan_revision_id"])
        connection.execute(
            "INSERT INTO path_plan_adoption_history VALUES (?, 1, ?, ?, ?)",
            (
                identity.research_path_id.value,
                target,
                command.created_by_turn_id.value,
                revision.value,
            ),
        )
        connection.execute(
            "INSERT INTO path_plan_adoptions VALUES (?, ?, 1, ?, ?)",
            (
                identity.research_path_id.value,
                target,
                command.created_by_turn_id.value,
                revision.value,
            ),
        )
        return {"target_plan_revision_id": target, "pointer_revision": 1}

    @staticmethod
    def _validate_bundle_data(
        connection: sqlite3.Connection,
        command: AdoptResearchBundleCommand,
    ) -> dict[str, str]:
        current_rows = connection.execute(
            """
            SELECT slot.canonical_key, adoption.target_data_version_id
            FROM path_data_slots AS slot
            LEFT JOIN path_data_adoptions AS adoption
              ON adoption.path_data_slot_id = slot.path_data_slot_id
            WHERE slot.research_path_id = ? AND slot.lifecycle = 'active'
            """,
            (command.research_path_id.value,),
        ).fetchall()
        proposed = {
            str(row["canonical_key"]): str(row["target_data_version_id"])
            for row in current_rows
            if row["target_data_version_id"] is not None
        }
        for target in command.data_targets:
            row = connection.execute(
                """
                SELECT slot.canonical_key, slot.lifecycle,
                       adoption.pointer_revision, state.availability,
                       receipt.verdict AS verification_verdict
                FROM path_data_slots AS slot
                JOIN data_versions AS data ON data.data_version_id = ?
                JOIN artifact_states AS state
                  ON state.artifact_id = data.canonical_artifact_id
                JOIN artifact_verification_receipts AS receipt
                  ON receipt.verification_receipt_id = ?
                 AND receipt.artifact_id = data.canonical_artifact_id
                LEFT JOIN path_data_adoptions AS adoption
                  ON adoption.path_data_slot_id = slot.path_data_slot_id
                WHERE slot.path_data_slot_id = ?
                  AND slot.research_path_id = ?
                """,
                (
                    target.data_version_id.value,
                    target.verification_receipt_id.value,
                    target.path_data_slot_id.value,
                    command.research_path_id.value,
                ),
            ).fetchone()
            if row is None:
                raise ValueError("unknown Path Data Slot or Data Version")
            if str(row["lifecycle"]) != "active":
                raise ValueError("Path Data Slot is retired")
            if str(row["availability"]) != "available":
                raise ValueError("Data Version canonical payload is not available")
            if str(row["verification_verdict"]) != "verified":
                raise ValueError("Data Version Verification Receipt is not acceptable")
            actual = 0 if row["pointer_revision"] is None else int(row["pointer_revision"])
            if actual != target.expected_pointer_revision:
                raise ValueError(
                    "stale Data pointer in Research Bundle: "
                    f"expected={target.expected_pointer_revision}, actual={actual}"
                )
            proposed[str(row["canonical_key"])] = target.data_version_id.value
        return proposed

    @staticmethod
    def _validate_bundle_results(
        connection: sqlite3.Connection,
        command: AdoptResearchBundleCommand,
        proposed_data: Mapping[str, str],
    ) -> None:
        for target in command.result_targets:
            row = connection.execute(
                """
                SELECT slot.lifecycle, adoption.pointer_revision
                FROM result_slots AS slot
                JOIN results AS result ON result.result_id = ?
                LEFT JOIN path_result_adoptions AS adoption
                  ON adoption.result_slot_id = slot.result_slot_id
                WHERE slot.result_slot_id = ? AND slot.research_path_id = ?
                """,
                (
                    target.result_id.value,
                    target.result_slot_id.value,
                    command.research_path_id.value,
                ),
            ).fetchone()
            if row is None:
                raise ValueError("unknown Result Slot or qualified Result")
            if str(row["lifecycle"]) != "active":
                raise ValueError("Result Slot is retired")
            actual = 0 if row["pointer_revision"] is None else int(row["pointer_revision"])
            if actual != target.expected_pointer_revision:
                raise ValueError(
                    "stale Result pointer in Research Bundle: "
                    f"expected={target.expected_pointer_revision}, actual={actual}"
                )
            dependencies = connection.execute(
                """
                SELECT input_data_slot_key, data_version_id
                FROM result_required_data_dependencies WHERE result_id = ?
                """,
                (target.result_id.value,),
            ).fetchall()
            for dependency in dependencies:
                key = str(dependency["input_data_slot_key"])
                required_data = str(dependency["data_version_id"])
                if proposed_data.get(key) != required_data:
                    raise ValueError(
                        f"Result required Data dependency is not atomically adopted: {key}"
                    )
        if command.plan_revision_id is not None:
            if (
                connection.execute(
                    "SELECT 1 FROM plan_revisions WHERE plan_revision_id = ?",
                    (command.plan_revision_id.value,),
                ).fetchone()
                is None
            ):
                raise ValueError("unknown Plan Revision in Research Bundle")
            row = connection.execute(
                """
                SELECT pointer_revision FROM path_plan_adoptions
                WHERE research_path_id = ?
                """,
                (command.research_path_id.value,),
            ).fetchone()
            actual = 0 if row is None else int(row["pointer_revision"])
            assert command.expected_plan_pointer_revision is not None
            if actual != command.expected_plan_pointer_revision:
                raise ValueError(
                    "stale Plan pointer in Research Bundle: "
                    f"expected={command.expected_plan_pointer_revision}, actual={actual}"
                )

    @staticmethod
    def _validate_plan_parents(
        connection: sqlite3.Connection, command: CreatePlanRevisionCommand
    ) -> None:
        if command.plan_id is None:
            raise ValueError("existing Plan identity is required")
        if not command.parent_revision_ids:
            raise ValueError("a new revision of an existing Plan requires a parent")
        for parent in command.parent_revision_ids:
            row = connection.execute(
                "SELECT plan_id FROM plan_revisions WHERE plan_revision_id = ?",
                (parent.value,),
            ).fetchone()
            if row is None or str(row["plan_id"]) != command.plan_id.value:
                raise ValueError("Plan Revision parent must belong to the same Plan")

    @staticmethod
    def _assert_active_write_turn(
        connection: sqlite3.Connection,
        turn_id: str,
        *,
        required_path_id: str | None = None,
    ) -> None:
        row = connection.execute(
            """
            SELECT research_path_id, execution_mode, status FROM turns WHERE turn_id = ?
            """,
            (turn_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown Turn: {turn_id}")
        if str(row["execution_mode"]) != "write" or str(row["status"]) != "running":
            raise ValueError("Research State mutation requires a Running write Turn")
        if required_path_id is not None and str(row["research_path_id"]) != required_path_id:
            raise ValueError("Turn is not bound to the target Research Path")
        lane = connection.execute(
            """
            SELECT active_write_turn_id FROM workspace_write_lane WHERE singleton_id = 1
            """
        ).fetchone()
        if lane is None or str(lane["active_write_turn_id"]) != turn_id:
            raise ValueError("Research State mutation requires write-lane ownership")
