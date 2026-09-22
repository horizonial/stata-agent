"""Physical/package identity checks for M0-01."""

import sys
import tomllib
from pathlib import Path

import stata_research_agent

PROJECT_ROOT = Path(__file__).parents[2]


def test_distribution_and_import_package_have_stable_public_identities() -> None:
    project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["name"] == "stata-research-agent"
    assert stata_research_agent.__name__ == "stata_research_agent"
    assert stata_research_agent.__name__ != "stata_agent"


def test_loaded_package_is_inside_source_root() -> None:
    package_file = Path(stata_research_agent.__file__).resolve()
    expected_root = (PROJECT_ROOT / "src" / "stata_research_agent").resolve()
    assert package_file.is_relative_to(expected_root)


def test_deprecated_source_root_is_not_on_python_path() -> None:
    legacy_root = (PROJECT_ROOT / "app" / "src").resolve()
    resolved_paths = {Path(path or ".").resolve() for path in sys.path}
    assert legacy_root not in resolved_paths
