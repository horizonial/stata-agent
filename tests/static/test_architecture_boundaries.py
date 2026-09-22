"""L0 dependency rules for the physically isolated implementation."""

from __future__ import annotations

import ast
from pathlib import Path

SOURCE_ROOT = Path(__file__).parents[2] / "src" / "stata_research_agent"

DOMAIN_FORBIDDEN_ROOTS = {
    "anthropic",
    "docx",
    "fastapi",
    "fitz",
    "httpx",
    "mcp",
    "openai",
    "requests",
    "sqlalchemy",
    "sqlite3",
}

APPLICATION_ADAPTER_PACKAGES = {
    "artifacts",
    "diagnostics",
    "documents",
    "evidence",
    "interfaces",
    "persistence",
    "projections",
    "runtime",
    "stata",
}


def imported_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def production_files() -> list[Path]:
    return sorted(SOURCE_ROOT.rglob("*.py"))


def test_new_implementation_never_imports_legacy_package() -> None:
    violations: list[str] = []
    for path in production_files():
        for imported in imported_names(path):
            if imported == "stata_agent" or imported.startswith("stata_agent."):
                violations.append(f"{path.relative_to(SOURCE_ROOT)} -> {imported}")
    assert violations == [], "legacy imports are forbidden:\n" + "\n".join(violations)


def test_domain_is_framework_free_and_cannot_import_adapters() -> None:
    domain_root = SOURCE_ROOT / "domain"
    violations: list[str] = []
    for path in sorted(domain_root.rglob("*.py")):
        for imported in imported_names(path):
            root = imported.split(".", 1)[0]
            if root in DOMAIN_FORBIDDEN_ROOTS:
                violations.append(f"{path.name} -> {imported}")
            if imported.startswith("stata_research_agent.") and not imported.startswith(
                "stata_research_agent.domain"
            ):
                violations.append(f"{path.name} -> {imported}")
    assert violations == [], "domain dependency violations:\n" + "\n".join(violations)


def test_application_and_runtime_depend_only_inward() -> None:
    violations: list[str] = []
    for package_name in ("application", "runtime"):
        package_root = SOURCE_ROOT / package_name
        for path in sorted(package_root.rglob("*.py")):
            for imported in imported_names(path):
                if not imported.startswith("stata_research_agent."):
                    continue
                parts = imported.split(".")
                imported_package = parts[1] if len(parts) > 1 else ""
                if (
                    package_name == "application"
                    and imported_package in APPLICATION_ADAPTER_PACKAGES
                ):
                    violations.append(f"{path.relative_to(SOURCE_ROOT)} -> {imported}")
                if package_name == "runtime" and imported_package not in {
                    "application",
                    "domain",
                    "runtime",
                }:
                    violations.append(f"{path.relative_to(SOURCE_ROOT)} -> {imported}")
    assert violations == [], "inward dependency violations:\n" + "\n".join(violations)


def test_sqlite_driver_is_confined_to_persistence_adapter() -> None:
    violations: list[str] = []
    for path in production_files():
        if path.is_relative_to(SOURCE_ROOT / "persistence"):
            continue
        if "sqlite3" in imported_names(path):
            violations.append(str(path.relative_to(SOURCE_ROOT)))
    assert violations == [], "sqlite3 imports outside persistence:\n" + "\n".join(violations)


def test_expected_top_level_packages_exist() -> None:
    expected = {
        "application",
        "artifacts",
        "diagnostics",
        "documents",
        "domain",
        "evidence",
        "interfaces",
        "persistence",
        "projections",
        "runtime",
        "stata",
    }
    actual = {path.name for path in SOURCE_ROOT.iterdir() if (path / "__init__.py").is_file()}
    assert expected <= actual
