"""Fail-closed audit of Python and Node release dependency inputs."""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
import zipfile
from dataclasses import dataclass
from email.parser import Parser
from pathlib import Path
from typing import Any

from stata_research_agent.application.release_supply_chain import ReleaseSupplyChainError

_EXACT_PYTHON = re.compile(r"^[A-Za-z0-9_.-]+(?:\[[A-Za-z0-9_,.-]+\])?==[A-Za-z0-9_.+!-]+$")
_EXACT_NODE = re.compile(r"^\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?$")


@dataclass(frozen=True, slots=True)
class ReleaseInputAudit:
    pyproject_sha256: str
    uv_lock_sha256: str
    package_json_sha256: str
    package_lock_sha256: str
    npmrc_sha256: str
    stata_mcp_lock_sha256: str
    stata_mcp_artifact_sha256: str
    python_locked_packages: int
    node_locked_packages: int


class ReleaseInputAuditor:
    def audit(self, project_root: Path) -> ReleaseInputAudit:
        root = project_root.resolve()
        pyproject_path = root / "pyproject.toml"
        uv_lock_path = root / "uv.lock"
        package_path = root / "web" / "package.json"
        package_lock_path = root / "web" / "package-lock.json"
        npmrc_path = root / "web" / ".npmrc"
        stata_mcp_lock_path = root / "build" / "stata-mcp.lock.json"
        paths = (
            pyproject_path,
            uv_lock_path,
            package_path,
            package_lock_path,
            npmrc_path,
            stata_mcp_lock_path,
        )
        if not all(path.is_file() for path in paths):
            raise ReleaseSupplyChainError("release dependency input is missing")
        pyproject = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
        self._audit_pyproject(pyproject)
        uv_lock = tomllib.loads(uv_lock_path.read_text(encoding="utf-8"))
        python_count = self._audit_uv_lock(uv_lock)
        package = json.loads(package_path.read_text(encoding="utf-8"))
        package_lock = json.loads(package_lock_path.read_text(encoding="utf-8"))
        node_count = self._audit_node(package, package_lock)
        stata_mcp_artifact_sha256 = self._audit_stata_mcp_lock(
            root,
            json.loads(stata_mcp_lock_path.read_text(encoding="utf-8")),
        )
        npmrc = {
            line.strip()
            for line in npmrc_path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        if "ignore-scripts=true" not in npmrc:
            raise ReleaseSupplyChainError("npm lifecycle scripts are not disabled")
        return ReleaseInputAudit(
            self._sha256(pyproject_path),
            self._sha256(uv_lock_path),
            self._sha256(package_path),
            self._sha256(package_lock_path),
            self._sha256(npmrc_path),
            self._sha256(stata_mcp_lock_path),
            stata_mcp_artifact_sha256,
            python_count,
            node_count,
        )

    @classmethod
    def _audit_stata_mcp_lock(cls, root: Path, value: object) -> str:
        if not isinstance(value, dict):
            raise ReleaseSupplyChainError("Stata MCP artifact lock must be an object")
        required = {
            "schema_version",
            "name",
            "version",
            "artifact_path",
            "artifact_sha256",
            "source_base_revision",
            "runtime_module",
            "executor_capability_schema",
            "execution_receipt_schema",
        }
        if set(value) != required:
            raise ReleaseSupplyChainError("Stata MCP artifact lock fields are invalid")
        if (
            value["schema_version"] != "stata-mcp-artifact-lock/v1"
            or value["name"] != "stata-mcp"
            or value["runtime_module"] != "stata_mcp.server"
            or value["executor_capability_schema"] != "stata.executor-capabilities/v1alpha1"
            or value["execution_receipt_schema"] != "stata-mcp.execution-receipt/v1"
        ):
            raise ReleaseSupplyChainError("Stata MCP artifact contract is unsupported")
        version = value["version"]
        digest = value["artifact_sha256"]
        revision = value["source_base_revision"]
        relative = value["artifact_path"]
        if (
            not isinstance(version, str)
            or re.fullmatch(r"\d+\.\d+\.\d+", version) is None
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or not isinstance(revision, str)
            or re.fullmatch(r"[0-9a-f]{40}", revision) is None
            or not isinstance(relative, str)
        ):
            raise ReleaseSupplyChainError("Stata MCP artifact identity is invalid")
        artifact = (root / relative).resolve()
        vendor_root = (root / "build" / "vendor").resolve()
        if vendor_root not in artifact.parents or not artifact.is_file():
            raise ReleaseSupplyChainError("Stata MCP artifact is missing or outside vendor root")
        observed = cls._sha256(artifact)
        if observed != digest:
            raise ReleaseSupplyChainError("Stata MCP artifact hash does not match its lock")
        try:
            with zipfile.ZipFile(artifact) as wheel:
                names = set(wheel.namelist())
                if any(name.startswith(("/", "\\")) or ".." in Path(name).parts for name in names):
                    raise ReleaseSupplyChainError("Stata MCP wheel contains an unsafe path")
                required_members = {
                    "stata_mcp/__init__.py",
                    "stata_mcp/server.py",
                    "stata_mcp/tools/session_control.py",
                }
                metadata_name = f"stata_mcp-{version}.dist-info/METADATA"
                if not required_members.issubset(names) or metadata_name not in names:
                    raise ReleaseSupplyChainError(
                        "Stata MCP wheel is missing its reviewed runtime contract"
                    )
                metadata = wheel.read(metadata_name).decode("utf-8")
        except (OSError, UnicodeDecodeError, zipfile.BadZipFile) as exc:
            raise ReleaseSupplyChainError("Stata MCP artifact is not a valid wheel") from exc
        headers = Parser().parsestr(metadata)
        if headers.get("Name") != "stata-mcp" or headers.get("Version") != version:
            raise ReleaseSupplyChainError("Stata MCP wheel metadata does not match its lock")
        return observed

    @staticmethod
    def _audit_pyproject(value: dict[str, Any]) -> None:
        project = value.get("project")
        build = value.get("build-system")
        groups = value.get("dependency-groups")
        if not isinstance(project, dict) or not isinstance(build, dict):
            raise ReleaseSupplyChainError("pyproject dependency sections are missing")
        requirements: list[object] = []
        requirements.extend(project.get("dependencies", []))
        requirements.extend(build.get("requires", []))
        if isinstance(groups, dict):
            for group in groups.values():
                if isinstance(group, list):
                    requirements.extend(group)
        if not requirements or any(
            not isinstance(item, str) or _EXACT_PYTHON.fullmatch(item) is None
            for item in requirements
        ):
            raise ReleaseSupplyChainError("Python release dependencies must be exact == pins")

    @staticmethod
    def _audit_uv_lock(value: dict[str, Any]) -> int:
        if value.get("version") != 1 or value.get("requires-python") != "==3.12.*":
            raise ReleaseSupplyChainError("uv lock schema or Python target is not frozen")
        packages = value.get("package")
        if not isinstance(packages, list) or not packages:
            raise ReleaseSupplyChainError("uv lock contains no packages")
        for package in packages:
            if (
                not isinstance(package, dict)
                or not package.get("name")
                or not package.get("version")
            ):
                raise ReleaseSupplyChainError("uv package identity is incomplete")
            source = package.get("source")
            if not isinstance(source, dict):
                raise ReleaseSupplyChainError("uv package source is missing")
            if "git" in source:
                revision = str(source.get("rev", ""))
                if re.fullmatch(r"[0-9a-f]{40}", revision) is None:
                    raise ReleaseSupplyChainError("Git dependency is not pinned to a full commit")
            artifacts: list[dict[str, Any]] = []
            sdist = package.get("sdist")
            if isinstance(sdist, dict):
                artifacts.append(sdist)
            wheels = package.get("wheels", [])
            if isinstance(wheels, list):
                artifacts.extend(item for item in wheels if isinstance(item, dict))
            if package.get("name") != "stata-research-agent" and not artifacts:
                raise ReleaseSupplyChainError("locked Python package has no hashed artifact")
            for artifact in artifacts:
                digest = artifact.get("hash")
                if (
                    not isinstance(digest, str)
                    or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None
                ):
                    raise ReleaseSupplyChainError("Python artifact hash is invalid")
        return len(packages)

    @staticmethod
    def _audit_node(package: object, lock: object) -> int:
        if not isinstance(package, dict) or not isinstance(lock, dict):
            raise ReleaseSupplyChainError("Node manifests must be objects")
        if lock.get("lockfileVersion") != 3:
            raise ReleaseSupplyChainError("package-lock must use lockfileVersion 3")
        packages = lock.get("packages")
        if not isinstance(packages, dict) or "" not in packages:
            raise ReleaseSupplyChainError("package-lock package map is invalid")
        root = packages[""]
        if not isinstance(root, dict):
            raise ReleaseSupplyChainError("package-lock root is invalid")
        for section in ("dependencies", "devDependencies"):
            declared = package.get(section, {})
            locked_declared = root.get(section, {})
            if declared != locked_declared or not isinstance(declared, dict):
                raise ReleaseSupplyChainError("package.json and package-lock disagree")
            if any(
                not isinstance(version, str) or _EXACT_NODE.fullmatch(version) is None
                for version in declared.values()
            ):
                raise ReleaseSupplyChainError("Node dependencies must use exact versions")
        for path, item in packages.items():
            if path == "":
                continue
            if not isinstance(item, dict):
                raise ReleaseSupplyChainError("Node lock entry is invalid")
            integrity = item.get("integrity")
            resolved = item.get("resolved")
            if not isinstance(integrity, str) or not integrity.startswith("sha512-"):
                raise ReleaseSupplyChainError("Node artifact integrity is missing")
            if not isinstance(resolved, str) or not resolved.startswith(
                "https://registry.npmjs.org/"
            ):
                raise ReleaseSupplyChainError("Node artifact source is not allowlisted")
        return len(packages) - 1

    @staticmethod
    def _sha256(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()
