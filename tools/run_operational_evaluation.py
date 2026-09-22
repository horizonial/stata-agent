"""Evaluate real Workspace usage and persist an explainable L1/L2/L3 snapshot."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from stata_research_agent.persistence.operational_evaluation_query import (
    SqliteOperationalEvaluationQuery,
)

ROOT = Path(__file__).resolve().parents[1]


def _open_readonly(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _discover(roots: tuple[Path, ...]) -> tuple[Path, ...]:
    found: set[Path] = set()
    for root in roots:
        resolved = root.resolve()
        if resolved.is_file() and resolved.name == "workspace.sqlite3":
            found.add(resolved)
        elif resolved.is_dir():
            found.update(path.resolve() for path in resolved.rglob("workspace.sqlite3"))
    return tuple(sorted(found))


def _rollup(
    workspaces: list[dict[str, Any]], *, count_label: str = "workspace"
) -> dict[str, Any]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for workspace in workspaces:
        for layer in workspace["layers"]:
            for metric in layer["metrics"]:
                grouped[(layer["layer"], metric["metric_id"])].append(metric)
    order = {
        "fail": 5,
        "warn": 4,
        "unknown": 3,
        "pass": 2,
        "observed": 1,
        "not_applicable": 0,
    }
    layers: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for (layer, metric_id), values in sorted(grouped.items()):
        applicable = [item for item in values if item["status"] != "not_applicable"]
        status = max(values, key=lambda item: order[item["status"]])["status"]
        numerator = sum(float(item["numerator"] or 0) for item in applicable)
        if applicable and all(item["denominator"] is not None for item in applicable):
            denominator: float | None = sum(float(item["denominator"]) for item in applicable)
            value = numerator / denominator if denominator else None
        elif applicable:
            denominator = None
            value = sum(float(item["value"] or 0) for item in applicable)
        else:
            denominator = 0.0
            value = None
        layers[layer].append(
            {
                "metric_id": metric_id,
                "subsystem": values[0]["subsystem"],
                "status": status,
                "value": value,
                "numerator": numerator,
                "denominator": denominator,
                "unit": values[0]["unit"],
                f"eligible_{count_label}_count": len(applicable),
                f"{count_label}_count": len(values),
                "explanation": values[0]["explanation"],
                "source_tables": sorted(
                    {table for item in values for table in item["source_tables"]}
                ),
            }
        )
    return {
        layer: {
            "status": (
                "fail"
                if any(item["status"] == "fail" for item in metrics)
                else "warn"
                if any(item["status"] == "warn" for item in metrics)
                else "pass"
                if any(item["status"] == "pass" for item in metrics)
                else "observed"
            ),
            "metrics": metrics,
        }
        for layer, metrics in sorted(layers.items())
    }


def _slice_rollups(workspaces: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    turns = [turn for workspace in workspaces for turn in workspace["turns"]]
    dimensions: dict[str, dict[str, list[dict[str, Any]]]] = {
        "model": defaultdict(list),
        "main_skill": defaultdict(list),
        "tool_catalog": defaultdict(list),
        "turn_status": defaultdict(list),
        "created_date": defaultdict(list),
    }
    for turn in turns:
        configuration = turn["configuration"]
        model = " + ".join(configuration["model_names"]) or "not_invoked"
        skill_name = configuration["main_skill_name"] or "not_frozen"
        skill_revision = configuration["main_skill_revision"] or "unknown"
        tool_catalog = configuration["tool_catalog_revision"] or "not_frozen"
        created_date = str(turn["created_at"])[:10]
        dimensions["model"][model].append(turn)
        dimensions["main_skill"][f"{skill_name}@{skill_revision}"].append(turn)
        dimensions["tool_catalog"][tool_catalog].append(turn)
        dimensions["turn_status"][str(turn["turn_status"])].append(turn)
        dimensions["created_date"][created_date].append(turn)
    return {
        dimension: [
            {
                "value": value,
                "turn_count": len(selected),
                "layers": _rollup(selected, count_label="turn"),
            }
            for value, selected in sorted(groups.items())
        ]
        for dimension, groups in dimensions.items()
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "roots",
        nargs="+",
        type=Path,
        help="Workspace database, Workspace directory, or directory tree to scan.",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--scope-kind",
        choices=("ordinary_product_use", "development_use", "release_fixture"),
        default="ordinary_product_use",
        help="Provenance label; development or release fixtures must not be reported as user use.",
    )
    parser.add_argument(
        "--cohort-label",
        default="unspecified",
        help="Human-readable cohort label retained in the report.",
    )
    args = parser.parse_args()
    databases = _discover(tuple(args.roots))
    if not databases:
        raise SystemExit("No workspace.sqlite3 databases found")
    workspaces: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for path in databases:
        try:
            connection = _open_readonly(path)
            try:
                snapshot = SqliteOperationalEvaluationQuery(connection).workspace()
            finally:
                connection.close()
        except (sqlite3.Error, ValueError) as error:
            failures.append({"database": str(path), "error": str(error)})
            continue
        payload = asdict(snapshot)
        payload["database"] = str(path)
        workspaces.append(payload)
    generated = datetime.now(UTC)
    report = {
        "schema_version": "stata-research-agent.operational-evaluation-report/v2",
        "policy_revision": "operational-evaluation/v2",
        "generated_at": generated.isoformat().replace("+00:00", "Z"),
        "scope": {
            "kind": args.scope_kind,
            "cohort_label": args.cohort_label,
            "roots": [str(path.resolve()) for path in args.roots],
            "discovered_database_count": len(databases),
            "evaluated_workspace_count": len(workspaces),
            "failed_database_count": len(failures),
            "turn_count": sum(len(item["turns"]) for item in workspaces),
        },
        "layers": _rollup(workspaces),
        "slices": _slice_rollups(workspaces),
        "workspaces": workspaces,
        "failures": failures,
        "interpretation": {
            "hard_gate": "fail means an authoritative product invariant was violated",
            "observed": "descriptive real-use metric without an invented universal threshold",
            "not_applicable": "the real Turn had no eligible denominator",
            "unknown": "the Workspace lacks enough authoritative facts to classify the metric",
        },
    }
    output = args.output or (
        ROOT
        / "verification/runs"
        / generated.strftime("operational-evaluation-%Y%m%dT%H%M%SZ.json")
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(output)
    print(
        json.dumps(
            {
                "scope": report["scope"],
                "layer_status": {
                    layer: {
                        "status": payload["status"],
                        "metric_count": len(payload["metrics"]),
                        "failed_metric_ids": [
                            item["metric_id"]
                            for item in payload["metrics"]
                            if item["status"] == "fail"
                        ],
                        "warning_metric_ids": [
                            item["metric_id"]
                            for item in payload["metrics"]
                            if item["status"] == "warn"
                        ],
                    }
                    for layer, payload in report["layers"].items()
                },
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
