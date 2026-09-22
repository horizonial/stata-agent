"""Run M0 gates and append a non-overwriting verification evidence report."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from zipfile import ZipFile

ROOT = Path(__file__).parents[1]
EXCLUDED_PARTS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "dist",
    "node_modules",
    "runs",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def source_fingerprint() -> str:
    digest = hashlib.sha256()
    roots = [
        ROOT / "pyproject.toml",
        ROOT / "uv.lock",
        ROOT / "src",
        ROOT / "tests",
        ROOT / "contracts",
        ROOT / "tools",
        ROOT / "verification" / "register.yaml",
        ROOT / "web" / "package.json",
        ROOT / "web" / "package-lock.json",
        ROOT / "web" / "tsconfig.json",
        ROOT / "web" / "src",
    ]
    files: list[Path] = []
    for entry in roots:
        if entry.is_file():
            files.append(entry)
        else:
            files.extend(path for path in entry.rglob("*") if path.is_file())
    for path in sorted(set(files)):
        relative = path.relative_to(ROOT)
        if any(part in EXCLUDED_PARTS for part in relative.parts):
            continue
        digest.update(relative.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def run(
    step: str,
    command: list[str],
    *,
    cwd: Path = ROOT,
    env: dict[str, str] | None = None,
) -> None:
    print(f"[M0] {step}", flush=True)
    subprocess.run(command, cwd=cwd, env=env, check=True)


def canonical_sdist_manifest(path: Path) -> str:
    """Hash sdist payload identity while ignoring container/filesystem timestamps."""

    digest = hashlib.sha256()
    with tarfile.open(path, "r:gz") as archive:
        for member in sorted(archive.getmembers(), key=lambda item: item.name):
            if member.isfile():
                classification = "file"
                payload = archive.extractfile(member)
                if payload is None:
                    raise RuntimeError(f"cannot read sdist member: {member.name}")
                content_hash = hashlib.sha256(payload.read()).hexdigest()
            elif member.isdir():
                classification = "directory"
                content_hash = ""
            elif member.issym():
                classification = "symlink"
                content_hash = hashlib.sha256(member.linkname.encode("utf-8")).hexdigest()
            else:
                classification = f"tar-type-{member.type!r}"
                content_hash = ""
            canonical = {
                "classification": classification,
                "content_sha256": content_hash,
                "mode": member.mode,
                "path": member.name,
                "size": member.size,
            }
            digest.update(
                json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
            )
            digest.update(b"\n")
    return digest.hexdigest()


def artifact_hashes(directory: Path) -> dict[str, str]:
    wheel = next(directory.glob("*.whl"))
    sdist = next(directory.glob("*.tar.gz"))
    return {
        f"wheel:{wheel.name}:sha256": sha256_file(wheel),
        f"sdist:{sdist.name}:canonical_manifest_sha256": canonical_sdist_manifest(sdist),
    }


def assert_wheel_is_clean(directory: Path) -> None:
    wheel = next(directory.glob("*.whl"))
    with ZipFile(wheel) as archive:
        names = archive.namelist()
    if any(name.startswith("stata_agent/") or name.startswith("app/") for name in names):
        raise RuntimeError("clean wheel contains legacy implementation paths")


def main() -> None:
    started = datetime.now(UTC)
    fingerprint = source_fingerprint()
    run_id = f"{started.strftime('%Y%m%dT%H%M%S.%fZ')}-{fingerprint[:12]}"
    report_directory = ROOT / "verification" / "runs" / run_id
    report_directory.mkdir(parents=True, exist_ok=False)
    report_path = report_directory / "m0-exit-report.json"
    completed_steps: list[str] = []
    report: dict[str, object] = {
        "schema_version": "1",
        "milestone": "M0",
        "run_id": run_id,
        "started_at": started.isoformat(),
        "source_fingerprint": fingerprint,
        "status": "RUNNING",
        "completed_steps": completed_steps,
    }
    try:
        npm_command = shutil.which("npm.cmd") or shutil.which("npm")
        if npm_command is None:
            raise RuntimeError("npm executable is unavailable")
        run(
            "contract generation",
            [
                "uv",
                "run",
                "--locked",
                "--group",
                "dev",
                "python",
                "tools/generate_contracts.py",
            ],
        )
        completed_steps.append("contract_generation")
        run("Python tests", ["uv", "run", "--locked", "--group", "dev", "pytest"])
        completed_steps.append("python_tests")
        run(
            "Node clean install",
            [npm_command, "ci", "--ignore-scripts"],
            cwd=ROOT / "web",
        )
        completed_steps.append("node_clean_install")
        run(
            "TypeScript contract check",
            [npm_command, "run", "typecheck"],
            cwd=ROOT / "web",
        )
        completed_steps.append("typescript_typecheck")

        build_environment = os.environ.copy()
        build_environment["SOURCE_DATE_EPOCH"] = "1789689600"
        build_environment["UV_OFFLINE"] = "1"
        with (
            tempfile.TemporaryDirectory(prefix="stata-agent-build-a-") as first_temp,
            tempfile.TemporaryDirectory(prefix="stata-agent-build-b-") as second_temp,
        ):
            first = Path(first_temp)
            second = Path(second_temp)
            run(
                "offline clean build A",
                ["uv", "build", "--offline", "--no-build-logs", "--out-dir", str(first)],
                env=build_environment,
            )
            run(
                "offline clean build B",
                ["uv", "build", "--offline", "--no-build-logs", "--out-dir", str(second)],
                env=build_environment,
            )
            first_hashes = artifact_hashes(first)
            second_hashes = artifact_hashes(second)
            if first_hashes != second_hashes:
                raise RuntimeError(
                    f"clean build mismatch: first={first_hashes!r}, second={second_hashes!r}"
                )
            assert_wheel_is_clean(first)
        completed_steps.append("offline_double_build")

        register = json.loads((ROOT / "verification" / "register.yaml").read_text(encoding="utf-8"))
        report.update(
            {
                "status": "PASSED",
                "completed_at": datetime.now(UTC).isoformat(),
                "applicable_set": register["applicable_set"],
                "build_artifacts": first_hashes,
                "reproducibility_comparison": {
                    "wheel": "byte-for-byte SHA-256",
                    "sdist": (
                        "canonical member path/classification/mode/size/content; mtime ignored"
                    ),
                },
                "locks": {
                    "uv.lock": sha256_file(ROOT / "uv.lock"),
                    "web/package-lock.json": sha256_file(ROOT / "web" / "package-lock.json"),
                },
                "contracts": {
                    "openapi-v1.json": sha256_file(ROOT / "contracts" / "api" / "openapi-v1.json"),
                    "agent-ipc-v1.schema.json": sha256_file(
                        ROOT / "contracts" / "ipc" / "agent-ipc-v1.schema.json"
                    ),
                    "tool-contract-v1.schema.json": sha256_file(
                        ROOT / "contracts" / "tools" / "tool-contract-v1.schema.json"
                    ),
                },
                "environment": {
                    "os": platform.platform(),
                    "python": sys.version.split()[0],
                },
            }
        )
    except BaseException as error:
        report.update(
            {
                "status": "FAILED",
                "completed_at": datetime.now(UTC).isoformat(),
                "failure_type": type(error).__name__,
            }
        )
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        raise
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(report_path, flush=True)


if __name__ == "__main__":
    main()
