"""Build a deterministic unsigned local onedir candidate for release rehearsal."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import zipfile
from dataclasses import asdict
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from stata_research_agent.application.release_supply_chain import ReleaseComponent
from stata_research_agent.application.sensitive_output import SensitiveOutputGate
from stata_research_agent.interfaces.release_evidence import (
    CanonicalPayloadScanner,
    ReleaseEvidenceBuilder,
    payload_manifest_json,
)
from stata_research_agent.interfaces.release_input_audit import ReleaseInputAuditor

SOURCE_DATE_EPOCH = "1789747200"


def _run(command: list[str], *, cwd: Path, environment: dict[str, str]) -> None:
    subprocess.run(command, cwd=cwd, env=environment, check=True)


def _canonical_build_source(project: Path) -> tuple[str, int]:
    """Hash every first-party file that can affect the emitted payload."""

    candidates = [
        project / "tools" / "build_local_candidate.py",
        project / "tools" / "service_frozen_entry.py",
    ]
    candidates.extend(path for path in (project / "src").rglob("*") if path.is_file())
    candidates.extend(path for path in (project / "web" / "dist").rglob("*") if path.is_file())
    candidates.extend(path for path in (project / "skills").rglob("*") if path.is_file())
    records = []
    for path in sorted(set(candidates), key=lambda item: item.relative_to(project).as_posix()):
        relative = path.relative_to(project).as_posix()
        records.append((relative, hashlib.sha256(path.read_bytes()).hexdigest()))
    canonical = json.dumps(records, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    return hashlib.sha256(canonical).hexdigest(), len(records)


def _component_digest(paths: tuple[str, ...], manifest_entries: dict[str, str]) -> str:
    canonical = json.dumps(
        [(path, manifest_entries[path]) for path in sorted(paths)],
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _runtime_ignore(directory: str, names: list[str]) -> set[str]:
    ignored = {name for name in names if name == "__pycache__" or name.endswith(".pyc")}
    if Path(directory).name.casefold() == "lib":
        ignored.update(name for name in names if name.casefold() == "site-packages")
    return ignored


def _copy_distribution_closure(roots: tuple[str, ...], destination: Path) -> tuple[str, ...]:
    pending = [(canonicalize_name(name), frozenset[str]()) for name in roots]
    resolved: dict[str, importlib.metadata.Distribution] = {}
    requested_extras: dict[str, set[str]] = {}
    processed_extras: dict[str, set[str]] = {}
    while pending:
        name, extras = pending.pop()
        requested_extras.setdefault(name, set()).update(extras)
        active_extras = requested_extras[name]
        if name in processed_extras and active_extras <= processed_extras[name]:
            continue
        distribution = resolved.setdefault(name, importlib.metadata.distribution(name))
        processed_extras[name] = set(active_extras)
        resolved[name] = distribution
        for requirement_text in distribution.requires or ():
            requirement = Requirement(requirement_text)
            marker_contexts = ("", *sorted(active_extras))
            if requirement.marker is not None and not any(
                requirement.marker.evaluate({"extra": extra}) for extra in marker_contexts
            ):
                continue
            pending.append((canonicalize_name(requirement.name), frozenset(requirement.extras)))

    destination.mkdir(parents=True, exist_ok=True)
    for name, distribution in sorted(resolved.items()):
        base = Path(distribution.locate_file("")).resolve()
        for member in distribution.files or ():
            source = Path(distribution.locate_file(member)).resolve()
            if not source.is_file() or not source.is_relative_to(base):
                continue
            relative = source.relative_to(base)
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    return tuple(resolved)


def _normalize_zip_member_order(path: Path) -> None:
    temporary = path.with_suffix(path.suffix + ".normalized")
    with zipfile.ZipFile(path, "r") as source, zipfile.ZipFile(temporary, "w") as destination:
        for member in sorted(source.infolist(), key=lambda item: item.filename):
            normalized = zipfile.ZipInfo(member.filename, date_time=(1980, 1, 1, 0, 0, 0))
            normalized.compress_type = member.compress_type
            normalized.comment = member.comment
            normalized.extra = member.extra
            normalized.internal_attr = member.internal_attr
            normalized.external_attr = member.external_attr
            normalized.create_system = member.create_system
            destination.writestr(normalized, source.read(member.filename))
    os.replace(temporary, path)


def build_candidate(project_root: Path, output_root: Path, *, release_id: str) -> Path:
    project = project_root.resolve()
    output = output_root.resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError("candidate output directory must be absent or empty")
    output.mkdir(parents=True, exist_ok=True)

    audit = ReleaseInputAuditor().audit(project)
    source_manifest_sha256, source_entry_count = _canonical_build_source(project)
    lock_path = project / "build" / "stata-mcp.lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    wheel_path = (project / str(lock["artifact_path"])).resolve()
    environment = dict(os.environ)
    environment["SOURCE_DATE_EPOCH"] = SOURCE_DATE_EPOCH
    components_root = output / "components"
    work_root = output / "pyinstaller-work"
    spec_root = output / "spec"
    pyinstaller = [sys.executable, "-m", "PyInstaller"]

    _run(
        pyinstaller
        + [
            "--noconfirm",
            "--clean",
            "--log-level",
            "WARN",
            "--onedir",
            "--name",
            "stata-research-agent",
            "--distpath",
            str(components_root),
            "--workpath",
            str(work_root / "service"),
            "--specpath",
            str(spec_root),
            "--paths",
            str(project / "src"),
            "--collect-submodules",
            "uvicorn",
            "--add-data",
            f"{project / 'web' / 'dist'}{os.pathsep}web",
            "--add-data",
            f"{project / 'skills'}{os.pathsep}skills",
            str(project / "tools" / "service_frozen_entry.py"),
        ],
        cwd=project,
        environment=environment,
    )

    payload = output / "payload"
    shutil.copytree(components_root / "stata-research-agent", payload / "app")
    executor = payload / "executor"
    shutil.copytree(Path(sys.base_prefix).resolve(), executor, ignore=_runtime_ignore)
    site_packages = executor / "Lib" / "site-packages"
    executor_distributions = _copy_distribution_closure(("mcp",), site_packages)
    with zipfile.ZipFile(wheel_path) as wheel:
        wheel.extractall(site_packages)
    # The normal executor Python also hosts disposable Turn Worker subprocesses.  They only
    # execute the fixed first-party worker protocol, never MCP or user code, so copy the exact
    # canonical package source that was hashed for this candidate into its isolated site-packages.
    shutil.copytree(
        project / "src" / "stata_research_agent",
        site_packages / "stata_research_agent",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    _normalize_zip_member_order(payload / "app" / "_internal" / "base_library.zip")

    scanner = CanonicalPayloadScanner()
    manifest = scanner.scan(payload, release_id=release_id)
    entry_hashes = {entry.relative_path: entry.sha256 for entry in manifest.entries}
    app_paths = tuple(path for path in entry_hashes if path.startswith("app/"))
    executor_paths = tuple(path for path in entry_hashes if path.startswith("executor/"))
    components = (
        ReleaseComponent(
            "component:stata-research-agent-bundle",
            "stata-research-agent",
            "0.0.0",
            "application",
            "pkg:generic/stata-research-agent@0.0.0",
            "Stata Research Agent",
            "local-unsigned-rehearsal",
            _component_digest(app_paths, entry_hashes),
            "Proprietary",
            True,
            app_paths,
        ),
        ReleaseComponent(
            "component:stata-mcp-python-runtime-bundle",
            "stata-mcp-python-runtime",
            f"3.12.12+{lock['version']}",
            "application",
            None,
            "Stata MCP",
            f"locked-wheel:{audit.stata_mcp_artifact_sha256}",
            _component_digest(executor_paths, entry_hashes),
            "NOASSERTION",
            True,
            executor_paths,
        ),
    )
    builder = ReleaseEvidenceBuilder(SensitiveOutputGate())
    evidence = output / "evidence"
    build_input_sha256 = hashlib.sha256(
        "".join(
            (
                audit.pyproject_sha256,
                audit.uv_lock_sha256,
                audit.package_lock_sha256,
                audit.stata_mcp_lock_sha256,
                audit.stata_mcp_artifact_sha256,
                source_manifest_sha256,
            )
        ).encode("ascii")
    ).hexdigest()
    builder.write_evidence_member(
        evidence / "payload-manifest.json", payload_manifest_json(manifest)
    )
    builder.write_evidence_member(
        evidence / "runtime-sbom.json",
        builder.build_runtime_sbom(manifest, components),
    )
    builder.write_evidence_member(
        evidence / "build-sbom.json",
        builder.build_build_sbom(
            release_id=release_id,
            build_input_sha256=build_input_sha256,
            components=components,
        ),
    )
    builder.write_evidence_member(
        evidence / "candidate-build-receipt.json",
        {
            "schema_version": "stata-research-agent.local-candidate-build/v1",
            "release_id": release_id,
            "source_date_epoch": SOURCE_DATE_EPOCH,
            "payload_manifest_sha256": manifest.manifest_sha256,
            "build_input_sha256": build_input_sha256,
            "source_manifest_sha256": source_manifest_sha256,
            "source_entry_count": source_entry_count,
            "release_input_audit": asdict(audit),
            "executor_distributions": list(executor_distributions),
            "production_release": False,
        },
    )
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, default=Path(__file__).parents[1])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--release-id", required=True)
    arguments = parser.parse_args()
    payload = build_candidate(
        arguments.project_root,
        arguments.output,
        release_id=arguments.release_id,
    )
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
