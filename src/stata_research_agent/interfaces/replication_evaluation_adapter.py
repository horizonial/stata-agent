"""Executable public-paper replication benchmark for the Product Evaluation suite."""

from __future__ import annotations

import asyncio
import json
import math
import re
import shutil
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from stata_research_agent.application.control import CreateWorkspaceCommand
from stata_research_agent.application.product_evaluation import (
    EvaluationLevel,
    EvaluationPayloadReference,
    EvaluationScenario,
    EvaluationScore,
    GraderKind,
    GraderSpec,
    ProductEvaluationError,
    ScoreVerdict,
    SubsystemEvaluationMode,
    TrialBundle,
)
from stata_research_agent.application.workspace_service import WorkspaceControlService
from stata_research_agent.domain.identifiers import CommandId, WorkspaceId
from stata_research_agent.domain.stata_execution import StataExecutionStatus
from stata_research_agent.persistence.control_store import SqliteControlStore
from stata_research_agent.persistence.workspace import WorkspaceDatabase
from stata_research_agent.runtime.uuid_identity import UuidIdentityGenerator
from stata_research_agent.stata.stdio_runtime import StdioStataRuntime

_MARKER = re.compile(r"^@@(?P<name>[A-Za-z0-9_]+)(?:\s+(?P<value>.*?))?\s*$")


@dataclass(frozen=True)
class PackageReplicationSpec:
    """Filesystem contract for one frozen public replication package."""

    scenario_id: str
    fixture_destinations: dict[str, str]
    source_paths: dict[str, str]
    harness_role: str
    reference_role: str
    harness_relative_path: str
    output_paths: dict[str, str]
    output_minimum_bytes: dict[str, int]
    output_prefixes: dict[str, bytes]


POWERFUL_EXPERIMENTS_SPEC = PackageReplicationSpec(
    scenario_id="agent.replication.powerful-experiments-full-package",
    fixture_destinations={
        "package_master_do": "package/FiscalStudies.do",
        "package_baseline_data": "package/BaselineConstructed.dta",
        "package_export_data": "package/ExportOutcomes.dta",
        "replication_harness": "harness/run-full-package.do",
    },
    source_paths={
        "FiscalStudies.do": "package/FiscalStudies.do",
        "BaselineConstructed.dta": "package/BaselineConstructed.dta",
        "ExportOutcomes.dta": "package/ExportOutcomes.dta",
    },
    harness_role="replication_harness",
    reference_role="replication_reference",
    harness_relative_path="harness/run-full-package.do",
    output_paths={
        "fig1a-gph": "package/output/figures/fig1a.gph",
        "fig1b-gph": "package/output/figures/fig1b.gph",
        "combined-gph": "package/output/figures/FSFigure1.gph",
        "figure-png": "package/output/figures/FSFigure1.png",
        "figure-pdf": "package/output/figures/FSFigure1.pdf",
    },
    output_minimum_bytes={
        "fig1a-gph": 256,
        "fig1b-gph": 256,
        "combined-gph": 256,
        "figure-png": 256,
        "figure-pdf": 256,
    },
    output_prefixes={"figure-png": b"\x89PNG\r\n\x1a\n", "figure-pdf": b"%PDF-"},
)


JEL_DID_SPEC = PackageReplicationSpec(
    scenario_id="agent.replication.jel-did-table2-simple-did",
    fixture_destinations={
        "package_raw_csv": "package/data/county_mortality_data.csv",
        "package_make_data_do": "package/scripts/Stata/0_stata_Make_data.do",
        "package_table_do": "package/scripts/Stata/2_stata_2x2.do",
        "package_reference_tex": "package/tables/table2_stata.tex",
        "replication_harness": "harness/table2-simple-did.do",
    },
    source_paths={
        "county_mortality_data.csv": "package/data/county_mortality_data.csv",
        "0_stata_Make_data.do": "package/scripts/Stata/0_stata_Make_data.do",
        "2_stata_2x2.do": "package/scripts/Stata/2_stata_2x2.do",
        "table2_stata.tex": "package/tables/table2_stata.tex",
    },
    harness_role="replication_harness",
    reference_role="replication_reference",
    harness_relative_path="harness/table2-simple-did.do",
    output_paths={
        "analysis-data": "package/data/did_jel_aca_replication_data.dta",
        "table-rtf": "package/benchmark-output/table2-simple-did.rtf",
    },
    output_minimum_bytes={"analysis-data": 1_000_000, "table-rtf": 512},
    output_prefixes={"table-rtf": b"{\\rtf"},
)


