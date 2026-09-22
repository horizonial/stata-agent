"""Windows BaseContainer staged executor for arbitrary Python and PowerShell."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import shutil
import stat
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from stata_research_agent.application.sandbox_execution import (
    SandboxExecutionError,
    SandboxExecutionReceipt,
    SandboxExecutionRequest,
    SandboxInput,
    SandboxIntegrityViolationError,
    SandboxOutcomeUnknownError,
    SandboxOutputCandidate,
)
from stata_research_agent.application.sensitive_output import SensitiveOutputGate

PINNED_WXC_SHA256 = "45bdb9b58eac1e6e3d110e37c2950b4b0d8c7b4f6d1ef34b365983b5bfa1b8e6"


@dataclass(frozen=True, slots=True)
class StagingAliasFinding:
    relative_path: str
    reason_code: str


class WindowsStagingAliasValidator:
    """Reject reparse points and multiple hardlink aliases before/after execution."""

    def inspect_path(self, path: Path) -> StagingAliasFinding | None:
        """Validate one exact file without making claims about unrelated siblings."""

        path = path.absolute()
        info = path.stat(follow_symlinks=False)
        attributes = getattr(info, "st_file_attributes", 0)
        is_reparse = bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
        is_junction = bool(getattr(os.path, "isjunction", lambda _path: False)(path))
        if path.is_symlink() or is_reparse or is_junction:
            return StagingAliasFinding(path.name, "reparse_point")
        if not path.is_file():
            return StagingAliasFinding(path.name, "not_regular_file")
        if self._link_count(path) > 1:
            return StagingAliasFinding(path.name, "multiple_hardlinks")
        return None

    def inspect(self, root: Path) -> tuple[StagingAliasFinding, ...]:
        root = root.resolve(strict=True)
        findings: list[StagingAliasFinding] = []
        pending = [root]
        while pending:
            current = pending.pop()
            with os.scandir(current) as entries:
                for entry in entries:
                    path = Path(entry.path)
                    info = entry.stat(follow_symlinks=False)
                    attributes = getattr(info, "st_file_attributes", 0)
                    is_reparse = bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
                    is_junction = bool(getattr(os.path, "isjunction", lambda _path: False)(path))
                    relative = path.relative_to(root).as_posix()
                    if entry.is_symlink() or is_reparse or is_junction:
                        findings.append(StagingAliasFinding(relative, "reparse_point"))
                        continue
                    if entry.is_file(follow_symlinks=False) and self._link_count(path) > 1:
                        findings.append(StagingAliasFinding(relative, "multiple_hardlinks"))
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(path)
        return tuple(sorted(findings, key=lambda item: item.relative_path))

    @staticmethod
    def _link_count(path: Path) -> int:
        if os.name != "nt":
            return path.stat(follow_symlinks=False).st_nlink

        class FileTime(ctypes.Structure):
            _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]

        class ByHandleFileInformation(ctypes.Structure):
            _fields_ = [
                ("attributes", ctypes.c_uint32),
                ("creation_time", FileTime),
                ("last_access_time", FileTime),
                ("last_write_time", FileTime),
                ("volume_serial_number", ctypes.c_uint32),
                ("file_size_high", ctypes.c_uint32),
                ("file_size_low", ctypes.c_uint32),
                ("number_of_links", ctypes.c_uint32),
                ("file_index_high", ctypes.c_uint32),
                ("file_index_low", ctypes.c_uint32),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
        ]
        create_file.restype = ctypes.c_void_p
        get_info = kernel32.GetFileInformationByHandle
        get_info.argtypes = [ctypes.c_void_p, ctypes.POINTER(ByHandleFileInformation)]
        get_info.restype = ctypes.c_int
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [ctypes.c_void_p]
        close_handle.restype = ctypes.c_int
        handle = create_file(
            str(path),
            0x0080,
            0x0001 | 0x0002 | 0x0004,
            None,
            3,
            0,
            None,
        )
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            information = ByHandleFileInformation()
            if not get_info(handle, ctypes.byref(information)):
                raise ctypes.WinError(ctypes.get_last_error())
            return int(information.number_of_links)
        finally:
            close_handle(handle)


class WindowsSandboxExecutor:
    """Copy authorized inputs into a fresh staging root and run only in BaseContainer."""

    def __init__(
        self,
        *,
        wxc_executable: Path,
        python_executable: Path,
        powershell_executable: Path,
        execution_root: Path,
        sensitive_output_gate: SensitiveOutputGate | None = None,
        validator: WindowsStagingAliasValidator | None = None,
    ) -> None:
        if os.name != "nt":
            raise SandboxExecutionError("BaseContainer executor requires Windows")
        self._wxc = wxc_executable.resolve(strict=True)
        if self._sha256(self._wxc) != PINNED_WXC_SHA256:
            raise SandboxExecutionError("wxc-exec binary does not match the pinned release")
        self._python = python_executable.resolve(strict=True)
        self._powershell = powershell_executable.resolve(strict=True)
        self._root = execution_root.resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        self._staging_root = self._root / ".stata-agent" / "staging"
        self._staging_root.mkdir(parents=True, exist_ok=True)
        self._gate = sensitive_output_gate or SensitiveOutputGate()
        self._validator = validator or WindowsStagingAliasValidator()

    def execute(self, request: SandboxExecutionRequest) -> SandboxExecutionReceipt:
        attempt_root = (self._staging_root / request.attempt_id).resolve()
        if not attempt_root.is_relative_to(self._staging_root):
            raise SandboxExecutionError("sandbox attempt escaped the execution root")
        try:
            attempt_root.mkdir(parents=False, exist_ok=False)
        except FileExistsError as error:
            raise SandboxExecutionError("sandbox attempt identity was already used") from error
        scratch = attempt_root / "scratch"
        inputs = scratch / "inputs"
        outputs = scratch / "outputs"
        local_app_data = scratch / "localappdata"
        for path in (scratch, inputs, outputs, local_app_data):
            path.mkdir()
        self._copy_inputs(request.inputs, inputs)
        program = scratch / ("program.py" if request.language == "python" else "program.ps1")
        program.write_text(request.code, encoding="utf-8")
        preflight = self._validator.inspect(scratch)
        if preflight:
            raise SandboxExecutionError("sandbox staging preflight rejected aliases")

        executable = self._python if request.language == "python" else self._powershell
        argv = (
            (str(executable), "-I", "-B", str(program))
            if request.language == "python"
            else (
                str(executable),
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(program),
            )
        )
        policy = self._policy(request, scratch, executable, argv)
        policy_json = json.dumps(
            policy,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        policy_sha256 = hashlib.sha256(policy_json.encode()).hexdigest()
        policy_path = attempt_root / "policy.json"
        policy_path.write_text(policy_json, encoding="utf-8")
        probe = subprocess.run(
            [str(self._wxc), "--experimental", "--probe", str(policy_path)],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        try:
            capabilities = json.loads(probe.stdout)
        except json.JSONDecodeError as error:
            raise SandboxExecutionError("BaseContainer capability probe was invalid") from error
        if (
            probe.returncode != 0
            or capabilities.get("tier") != "base-container"
            or capabilities.get("needsDaclAugmentation") is not False
        ):
            raise SandboxExecutionError("BaseContainer isolation is unavailable")
        try:
            completed = subprocess.run(
                [str(self._wxc), "--experimental", str(policy_path)],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=request.timeout_seconds + 30,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except subprocess.TimeoutExpired as error:
            raise SandboxOutcomeUnknownError(
                "BaseContainer execution timed out without a reliable receipt"
            ) from error
        postflight = self._validator.inspect(scratch)
        if postflight:
            raise SandboxIntegrityViolationError("sandbox staging postflight rejected aliases")
        safe_stdout = self._gate.inspect_text("sandbox.stdout", completed.stdout)
        safe_stderr = self._gate.inspect_text("sandbox.stderr", completed.stderr)
        if safe_stdout.verdict != "safe" or safe_stderr.verdict != "safe":
            raise SandboxExecutionError("CREDENTIAL_OUTPUT_BLOCKED")
        candidates = self._outputs(outputs)
        for candidate in candidates:
            result = self._gate.assert_clean_file(
                "sandbox.output_candidate", outputs / PurePosixPath(candidate.relative_path)
            )
            if result.verdict != "safe":
                raise SandboxExecutionError("CREDENTIAL_OUTPUT_BLOCKED")
        return SandboxExecutionReceipt(
            "stata-agent.sandbox-receipt/v1",
            request.attempt_id,
            "base-container",
            policy_sha256,
            request.network_mode,
            (str(executable.parent),),
            (str(scratch),),
            False,
            "passed",
            "passed",
            completed.returncode,
            str(safe_stdout.safe_value),
            str(safe_stderr.safe_value),
            program,
            candidates,
        )

    def _copy_inputs(self, sources: tuple[SandboxInput, ...], input_root: Path) -> None:
        for source in sources:
            relative = PurePosixPath(source.relative_target)
            if relative.is_absolute() or ".." in relative.parts or not relative.parts:
                raise SandboxExecutionError("sandbox input target is invalid")
            source_path = source.source_path.absolute()
            if self._validator.inspect_path(source_path) is not None:
                raise SandboxExecutionError("sandbox input source failed alias validation")
            target = input_root.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_path, target, follow_symlinks=False)

    def _policy(
        self,
        request: SandboxExecutionRequest,
        scratch: Path,
        executable: Path,
        argv: tuple[str, ...],
    ) -> dict[str, object]:
        environment = (
            f"SYSTEMROOT={os.environ['SYSTEMROOT']}",
            f"WINDIR={os.environ['WINDIR']}",
            f"LOCALAPPDATA={scratch / 'localappdata'}",
            f"TEMP={scratch}",
            f"TMP={scratch}",
            f"SRA_INPUT_DIR={scratch / 'inputs'}",
            f"SRA_OUTPUT_DIR={scratch / 'outputs'}",
            "MPLBACKEND=Agg",
            "PYTHONNOUSERSITE=1",
        )
        return {
            "version": "0.8.0-alpha",
            "containerId": f"stataagent-{uuid.uuid4().hex}",
            "containment": "processcontainer",
            "lifecycle": {"destroyOnExit": True, "preservePolicy": False},
            "process": {
                "commandLine": subprocess.list2cmdline(list(argv)),
                "cwd": str(scratch),
                "env": list(environment),
                "timeout": request.timeout_seconds * 1000,
            },
            "filesystem": {
                "readwritePaths": [str(scratch)],
                "readonlyPaths": [str(executable.parent)],
                "deniedPaths": [],
            },
            "fallback": {"allowDaclMutation": False},
            "network": {
                "defaultPolicy": request.network_mode,
                "enforcementMode": "capabilities",
            },
            "ui": {"disable": False, "clipboard": "none", "injection": False},
            "processContainer": {
                "capabilities": ["internetClient"] if request.network_mode == "allow" else [],
                "ui": {
                    "isolation": "desktop",
                    "desktopSystemControl": False,
                    "systemSettings": "none",
                    "ime": False,
                },
            },
        }

    @staticmethod
    def _outputs(root: Path) -> tuple[SandboxOutputCandidate, ...]:
        candidates = []
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            candidates.append(
                SandboxOutputCandidate(
                    path.relative_to(root).as_posix(),
                    path,
                    path.stat().st_size,
                    WindowsSandboxExecutor._sha256(path),
                )
            )
        return tuple(candidates)

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()
