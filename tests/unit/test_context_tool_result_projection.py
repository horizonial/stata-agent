"""Agent context keeps actionable Stata catalogs without duplicating locator graphs."""

from __future__ import annotations

import json

from stata_research_agent.persistence.context_authority import _agent_tool_result_payload


def test_stata_tool_result_context_projection_preserves_every_selectable_source_key() -> None:
    elements = [
        {
            "source_key": f"term.x{index}.coefficient",
            "statistic_kind": "coefficient",
            "value": float(index),
            "locator": {
                "locator_type": "E_MATRIX_CELL",
                "matrix": "e(b)",
                "column_key": f"x{index}",
            },
            "primitive_locators": [{"large": "x" * 200}],
        }
        for index in range(100)
    ]
    canonical = {
        "operation_id": "op_context_projection",
        "execution_status": "succeeded",
        "structured": {
            "cmd": "areg",
            "cmdline": "areg y x*, absorb(id)",
            "N": 500.0,
            "coefs": [{"large": "x" * 500} for _ in range(100)],
            "result_catalog": {
                "schema_version": "stata.result-catalog/v1alpha1",
                "elements": elements,
            },
            "stored_result_source_map": {"large": "x" * 10_000},
        },
    }

    projected = _agent_tool_result_payload(canonical)

    assert isinstance(projected, dict)
    structured = projected["structured"]
    assert isinstance(structured, dict)
    projected_elements = structured["result_catalog"]["elements"]
    assert [item["source_key"] for item in projected_elements] == [
        f"term.x{index}.coefficient" for index in range(100)
    ]
    assert all("locator" not in item for item in projected_elements)
    assert projected["context_projection"]["canonical_payload_preserved"] is True
    assert len(json.dumps(projected)) < len(json.dumps(canonical)) // 4


def test_non_stata_tool_payload_is_not_rewritten() -> None:
    payload = {"content": "full workspace text", "truncated": False}
    assert _agent_tool_result_payload(payload) is payload


def test_unstructured_stata_diagnostic_keeps_bounded_raw_output_excerpt() -> None:
    payload = {
        "operation_id": "op_diagnostic",
        "execution_status": "succeeded",
        "structured": None,
        "raw_output_excerpt": "rc_reg = 198\ninvalid syntax",
    }

    assert _agent_tool_result_payload(payload) is payload
    assert payload["raw_output_excerpt"].endswith("invalid syntax")


def test_structured_data_step_excerpt_survives_context_projection() -> None:
    payload = {
        "operation_id": "op_structured_diagnostic",
        "execution_status": "succeeded",
        "structured": {
            "result_catalog": {
                "schema_version": "stata.result-catalog/v1",
                "elements": [
                    {
                        "source_key": "scalar.N",
                        "statistic_kind": "stata_returned_scalar",
                        "value": 200.0,
                    }
                ],
            }
        },
        "raw_output_excerpt": "pwd = C:/scope\nuse_rel_rc=0\n200 observations",
    }

    projected = _agent_tool_result_payload(payload)

    assert projected["raw_output_excerpt"].endswith("200 observations")