class RadicalReformReplicationAdapter:
    """Run the frozen Table 3 Column 1 harness through the production Stata adapter."""

    scenario_id = "agent.replication.radical-reform-table3-column1"

    def __init__(
        self,
        project_root: Path,
        *,
        mcp_python: Path,
        mcp_source_root: Path,
        stata_home: Path,
    ) -> None:
        self._project_root = project_root.resolve()
        self._mcp_python = mcp_python.resolve()
        self._mcp_source_root = mcp_source_root.resolve()
        self._stata_home = stata_home.resolve()

    def execute(
        self,
        scenario: EvaluationScenario,
        *,
        trial_id: str,
        trial_root: Path,
    ) -> TrialBundle:
        self._validate_scenario(scenario)
        if trial_root.exists() and any(trial_root.iterdir()):
            raise ProductEvaluationError("replication Trial bundle directory is not empty")
        trial_root.mkdir(parents=True, exist_ok=True)
        fixtures = self._verified_fixtures(scenario)
        benchmark_root = trial_root / "benchmark"
        source_root = benchmark_root / "source"
        harness_root = benchmark_root / "harness"
        reference_root = benchmark_root / "reference"
        source_root.mkdir(parents=True)
        harness_root.mkdir()
        reference_root.mkdir()
        for role, filename in (
            ("replication_do", "20100816_replication.do"),
            ("replication_main_data", "20100816_replication_dataset.dta"),
            ("replication_table5_data", "20100816_replication_dataset_t5.dta"),
        ):
            shutil.copy2(fixtures[role], source_root / filename)
        shutil.copy2(fixtures["replication_harness"], harness_root / "table3-column1.do")
        shutil.copy2(fixtures["replication_reference"], reference_root / "table3-column1.json")

        workspace_root = trial_root / "workspace"
        database = WorkspaceDatabase(
            workspace_root,
            WorkspaceId(f"ws_{trial_id.replace('-', '_')}"),
        )
        database.create()
        connection = database.open(writable=True)
        try:
            WorkspaceControlService(
                SqliteControlStore(connection), UuidIdentityGenerator()
            ).create_workspace(
                CreateWorkspaceCommand(
                    CommandId(f"cmd_{trial_id}_workspace"), database.workspace_id
                )
            )
        finally:
            connection.close()

        result = asyncio.run(
            self._execute_stata(
                benchmark_root,
                session_id=f"replication-{trial_id}",
                timeout_seconds=scenario.trial_policy.max_wall_clock_seconds,
            )
        )
        stdout_path = trial_root / "stata-output.txt"
        stdout_path.write_text(result.text, encoding="utf-8", newline="\n")
        markers = self._parse_markers(result.text)
        rtf_path = benchmark_root / "output" / "table3-column1.rtf"
        rtf_bytes = rtf_path.read_bytes() if rtf_path.is_file() else b""

        reference = json.loads(fixtures["replication_reference"].read_text(encoding="utf-8"))
        source_digests = {
            name: sha256((source_root / name).read_bytes()).hexdigest()
            for name in reference["source"]["source_artifacts"]
        }
        report = {
            "schema_version": "stata-research-agent.replication-report/v1",
            "benchmark_id": reference["benchmark_id"],
            "target_id": markers.get("TARGET"),
            "receipt": {
                "schema_version": result.receipt.schema_version,
                "session_id": result.receipt.session_id,
                "session_generation": result.receipt.session_generation,
                "exec_seq": result.receipt.exec_seq,
                "execution_status": result.receipt.execution_status.value,
                "rc": result.receipt.rc,
                "raw_output_status": result.receipt.raw_output_status,
                "structured_result_status": result.receipt.structured_result_status,
                "command_hash": result.receipt.command_hash,
                "data_signature": result.receipt.data_signature,
                "session_reset": result.receipt.session_reset,
            },
            "markers": markers,
            "source_sha256": source_digests,
            "harness_sha256": sha256((harness_root / "table3-column1.do").read_bytes()).hexdigest(),
            "rtf": {
                "exists": rtf_path.is_file(),
                "size_bytes": len(rtf_bytes),
                "sha256": sha256(rtf_bytes).hexdigest() if rtf_bytes else None,
                "header_valid": rtf_bytes.startswith(b"{\\rtf"),
            },
            "stdout_sha256": sha256(result.text.encode("utf-8")).hexdigest(),
        }
        report_path = trial_root / "replication-report.json"
        report_bytes = self._json(report).encode("utf-8")
        report_path.write_bytes(report_bytes)

        trace_path = trial_root / "trace.jsonl"
        trace_path.write_text(
            json.dumps(
                {
                    "event_type": "replication.stata_execution_observed",
                    "receipt": report["receipt"],
                    "markers": markers,
                    "stdout_sha256": report["stdout_sha256"],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
        artifact_manifest_path = trial_root / "artifact-manifest.json"
        artifact_manifest_path.write_text(
            self._json(
                {
                    "schema_version": "stata-research-agent.eval-artifacts/v1",
                    "artifacts": [
                        {"role": "replication_report", "locator": "replication-report.json"},
                        {"role": "stata_output", "locator": "stata-output.txt"},
                        {
                            "role": "esttab_rtf",
                            "locator": "benchmark/output/table3-column1.rtf",
                        },
                        {
                            "role": "workspace_database",
                            "locator": "workspace/workspace.sqlite3",
                        },
                    ],
                }
            ),
            encoding="utf-8",
            newline="\n",
        )

        observations = {"replication.source_integrity_verified"}
        if result.receipt.execution_status is StataExecutionStatus.SUCCEEDED:
            observations.add("replication.stata_execution_succeeded")
        if rtf_bytes.startswith(b"{\\rtf"):
            observations.add("replication.esttab_rtf_created")
        if self._matches_reference(markers, reference):
            observations.add("replication.numeric_oracle_matched")
        if result.receipt.execution_status is not StataExecutionStatus.SUCCEEDED:
            observations.add("replication.stata_execution_failed")

        payloads = [
            EvaluationPayloadReference(
                "replication-report",
                "application/json",
                report_path,
                sha256(report_bytes).hexdigest(),
            ),
            EvaluationPayloadReference(
                "stata-output",
                "text/plain",
                stdout_path,
                sha256(stdout_path.read_bytes()).hexdigest(),
            ),
            EvaluationPayloadReference(
                "replication-reference",
                "application/json",
                reference_root / "table3-column1.json",
                sha256((reference_root / "table3-column1.json").read_bytes()).hexdigest(),
            ),
        ]
        if rtf_path.is_file():
            payloads.append(
                EvaluationPayloadReference(
                    "esttab-rtf",
                    "application/rtf",
                    rtf_path,
                    sha256(rtf_bytes).hexdigest(),
                )
            )
        return TrialBundle(
            trial_id,
            scenario.scenario_id,
            scenario.revision,
            scenario.scenario_sha256,
            workspace_root,
            database.database_path,
            trace_path,
            artifact_manifest_path,
            tuple(sorted(observations)),
            (
                "evaluation_payload:replication-report",
                "evaluation_payload:stata-output",
                "evaluation_payload:esttab-rtf",
                "evaluation_payload:replication-reference",
            ),
            tuple(payloads),
        )

    async def _execute_stata(
        self,
        working_directory: Path,
        *,
        session_id: str,
        timeout_seconds: float,
    ) -> Any:
        runtime = StdioStataRuntime(
            python_executable=self._mcp_python,
            mcp_source_root=self._mcp_source_root,
            working_directory=working_directory,
            stata_home=self._stata_home,
        )
        async with runtime:
            try:
                return await runtime.execute(
                    session_id=session_id,
                    code='do "harness/table3-column1.do"',
                    timeout_seconds=timeout_seconds,
                )
            finally:
                await runtime.close_session(
                    session_id=session_id,
                    reason="replication benchmark complete",
                )

    def _verified_fixtures(self, scenario: EvaluationScenario) -> dict[str, Path]:
        fixtures: dict[str, Path] = {}
        for fixture in scenario.fixtures:
            path = (self._project_root / fixture.locator).resolve()
            if not path.is_relative_to(self._project_root) or not path.is_file():
                raise ProductEvaluationError("replication fixture is unavailable")
            if sha256(path.read_bytes()).hexdigest() != fixture.sha256:
                raise ProductEvaluationError("replication fixture digest mismatch")
            fixtures[fixture.role] = path
        required = {
            "replication_do",
            "replication_main_data",
            "replication_table5_data",
            "replication_harness",
            "replication_reference",
        }
        if not required <= fixtures.keys():
            raise ProductEvaluationError("replication scenario is missing a required fixture")
        return fixtures

    @staticmethod
    def _parse_markers(output: str) -> dict[str, str | int | float]:
        markers: dict[str, str | int | float] = {}
        for line in output.splitlines():
            match = _MARKER.match(line.strip())
            if match is None:
                continue
            name = match.group("name")
            raw = (match.group("value") or "").strip()
            if name in {"BENCHMARK", "TARGET"}:
                value: str | int | float = raw
            else:
                try:
                    numeric = float(raw)
                except ValueError as error:
                    raise ProductEvaluationError(
                        f"replication marker {name} is not numeric"
                    ) from error
                value = int(numeric) if numeric.is_integer() else numeric
            if name in markers:
                raise ProductEvaluationError(f"duplicate replication marker: {name}")
            markers[name] = value
        return markers

    @staticmethod
    def _matches_reference(markers: dict[str, Any], reference: dict[str, Any]) -> bool:
        if markers.get("BENCHMARK") != reference["benchmark_id"]:
            return False
        if markers.get("TARGET") != reference["target"]["id"]:
            return False
        tolerance = reference["oracle"]["numeric_tolerance"]
        return all(
            key in markers
            and math.isclose(
                float(markers[key]),
                float(expected),
                abs_tol=float(tolerance["absolute"]),
                rel_tol=float(tolerance["relative"]),
            )
            for key, expected in reference["oracle"]["values"].items()
        )

    @staticmethod
    def _validate_scenario(scenario: EvaluationScenario) -> None:
        if scenario.scenario_id != RadicalReformReplicationAdapter.scenario_id:
            raise ProductEvaluationError("replication adapter received another scenario")
        if (
            scenario.level is not EvaluationLevel.SUBSYSTEM
            or scenario.subsystem_mode is not SubsystemEvaluationMode.EXTRINSIC
            or "stata_execution" not in scenario.subsystems
        ):
            raise ProductEvaluationError(
                "replication baseline requires an extrinsic Stata subsystem scenario"
            )

    @staticmethod
    def _json(value: object) -> str:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )


class ReplicationOracleGrader:
    """Grade raw Stata output and RTF, without trusting adapter observations."""

    grader_id = "replication.numeric_and_rtf_oracle"
    revision = "replication-oracle/v1"
    kind = GraderKind.REPRODUCIBILITY

    def grade(
        self,
        scenario: EvaluationScenario,
        spec: GraderSpec,
        bundle: TrialBundle,
    ) -> EvaluationScore:
        del scenario
        payloads = {item.payload_id: item for item in bundle.evaluation_payloads}
        required = {
            "replication-report",
            "stata-output",
            "esttab-rtf",
            "replication-reference",
        }
        missing = sorted(required - payloads.keys())
        if missing:
            return self._score(spec, False, {"missing_payloads": missing})
        for payload in payloads.values():
            if (
                not payload.path.is_file()
                or sha256(payload.path.read_bytes()).hexdigest() != payload.sha256
            ):
                raise ProductEvaluationError("replication evaluation payload digest mismatch")

        report = json.loads(payloads["replication-report"].path.read_text(encoding="utf-8"))
        config = json.loads(spec.config_json)
        reference = json.loads(payloads["replication-reference"].path.read_text(encoding="utf-8"))
        markers = RadicalReformReplicationAdapter._parse_markers(
            payloads["stata-output"].path.read_text(encoding="utf-8")
        )
        rtf = payloads["esttab-rtf"].path.read_bytes()
        expected_sources = reference["source"]["source_artifacts"]
        passed = (
            report["receipt"]["execution_status"] == "succeeded"
            and report["receipt"]["rc"] == 0
            and report["receipt"]["command_hash"]
            and RadicalReformReplicationAdapter._matches_reference(markers, reference)
            and report["source_sha256"] == expected_sources
            and rtf.startswith(b"{\\rtf")
            and len(rtf) >= 512
            and all(
                f"{float(reference['oracle']['values'][name]):.4f}".encode("ascii") in rtf
                for name in (
                    "B_fpresence1750",
                    "B_fpresence1800",
                    "B_fpresence1850",
                    "B_fpresence1875",
                    "B_fpresence1900",
                )
            )
            and report["harness_sha256"] == config["harness_sha256"]
        )
        return self._score(
            spec,
            bool(passed),
            {
                "execution_status": report["receipt"]["execution_status"],
                "rc": report["receipt"]["rc"],
                "target_id": markers.get("TARGET"),
                "numeric_oracle_matched": RadicalReformReplicationAdapter._matches_reference(
                    markers, reference
                ),
                "rtf_size_bytes": len(rtf),
                "source_integrity_matched": report["source_sha256"] == expected_sources,
                "harness_integrity_matched": (report["harness_sha256"] == config["harness_sha256"]),
            },
        )

    def _score(
        self,
        spec: GraderSpec,
        passed: bool,
        detail: dict[str, Any],
    ) -> EvaluationScore:
        return EvaluationScore(
            self.grader_id,
            self.revision,
            self.kind,
            spec.role,
            ScoreVerdict.PASS if passed else ScoreVerdict.FAIL,
            passed,
            json.dumps(detail, sort_keys=True),
            (
                "evaluation_payload:replication-report",
                "evaluation_payload:stata-output",
                "evaluation_payload:esttab-rtf",
            ),
        )


class PublicPackageReplicationAdapter:
    """Run a frozen public package while preserving its native Stata workflow."""

    def __init__(
        self,
        project_root: Path,
        spec: PackageReplicationSpec,
        *,
        mcp_python: Path,
        mcp_source_root: Path,
        stata_home: Path,
    ) -> None:
        self.scenario_id = spec.scenario_id
        self._project_root = project_root.resolve()
        self._spec = spec
        self._mcp_python = mcp_python.resolve()
        self._mcp_source_root = mcp_source_root.resolve()
        self._stata_home = stata_home.resolve()

    def execute(
        self,
        scenario: EvaluationScenario,
        *,
        trial_id: str,
        trial_root: Path,
    ) -> TrialBundle:
        self._validate_scenario(scenario)
        if trial_root.exists() and any(trial_root.iterdir()):
            raise ProductEvaluationError("replication Trial bundle directory is not empty")
        trial_root.mkdir(parents=True, exist_ok=True)
        fixtures = self._verified_fixtures(scenario)
        benchmark_root = trial_root / "benchmark"
        package_root = benchmark_root / "package"
        package_root.mkdir(parents=True)
        for role, relative in self._spec.fixture_destinations.items():
            destination = benchmark_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(fixtures[role], destination)

        workspace_root = trial_root / "workspace"
        database = WorkspaceDatabase(
            workspace_root,
            WorkspaceId(f"ws_{trial_id.replace('-', '_')}"),
        )
        database.create()
        connection = database.open(writable=True)
        try:
            WorkspaceControlService(
                SqliteControlStore(connection), UuidIdentityGenerator()
            ).create_workspace(
                CreateWorkspaceCommand(
                    CommandId(f"cmd_{trial_id}_workspace"), database.workspace_id
                )
            )
        finally:
            connection.close()

        result = asyncio.run(
            self._execute_stata(
                benchmark_root,
                package_root,
                session_id=f"replication-{trial_id}",
                timeout_seconds=scenario.trial_policy.max_wall_clock_seconds,
            )
        )
        stdout_path = trial_root / "stata-output.txt"
        stdout_path.write_text(result.text, encoding="utf-8", newline="\n")
        markers = RadicalReformReplicationAdapter._parse_markers(result.text)
        reference_path = fixtures[self._spec.reference_role]
        reference = json.loads(reference_path.read_text(encoding="utf-8"))
        source_digests = {
            name: sha256((benchmark_root / relative).read_bytes()).hexdigest()
            for name, relative in self._spec.source_paths.items()
        }
        outputs: dict[str, dict[str, Any]] = {}
        payloads: list[EvaluationPayloadReference] = []
        for output_id, relative in self._spec.output_paths.items():
            path = benchmark_root / relative
            payload_id = f"replication-output-{output_id}"
            content = path.read_bytes() if path.is_file() else b""
            outputs[output_id] = {
                "payload_id": payload_id,
                "relative_path": relative,
                "exists": path.is_file(),
                "size_bytes": len(content),
                "sha256": sha256(content).hexdigest() if content else None,
            }
            if content:
                payloads.append(
                    EvaluationPayloadReference(
                        payload_id,
                        self._media_type(path),
                        path,
                        sha256(content).hexdigest(),
                    )
                )

        harness_path = benchmark_root / self._spec.harness_relative_path
        report = {
            "schema_version": "stata-research-agent.replication-report/v1",
            "benchmark_id": reference["benchmark_id"],
            "target_id": markers.get("TARGET"),
            "receipt": {
                "schema_version": result.receipt.schema_version,
                "session_id": result.receipt.session_id,
                "session_generation": result.receipt.session_generation,
                "exec_seq": result.receipt.exec_seq,
                "execution_status": result.receipt.execution_status.value,
                "rc": result.receipt.rc,
                "raw_output_status": result.receipt.raw_output_status,
                "structured_result_status": result.receipt.structured_result_status,
                "command_hash": result.receipt.command_hash,
                "data_signature": result.receipt.data_signature,
                "session_reset": result.receipt.session_reset,
            },
            "markers": markers,
            "source_sha256": source_digests,
            "harness_sha256": sha256(harness_path.read_bytes()).hexdigest(),
            "outputs": outputs,
            "stdout_sha256": sha256(result.text.encode("utf-8")).hexdigest(),
        }
        report_path = trial_root / "replication-report.json"
        report_bytes = RadicalReformReplicationAdapter._json(report).encode("utf-8")
        report_path.write_bytes(report_bytes)
        copied_reference = benchmark_root / "reference.json"
        shutil.copy2(reference_path, copied_reference)

        trace_path = trial_root / "trace.jsonl"
        trace_path.write_text(
            json.dumps(
                {
                    "event_type": "replication.stata_execution_observed",
                    "receipt": report["receipt"],
                    "markers": markers,
                    "outputs": outputs,
                    "stdout_sha256": report["stdout_sha256"],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
        artifact_manifest_path = trial_root / "artifact-manifest.json"
        artifact_manifest_path.write_text(
            RadicalReformReplicationAdapter._json(
                {
                    "schema_version": "stata-research-agent.eval-artifacts/v1",
                    "artifacts": [
                        {"role": "replication_report", "locator": "replication-report.json"},
                        {"role": "stata_output", "locator": "stata-output.txt"},
                        {"role": "workspace_database", "locator": "workspace/workspace.sqlite3"},
                    ]
                    + [
                        {"role": output_id, "locator": f"benchmark/{relative}"}
                        for output_id, relative in self._spec.output_paths.items()
                    ],
                }
            ),
            encoding="utf-8",
            newline="\n",
        )

        observations = {"replication.source_integrity_verified"}
        if result.receipt.execution_status is StataExecutionStatus.SUCCEEDED:
            observations.add("replication.stata_execution_succeeded")
        else:
            observations.add("replication.stata_execution_failed")
        if RadicalReformReplicationAdapter._matches_reference(markers, reference):
            observations.add("replication.numeric_oracle_matched")
        if all(
            (benchmark_root / relative).is_file()
            for relative in self._spec.output_paths.values()
        ):
            observations.add("replication.required_artifacts_created")

        payloads.extend(
            (
                EvaluationPayloadReference(
                    "replication-report",
                    "application/json",
                    report_path,
                    sha256(report_bytes).hexdigest(),
                ),
                EvaluationPayloadReference(
                    "stata-output",
                    "text/plain",
                    stdout_path,
                    sha256(stdout_path.read_bytes()).hexdigest(),
                ),
                EvaluationPayloadReference(
                    "replication-reference",
                    "application/json",
                    copied_reference,
                    sha256(copied_reference.read_bytes()).hexdigest(),
                ),
            )
        )
        evidence_references = tuple(
            f"evaluation_payload:{payload.payload_id}" for payload in payloads
        )
        return TrialBundle(
            trial_id,
            scenario.scenario_id,
            scenario.revision,
            scenario.scenario_sha256,
            workspace_root,
            database.database_path,
            trace_path,
            artifact_manifest_path,
            tuple(sorted(observations)),
            evidence_references,
            tuple(payloads),
        )

    async def _execute_stata(
        self,
        working_directory: Path,
        package_root: Path,
        *,
        session_id: str,
        timeout_seconds: float,
    ) -> Any:
        runtime = StdioStataRuntime(
            python_executable=self._mcp_python,
            mcp_source_root=self._mcp_source_root,
            working_directory=working_directory,
            stata_home=self._stata_home,
        )
        async with runtime:
            try:
                return await runtime.execute(
                    session_id=session_id,
                    code=(
                        f'do "{self._spec.harness_relative_path}" '
                        f'"{package_root.as_posix()}"'
                    ),
                    timeout_seconds=timeout_seconds,
                )
            finally:
                await runtime.close_session(
                    session_id=session_id,
                    reason="replication benchmark complete",
                )

    def _verified_fixtures(self, scenario: EvaluationScenario) -> dict[str, Path]:
        fixtures: dict[str, Path] = {}
        for fixture in scenario.fixtures:
            path = (self._project_root / fixture.locator).resolve()
            if not path.is_relative_to(self._project_root) or not path.is_file():
                raise ProductEvaluationError("replication fixture is unavailable")
            if sha256(path.read_bytes()).hexdigest() != fixture.sha256:
                raise ProductEvaluationError("replication fixture digest mismatch")
            fixtures[fixture.role] = path
        required = set(self._spec.fixture_destinations) | {self._spec.reference_role}
        if not required <= fixtures.keys():
            raise ProductEvaluationError("replication scenario is missing a required fixture")
        return fixtures

    def _validate_scenario(self, scenario: EvaluationScenario) -> None:
        if scenario.scenario_id != self.scenario_id:
            raise ProductEvaluationError("replication adapter received another scenario")
        if (
            scenario.level is not EvaluationLevel.SUBSYSTEM
            or scenario.subsystem_mode is not SubsystemEvaluationMode.EXTRINSIC
            or "stata_execution" not in scenario.subsystems
        ):
            raise ProductEvaluationError(
                "replication baseline requires an extrinsic Stata subsystem scenario"
            )

    @staticmethod
    def _media_type(path: Path) -> str:
        return {
            ".dta": "application/x-stata-dta",
            ".gph": "application/x-stata-graph",
            ".pdf": "application/pdf",
            ".png": "image/png",
            ".rtf": "application/rtf",
        }.get(path.suffix.lower(), "application/octet-stream")


class PackageReplicationOracleGrader:
    """Independently grade numeric markers and every declared package Artifact."""

    grader_id = "replication.package_numeric_and_artifact_oracle"
    revision = "replication-package-oracle/v1"
    kind = GraderKind.REPRODUCIBILITY

    def __init__(self, specs: tuple[PackageReplicationSpec, ...]) -> None:
        self._specs = {spec.scenario_id: spec for spec in specs}

    def grade(
        self,
        scenario: EvaluationScenario,
        spec: GraderSpec,
        bundle: TrialBundle,
    ) -> EvaluationScore:
        package_spec = self._specs.get(scenario.scenario_id)
        if package_spec is None:
            raise ProductEvaluationError("package replication grader received another scenario")
        payloads = {item.payload_id: item for item in bundle.evaluation_payloads}
        required_payloads = {
            "replication-report",
            "stata-output",
            "replication-reference",
        } | {f"replication-output-{name}" for name in package_spec.output_paths}
        missing = sorted(required_payloads - payloads.keys())
        if missing:
            return self._score(spec, False, {"missing_payloads": missing})
        for payload in payloads.values():
            if (
                not payload.path.is_file()
                or sha256(payload.path.read_bytes()).hexdigest() != payload.sha256
            ):
                raise ProductEvaluationError("replication evaluation payload digest mismatch")

        report = json.loads(payloads["replication-report"].path.read_text(encoding="utf-8"))
        reference = json.loads(
            payloads["replication-reference"].path.read_text(encoding="utf-8")
        )
        markers = RadicalReformReplicationAdapter._parse_markers(
            payloads["stata-output"].path.read_text(encoding="utf-8")
        )
        grader_config = json.loads(spec.config_json)
        output_checks: dict[str, bool] = {}
        for output_id in package_spec.output_paths:
            content = payloads[f"replication-output-{output_id}"].path.read_bytes()
            prefix = package_spec.output_prefixes.get(output_id)
            output_checks[output_id] = (
                len(content) >= package_spec.output_minimum_bytes[output_id]
                and (prefix is None or content.startswith(prefix))
            )
        numeric_match = RadicalReformReplicationAdapter._matches_reference(markers, reference)
        passed = (
            report["receipt"]["execution_status"] == "succeeded"
            and report["receipt"]["rc"] == 0
            and bool(report["receipt"]["command_hash"])
            and numeric_match
            and report["source_sha256"] == reference["source"]["source_artifacts"]
            and report["harness_sha256"] == grader_config["harness_sha256"]
            and all(output_checks.values())
        )
        return self._score(
            spec,
            bool(passed),
            {
                "execution_status": report["receipt"]["execution_status"],
                "rc": report["receipt"]["rc"],
                "numeric_oracle_matched": numeric_match,
                "source_integrity_matched": (
                    report["source_sha256"] == reference["source"]["source_artifacts"]
                ),
                "harness_integrity_matched": (
                    report["harness_sha256"] == grader_config["harness_sha256"]
                ),
                "output_checks": output_checks,
            },
        )

    def _score(
        self,
        spec: GraderSpec,
        passed: bool,
        detail: dict[str, Any],
    ) -> EvaluationScore:
        return EvaluationScore(
            self.grader_id,
            self.revision,
            self.kind,
            spec.role,
            ScoreVerdict.PASS if passed else ScoreVerdict.FAIL,
            passed,
            json.dumps(detail, sort_keys=True),
            ("evaluation_payload:replication-report", "evaluation_payload:stata-output"),
        )
