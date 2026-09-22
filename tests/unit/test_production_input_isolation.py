"""Managed Data Versions cannot be mutated through Execution Scope bindings."""

import os
from pathlib import Path

import pytest

from stata_research_agent.interfaces.production_turn_runner import ProductionTurnRunner


def test_execution_binding_is_a_verified_copy_not_a_hard_link(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    execution = workspace / ".runtime" / "scope"
    source = workspace / ".stata-agent" / "managed" / "data.dta"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"immutable-data-version")

    ProductionTurnRunner._bind_managed_input(workspace, execution, ".stata-agent/managed/data.dta")

    target = execution / ".stata-agent" / "managed" / "data.dta"
    assert target.read_bytes() == source.read_bytes()
    assert not os.path.samefile(source, target)
    target.write_bytes(b"working-copy-changed")
    assert source.read_bytes() == b"immutable-data-version"
    with pytest.raises(ValueError, match="other bytes"):
        ProductionTurnRunner._bind_managed_input(
            workspace, execution, ".stata-agent/managed/data.dta"
        )


def test_execution_binding_exposes_safe_workspace_relative_alias(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    execution = workspace / ".runtime" / "scope"
    source = workspace / ".stata-agent" / "managed" / "data.dta"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"immutable-data-version")

    ProductionTurnRunner._bind_managed_input(
        workspace,
        execution,
        ".stata-agent/managed/data.dta",
        "inputs/auto.dta",
    )

    alias = execution / "inputs" / "auto.dta"
    assert alias.read_bytes() == source.read_bytes()
    assert not os.path.samefile(source, alias)
    alias.write_bytes(b"isolated-working-copy")
    assert source.read_bytes() == b"immutable-data-version"
    ProductionTurnRunner._bind_managed_input(
        workspace,
        execution,
        ".stata-agent/managed/data.dta",
        "inputs/auto.dta",
    )
    assert alias.read_bytes() == source.read_bytes()


def test_execution_binding_rejects_unsafe_dataset_alias(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    execution = workspace / ".runtime" / "scope"
    source = workspace / ".stata-agent" / "managed" / "data.dta"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"immutable-data-version")

    with pytest.raises(ValueError, match="unsafe"):
        ProductionTurnRunner._bind_managed_input(
            workspace,
            execution,
            ".stata-agent/managed/data.dta",
            "../auto.dta",
        )
