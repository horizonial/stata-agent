"""The production model sees reviewed positive and negative Tool affordances."""

from stata_research_agent.application.default_tool_contracts import (
    python_run_contract,
    shell_run_contract,
)
from stata_research_agent.interfaces.production_turn_runner import (
    ProductionTurnRunner,
    production_adopt_analysis_output_contract,
    production_model_tool_schema,
    production_open_memory_contract,
    production_open_stata_contract,
    production_search_knowledge_contract,
    production_search_memory_contract,
)
from stata_research_agent.interfaces.workspace_file_tools import (
    workspace_list_artifacts_contract,
)


def test_production_tool_schemas_include_use_boundaries() -> None:
    schemas = {
        item["name"]: item
        for item in (
            production_model_tool_schema(production_open_stata_contract()),
            production_model_tool_schema(python_run_contract()),
            production_model_tool_schema(shell_run_contract()),
            production_model_tool_schema(production_adopt_analysis_output_contract()),
            production_model_tool_schema(production_search_knowledge_contract()),
            production_model_tool_schema(production_search_memory_contract()),
            production_model_tool_schema(production_open_memory_contract()),
            production_model_tool_schema(workspace_list_artifacts_contract()),
        )
    }
    assert all(item["description"].strip() for item in schemas.values())
    assert "formal estimations" in schemas["stata.execute"]["description"]
    assert "Do not use it to claim adoption" in schemas["stata.execute"]["description"]
    assert schemas["stata.execute"]["input_schema"]["properties"]["execution_role"]["enum"] == [
        "data_step",
        "formal_result_candidate",
        "formal_post_estimation",
    ]
    artifact_outputs = schemas["stata.execute"]["input_schema"]["properties"]["artifact_outputs"]
    assert artifact_outputs["maxItems"] == 32
    assert "<ATTEMPT_STAGING>" in artifact_outputs["description"]
    assert (
        "Raw Python output is not formal document evidence" in schemas["python.run"]["description"]
    )
    assert "Do not use it for Stata estimation" in schemas["shell.run"]["description"]
    assert "including a regression" in schemas["research.adopt_analysis_output"]["description"]
    assert schemas["research.search_knowledge"]["input_schema"]["required"] == ["query"]
    assert schemas["memory.search"]["input_schema"]["required"] == ["query"]
    assert schemas["memory.open"]["input_schema"]["required"] == ["memory_item_ids"]
    assert "never statistical Evidence" in schemas["memory.open"]["description"]
    assert "authoritative Artifact Ledger" in schemas["research.list_artifacts"]["description"]


def test_provider_billing_failure_prompt_preserves_completed_research_state() -> None:
    prompt = ProductionTurnRunner._runtime_failure_prompt("failed", "provider_http_402")

    assert "HTTP 402" in prompt
    assert "Stata Run" in prompt
    assert "Result" in prompt
