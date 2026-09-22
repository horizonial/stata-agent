"""Tool Broker adapter for BaseContainer-staged Python and PowerShell."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from stata_research_agent.application.broker_execution import (
    BeginBrokerExecutionCommand,
    BrokerExecutionHandle,
    CompleteBrokerExecutionCommand,
)
from stata_research_agent.application.broker_execution_service import (
    BrokerExecutionService,
)
from stata_research_agent.application.ports.identity import IdentityGenerator
from stata_research_agent.application.ports.sandbox_executor import SandboxExecutor
from stata_research_agent.application.produced_artifact import (
    CaptureProducedArtifactsCommand,
    ProducedArtifactCandidate,
)
from stata_research_agent.application.produced_artifact_service import (
    ProducedArtifactService,
)
from stata_research_agent.application.sandbox_execution import (
    SandboxExecutionError,
    SandboxExecutionRequest,
    SandboxInput,
    SandboxIntegrityViolationError,
    SandboxOutcomeUnknownError,
)
from stata_research_agent.application.turn_driver import (
    AdmittedToolExecutor,
    ToolExecutionRequest,
    ToolExecutionResult,
)
from stata_research_agent.domain.artifact_data import ArtifactKind
from stata_research_agent.domain.identifiers import CommandId

ArtifactPathResolver = Callable[[str], Path]


@dataclass(frozen=True, slots=True)
class SandboxToolExecutor:
    sandbox: SandboxExecutor
    bridge: BrokerExecutionService
    identities: IdentityGenerator
    artifact_path_resolver: ArtifactPathResolver | None = None
    network_allowed: bool = False
    produced_artifacts: ProducedArtifactService | None = None

    async def execute(self, request: ToolExecutionRequest) -> ToolExecutionResult:
        language = self._language(request.tool_name)
        code = request.arguments.get("code")
        if not isinstance(code, str) or not code.strip():
            raise ValueError(f"{request.tool_name} requires non-empty string code")
        network_mode = request.arguments.get("network_mode", "block")
        if network_mode not in {"block", "allow"}:
            raise ValueError("sandbox network_mode must be block or allow")
        if network_mode == "allow" and not self.network_allowed:
            raise ValueError("sandbox network access is denied by the current permission policy")
        timeout = request.arguments.get("timeout_seconds", 300)
        if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 3600:
            raise ValueError("sandbox timeout_seconds must be an integer from 1 to 3600")
        if request.remaining_time_seconds is not None:
            timeout = min(timeout, max(1, int(request.remaining_time_seconds)))
        inputs = self._inputs(request.arguments.get("inputs", []))
        handle = self.bridge.begin(
            BeginBrokerExecutionCommand(
                self.identities.new(CommandId),
                request.turn_id,
                request.tool_call_id,
                request.operation_id,
            )
        )
        if handle.replayed:
            return ToolExecutionResult(False, "SANDBOX_HANDOFF_ALREADY_COMMITTED")
        try:
            receipt = await asyncio.to_thread(
                self.sandbox.execute,
                SandboxExecutionRequest(
                    handle.attempt_id.value,
                    language,
                    code,
                    inputs,
                    str(network_mode),
                    timeout,
                ),
            )
        except SandboxOutcomeUnknownError:
            return self._finish_error(handle, "outcome_unknown", "SANDBOX_OUTCOME_UNKNOWN")
        except SandboxIntegrityViolationError:
            return self._finish_error(
                handle, "integrity_violation", "SANDBOX_STAGING_INTEGRITY_VIOLATION"
            )
        except SandboxExecutionError as error:
            return self._finish_error(handle, "failed", str(error))
        except Exception as error:
            return self._finish_error(
                handle,
                "outcome_unknown",
                f"SANDBOX_HOST_FAILURE:{type(error).__name__}",
            )

        captured = None
        if self.produced_artifacts is not None:
            try:
                captured = self.produced_artifacts.capture(
                    CaptureProducedArtifactsCommand(
                        self.identities.new(CommandId),
                        handle.operation_id,
                        handle.attempt_id,
                        (
                            ProducedArtifactCandidate(
                                receipt.executable_source_path,
                                ArtifactKind.CODE,
                                ("text/x-python" if language == "python" else "text/x-powershell"),
                                "executable",
                                receipt.executable_source_path.name,
                            ),
                            *tuple(
                                ProducedArtifactCandidate(
                                    item.source_path,
                                    *self._artifact_type(item.relative_path),
                                    "output",
                                    item.relative_path,
                                )
                                for item in receipt.output_candidates
                            ),
                        ),
                    )
                )
            except Exception as error:
                return self._finish_error(
                    handle,
                    "completed_unreconciled",
                    f"SANDBOX_ARTIFACT_FINALIZATION_REQUIRED:{type(error).__name__}",
                )

        success = receipt.exit_code == 0
        payload: dict[str, Any] = {
            "operation_id": handle.operation_id.value,
            "operation_attempt_id": handle.attempt_id.value,
            "isolation_tier": receipt.isolation_tier,
            "network_mode": receipt.network_mode,
            "exit_code": receipt.exit_code,
            "stdout": receipt.safe_stdout,
            "stderr": receipt.safe_stderr,
            "input_artifact_ids": self._input_artifact_ids(request.arguments.get("inputs", [])),
            "captured_artifacts": (
                []
                if captured is None
                else [
                    {
                        "artifact_id": item.artifact_id.value,
                        "artifact_kind": item.artifact_kind.value,
                        "media_type": item.media_type,
                        "role": item.role,
                        "relative_name": item.relative_name,
                        "sha256": item.content_sha256,
                        "size_bytes": item.size_bytes,
                        "verification_receipt_id": (item.verification_receipt_id.value),
                    }
                    for item in captured.artifacts
                ]
            ),
            "output_candidates": [
                {
                    "relative_path": item.relative_path,
                    "size_bytes": item.size_bytes,
                    "sha256": item.sha256,
                }
                for item in receipt.output_candidates
            ],
        }
        terminal_status = "completed" if success else "failed"
        outcome = self.bridge.complete(
            CompleteBrokerExecutionCommand(
                self.identities.new(CommandId),
                handle,
                success,
                "Sandbox execution completed" if success else "Sandbox command failed",
                payload,
                artifact_references=(
                    ()
                    if captured is None
                    else tuple(item.artifact_id.value for item in captured.artifacts)
                ),
                execution_receipt=receipt.authoritative_payload(),
                terminal_status=terminal_status,
            )
        )
        payload["status"] = outcome.status
        return ToolExecutionResult(
            success,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        )

    def _finish_error(
        self, handle: BrokerExecutionHandle, terminal_status: str, reason_code: str
    ) -> ToolExecutionResult:
        payload = {"reason_code": reason_code}
        outcome = self.bridge.complete(
            CompleteBrokerExecutionCommand(
                self.identities.new(CommandId),
                handle,
                False,
                reason_code,
                payload,
                terminal_status=terminal_status,
            )
        )
        payload["status"] = outcome.status
        return ToolExecutionResult(
            False,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        )

    @staticmethod
    def _input_artifact_ids(raw: object) -> list[str]:
        if not isinstance(raw, list):
            return []
        return [
            str(binding["artifact_id"])
            for binding in raw
            if isinstance(binding, Mapping) and isinstance(binding.get("artifact_id"), str)
        ]

    @staticmethod
    def _artifact_type(relative_path: str) -> tuple[ArtifactKind, str]:
        suffix = Path(relative_path).suffix.lower()
        known: dict[str, tuple[ArtifactKind, str]] = {
            ".csv": (ArtifactKind.TABLE, "text/csv"),
            ".json": (ArtifactKind.TABLE, "application/json"),
            ".xlsx": (
                ArtifactKind.TABLE,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ),
            ".dta": (ArtifactKind.DATASET, "application/x-stata-dta"),
            ".png": (ArtifactKind.DIAGNOSTIC, "image/png"),
            ".jpg": (ArtifactKind.DIAGNOSTIC, "image/jpeg"),
            ".jpeg": (ArtifactKind.DIAGNOSTIC, "image/jpeg"),
            ".svg": (ArtifactKind.DIAGNOSTIC, "image/svg+xml"),
            ".pdf": (ArtifactKind.DIAGNOSTIC, "application/pdf"),
            ".txt": (ArtifactKind.LOG, "text/plain"),
            ".log": (ArtifactKind.LOG, "text/plain"),
            ".rtf": (ArtifactKind.DOCUMENT, "application/rtf"),
            ".docx": (
                ArtifactKind.DOCUMENT,
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ),
        }
        return known.get(suffix, (ArtifactKind.DIAGNOSTIC, "application/octet-stream"))

    def _inputs(self, raw: object) -> tuple[SandboxInput, ...]:
        if not isinstance(raw, list):
            raise ValueError("sandbox inputs must be an array")
        if raw and self.artifact_path_resolver is None:
            raise ValueError("sandbox Artifact input resolver is unavailable")
        resolved: list[SandboxInput] = []
        for binding in raw:
            if not isinstance(binding, Mapping):
                raise ValueError("sandbox input binding must be an object")
            artifact_id = binding.get("artifact_id")
            relative_target = binding.get("relative_target")
            if not isinstance(artifact_id, str) or not artifact_id.startswith("artifact_"):
                raise ValueError("sandbox input requires an Artifact ID")
            if not isinstance(relative_target, str):
                raise ValueError("sandbox input relative_target must be a string")
            assert self.artifact_path_resolver is not None
            resolved.append(SandboxInput(self.artifact_path_resolver(artifact_id), relative_target))
        return tuple(resolved)

    @staticmethod
    def _language(tool_name: str) -> str:
        if tool_name == "python.run":
            return "python"
        if tool_name == "shell.run":
            return "powershell"
        raise ValueError(f"unsupported admitted tool: {tool_name}")


@dataclass(frozen=True, slots=True)
class RoutedToolExecutor:
    """Route exact open Tool names without giving any executor a generic bypass."""

    routes: Mapping[str, AdmittedToolExecutor]

    async def execute(self, request: ToolExecutionRequest) -> ToolExecutionResult:
        executor = self.routes.get(request.tool_name)
        if executor is None:
            raise ValueError(f"no admitted executor route for {request.tool_name}")
        return await executor.execute(request)
