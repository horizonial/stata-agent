from __future__ import annotations

from stata_research_agent.application.result_profile import RegressOperationFacts
from stata_research_agent.application.result_profile_service import (
    RegisteredResultProfileService,
)
from stata_research_agent.domain.result_profile import (
    GenericStataResultProfile,
    QualificationVerdict,
    RegressProfileEvaluation,
)


def _snapshot() -> dict[str, object]:
    return {
        "result_source_capability": {
            "capability_id": "stata.generic-result-source.v1",
            "capability_version": 1,
            "snapshot_schema_version": "stata.generic-result.snapshot/v1",
            "extractor_contract_hash": (
                "320d520badfe130782be6f85832f8e797687259d671172e49a2a5d12ed62d7e0"
            ),
        },
        "result_catalog": {
            "schema_version": "stata.result-catalog/v1",
            "elements": [
                {
                    "source_key": "scalar.N",
                    "value": 74.0,
                    "statistic_kind": "stata_returned_scalar",
                    "locator": {"locator_type": "E_SCALAR", "name": "e(N)"},
                    "primitive_locators": [],
                },
                {
                    "source_key": "term.mpg.coefficient",
                    "value": -238.9,
                    "statistic_kind": "coefficient",
                    "locator": {
                        "locator_type": "E_MATRIX_CELL",
                        "matrix": "e(b)",
                        "column_key": "mpg",
                    },
                    "primitive_locators": [],
                },
                {
                    "source_key": "return.scalar.p",
                    "value": 0.0883173266,
                    "statistic_kind": "stata_returned_scalar",
                    "locator": {"locator_type": "R_SCALAR", "name": "r(p)"},
                    "primitive_locators": [],
                },
            ],
        },
    }


def test_generic_profile_promotes_selected_stata_values_without_method_identity() -> None:
    evaluation = GenericStataResultProfile().evaluate(
        _snapshot(), selected_source_keys=("term.mpg.coefficient", "scalar.N")
    )

    assert evaluation.verdict is QualificationVerdict.QUALIFIED
    assert [element.semantic_key for element in evaluation.elements] == [
        "term.mpg.coefficient",
        "scalar.N",
    ]


def test_generic_profile_rejects_agent_invented_source_key() -> None:
    evaluation = GenericStataResultProfile().evaluate(
        _snapshot(), selected_source_keys=("scalar.invented",)
    )

    assert evaluation.verdict is QualificationVerdict.REJECTED
    assert evaluation.findings == ("RESULT_SOURCE_KEY_MISSING:scalar.invented",)
    assert evaluation.elements == ()


def test_generic_profile_promotes_postestimation_return_scalar() -> None:
    evaluation = GenericStataResultProfile().evaluate(
        _snapshot(), selected_source_keys=("return.scalar.p",)
    )

    assert evaluation.verdict is QualificationVerdict.QUALIFIED
    assert evaluation.elements[0].semantic_key == "return.scalar.p"
    assert evaluation.elements[0].statistic_kind == "stata_returned_scalar"
    assert evaluation.elements[0].locator == {
        "locator_type": "R_SCALAR",
        "name": "r(p)",
    }


def test_authority_gate_allows_traceable_in_command_data_transformation() -> None:
    facts = RegressOperationFacts(
        operation_id="op_test",
        attempt_id="attempt_test",
        manifest_id="manifest_test",
        execution_status="succeeded",
        structured_result_status="complete",
        structured={
            "cmdline": "regress lnprice mpg weight",
            "provenance": {
                "command_hash": "script-hash",
                "data_signature": "post-transform-state",
                "exec_seq": 7,
            },
        },
        receipt={
            "command_hash": "script-hash",
            "data_signature": "post-transform-state",
            "exec_seq": 7,
            "session_generation": 2,
            "runtime_environment": {"stata_version": "18"},
        },
        input_data_version_id="data_test",
        input_data_slot_key="analysis.primary",
        input_verification_receipt_id="verify_test",
        input_is_currently_verified=True,
        source_data_state_operation_id="op_predecessor",
        expected_data_state_token="pre-transform-state",
        expected_session_generation=2,
        executable_source_id="source_test",
        command_text="gen lnprice = ln(price)\nregress lnprice mpg weight",
        command_sha256="a" * 64,
        session_id="session_test",
        session_generation=2,
    )
    evaluation = RegressProfileEvaluation(QualificationVerdict.QUALIFIED, (), (), None)

    gated = RegisteredResultProfileService._apply_authority_gates(facts, evaluation)

    assert gated is evaluation
