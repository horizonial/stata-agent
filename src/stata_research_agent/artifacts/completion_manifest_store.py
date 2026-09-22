"""Atomic, durable Completion Manifest files outside the SQLite transaction."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import cast

from stata_research_agent.application.ports.completion_manifest import (
    IsolatedCompletion,
    ObservedCompletionArtifact,
    RecoveredIsolatedCompletion,
)
from stata_research_agent.application.stata_operation import StataOperationHandle
from stata_research_agent.domain.identifiers import CompletionManifestId
from stata_research_agent.domain.serialization import to_primitive
from stata_research_agent.domain.stata_execution import (
    StataArtifactOutput,
    StataExecutionReceipt,
    StataExecutionStatus,
    StataRuntimeResult,
)


class CompletionManifestIntegrityError(RuntimeError):
    pass


class FilesystemCompletionManifestStore:
    def __init__(
        self,
        workspace_root: Path,
        *,
        execution_root: Path | None = None,
    ) -> None:
        self._workspace_root = workspace_root.resolve()
        self._execution_root = (
            execution_root.resolve() if execution_root is not None else self._workspace_root
        )
        if not self._execution_root.is_relative_to(self._workspace_root):
            raise ValueError("Completion staging root escaped its Workspace")
        self._root = self._workspace_root / ".stata-agent" / "completions"

    def publish(
        self,
        *,
        manifest_id: CompletionManifestId,
        handle: StataOperationHandle,
        result: StataRuntimeResult,
    ) -> IsolatedCompletion:
        planned = {item.expectation.output_slot: item for item in handle.planned_candidates}
        staging_root = (
            self._execution_root / ".stata-agent" / "staging" / handle.attempt_id.value
        ).resolve()
        artifacts: list[dict[str, object]] = []
        observed: list[ObservedCompletionArtifact] = []
        for output in result.artifacts:
            candidate = planned.get(output.output_slot)
            if candidate is None:
                raise CompletionManifestIntegrityError(
                    f"unplanned output slot {output.output_slot!r}"
                )
            source = Path(output.source_path).resolve(strict=True)
            if not source.is_relative_to(staging_root):
                raise CompletionManifestIntegrityError(
                    "captured output escaped its isolated Attempt root"
                )
            if source.relative_to(staging_root).as_posix() != output.relative_staging_path:
                raise CompletionManifestIntegrityError(
                    "captured output path does not match the Capture Plan"
                )
            size, digest = self._hash_file(source)
            artifacts.append(
                {
                    "artifact_candidate_id": candidate.candidate_id.value,
                    "reserved_artifact_id": candidate.artifact_id.value,
                    "output_slot": output.output_slot,
                    "relative_staging_path": output.relative_staging_path,
                    "expected": output.expected,
                    "artifact_kind": output.artifact_kind,
                    "media_type": output.media_type,
                    "size_bytes": size,
                    "sha256": digest,
                    "producer_locator": output.producer_locator,
                    "capture_status": "captured",
                }
            )
            observed.append(ObservedCompletionArtifact(output.output_slot, size, digest))

        document = {
            "schema_version": "stata-agent.completion-manifest/v1",
            "completion_manifest_id": manifest_id.value,
            "operation_id": handle.operation_id.value,
            "operation_attempt_id": handle.attempt_id.value,
            "session_generation": result.receipt.session_generation,
            "execution_status": result.receipt.execution_status.value,
            "execution_receipt": to_primitive(result.receipt),
            "envelope_schema_version": result.envelope_schema_version,
            "raw_text": result.text,
            "structured_result": to_primitive(result.structured),
            "is_error": result.is_error,
            "artifacts": artifacts,
        }
        encoded = json.dumps(
            document,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        attempt_root = self._root / handle.attempt_id.value
        attempt_root.mkdir(parents=True, exist_ok=True)
        target = attempt_root / "completion.json"
        if target.exists():
            if target.read_bytes() != encoded:
                raise CompletionManifestIntegrityError(
                    "Completion Manifest identity was reused with different content"
                )
        else:
            temporary = attempt_root / f".{manifest_id.value}.partial"
            with temporary.open("xb") as writer:
                writer.write(encoded)
                writer.flush()
                os.fsync(writer.fileno())
            os.replace(temporary, target)
        return IsolatedCompletion(
            manifest_id,
            target.relative_to(self._workspace_root).as_posix(),
            tuple(observed),
        )

    def verify_attempt(self, attempt_id: str) -> dict[str, object] | None:
        target = self._root / attempt_id / "completion.json"
        if not target.is_file():
            return None
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise CompletionManifestIntegrityError("Completion Manifest is unreadable") from error
        if (
            raw.get("schema_version") != "stata-agent.completion-manifest/v1"
            or raw.get("operation_attempt_id") != attempt_id
            or not isinstance(raw.get("artifacts"), list)
        ):
            raise CompletionManifestIntegrityError("Completion Manifest schema mismatch")
        for artifact in raw["artifacts"]:
            if not isinstance(artifact, dict):
                raise CompletionManifestIntegrityError("invalid Artifact manifest entry")
            relative = Path(str(artifact.get("relative_staging_path", "")))
            if relative.is_absolute() or ".." in relative.parts:
                raise CompletionManifestIntegrityError("unsafe Artifact staging path")
            source = (
                self._workspace_root / ".stata-agent" / "staging" / attempt_id / relative
            ).resolve()
            if not source.is_file():
                raise CompletionManifestIntegrityError("captured output is missing")
            size, digest = self._hash_file(source)
            if size != artifact.get("size_bytes") or digest != artifact.get("sha256"):
                raise CompletionManifestIntegrityError("captured output changed after completion")
        return cast(dict[str, object], raw)

    def load_attempt(self, attempt_id: str) -> RecoveredIsolatedCompletion:
        raw = self.verify_attempt(attempt_id)
        if raw is None:
            raise FileNotFoundError(f"no Completion Manifest for {attempt_id}")
        receipt_raw = raw.get("execution_receipt")
        if not isinstance(receipt_raw, dict):
            raise CompletionManifestIntegrityError("missing execution receipt")
        try:
            receipt = StataExecutionReceipt(
                schema_version=str(receipt_raw["schema_version"]),
                executor_instance_id=str(receipt_raw["executor_instance_id"]),
                session_id=str(receipt_raw["session_id"]),
                session_generation=int(receipt_raw["session_generation"]),
                exec_seq=(
                    int(receipt_raw["exec_seq"])
                    if receipt_raw.get("exec_seq") is not None
                    else None
                ),
                execution_status=StataExecutionStatus(str(receipt_raw["execution_status"])),
                rc=int(receipt_raw["rc"]),
                raw_output_status=str(receipt_raw["raw_output_status"]),
                structured_result_status=str(receipt_raw["structured_result_status"]),
                command_hash=(
                    str(receipt_raw["command_hash"])
                    if receipt_raw.get("command_hash") is not None
                    else None
                ),
                data_signature=(
                    str(receipt_raw["data_signature"])
                    if receipt_raw.get("data_signature") is not None
                    else None
                ),
                session_reset=bool(receipt_raw["session_reset"]),
                runtime_environment=dict(receipt_raw["runtime_environment"]),
                supervision_proof=dict(receipt_raw["supervision_proof"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise CompletionManifestIntegrityError(
                "invalid execution receipt in Completion Manifest"
            ) from error
        artifacts_raw = raw["artifacts"]
        assert isinstance(artifacts_raw, list)
        outputs = tuple(
            StataArtifactOutput(
                output_slot=str(item["output_slot"]),
                source_path=str(
                    self._workspace_root
                    / ".stata-agent"
                    / "staging"
                    / attempt_id
                    / str(item["relative_staging_path"])
                ),
                relative_staging_path=str(item["relative_staging_path"]),
                artifact_kind=str(item["artifact_kind"]),
                media_type=str(item["media_type"]),
                producer_locator=str(item["producer_locator"]),
                expected=bool(item["expected"]),
            )
            for item in artifacts_raw
            if isinstance(item, dict)
        )
        structured = raw.get("structured_result")
        if structured is not None and not isinstance(structured, dict):
            raise CompletionManifestIntegrityError("invalid structured result")
        result = StataRuntimeResult(
            envelope_schema_version=str(raw["envelope_schema_version"]),
            text=str(raw["raw_text"]),
            structured=structured,
            receipt=receipt,
            is_error=bool(raw["is_error"]),
            artifacts=outputs,
        )
        return RecoveredIsolatedCompletion(
            CompletionManifestId(str(raw["completion_manifest_id"])),
            str(raw["operation_id"]),
            attempt_id,
            result,
        )

    @staticmethod
    def _hash_file(path: Path) -> tuple[int, str]:
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as reader:
            while block := reader.read(1024 * 1024):
                size += len(block)
                digest.update(block)
        return size, digest.hexdigest()
