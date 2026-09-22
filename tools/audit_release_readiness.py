"""Audit whether two local candidate builds are eligible to become a real RC."""

from __future__ import annotations

import argparse
from pathlib import Path

from stata_research_agent.interfaces.release_readiness import LocalReleaseReadinessAuditor


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, default=Path(__file__).parents[1])
    parser.add_argument("--candidate-a", type=Path, required=True)
    parser.add_argument("--candidate-b", type=Path, required=True)
    arguments = parser.parse_args()
    report = LocalReleaseReadinessAuditor().audit(
        arguments.project_root,
        arguments.candidate_a,
        arguments.candidate_b,
    )
    print(report.to_json())
    return 0 if report.ready_for_g7 else 2


if __name__ == "__main__":
    raise SystemExit(main())
