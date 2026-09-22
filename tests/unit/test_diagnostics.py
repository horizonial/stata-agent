"""D-231 typed diagnostics, health marker, retention, and crash capsule tests."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from stata_research_agent.application.diagnostic_service import DiagnosticService
from stata_research_agent.application.diagnostics import (
    DiagnosticDebugMode,
    DiagnosticEventCandidate,
    default_diagnostic_registry,
)
from stata_research_agent.application.sensitive_output import SensitiveOutputGate
from stata_research_agent.interfaces.filesystem_diagnostics import (
    FilesystemDiagnosticSink,
)


def candidate(**overrides) -> DiagnosticEventCandidate:
    values = {
        "event_name": "process.lifecycle",
        "severity_number": 9,
        "severity_text": "INFO",
        "safe_code": "PROCESS_READY",
        "component": "host",
        "process_role": "main_service",
        "safe_attributes": {"state_code": "ready", "generation": 1},
    }
    values.update(overrides)
    return DiagnosticEventCandidate(**values)


def service(tmp_path: Path, **sink_options):
    sink = FilesystemDiagnosticSink(tmp_path / "diagnostics", **sink_options)
    diagnostic = DiagnosticService(
        sink,
        default_diagnostic_registry(),
        SensitiveOutputGate(),
        release_id="release-test",
        build_id="build-test",
        instance_id="instance-test",
    )
    return sink, diagnostic


def test_unregistered_event_and_field_are_rejected_before_sink(tmp_path: Path) -> None:
    sink, diagnostic = service(tmp_path)
    with pytest.raises(ValueError, match="unregistered"):
        diagnostic.record(candidate(event_name="arbitrary.message"))
    with pytest.raises(ValueError, match="registered schema"):
        diagnostic.record(
            candidate(
                safe_attributes={
                    "state_code": "ready",
                    "generation": 1,
                    "message": "raw research content",
                }
            )
        )
    assert sink.snapshot_end() == 0
    assert list((tmp_path / "diagnostics" / "logs").glob("*.jsonl")) == []


def test_sensitive_candidate_is_dropped_and_exception_projection_has_no_raw_text(
    tmp_path: Path,
) -> None:
    sink, diagnostic = service(tmp_path)
    assert diagnostic.record(candidate(safe_code="sk-never-log-this-token")) is None
    secret_local = "research-value-never-log"
    try:
        raise RuntimeError(f"failure at C:/Users/alice/project: {secret_local}")
    except RuntimeError as error:
        event = diagnostic.record_exception(
            error,
            component="provider",
            process_role="main_service",
            safe_code="PROVIDER_SAFE_FAILURE",
        )
    assert event is not None
    logs = b"".join(
        path.read_bytes() for path in (tmp_path / "diagnostics" / "logs").glob("*.jsonl")
    )
    assert b"alice" not in logs
    assert secret_local.encode() not in logs
    assert b"failure at" not in logs
    assert b"RuntimeError" in logs
    assert sink.snapshot_end() == 1


def test_sink_failure_updates_minimal_health_marker_without_error_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sink, _ = service(tmp_path)
    missing_parent = tmp_path / "never-created" / "diagnostics.jsonl"
    monkeypatch.setattr(sink, "_active_log", lambda: missing_parent)
    definition = default_diagnostic_registry().validate(candidate())
    with pytest.raises(RuntimeError, match="diagnostics_degraded"):
        sink.append(
            candidate(),
            definition,
            release_id="release-test",
            build_id="build-test",
            instance_id="instance-test",
        )
    health_path = tmp_path / "diagnostics" / "diagnostics_health.json"
    health = json.loads(health_path.read_text(encoding="utf-8"))
    assert health["degraded_since"] is not None
    assert health["last_failure_code"] == "DIAGNOSTIC_SINK_FILENOTFOUNDERROR"
    rendered = health_path.read_text(encoding="utf-8")
    assert str(missing_parent) not in rendered
    assert "never-created" not in rendered


def test_rotation_retention_clear_and_safe_crash_capsule_are_non_authoritative(
    tmp_path: Path,
) -> None:
    sink, diagnostic = service(tmp_path, rotate_bytes=1, max_total_bytes=1)
    first = diagnostic.record(candidate(safe_code="PROCESS_ONE"))
    second = diagnostic.record(candidate(safe_code="PROCESS_TWO"))
    assert first is not None and second is not None
    assert len(list((tmp_path / "diagnostics" / "logs").glob("*.jsonl"))) == 2
    retention = sink.enforce_retention()
    assert retention["logs_deleted"] == 1

    capsule = diagnostic.write_crash_capsule(
        process_role="worker",
        exit_classification="unexpected_exit",
        last_state_codes=("running", "transport_lost"),
    )
    assert capsule is not None
    capsule_path = tmp_path / "diagnostics" / "crash-capsules" / capsule.path_name
    payload = json.loads(capsule_path.read_text(encoding="utf-8"))
    forbidden = ("locals", "environment", "prompt", "stdout", "stderr", "heap")
    rendered = json.dumps(payload).lower()
    assert all(item not in rendered for item in forbidden)
    assert payload["events"]

    cleared = diagnostic.clear()
    assert cleared["logs_deleted"] == 1
    assert cleared["capsules_deleted"] == 1
    assert not capsule_path.exists()
    assert os.path.isfile(tmp_path / "diagnostics" / "diagnostics_health.json")


def test_debug_mode_auto_expires_and_does_not_change_event_schema() -> None:
    debug = DiagnosticDebugMode()
    now = datetime(2026, 9, 19, tzinfo=UTC)
    expires = debug.enable(now=now)
    assert expires == now + timedelta(minutes=30)
    assert debug.is_active(now=now + timedelta(minutes=29)) is True
    assert debug.is_active(now=now + timedelta(minutes=30)) is False

    registry = default_diagnostic_registry()
    with pytest.raises(ValueError, match="registered schema"):
        registry.validate(
            candidate(
                safe_attributes={
                    "state_code": "ready",
                    "generation": 1,
                    "raw_body": "debug does not expand content fields",
                }
            )
        )


def test_unknown_diagnostic_major_projects_only_safe_version_metadata(
    tmp_path: Path,
) -> None:
    sink, _ = service(tmp_path)
    log = tmp_path / "diagnostics" / "logs" / "diagnostics-000001.jsonl"
    log.write_text(
        json.dumps(
            {
                "sequence": 1,
                "diagnostic_schema_version": "99.0",
                "event_schema_version": "8.2",
                "raw_payload": "private research value must disappear",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    event = sink.read_through(1)[0]
    assert event.event_name == "UNKNOWN_SAFE_EVENT"
    rendered = json.dumps(event.to_payload())
    assert "private research" not in rendered
    assert event.safe_attributes == {
        "original_diagnostic_schema_version": "99.0",
        "original_event_schema_version": "8.2",
    }
