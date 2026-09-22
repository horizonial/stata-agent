"""Real modern DiD benchmark over a frozen public author package."""

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
BENCHMARK = ROOT / "verification" / "replication-benchmarks" / "jel-did-practitioners-guide"
FROZEN_PACKAGE_ROOT = BENCHMARK / "source" / "frozen-package"
if not FROZEN_PACKAGE_ROOT.is_dir():
    pytest.skip(
        "optional frozen JEL replication package is not installed",
        allow_module_level=True,
    )
FROZEN_PACKAGE = next(FROZEN_PACKAGE_ROOT.iterdir())
REFERENCE = BENCHMARK / "reference" / "table2-simple-did.json"
HARNESS = BENCHMARK / "harness" / "table2-simple-did.do"
MCP_ROOT = Path.home() / "stata-mcp"
MCP_PYTHON = MCP_ROOT / ".venv-uv" / "Scripts" / "python.exe"
STATA_HOME = Path(r"C:\Program Files\Stata18")


def test_modern_did_table2_rebuilds_data_and_matches_oracle(tmp_path: Path) -> None:
    if not (MCP_PYTHON.is_file() and STATA_HOME.is_dir()):
        pytest.skip("certified local Stata MCP environment is not installed")
    package = tmp_path / "package"
    (package / "data").mkdir(parents=True)
    (package / "scripts" / "Stata").mkdir(parents=True)
    (tmp_path / "harness").mkdir()
    shutil.copy2(
        FROZEN_PACKAGE / "data" / "county_mortality_data.csv",
        package / "data" / "county_mortality_data.csv",
    )
    for filename in ("0_stata_Make_data.do", "2_stata_2x2.do"):
        shutil.copy2(
            FROZEN_PACKAGE / "scripts" / "Stata" / filename,
            package / "scripts" / "Stata" / filename,
        )
    shutil.copy2(HARNESS, tmp_path / "harness" / HARNESS.name)
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
                    session_id="replication-jel-did",
                    code=(f'do "harness/table2-simple-did.do" "{package.as_posix()}"'),
                    timeout_seconds=180,
                )
            finally:
                await runtime.close_session(
                    session_id="replication-jel-did",
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
    generated_data = package / "data" / "did_jel_aca_replication_data.dta"
    table = package / "benchmark-output" / "table2-simple-did.rtf"
    assert generated_data.is_file() and generated_data.stat().st_size > 1_000_000
    assert table.is_file() and table.read_bytes().startswith(b"{\\rtf")
    source_paths = {
        "county_mortality_data.csv": FROZEN_PACKAGE / "data" / "county_mortality_data.csv",
        "0_stata_Make_data.do": FROZEN_PACKAGE / "scripts" / "Stata" / "0_stata_Make_data.do",
        "2_stata_2x2.do": FROZEN_PACKAGE / "scripts" / "Stata" / "2_stata_2x2.do",
        "table2_stata.tex": FROZEN_PACKAGE / "tables" / "table2_stata.tex",
    }
    for filename, expected in reference["source"]["source_artifacts"].items():
        assert sha256(source_paths[filename].read_bytes()).hexdigest() == expected
