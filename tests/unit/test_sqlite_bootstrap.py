"""M0-03 SQLite connection, migration, and Workspace identity contracts."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from stata_research_agent.domain.identifiers import WorkspaceId
from stata_research_agent.persistence.errors import (
    MigrationChecksumError,
    MigrationLeaseHeldError,
    SchemaCompatibilityError,
    WorkspaceIdentityError,
)
from stata_research_agent.persistence.migrations import MIGRATIONS, Migration, MigrationRunner
from stata_research_agent.persistence.workspace import WorkspaceDatabase


def create_workspace(tmp_path: Path, value: str = "ws_test") -> WorkspaceDatabase:
    workspace = WorkspaceDatabase(tmp_path / value, WorkspaceId(value))
    workspace.create()
    return workspace


def test_create_and_reopen_workspace_with_connection_contract(tmp_path: Path) -> None:
    workspace = create_workspace(tmp_path)
    connection = workspace.open(writable=True)
    try:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5_000
        assert connection.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
    finally:
        connection.close()


def test_schema_29_migrates_data_binding_cache_and_project_memory(
    tmp_path: Path,
) -> None:
    workspace_id = WorkspaceId("ws_schema_27")
    legacy = WorkspaceDatabase(
        tmp_path / workspace_id.value,
        workspace_id,
        migration_runner=MigrationRunner(MIGRATIONS[:26]),
    )
    legacy.create()
    connection = legacy.connection_contract.connect(legacy.database_path, writable=True)
    try:
        MigrationRunner().migrate(connection, workspace_id)
        MigrationRunner().validate(connection, workspace_id)
        columns = {
            str(row["name"])
            for row in connection.execute(
                "PRAGMA table_info(stata_operation_input_bindings)"
            ).fetchall()
        }
        assert "source_data_state_operation_id" in columns
        assert "source_data_load_operation_id" not in columns
        sql = str(
            connection.execute(
                """
                SELECT sql FROM sqlite_schema
                WHERE type = 'table' AND name = 'stata_operation_input_bindings'
                """
            ).fetchone()[0]
        )
        assert "data_step" in sql
        assert connection.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
        assert connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = 'memory_revisions'"
        ).fetchone()
        provider_columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(provider_attempts)").fetchall()
        }
        assert {"cached_input_tokens", "uncached_input_tokens"} <= provider_columns
    finally:
        connection.close()


def test_every_authoritative_table_is_strict(tmp_path: Path) -> None:
    workspace = create_workspace(tmp_path)
    connection = workspace.open(writable=False)
    try:
        rows = connection.execute(
            """
            SELECT name, sql FROM sqlite_schema
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
              AND name NOT LIKE 'knowledge_nodes_fts%'
            ORDER BY name
            """
        ).fetchall()
        assert {row["name"] for row in rows} == {
            "analysis_document_eligibility_receipts",
            "analysis_environment_snapshots",
            "analysis_evidence_records",
            "analysis_output_adoptions",
            "analysis_output_artifacts",
            "analysis_output_classifications",
            "analysis_output_elements",
            "analysis_output_inputs",
            "analysis_outputs",
            "artifact_candidate_sources",
            "artifact_capture_plan_outputs",
            "artifact_capture_plans",
            "artifact_file_observation_sources",
            "artifact_location_history",
            "artifact_locations",
            "artifact_state_history",
            "artifact_states",
            "artifact_verification_receipts",
            "artifact_promotions",
            "artifacts",
            "assistant_dispatch_plan_adoptions",
            "assistant_outputs",
            "budget_policy_snapshots",
            "canonical_tool_argument_snapshots",
            "canonical_tool_results",
            "command_receipts",
            "completion_contract_revisions",
            "completion_contract_revision_profiles",
            "completion_contracts",
            "completion_obligations",
            "completion_manifests",
            "completion_manifest_artifacts",
            "context_build_decisions",
            "context_items",
            "context_manifests",
            "conversations",
            "execution_scopes",
            "environment_snapshots",
            "evidence_issuance_receipts",
            "evidence_presentation_uses",
            "evidence_records",
            "evidence_render_bindings",
            "evidence_render_receipts",
            "evidence_statistical_sources",
            "evidence_scope_manifests",
            "evaluation_findings",
            "evaluation_policy_snapshots",
            "evaluation_reports",
            "evaluation_requests",
            "estimation_sample_manifests",
            "executable_sources",
            "file_observations",
            "formal_result_blocks",
            "format_rule_snapshots",
            "goal_coverages",
            "journal_entries",
            "knowledge_chunks",
            "knowledge_context_uses",
            "knowledge_node_context_uses",
            "knowledge_edges",
            "knowledge_embedding_index_revisions",
            "knowledge_node_embeddings",
            "knowledge_nodes",
            "knowledge_parse_revisions",
            "knowledge_retrieval_hops",
            "knowledge_retrieval_query_variants",
            "knowledge_retrieval_candidates",
            "knowledge_retrieval_selections",
            "knowledge_retrieval_session_state_history",
            "knowledge_retrieval_session_states",
            "knowledge_retrieval_sessions",
            "knowledge_source_memberships",
            "knowledge_source_revisions",
            "knowledge_source_states",
            "knowledge_sources",
            "knowledge_document_revisions",
            "knowledge_document_states",
            "knowledge_documents",
            "knowledge_index_runs",
            "messages",
            "memory_context_uses",
            "memory_candidates",
            "memory_compaction_checkpoints",
            "memory_current_states",
            "memory_episodes",
            "memory_items",
            "memory_maintenance_jobs",
            "memory_payload_files",
            "memory_provider_attempts",
            "memory_retention_history",
            "memory_retention_states",
            "memory_revision_sources",
            "memory_revisions",
            "memory_state_history",
            "memory_summary_projections",
            "conversation_memory_policies",
            "migration_lease",
            "model_input_snapshots",
            "model_invocations",
            "model_policy_snapshots",
            "outbox_entries",
            "outbound_material_records",
            "operation_attempts",
            "operation_reconciliation_authorizations",
            "operation_tool_call_links",
            "operations",
            "obligation_state_observations",
            "path_data_adoption_history",
            "path_data_adoptions",
            "path_data_slot_origins",
            "path_data_slots",
            "path_branch_manifests",
            "path_document_adoption_history",
            "path_document_adoptions",
            "path_document_slot_origins",
            "path_plan_adoption_history",
            "path_plan_adoptions",
            "path_result_adoption_history",
            "path_result_adoptions",
            "path_result_slot_origins",
            "pause_intents",
            "permission_snapshots",
            "provider_attempts",
            "provider_request_snapshots",
            "research_path_parents",
            "research_path_profiles",
            "research_paths",
            "recovery_reports",
            "revision_obligation_entries",
            "research_command_instances",
            "result_candidates",
            "result_capture_points",
            "result_capture_snapshots",
            "result_contracts",
            "result_elements",
            "result_profiles",
            "result_qualification_reports",
            "result_required_data_dependencies",
            "result_slots",
            "result_source_locators",
            "results",
            "plan_node_dependencies",
            "plan_nodes",
            "plan_revision_nodes",
            "plan_revision_parents",
            "plan_revisions",
            "plans",
            "schema_meta",
            "schema_migrations",
            "skill_activation_manifests",
            "skill_adoption_history",
            "skill_adoptions",
            "skill_change_candidates",
            "skill_change_current_states",
            "skill_change_state_history",
            "skill_context_uses",
            "skill_evolution_candidate_sources",
            "skill_evolution_candidates",
            "skill_evolution_current_states",
            "skill_evolution_state_history",
            "skill_evaluation_attempts",
            "skill_evaluation_reports",
            "skill_evaluation_runs",
            "skill_improvement_proposals",
            "skill_outcome_observations",
            "skill_publication_manifests",
            "skill_version_change_sources",
            "skill_versions",
            "sandbox_execution_receipts",
            "stata_operation_requests",
            "stata_operation_input_bindings",
            "stata_run_plan_bindings",
            "stata_runs",
            "steps",
            "stop_guard_decisions",
            "table_cell_evidence_uses",
            "table_export_input_manifests",
            "table_export_manifests",
            "table_export_profiles",
            "table_numeric_coverage_manifests",
            "table_render_receipts",
            "table_render_shape_manifests",
            "tool_admissions",
            "tool_call_status_history",
            "tool_calls",
            "tool_contracts",
            "tool_dispatch_plan_entries",
            "tool_dispatch_plans",
            "tool_resource_claims",
            "tool_resource_leases",
            "turns",
            "turn_runtime_budgets",
            "turn_budget_accounts",
            "turn_budget_usage_history",
            "turn_context_baselines",
            "turn_continuations",
            "turn_goal_policies",
            "turn_outcome_feedback",
            "raw_tool_argument_snapshots",
            "data_version_code_artifacts",
            "data_version_parents",
            "data_versions",
            "delivery_gate_reports",
            "document_manifests",
            "document_diffs",
            "document_merge_receipts",
            "document_parse_receipts",
            "document_return_reports",
            "document_raw_return_reports",
            "evidence_projection_checkpoints",
            "statistical_evidence_current_states",
            "analysis_evidence_current_states",
            "statistical_evidence_use_validation_receipts",
            "analysis_evidence_use_validation_receipts",
            "document_revisions",
            "document_revision_merge_inputs",
            "document_revision_view_policies",
            "document_slots",
            "documents",
            "workspace_commits",
            "workspace_identity",
            "workspace_migration_attempt_history",
            "workspace_migration_attempts",
            "workspace_migration_lock",
            "workspace_migration_receipts",
            "workspace_write_lane",
            "waiting_answers",
            "waiting_requests",
            "trusted_derivation_receipts",
            "numeric_coverage_manifests",
            "numeric_occurrences",
        }
        assert all(str(row["sql"]).rstrip().endswith("STRICT") for row in rows)
    finally:
        connection.close()


def test_strict_typing_and_foreign_keys_are_enforced(tmp_path: Path) -> None:
    workspace = create_workspace(tmp_path)
    connection = workspace.open(writable=True)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE schema_meta SET current_version = 'not-an-integer' WHERE singleton_id = 1"
            )
        connection.execute("CREATE TEMP TABLE parent(id INTEGER PRIMARY KEY)")
        connection.execute("CREATE TEMP TABLE child(parent_id INTEGER REFERENCES parent(id))")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO child(parent_id) VALUES (999)")
    finally:
        connection.close()


def test_applied_migration_rows_are_immutable(tmp_path: Path) -> None:
    workspace = create_workspace(tmp_path)
    connection = workspace.open(writable=True)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("UPDATE schema_migrations SET name = 'changed' WHERE version = 1")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("DELETE FROM schema_migrations WHERE version = 1")
    finally:
        connection.close()


def test_registry_checksum_drift_fails_closed(tmp_path: Path) -> None:
    workspace = create_workspace(tmp_path)
    changed_first = Migration(
        version=1,
        name=MIGRATIONS[0].name,
        statements=MIGRATIONS[0].statements + ("SELECT 1",),
    )
    incompatible = WorkspaceDatabase(
        workspace.root,
        workspace.workspace_id,
        migration_runner=MigrationRunner((changed_first, *MIGRATIONS[1:])),
    )
    with pytest.raises(MigrationChecksumError):
        incompatible.open(writable=False)


def test_newer_schema_version_refuses_write_mode(tmp_path: Path) -> None:
    workspace = create_workspace(tmp_path)
    raw = sqlite3.connect(workspace.database_path)
    try:
        raw.execute("PRAGMA user_version = 999")
    finally:
        raw.close()
    with pytest.raises(SchemaCompatibilityError, match="newer"):
        workspace.open(writable=True)


def test_workspace_identity_mismatch_fails_closed(tmp_path: Path) -> None:
    workspace = create_workspace(tmp_path, "ws_original")
    wrong = WorkspaceDatabase(workspace.root, WorkspaceId("ws_other"))
    with pytest.raises(WorkspaceIdentityError):
        wrong.open(writable=False)


def test_live_migration_lease_blocks_a_pending_upgrade(tmp_path: Path) -> None:
    workspace = create_workspace(tmp_path)
    connection = workspace.open(writable=True)
    try:
        connection.execute(
            """
            UPDATE migration_lease
            SET owner_token = 'other', acquired_at_epoch = 100, expires_at_epoch = 10000
            WHERE singleton_id = 1
            """
        )
    finally:
        connection.close()

    next_migration = Migration(
        version=len(MIGRATIONS) + 1,
        name="test_pending_upgrade",
        statements=("CREATE TABLE future_table(id INTEGER PRIMARY KEY) STRICT",),
    )
    runner = MigrationRunner((*MIGRATIONS, next_migration), now_epoch=lambda: 500)
    connection = workspace.connection_contract.connect(workspace.database_path, writable=True)
    try:
        with pytest.raises(MigrationLeaseHeldError):
            runner.migrate(connection, workspace.workspace_id)
    finally:
        connection.close()


def test_expired_migration_lease_can_be_recovered_by_new_owner(tmp_path: Path) -> None:
    workspace = create_workspace(tmp_path)
    connection = workspace.open(writable=True)
    try:
        connection.execute(
            """
            UPDATE migration_lease
            SET owner_token = 'dead', acquired_at_epoch = 100, expires_at_epoch = 200
            WHERE singleton_id = 1
            """
        )
    finally:
        connection.close()

    next_migration = Migration(
        version=len(MIGRATIONS) + 1,
        name="test_recovered_upgrade",
        statements=("CREATE TABLE future_table(id INTEGER PRIMARY KEY) STRICT",),
    )
    runner = MigrationRunner((*MIGRATIONS, next_migration), now_epoch=lambda: 500)
    connection = workspace.connection_contract.connect(workspace.database_path, writable=True)
    try:
        runner.migrate(connection, workspace.workspace_id)
        runner.validate(connection, workspace.workspace_id)
        assert connection.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS) + 1
        lease = connection.execute(
            "SELECT owner_token, expires_at_epoch FROM migration_lease WHERE singleton_id = 1"
        ).fetchone()
        assert tuple(lease) == (None, None)
    finally:
        connection.close()
