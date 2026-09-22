"""M0-02 identity, revision, status, and serialization contracts."""

import inspect
import json
from uuid import UUID

import pytest

from stata_research_agent.domain import identifiers
from stata_research_agent.domain.errors import DomainValidationError
from stata_research_agent.domain.identifiers import (
    ConversationId,
    MessageId,
    OpaqueId,
    OperationAttemptId,
    ResearchPathId,
    TurnId,
    WorkspaceId,
)
from stata_research_agent.domain.provenance import (
    OperationAttemptProvenance,
    UserMessageProvenance,
)
from stata_research_agent.domain.revisions import EntityRevision, Ordinal, WorkspaceRevision
from stata_research_agent.domain.serialization import to_primitive
from stata_research_agent.domain.status import (
    ContractContinuationDecision,
    RecoveryClassification,
    StopGuardDecision,
    TurnStatus,
)


@pytest.mark.parametrize(
    ("identity_type", "value"),
    [
        (WorkspaceId, "ws_01HZX0EXAMPLE"),
        (ConversationId, "conv_01HZX0EXAMPLE"),
        (MessageId, "msg_01HZX0EXAMPLE"),
        (TurnId, "turn_01HZX0EXAMPLE"),
        (ResearchPathId, "path_01HZX0EXAMPLE"),
        (OperationAttemptId, "attempt_01HZX0EXAMPLE"),
    ],
)
def test_opaque_ids_round_trip_through_their_concrete_type(identity_type: type, value: str) -> None:
    identity = identity_type(value)
    assert identity_type.from_primitive(identity.to_primitive()) == identity


def test_equal_text_does_not_make_different_identity_types_equal() -> None:
    assert WorkspaceId("ws_same") != ConversationId("conv_same")
    assert ResearchPathId("path_same") != TurnId("turn_same")


@pytest.mark.parametrize(
    "invalid",
    ["", " ws_leading", "ws_trailing ", "ws_two words", "ws_line\nbreak", "wrong_value", "ws_"],
)
def test_opaque_ids_reject_ambiguous_values(invalid: str) -> None:
    with pytest.raises(DomainValidationError):
        WorkspaceId(invalid)


def test_revision_numeric_domains_and_increment_are_explicit() -> None:
    assert WorkspaceRevision(0).next() == WorkspaceRevision(1)
    assert EntityRevision(1).next() == EntityRevision(2)
    assert Ordinal(1).to_primitive() == 1
    for invalid in (-1, True, 1.5):
        with pytest.raises(DomainValidationError):
            WorkspaceRevision(invalid)  # type: ignore[arg-type]
    with pytest.raises(DomainValidationError):
        EntityRevision(0)


def test_turn_terminal_semantics_are_closed() -> None:
    assert TurnStatus.RUNNING.is_terminal is False
    assert TurnStatus.WAITING.is_terminal is False
    assert TurnStatus.SUCCEEDED.is_terminal is True
    assert TurnStatus.PARTIAL.is_terminal is True
    assert TurnStatus.PAUSED.is_terminal is True
    assert TurnStatus.FAILED.is_terminal is True


def test_closed_vocabularies_have_stable_wire_values() -> None:
    assert StopGuardDecision.TERMINATE.value == "terminate"
    assert RecoveryClassification.OUTCOME_UNKNOWN.value == "outcome_unknown"
    assert ContractContinuationDecision.RETAIN_CONTRACT_REVISION.value == "retain_contract_revision"


def test_provenance_union_serializes_to_deterministic_json_primitives() -> None:
    user = UserMessageProvenance(kind="user_message", message_id=MessageId("msg_1"))
    attempt = OperationAttemptProvenance(
        kind="operation_attempt", operation_attempt_id=OperationAttemptId("attempt_1")
    )
    assert json.dumps(to_primitive(user), sort_keys=True) == (
        '{"kind": "user_message", "message_id": "msg_1"}'
    )
    assert to_primitive(attempt) == {
        "kind": "operation_attempt",
        "operation_attempt_id": "attempt_1",
    }


def test_unknown_values_fail_closed_at_enum_construction() -> None:
    with pytest.raises(ValueError):
        TurnStatus("sleeping")


def test_every_registered_identity_type_has_a_unique_prefix_and_round_trips() -> None:
    identity_types = [
        value
        for value in vars(identifiers).values()
        if inspect.isclass(value) and value is not OpaqueId and issubclass(value, OpaqueId)
    ]
    prefixes = [identity_type.prefix for identity_type in identity_types]
    assert all(prefix.endswith("_") for prefix in prefixes)
    assert len(prefixes) == len(set(prefixes))

    stable_payload = UUID(int=42).hex
    for identity_type in identity_types:
        identity = identity_type(f"{identity_type.prefix}{stable_payload}")
        assert identity_type.from_primitive(identity.to_primitive()) == identity


def test_identity_prefixes_fail_closed_across_object_types() -> None:
    with pytest.raises(DomainValidationError):
        WorkspaceId("conv_01HZX0EXAMPLE")
