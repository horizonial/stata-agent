"""Run the unified Research Agent release evaluation gate."""

from __future__ import annotations

import argparse
from pathlib import Path

from stata_research_agent.interfaces.release_evaluation_gate import ReleaseEvaluationGate


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, default=Path(__file__).parents[1])
    parser.add_argument("--profile", choices=("core", "full"), default="core")
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    report = ReleaseEvaluationGate(arguments.project_root).run(arguments.profile)
    rendered = report.to_json()
    if arguments.output is not None:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if report.passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
