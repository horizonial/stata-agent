"""Tool schema round-trip and fail-closed boundary tests."""

import pytest
from pydantic import ValidationError

from stata_research_agent.runtime.tool_contract import (
    CanonicalToolCallEnvelope,
    CompletionManifestEnvelope,
    ManifestArtifact,
    canonical_arguments_hash,
)


def valid_call(**overrides):
    arguments = overrides.pop("canonical_arguments", {"artifact_id": "artifact_1"})
    payload = {
        "schema_version": "1",
        "tool_call_id": "toolcall_1",
        "assistant_output_id": "assistant_1",
        "call_ordinal": 1,
        "requested_tool_name": "artifact.read",
        "raw_arguments_snapshot_id": "argsraw_1",
        "canonical_arguments_snapshot_id": "argscanonical_1",
        "canonical_arguments": arguments,
        "arguments_hash": canonical_arguments_hash(arguments),
        "proposal_status": "proposed",
        "created_revision": 1,
    }
    payload.update(overrides)
    return payload


def test_tool_call_round_trip_and_argument_hash() -> None:
    call = CanonicalToolCallEnvelope.model_validate(valid_call())
    assert CanonicalToolCallEnvelope.model_validate_json(call.model_dump_json()) == call


def test_unknown_version_and_hash_mismatch_fail_closed_but_tool_namespace_is_open() -> None:
    with pytest.raises(ValidationError):
        CanonicalToolCallEnvelope.model_validate(valid_call(schema_version="2"))
    open_call = CanonicalToolCallEnvelope.model_validate(
        valid_call(requested_tool_name="community.custom-tool")
    )
    assert open_call.requested_tool_name == "community.custom-tool"
    with pytest.raises(ValidationError):
        CanonicalToolCallEnvelope.model_validate(valid_call(requested_tool_name="unsafe name"))
    with pytest.raises(ValidationError, match="arguments_hash"):
        CanonicalToolCallEnvelope.model_validate(valid_call(arguments_hash="0" * 64))


@pytest.mark.parametrize(
    "path",
    [r"C:\\research\\data.dta", "/home/user/data.dta", r"\\\\server\\share\\x"],
)
def test_arbitrary_absolute_paths_are_rejected_at_tool_boundary(path: str) -> None:
    with pytest.raises(ValidationError, match="absolute OS paths"):
        CanonicalToolCallEnvelope.model_validate(valid_call(canonical_arguments={"path": path}))


def test_completion_manifest_uses_logical_locator_and_exact_integrity_fields() -> None:
    manifest = CompletionManifestEnvelope(
        schema_version="1",
        manifest_id="manifest_1",
        operation_id="op_1",
        attempt_id="attempt_1",
        session_generation=1,
        execution_status="completed",
        artifacts=(
            ManifestArtifact(
                candidate_id="candidate_1",
                managed_locator="attempt://output/table.rtf",
                size=123,
                sha256="a" * 64,
            ),
        ),
    )
    assert manifest.artifacts[0].size == 123
    with pytest.raises(ValidationError):
        ManifestArtifact(
            candidate_id="candidate_2",
            managed_locator="../escaped.rtf",
            size=1,
            sha256="b" * 64,
        )
