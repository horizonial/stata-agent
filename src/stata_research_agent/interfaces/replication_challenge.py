"""Materialize an Agent-visible replication challenge without leaking its judge."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from stata_research_agent.application.product_evaluation import (
    EvaluationScenario,
    ProductEvaluationError,
)


@dataclass(frozen=True)
class ReplicationChallengeSpec:
    challenge_id: str
    scenario_id: str
    visible_fixture_destinations: dict[str, str]
    task: str
    required_deliverables: tuple[str, ...]


@dataclass(frozen=True)
class MaterializedReplicationChallenge:
    root: Path
    manifest_path: Path
    copied_fixture_roles: tuple[str, ...]


RADICAL_REFORM_AGENT_CHALLENGE = ReplicationChallengeSpec(
    challenge_id="radical-reform-aer-2011-faithful-reproduction",
    scenario_id="agent.replication.radical-reform-table3-column1",
    visible_fixture_destinations={
        "replication_do": "research-inputs/20100816_replication.do",
        "replication_main_data": "research-inputs/20100816_replication_dataset.dta",
        "replication_table5_data": "research-inputs/20100816_replication_dataset_t5.dta",
    },
    task=(
        "Using the supplied official replication package, reproduce Table 3 Column 1. "
        "Keep a traceable Stata execution record and produce an RTF or Word table whose "
        "reported numbers can be traced back to the Stata run."
    ),
    required_deliverables=("traceable Stata result", "Table 3 Column 1", "RTF or Word table"),
)


POWERFUL_EXPERIMENTS_AGENT_CHALLENGE = ReplicationChallengeSpec(
    challenge_id="powerful-experiments-wp-2025-faithful-reproduction",
    scenario_id="agent.replication.powerful-experiments-full-package",
    visible_fixture_destinations={
        "package_master_do": "research-inputs/package/FiscalStudies.do",
        "package_baseline_data": "research-inputs/package/BaselineConstructed.dta",
        "package_export_data": "research-inputs/package/ExportOutcomes.dta",
    },
    task=(
        "Run and audit the supplied official replication package. Reproduce Figure 1 and "
        "report the employee and export-distribution statistics used by the package. "
        "Preserve Stata provenance for every reported number and exported figure."
    ),
    required_deliverables=("traceable statistics", "Figure 1 GPH", "Figure 1 PNG", "Figure 1 PDF"),
)


JEL_DID_AGENT_CHALLENGE = ReplicationChallengeSpec(
    challenge_id="jel-did-practitioners-guide-table2-reproduction",
    scenario_id="agent.replication.jel-did-table2-simple-did",
    visible_fixture_destinations={
        "package_raw_csv": "research-inputs/package/data/county_mortality_data.csv",
        "package_make_data_do": (
            "research-inputs/package/scripts/Stata/0_stata_Make_data.do"
        ),
        "package_table_do": "research-inputs/package/scripts/Stata/2_stata_2x2.do",
        "package_reference_tex": "research-inputs/package/tables/table2_stata.tex",
    },
    task=(
        "Rebuild the analysis data from the supplied public author package and reproduce "
        "the simple two-by-two difference-in-differences calculation behind Table 2, "
        "including weighted and unweighted estimates and a traceable RTF or Word table."
    ),
    required_deliverables=("rebuilt Stata data", "traceable DiD results", "RTF or Word table"),
)


class ReplicationChallengeMaterializer:
    """Copy only declared Agent inputs into a fresh challenge directory."""

    def __init__(self, project_root: Path) -> None:
        self._project_root = project_root.resolve()

    def materialize(
        self,
        scenario: EvaluationScenario,
        spec: ReplicationChallengeSpec,
        destination: Path,
    ) -> MaterializedReplicationChallenge:
        if scenario.scenario_id != spec.scenario_id:
            raise ProductEvaluationError("replication challenge targets another scenario")
        destination = destination.resolve()
        if destination.exists() and any(destination.iterdir()):
            raise ProductEvaluationError("replication challenge destination is not empty")
        destination.mkdir(parents=True, exist_ok=True)
        fixtures = {fixture.role: fixture for fixture in scenario.fixtures}
        copied: list[dict[str, object]] = []
        for role, relative in spec.visible_fixture_destinations.items():
            fixture = fixtures.get(role)
            if fixture is None:
                raise ProductEvaluationError("replication challenge fixture is missing")
            source = (self._project_root / fixture.locator).resolve()
            target = (destination / relative).resolve()
            if (
                not source.is_relative_to(self._project_root)
                or not source.is_file()
                or not target.is_relative_to(destination)
            ):
                raise ProductEvaluationError("replication challenge fixture path is unsafe")
            source_bytes = source.read_bytes()
            if sha256(source_bytes).hexdigest() != fixture.sha256:
                raise ProductEvaluationError("replication challenge fixture digest mismatch")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            copied.append(
                {
                    "role": role,
                    "locator": target.relative_to(destination).as_posix(),
                    "media_type": fixture.media_type,
                    "size_bytes": len(source_bytes),
                    "sha256": fixture.sha256,
                }
            )

        manifest_path = destination / "challenge-manifest.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "schema_version": "stata-research-agent.replication-challenge/v1",
                    "challenge_id": spec.challenge_id,
                    "scenario_id": spec.scenario_id,
                    "task": spec.task,
                    "required_deliverables": list(spec.required_deliverables),
                    "inputs": copied,
                },
                ensure_ascii=False,
                allow_nan=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
        return MaterializedReplicationChallenge(
            destination,
            manifest_path,
            tuple(sorted(spec.visible_fixture_destinations)),
        )
