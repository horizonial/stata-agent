"""Reviewed first-party open Tool Contracts for staged arbitrary execution."""

from __future__ import annotations

from .tool_broker import ResourceClaimTemplate, ToolContractDefinition


def python_run_contract() -> ToolContractDefinition:
    return _sandbox_contract(
        "python.run",
        "python.execute",
        (
            "Run sandboxed Python for data processing, exploratory models, figures, or tests. "
            "Raw Python output is not formal document evidence; classify it and obtain a later "
            "user confirmation before adoption."
        ),
    )


def shell_run_contract() -> ToolContractDefinition:
    return _sandbox_contract(
        "shell.run",
        "shell.execute",
        (
            "Run sandboxed PowerShell for environment inspection or file automation. Do not use "
            "it for Stata estimation, literature retrieval, or direct formal evidence."
        ),
    )


def _sandbox_contract(
    tool_name: str, operation_kind: str, display_name: str
) -> ToolContractDefinition:
    return ToolContractDefinition(
        tool_name,
        "1.0.0",
        display_name,
        operation_kind,
        {
            "type": "object",
            "properties": {
                "code": {"type": "string"},
                "inputs": {
                    "type": "array",
                    "default": [],
                    "description": "Artifact ID to relative staging target bindings.",
                },
                "network_mode": {
                    "type": "string",
                    "default": "block",
                    "description": "block by default; allow requires permission policy.",
                },
                "timeout_seconds": {"type": "integer", "default": 300},
            },
            "required": ["code"],
            "additionalProperties": False,
        },
        {"type": "object"},
        "local_runtime",
        "write_or_unknown",
        "exclusive_scope",
        "non_replayable",
        "never",
        "terminatable_with_recovery",
        300.0,
        3600.0,
        1_000_000,
        (ResourceClaimTemplate("execution-scope:current", "exclusive"),),
        "sandboxed_staged_execution",
    )
