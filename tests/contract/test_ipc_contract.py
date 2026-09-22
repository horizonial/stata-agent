"""Worker IPC version/type/extra-field and NDJSON framing tests."""

import json

import pytest
from pydantic import ValidationError

from stata_research_agent.runtime.ipc_contract import (
    PlanProposal,
    WorkerBootstrap,
    WorkerBootstrapContext,
    encode_ndjson_line,
    encode_worker_input_line,
    parse_ndjson_line,
    parse_worker_input_line,
)


def valid_plan() -> dict:
    return {
        "protocol_version": "2",
        "message_type": "plan_proposal",
        "message_id": "ipc_1",
        "turn_id": "turn_1",
        "context_revision": 1,
        "summary": "Inspect the data before estimation.",
        "structured_plan": {"steps": ["inspect"]},
    }


def test_ipc_round_trip_is_one_canonical_ndjson_line() -> None:
    parsed = parse_ndjson_line(json.dumps(valid_plan()))
    assert isinstance(parsed, PlanProposal)
    encoded = encode_ndjson_line(parsed)
    assert encoded.endswith("\n")
    assert "\n" not in encoded[:-1]
    assert parse_ndjson_line(encoded[:-1]) == parsed


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("protocol_version", "1"),
        ("message_type", "execute_sql"),
    ],
)
def test_unknown_ipc_version_and_message_type_fail_closed(field: str, value: str) -> None:
    payload = valid_plan()
    payload[field] = value
    with pytest.raises(ValidationError):
        parse_ndjson_line(json.dumps(payload))


def test_ipc_extra_fields_and_multi_line_frames_are_rejected() -> None:
    payload = valid_plan()
    payload["database_connection"] = "secret"
    with pytest.raises(ValidationError):
        parse_ndjson_line(json.dumps(payload))
    with pytest.raises(ValueError, match="one newline-free"):
        parse_ndjson_line(json.dumps(valid_plan()) + "\n{}")


def test_main_to_worker_bootstrap_is_versioned_and_reference_only() -> None:
    bootstrap = WorkerBootstrap(
        protocol_version="2",
        message_type="worker_bootstrap",
        message_id="ipc_bootstrap",
        worker_session_id="worker_1",
        turn_id="turn_1",
        context_revision=3,
        context=WorkerBootstrapContext(
            workspace_id="ws_1",
            execution_scope_id="scope_1",
            research_path_id="path_1",
            plan_revision_id="planrev_1",
            research_state_revision=2,
            data_version_ids=("data_1",),
            artifact_refs=("artifact_1",),
            tool_names=("stata.run",),
            remaining_step_budget=61,
            remaining_tool_budget=127,
        ),
    )
    encoded = encode_worker_input_line(bootstrap)
    assert parse_worker_input_line(encoded[:-1]) == bootstrap

    payload = bootstrap.model_dump(mode="json")
    payload["context"]["artifact_refs"] = [r"C:\\research\\data.dta"]
    with pytest.raises(ValidationError, match="Artifact IDs"):
        parse_worker_input_line(json.dumps(payload))
