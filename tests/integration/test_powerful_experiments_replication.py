"""Real full-package Stata replication for the World Bank power-analysis paper."""

from __future__ import annotations

import asyncio
import json
import math
import shutil
from hashlib import sha256
from pathlib import Path

import pytest

from stata_research_agent.domain.stata_execution import (
    StataExecutionStatus,
    StataRuntimeResult,
)
from stata_research_agent.interfaces.replication_evaluation_adapter import (
    RadicalReformReplicationAdapter,
)
from stata_research_agent.stata.stdio_runtime import StdioStataRuntime

ROOT = Path(__file__).parents[2]
BENCHMARK = ROOT / "verification" / "replication-benchmarks" / "powerful-experiments-wp-2025"
if not BENCHMARK.is_dir():
    pytest.skip(
        "optional frozen World Bank replication package is not installed",
        allow_module_level=True,
    )
SOURCE_PACKAGE = BENCHMARK / "source" / "package" / "RR_WLD_2025_394" / "Reproducibility package"
REFERENCE = BENCHMARK / "reference" / "full-package.json"
HARNESS = BENCHMARK / "harness" / "run-full-package.do"
MCP_ROOT = Path.home() / "stata-mcp"
MCP_PYTHON = MCP_ROOT / ".venv-uv" / "Scripts" / "python.exe"
STATA_HOME = Path(r"C:\Program Files\Stata18")


def test_world_bank_full_package_reproduces_numbers_and_figures(tmp_path: Path) -> None:
    if not (MCP_PYTHON.is_file() and STATA_HOME.is_dir()):
        pytest.skip("certified local Stata MCP environment is not installed")
    package = tmp_path / "package"
    harness_root = tmp_path / "harness"
    package.mkdir()
    harness_root.mkdir()
    for filename in ("FiscalStudies.do", "BaselineConstructed.dta", "ExportOutcomes.dta"):
        shutil.copy2(SOURCE_PACKAGE / filename, package / filename)
    shutil.copy2(HARNESS, harness_root / HARNESS.name)
    reference = json.loads(REFERENCE.read_text(encoding="utf-8"))

    async def scenario() -> StataRuntimeResult:
        runtime = StdioStataRuntime(
            python_executable=MCP_PYTHON,
            mcp_source_root=MCP_ROOT / "src",
            working_directory=tmp_path,
            stata_home=STATA_HOME,
        )
        async with runtime:
            try:
                return await runtime.execute(
                    session_id="replication-powerful-experiments",
                    code=(f'do "harness/run-full-package.do" "{package.as_posix()}"'),
                    timeout_seconds=120,
                )
            finally:
                await runtime.close_session(
                    session_id="replication-powerful-experiments",
                    reason="replication benchmark complete",
                )

    result = asyncio.run(scenario())
    assert result.receipt.execution_status is StataExecutionStatus.SUCCEEDED
    assert result.receipt.rc == 0
    markers = RadicalReformReplicationAdapter._parse_markers(result.text)
    tolerance = reference["oracle"]["numeric_tolerance"]
    for name, expected in reference["oracle"]["values"].items():
        assert math.isclose(
            float(markers[name]),
            float(expected),
            abs_tol=float(tolerance["absolute"]),
            rel_tol=float(tolerance["relative"]),
        )
    for filename in reference["required_artifacts"]:
        artifact = package / "output" / "figures" / filename
        assert artifact.is_file()
        assert artifact.stat().st_size > 256
    assert (
        (package / "output" / "figures" / "FSFigure1.png")
        .read_bytes()
        .startswith(b"\x89PNG\r\n\x1a\n")
    )
    assert (package / "output" / "figures" / "FSFigure1.pdf").read_bytes().startswith(b"%PDF-")
    for filename, expected in reference["source"]["source_artifacts"].items():
        assert sha256((package / filename).read_bytes()).hexdigest() == expected
