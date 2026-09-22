"""D-231 frozen, local-only, recursively scanned Diagnostic Bundle vertical."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from stata_research_agent.application.control import (
    CreateWorkspaceCommand,
    SubmitMessageCommand,
)
from stata_research_agent.application.diagnostic_bundle import DiagnosticBundleMode
from stata_research_agent.application.diagnostic_service import DiagnosticService
from stata_research_agent.application.diagnostics import (
    DiagnosticEventCandidate,
    default_diagnostic_registry,
)
from stata_research_agent.application.sensitive_output import SensitiveOutputGate
from stata_research_agent.application.workspace_service import WorkspaceControlService
from stata_research_agent.domain.identifiers import CommandId, WorkspaceId
from stata_research_agent.interfaces.diagnostic_bundle_builder import (
    DiagnosticBundleBuilder,
)
from stata_research_agent.interfaces.filesystem_diagnostics import (
    FilesystemDiagnosticSink,
)
from stata_research_agent.persistence.control_store import SqliteControlStore
from stata_research_agent.persistence.diagnostic_projection import (
    SqliteDiagnosticWorkspaceProjection,
)
from stata_research_agent.persistence.workspace import WorkspaceDatabase
from stata_research_agent.runtime.uuid_identity import UuidIdentityGenerator


def initialized(tmp_path: Path):
    workspace_root = tmp_path / "workspace-with-private-title"
    database = WorkspaceDatabase(workspace_root, WorkspaceId("ws_diagnostic_bundle"))
    database.create()
    connection = database.open(writable=True)
    control = WorkspaceControlService(SqliteControlStore(connection), UuidIdentityGenerator())
    control.create_workspace(
        CreateWorkspaceCommand(
            CommandId("cmd_diagnostic_workspace"), WorkspaceId("ws_diagnostic_bundle")
        )
    )
    secret = "sk-diagnostic-canary-123456789"
    first_turn = control.submit_message(
        SubmitMessageCommand(
            CommandId("cmd_diagnostic_message_one"),
            f"Private variable wage_private and {secret}; Licensed to Researcher A",
        )
    )
    sink = FilesystemDiagnosticSink(tmp_path / "app-control" / "diagnostics")
    diagnostic = DiagnosticService(
        sink,
        default_diagnostic_registry(),
        SensitiveOutputGate(),
        release_id="release-test",
        build_id="build-test",
        instance_id="instance-test",
    )
    for code in ("PROCESS_READY", "PROCESS_IDLE"):
        assert (
            diagnostic.record(
                DiagnosticEventCandidate(
                    "process.lifecycle",
                    9,
                    "INFO",
                    code,
                    "host",
                    "main_service",
                    {"state_code": code.lower(), "generation": 1},
                    turn_id=first_turn.turn_id.value,
                )
            )
            is not None
        )
    projection = SqliteDiagnosticWorkspaceProjection(connection)
    builder = DiagnosticBundleBuilder(
        sink,
        SensitiveOutputGate(),
        tmp_path / "app-control" / "bundle-staging",
        system_profile={
            "release_id": "release-test",
            "build_id": "build-test",
            "windows_build": "windows-11",
            "cpu_arch": "x86_64",
            "browser_capability": True,
            "stata_version": "18",
            "stata_edition": "MP",
            "stata_arch": "x64",
            "supported_profile": True,
            "settings_flags": {"debug_mode": False},
            "resource_limits": {"tool_calls": 128},
        },
        workspace_projection=projection,
    )
    return connection, control, first_turn, sink, builder, secret


def read_zip(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def test_system_only_bundle_excludes_workspace_and_all_canary_variants(
    tmp_path: Path,
) -> None:
    connection, _, _, _, builder, secret = initialized(tmp_path)
    try:
        request = builder.create_request(
            DiagnosticBundleMode.SYSTEM_ONLY, requesting_user_action=True
        )
        preview = builder.preview(request)
        assert "workspace_sqlite" in preview["excluded_categories"]
        output = tmp_path / "system-only.zip"
        outcome = builder.build(request, output, protected_values=(secret,))
        assert outcome.output_path == output.absolute()
        members = read_zip(output)
        assert "workspace/safe-structure.json" not in members
        assert "bundle-manifest.json" in members
        whole = output.read_bytes().lower()
        assert secret.encode().lower() not in whole
        assert b"wage_private" not in whole
        assert b"researcher a" not in whole
        assert b"workspace.sqlite3" not in whole
        manifest = json.loads(members["bundle-manifest.json"])
        assert manifest["authenticity"] == "not provided; bundle is unsigned"
        assert manifest["diagnostic_snapshot_end"] == 2
        assert DiagnosticBundleBuilder.verify_integrity(output) is True
        tampered = tmp_path / "tampered.zip"
        with zipfile.ZipFile(output) as source, zipfile.ZipFile(tampered, "x") as target:
            for name in source.namelist():
                payload = source.read(name)
                if name == "README.txt":
                    payload = b"tampered\n"
                target.writestr(name, payload)
        assert DiagnosticBundleBuilder.verify_integrity(tampered) is False
        assert not any((tmp_path / "app-control" / "bundle-staging").iterdir())
    finally:
        connection.close()


def test_scoped_bundle_freezes_workspace_and_diagnostic_boundaries_and_pseudonyms(
    tmp_path: Path,
) -> None:
    connection, control, first_turn, sink, builder, secret = initialized(tmp_path)
    try:
        request_one = builder.create_request(
            DiagnosticBundleMode.SCOPED_WORKSPACE,
            turn_id=first_turn.turn_id.value,
            requesting_user_action=True,
        )
        frozen_revision = request_one.requested_workspace_revision
        frozen_diagnostics = request_one.diagnostic_snapshot_end
        control.submit_message(
            SubmitMessageCommand(CommandId("cmd_diagnostic_message_late"), "late private message")
        )
        definition = default_diagnostic_registry().validate(
            DiagnosticEventCandidate(
                "process.lifecycle",
                9,
                "INFO",
                "PROCESS_LATE",
                "host",
                "main_service",
                {"state_code": "late", "generation": 1},
            )
        )
        sink.append(
            DiagnosticEventCandidate(
                "process.lifecycle",
                9,
                "INFO",
                "PROCESS_LATE",
                "host",
                "main_service",
                {"state_code": "late", "generation": 1},
            ),
            definition,
            release_id="release-test",
            build_id="build-test",
            instance_id="instance-test",
        )

        first_path = tmp_path / "scoped-one.zip"
        first = builder.build(request_one, first_path, protected_values=(secret,))
        members = read_zip(first_path)
        summary = json.loads(members["workspace/safe-structure.json"])
        events = json.loads(members["diagnostics/events.json"])
        assert summary["authoritative_revision"] == frozen_revision
        assert first.diagnostic_snapshot_end == frozen_diagnostics == 2
        assert len(events) == 2
        assert "late private message" not in first_path.read_bytes().decode(
            "latin-1", errors="ignore"
        )
        turn_pseudonyms = {event["turn_id"] for event in events}
        assert len(turn_pseudonyms) == 1
        assert next(iter(turn_pseudonyms)).startswith("pseudo_")

        request_two = builder.create_request(
            DiagnosticBundleMode.SCOPED_WORKSPACE,
            turn_id=first_turn.turn_id.value,
            requesting_user_action=True,
        )
        second_path = tmp_path / "scoped-two.zip"
        builder.build(request_two, second_path, protected_values=(secret,))
        second_events = json.loads(read_zip(second_path)["diagnostics/events.json"])
        assert second_events[0]["turn_id"] != events[0]["turn_id"]
    finally:
        connection.close()


def test_bundle_fails_closed_and_cleans_staging_on_secret_in_safe_source(
    tmp_path: Path,
) -> None:
    connection, _, _, _, builder, _ = initialized(tmp_path)
    try:
        request = builder.create_request(
            DiagnosticBundleMode.SYSTEM_ONLY, requesting_user_action=True
        )
        with pytest.raises(ValueError, match="Sensitive Output Gate"):
            builder.build(
                request,
                tmp_path / "blocked.zip",
                protected_values=("release-test",),
            )
        assert not (tmp_path / "blocked.zip").exists()
        assert not any((tmp_path / "app-control" / "bundle-staging").iterdir())
    finally:
        connection.close()


def test_diagnostic_clear_does_not_change_workspace_revision(tmp_path: Path) -> None:
    connection, _, _, sink, _, _ = initialized(tmp_path)
    try:
        before = connection.execute(
            "SELECT max(workspace_revision) FROM workspace_commits"
        ).fetchone()[0]
        result = sink.clear()
        after = connection.execute(
            "SELECT max(workspace_revision) FROM workspace_commits"
        ).fetchone()[0]
        assert result["logs_deleted"] == 1
        assert after == before
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        connection.close()
