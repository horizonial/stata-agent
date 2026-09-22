"""SQLite repository for current Result adoption and formal Evidence rendering."""

from __future__ import annotations

import hashlib
import sqlite3

from stata_research_agent.application.evidence import (
    AdoptPathResultCommand,
    EvidenceLineage,
    FormalBlockIdentity,
    FormalBlockOutcome,
    PathResultAdoptionOutcome,
    PreparedFormalBlock,
    RenderFormalResultBlockCommand,
    ResultElementForEvidence,
)
from stata_research_agent.domain.identifiers import (
    EvidencePresentationUseId,
    EvidenceRecordId,
    FormalResultBlockId,
    NumericCoverageManifestId,
    NumericOccurrenceId,
    ResultElementId,
    ResultId,
    ResultSlotId,
)
from stata_research_agent.domain.revisions import WorkspaceRevision

from .atomic_commit import (
    AtomicCommitService,
    JournalDraft,
    MutationPayload,
    OutboxDraft,
    canonical_json,
)


class SqliteEvidenceRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._commits = AtomicCommitService(connection)

    def adopt_path_result(
        self, command: AdoptPathResultCommand, result_slot_id: ResultSlotId
    ) -> PathResultAdoptionOutcome:
        request = {
            "research_path_id": command.research_path_id.value,
            "canonical_slot_key": command.canonical_slot_key,
            "result_id": command.result_id.value,
            "updated_by_turn_id": command.updated_by_turn_id.value,
            "expected_pointer_revision": command.expected_pointer_revision,
            "display_name": command.display_name,
        }

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            self._assert_turn_path(
                connection,
                command.updated_by_turn_id.value,
                command.research_path_id.value,
            )
            result_path = connection.execute(
                """
                SELECT rc.research_path_id
                FROM results AS r
                JOIN result_candidates AS candidate
                  ON candidate.result_candidate_id = r.result_candidate_id
                JOIN result_qualification_reports AS report
                  ON report.result_qualification_report_id =
                     r.originating_qualification_report_id
                JOIN result_contracts AS rc
                  ON rc.result_contract_id = report.result_contract_id
                WHERE r.result_id = ?
                """,
                (command.result_id.value,),
            ).fetchone()
            if result_path is None:
                raise ValueError("Result does not exist")
            if str(result_path["research_path_id"]) != command.research_path_id.value:
                raise ValueError("Result belongs to a different Research Path")
            self._assert_result_data_current(
                connection,
                command.research_path_id.value,
                command.result_id.value,
            )

            slot = connection.execute(
                """
                SELECT result_slot_id FROM result_slots
                WHERE research_path_id = ? AND canonical_key = ? AND lifecycle = 'active'
                """,
                (command.research_path_id.value, command.canonical_slot_key),
            ).fetchone()
            if slot is None:
                selected_slot_id = result_slot_id.value
                connection.execute(
                    "INSERT INTO result_slots VALUES (?, ?, ?, ?, 'active', ?)",
                    (
                        selected_slot_id,
                        command.research_path_id.value,
                        command.canonical_slot_key,
                        command.display_name,
                        revision.value,
                    ),
                )
            else:
                selected_slot_id = str(slot["result_slot_id"])

            current = connection.execute(
                "SELECT pointer_revision FROM path_result_adoptions WHERE result_slot_id = ?",
                (selected_slot_id,),
            ).fetchone()
            current_revision = int(current["pointer_revision"]) if current else 0
            if current_revision != command.expected_pointer_revision:
                raise ValueError(
                    "Result adoption pointer changed; reload before adopting another Result"
                )
            next_pointer = current_revision + 1
            connection.execute(
                "INSERT INTO path_result_adoption_history VALUES (?, ?, ?, ?, ?)",
                (
                    selected_slot_id,
                    next_pointer,
                    command.result_id.value,
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
                    selected_slot_id,
                    command.result_id.value,
                    next_pointer,
                    command.updated_by_turn_id.value,
                    revision.value,
                ),
            )
            response = {
                "result_slot_id": selected_slot_id,
                "result_id": command.result_id.value,
                "pointer_revision": next_pointer,
            }
            return MutationPayload(
                response,
                (
                    JournalDraft(
                        "path_result.adopted",
                        "result_slot",
                        selected_slot_id,
                        response,
                    ),
                ),
                (OutboxDraft("path_result.changed", response),),
            )

        receipt = self._commits.commit_mutation(
            command_id=command.command_id,
            command_type="path_result.adopt",
            request=request,
            mutation=mutate,
        )
        response = receipt.response
        return PathResultAdoptionOutcome(
            ResultSlotId(str(response["result_slot_id"])),
            ResultId(str(response["result_id"])),
            int(response["pointer_revision"]),
            receipt.commit_revision,
            receipt.replayed,
        )

    def load_elements_for_render(
        self, command: RenderFormalResultBlockCommand
    ) -> tuple[ResultElementForEvidence, ...]:
        slot, result_id = self._current_result(
            self._connection,
            command.research_path_id.value,
            command.result_slot_key,
        )
        del slot
        self._assert_result_data_current(
            self._connection, command.research_path_id.value, result_id
        )
        self._assert_turn_path(
            self._connection,
            command.created_by_turn_id.value,
            command.research_path_id.value,
        )
        requested = tuple(slot.result_element_id.value for slot in command.slots)
        placeholders = ",".join("?" for _ in requested)
        rows = self._connection.execute(
            f"""
            SELECT result_element_id, result_id, semantic_key, statistic_kind,
                   canonical_binary64_bits, canonical_decimal_text
            FROM result_elements
            WHERE result_id = ? AND value_kind = 'finite'
              AND estimate_status = 'estimated'
              AND result_element_id IN ({placeholders})
            """,
            (result_id, *requested),
        ).fetchall()
        if len({str(row["result_element_id"]) for row in rows}) != len(set(requested)):
            raise ValueError("one or more Result Elements are missing or not formally renderable")
        return tuple(
            ResultElementForEvidence(
                ResultElementId(str(row["result_element_id"])),
                ResultId(str(row["result_id"])),
                str(row["semantic_key"]),
                str(row["statistic_kind"]),
                str(row["canonical_binary64_bits"]),
                str(row["canonical_decimal_text"]),
            )
            for row in rows
        )

    def commit_formal_block(
        self,
        command: RenderFormalResultBlockCommand,
        prepared: PreparedFormalBlock,
        identities: FormalBlockIdentity,
    ) -> FormalBlockOutcome:
        request = {
            "created_by_turn_id": command.created_by_turn_id.value,
            "research_path_id": command.research_path_id.value,
            "result_slot_key": command.result_slot_key,
            "template": command.template,
            "slots": [
                {
                    "name": slot.name,
                    "result_element_id": slot.result_element_id.value,
                    "decimal_places": slot.decimal_places,
                }
                for slot in command.slots
            ],
        }

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            self._assert_turn_path(
                connection,
                command.created_by_turn_id.value,
                command.research_path_id.value,
            )
            result_slot_id, result_id = self._current_result(
                connection,
                command.research_path_id.value,
                command.result_slot_key,
            )
            self._assert_result_data_current(connection, command.research_path_id.value, result_id)
            for occurrence in prepared.rendered.occurrences:
                element = prepared.elements_by_id[occurrence.result_element_id]
                if element.result_id.value != result_id:
                    raise ValueError("Result adoption changed before formal block commit")

            block_id = identities.formal_result_block_id.value
            connection.execute(
                "INSERT INTO formal_result_blocks VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    block_id,
                    command.research_path_id.value,
                    result_slot_id,
                    result_id,
                    prepared.rendered.content,
                    prepared.rendered.content_utf8_sha256,
                    command.created_by_turn_id.value,
                    revision.value,
                ),
            )
            evidence_ids: list[str] = []
            use_ids: list[str] = []
            occurrence_rows: list[tuple[object, ...]] = []
            for occurrence, identity in zip(
                prepared.rendered.occurrences, identities.occurrences, strict=True
            ):
                element_id = occurrence.result_element_id
                existing = connection.execute(
                    """
                    SELECT evidence_record_id FROM evidence_statistical_sources
                    WHERE result_element_id = ?
                    """,
                    (element_id,),
                ).fetchone()
                reused = existing is not None
                if existing is None:
                    evidence_id = identity.evidence_record_candidate_id.value
                    connection.execute(
                        "INSERT INTO evidence_records VALUES (?, 'statistical_element', ?)",
                        (evidence_id, revision.value),
                    )
                    connection.execute(
                        "INSERT INTO evidence_statistical_sources VALUES (?, ?)",
                        (evidence_id, element_id),
                    )
                else:
                    evidence_id = str(existing["evidence_record_id"])
                evidence_ids.append(evidence_id)
                connection.execute(
                    "INSERT INTO evidence_issuance_receipts VALUES (?, ?, ?, 'issued', ?, ?, ?)",
                    (
                        identity.issuance_receipt_id.value,
                        evidence_id,
                        element_id,
                        int(reused),
                        command.created_by_turn_id.value,
                        revision.value,
                    ),
                )
                rendered_hash = hashlib.sha256(occurrence.rendered_text.encode("utf-8")).hexdigest()
                connection.execute(
                    """
                    INSERT INTO format_rule_snapshots
                    VALUES (?, 'fixed_decimal', 1, ?, ?, ?, ?, ?)
                    """,
                    (
                        identity.format_rule_snapshot_id.value,
                        canonical_json({"decimal_places": occurrence.decimal_places}),
                        occurrence.binary64_bits,
                        occurrence.rendered_text,
                        rendered_hash,
                        revision.value,
                    ),
                )
                connection.execute(
                    "INSERT INTO evidence_render_receipts VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        identity.render_receipt_id.value,
                        evidence_id,
                        identity.format_rule_snapshot_id.value,
                        occurrence.rendered_text,
                        rendered_hash,
                        revision.value,
                    ),
                )
                span = occurrence.span
                connection.execute(
                    """
                    INSERT INTO evidence_presentation_uses VALUES (
                        ?, ?, ?, ?, ?, 'current_adopted_result', ?, ?
                    )
                    """,
                    (
                        identity.presentation_use_id.value,
                        evidence_id,
                        block_id,
                        span.byte_start,
                        span.byte_end,
                        command.created_by_turn_id.value,
                        revision.value,
                    ),
                )
                use_ids.append(identity.presentation_use_id.value)
                connection.execute(
                    "INSERT INTO evidence_render_bindings VALUES (?, ?, ?, ?)",
                    (
                        identity.render_binding_id.value,
                        identity.presentation_use_id.value,
                        identity.render_receipt_id.value,
                        revision.value,
                    ),
                )
                occurrence_rows.append(
                    (
                        identity.numeric_occurrence_id.value,
                        identities.coverage_manifest_id.value,
                        block_id,
                        span.char_start,
                        span.char_end,
                        span.byte_start,
                        span.byte_end,
                        span.lexeme,
                        identity.presentation_use_id.value,
                        revision.value,
                    )
                )

            count = len(occurrence_rows)
            connection.execute(
                """
                INSERT INTO numeric_coverage_manifests VALUES (
                    ?, ?, 'mandatory_formal_block', 'complete',
                    'plain_text_numeric', 1, ?, ?, ?, ?
                )
                """,
                (
                    identities.coverage_manifest_id.value,
                    block_id,
                    count,
                    count,
                    prepared.rendered.content_utf8_sha256,
                    revision.value,
                ),
            )
            connection.executemany(
                """
                INSERT INTO numeric_occurrences VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, 'bound', ?
                )
                """,
                occurrence_rows,
            )
            response = {
                "formal_result_block_id": block_id,
                "coverage_manifest_id": identities.coverage_manifest_id.value,
                "content": prepared.rendered.content,
                "evidence_record_ids": evidence_ids,
                "presentation_use_ids": use_ids,
            }
            return MutationPayload(
                response,
                (
                    JournalDraft(
                        "formal_result_block.committed",
                        "formal_result_block",
                        block_id,
                        {
                            "coverage_manifest_id": identities.coverage_manifest_id.value,
                            "numeric_occurrence_count": count,
                            "result_id": result_id,
                        },
                    ),
                ),
                (OutboxDraft("formal_result_block.changed", response),),
            )

        receipt = self._commits.commit_mutation(
            command_id=command.command_id,
            command_type="evidence.formal_block.render",
            request=request,
            mutation=mutate,
        )
        response = receipt.response
        return FormalBlockOutcome(
            FormalResultBlockId(str(response["formal_result_block_id"])),
            NumericCoverageManifestId(str(response["coverage_manifest_id"])),
            str(response["content"]),
            tuple(EvidenceRecordId(str(value)) for value in response["evidence_record_ids"]),
            tuple(
                EvidencePresentationUseId(str(value)) for value in response["presentation_use_ids"]
            ),
            receipt.commit_revision,
            receipt.replayed,
        )

    def load_lineage(self, formal_result_block_id: str, byte_start: int) -> EvidenceLineage:
        row = self._connection.execute(
            """
            SELECT occurrence.numeric_occurrence_id, occurrence.numeric_lexeme,
                   source.evidence_record_id, element.result_element_id,
                   element.semantic_key, element.result_id,
                   result.producing_stata_run_id,
                   command.research_command_instance_id, command.command_text,
                   run.input_data_version_id, element.result_source_locator_id,
                   locator.locator_json
            FROM numeric_occurrences AS occurrence
            JOIN evidence_presentation_uses AS use
              ON use.evidence_presentation_use_id =
                 occurrence.evidence_presentation_use_id
            JOIN evidence_statistical_sources AS source
              ON source.evidence_record_id = use.evidence_record_id
            JOIN result_elements AS element
              ON element.result_element_id = source.result_element_id
            JOIN results AS result ON result.result_id = element.result_id
            JOIN stata_runs AS run
              ON run.stata_run_id = result.producing_stata_run_id
            JOIN research_command_instances AS command
              ON command.stata_run_id = run.stata_run_id
             AND command.execution_ordinal = 1
            JOIN result_source_locators AS locator
              ON locator.result_source_locator_id = element.result_source_locator_id
            WHERE occurrence.formal_result_block_id = ?
              AND occurrence.byte_start = ?
            """,
            (formal_result_block_id, byte_start),
        ).fetchone()
        if row is None:
            raise ValueError("numeric occurrence does not exist at this byte offset")
        return EvidenceLineage(
            FormalResultBlockId(formal_result_block_id),
            NumericOccurrenceId(str(row["numeric_occurrence_id"])),
            str(row["numeric_lexeme"]),
            EvidenceRecordId(str(row["evidence_record_id"])),
            ResultElementId(str(row["result_element_id"])),
            str(row["semantic_key"]),
            ResultId(str(row["result_id"])),
            str(row["producing_stata_run_id"]),
            str(row["research_command_instance_id"]),
            str(row["command_text"]),
            str(row["input_data_version_id"]),
            str(row["result_source_locator_id"]),
            str(row["locator_json"]),
        )

    @staticmethod
    def _assert_turn_path(
        connection: sqlite3.Connection, turn_id: str, research_path_id: str
    ) -> None:
        row = connection.execute(
            """
            SELECT 1 FROM turns
            WHERE turn_id = ? AND research_path_id = ?
              AND status IN ('running', 'waiting')
            """,
            (turn_id, research_path_id),
        ).fetchone()
        if row is None:
            raise ValueError("authoring Turn is not active on this Research Path")

    @staticmethod
    def _assert_result_data_current(
        connection: sqlite3.Connection, research_path_id: str, result_id: str
    ) -> None:
        dependency_count = int(
            connection.execute(
                "SELECT count(*) FROM result_required_data_dependencies WHERE result_id = ?",
                (result_id,),
            ).fetchone()[0]
        )
        if dependency_count == 0:
            raise ValueError("formal Result has no declared Data dependency")
        invalid = connection.execute(
            """
            SELECT 1
            FROM result_required_data_dependencies AS dependency
            WHERE dependency.result_id = ?
              AND NOT EXISTS (
                  SELECT 1
                  FROM path_data_slots AS slot
                  JOIN path_data_adoptions AS adoption
                    ON adoption.path_data_slot_id = slot.path_data_slot_id
                  WHERE slot.research_path_id = ?
                    AND slot.canonical_key = dependency.input_data_slot_key
                    AND slot.lifecycle = 'active'
                    AND adoption.target_data_version_id = dependency.data_version_id
              )
            LIMIT 1
            """,
            (result_id, research_path_id),
        ).fetchone()
        if invalid is not None:
            raise ValueError("Result is stale because its required Data Version is not current")

    @staticmethod
    def _current_result(
        connection: sqlite3.Connection, research_path_id: str, result_slot_key: str
    ) -> tuple[str, str]:
        row = connection.execute(
            """
            SELECT slot.result_slot_id, adoption.target_result_id
            FROM result_slots AS slot
            JOIN path_result_adoptions AS adoption
              ON adoption.result_slot_id = slot.result_slot_id
            WHERE slot.research_path_id = ? AND slot.canonical_key = ?
              AND slot.lifecycle = 'active'
            """,
            (research_path_id, result_slot_key),
        ).fetchone()
        if row is None:
            raise ValueError("Result Slot has no current adopted Result")
        return str(row["result_slot_id"]), str(row["target_result_id"])
