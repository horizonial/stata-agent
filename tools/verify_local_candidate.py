"""Verify one unsigned local candidate without mutating its immutable payload."""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from stata_research_agent.domain.stata_execution import (
    StataArtifactOutputRequest,
    StataExecutionStatus,
)
from stata_research_agent.interfaces.release_evidence import CanonicalPayloadScanner
from stata_research_agent.stata.stdio_runtime import StdioStataRuntime


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


async def _verify_stata(payload: Path, stata_home: Path) -> dict[str, object]:
    executor = payload / "executor" / "python.exe"
    source = stata_home / "auto.dta"
    if not executor.is_file():
        raise FileNotFoundError("candidate bundled Python runtime is missing")
    if not source.is_file():
        raise FileNotFoundError("certified Stata auto.dta fixture is missing")

    with tempfile.TemporaryDirectory(prefix="stata-agent-candidate-") as raw_directory:
        working_directory = Path(raw_directory).resolve()
        shutil.copy2(source, working_directory / "auto.dta")
        runtime = StdioStataRuntime(
            python_executable=executor,
            mcp_source_root=None,
            working_directory=working_directory,
            stata_home=stata_home,
        )
        async with runtime:
            loaded = await runtime.execute(
                session_id="candidate-verification",
                code='use "auto.dta", clear',
                timeout_seconds=30,
            )
            regression = await runtime.execute(
                session_id="candidate-verification",
                code="regress price mpg weight",
                timeout_seconds=30,
            )
            table = await runtime.execute(
                session_id="candidate-verification",
                code=(
                    'esttab using ".stata-agent/staging/attempt_candidate/table.rtf", '
                    "replace rtf b(%9.3f) se(%9.3f) "
                    'stats(N r2, fmt(0 3) labels("Observations" "R-squared"))'
                ),
                timeout_seconds=30,
                operation_attempt_id="attempt_candidate",
                artifact_outputs=(
                    StataArtifactOutputRequest(
                        "table.candidate",
                        "table.rtf",
                        "table",
                        "application/rtf",
                    ),
                ),
            )
            closed = await runtime.close_session(
                session_id="candidate-verification",
                reason="candidate verification complete",
            )

        if loaded.receipt.execution_status is not StataExecutionStatus.SUCCEEDED:
            raise RuntimeError("candidate could not load the Stata fixture")
        if regression.receipt.execution_status is not StataExecutionStatus.SUCCEEDED:
            raise RuntimeError("candidate regression did not succeed")
        if regression.structured is None or regression.structured.get("N") != 74.0:
            raise RuntimeError("candidate regression returned the wrong sample size")
        if (
            table.receipt.execution_status is not StataExecutionStatus.SUCCEEDED
            or len(table.artifacts) != 1
        ):
            raise RuntimeError("candidate esttab Artifact contract did not succeed")
        table_path = Path(table.artifacts[0].source_path)
        if not table_path.read_bytes().startswith(b"{\\rtf"):
            raise RuntimeError("candidate esttab output is not an RTF payload")
        if not closed.closed:
            raise RuntimeError("candidate Stata session did not close")
        environment = regression.receipt.runtime_environment
        return {
            "execution_status": regression.receipt.execution_status.value,
            "sample_size": regression.structured["N"],
            "stata_version": environment.get("stata_version"),
            "stata_os": environment.get("stata_os"),
            "stata_is_mp": environment.get("stata_is_mp"),
            "esttab_artifact_count": len(table.artifacts),
            "session_closed": closed.closed,
        }


def verify(candidate: Path, stata_home: Path) -> dict[str, object]:
    candidate_root = candidate.resolve()
    payload = candidate_root / "payload"
    receipt = _read_object(candidate_root / "evidence" / "candidate-build-receipt.json")
    release_id = str(receipt["release_id"])
    expected_manifest = str(receipt["payload_manifest_sha256"])
    scanner = CanonicalPayloadScanner()
    before = scanner.scan(payload, release_id=release_id)
    if before.manifest_sha256 != expected_manifest:
        raise RuntimeError("candidate payload does not match its build receipt")

    service = payload / "app" / "stata-research-agent.exe"
    probe = subprocess.run(
        [str(service), "--probe"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    probe_result = json.loads(probe.stdout)
    if not isinstance(probe_result, dict) or probe_result.get("read_only") is not True:
        raise RuntimeError("candidate release probe did not return its read-only receipt")

    stata_result = asyncio.run(_verify_stata(payload, stata_home.resolve()))
    after = scanner.scan(payload, release_id=release_id)
    if after.manifest_sha256 != before.manifest_sha256:
        raise RuntimeError("candidate payload changed during verification")
    return {
        "schema_version": "stata-research-agent.local-candidate-verification/v1",
        "release_id": release_id,
        "payload_manifest_sha256": before.manifest_sha256,
        "payload_entry_count": len(before.entries),
        "payload_unchanged": True,
        "release_probe": probe_result,
        "stata": stata_result,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--stata-home", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    arguments = parser.parse_args()
    result = verify(arguments.candidate, arguments.stata_home)
    encoded = json.dumps(
        result,
        ensure_ascii=False,
        indent=2 if arguments.report is not None else None,
        sort_keys=True,
    )
    if arguments.report is not None:
        report = arguments.report.resolve()
        report.parent.mkdir(parents=True, exist_ok=True)
        if report.exists():
            raise FileExistsError("candidate verification report is immutable")
        report.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
