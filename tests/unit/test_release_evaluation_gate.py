from __future__ import annotations

from stata_research_agent.interfaces.release_evaluation_gate import (
    CORE_DIMENSIONS,
    FULL_ONLY_DIMENSIONS,
)


def test_release_gate_has_non_overlapping_named_capability_dimensions() -> None:
    dimensions = CORE_DIMENSIONS + FULL_ONLY_DIMENSIONS
    assert len({item.name for item in dimensions}) == len(dimensions)
    nodes = [node for item in dimensions for node in item.test_nodes]
    assert len(set(nodes)) == len(nodes)
    assert {item.name for item in CORE_DIMENSIONS} == {
        "autonomous_research",
        "researcher_control",
        "data_fidelity_and_lineage",
        "crash_recovery",
        "retrieval_grounding_and_safety",
        "runtime_resilience",
        "memory_tool_and_judge_quality",
        "retrieval_index_upgrade",
        "provider_delta_streaming",
        "multilingual_contract_integrity",
    }
    assert all(not item.allow_skips for item in dimensions)
