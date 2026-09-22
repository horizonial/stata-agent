"""Filesystem adapter for immutable managed Artifact payloads."""

from __future__ import annotations

import hashlib
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from stata_research_agent.application.ports.artifact_data import ManagedPayload
from stata_research_agent.domain.identifiers import ArtifactId, OperationAttemptId


class UnstableSourceError(RuntimeError):
    """The source path or open file changed while it was being captured."""


class ManagedPayloadIntegrityError(RuntimeError):
    """A previously published managed handle no longer matches its payload."""


class InsufficientArtifactCapacityError(RuntimeError):
    """The managed store cannot preserve the configured recovery headroom."""

    def __init__(self, *, required_bytes: int, available_bytes: int) -> None:
        self.required_bytes = required_bytes
        self.available_bytes = available_bytes
        super().__init__(
            "insufficient managed-store capacity: "
            f"required={required_bytes}, available={available_bytes}"
        )


@dataclass(frozen=True, slots=True)
class PublishedManagedPayload:
    managed_handle: str
    size_bytes: int
    sha256: str
    source_locator: str
    source_size_bytes: int
    source_modified_ns: int
    source_file_identity: str


def _stat_identity(stat_result: os.stat_result) -> tuple[int, int, int, int]:
    return (
        int(stat_result.st_dev),
        int(stat_result.st_ino),
        int(stat_result.st_size),
        int(stat_result.st_mtime_ns),
    )


class FilesystemManagedArtifactStore:
    """Copies bytes into the hidden store; managed handles are Workspace-relative."""

    CAPACITY_MULTIPLIER = 3
    RECOVERY_RESERVE_BYTES = 512 * 1024 * 1024

    def __init__(
        self,
        workspace_root: Path,
        *,
        execution_root: Path | None = None,
        chunk_size: int = 1024 * 1024,
        after_copy_hook: Callable[[Path], None] | None = None,
        free_space_provider: Callable[[Path], int] | None = None,
    ) -> None:
        if chunk_size < 64 * 1024:
            raise ValueError("chunk_size must be at least 64 KiB")
        self._workspace_root = workspace_root.resolve()
        self._private_root = self._workspace_root / ".stata-agent"
        self._objects_root = self._private_root / "objects"
        self._staging_root = self._private_root / "staging"
        candidate_root = (
            execution_root.resolve() if execution_root is not None else self._workspace_root
        )
        if not candidate_root.is_relative_to(self._workspace_root):
            raise ValueError("candidate staging root escaped its Workspace")
        self._candidate_staging_root = candidate_root / ".stata-agent" / "staging"
        self._chunk_size = chunk_size
        self._after_copy_hook = after_copy_hook
        self._free_space_provider = free_space_provider or (
            lambda path: int(shutil.disk_usage(path).free)
        )

    @classmethod
    def required_capacity_bytes(cls, payload_size_bytes: int) -> int:
        if payload_size_bytes < 0:
            raise ValueError("payload size cannot be negative")
        return cls.CAPACITY_MULTIPLIER * payload_size_bytes + cls.RECOVERY_RESERVE_BYTES

    def capture(
        self,
        source_path: Path,
        *,
        artifact_id: ArtifactId,
        attempt_id: OperationAttemptId,
    ) -> ManagedPayload:
        source = source_path.resolve(strict=True)
        if source.is_relative_to(self._private_root):
            raise ValueError("private Workspace storage cannot be recaptured as a working file")

        return self._publish(source, artifact_id=artifact_id, attempt_id=attempt_id)

    def prepare_attempt_staging(self, attempt_id: OperationAttemptId) -> Path:
        root = (self._staging_root / attempt_id.value).resolve()
        if not root.is_relative_to(self._staging_root.resolve()):
            raise ValueError("Attempt staging path escaped the private staging root")
        root.mkdir(parents=True, exist_ok=True)
        return root

    def write_staging_bytes(
        self, attempt_id: OperationAttemptId, relative_path: str, payload: bytes
    ) -> Path:
        raw = Path(relative_path)
        if raw.is_absolute() or ".." in raw.parts:
            raise ValueError("staging relative path is unsafe")
        root = self.prepare_attempt_staging(attempt_id)
        target = (root / raw).resolve()
        if not target.is_relative_to(root):
            raise ValueError("staging target escaped the Attempt root")
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with target.open("xb") as writer:
                writer.write(payload)
                writer.flush()
                os.fsync(writer.fileno())
        except FileExistsError:
            if target.read_bytes() != payload:
                raise ValueError("staging target already contains different bytes") from None
        return target

    def publish_candidate(
        self,
        source_path: Path,
        *,
        artifact_id: ArtifactId,
        attempt_id: OperationAttemptId,
    ) -> ManagedPayload:
        source = source_path.resolve(strict=True)
        attempt_root = (self._candidate_staging_root / attempt_id.value).resolve()
        if not source.is_relative_to(attempt_root):
            raise ValueError("candidate output escaped its isolated Attempt root")
        return self._publish(source, artifact_id=artifact_id, attempt_id=attempt_id)

    def _publish(
        self,
        source: Path,
        *,
        artifact_id: ArtifactId,
        attempt_id: OperationAttemptId,
    ) -> ManagedPayload:
        if not source.is_file():
            raise ValueError("capture source must be a regular file")

        before_path = source.stat()
        required_capacity = self.required_capacity_bytes(int(before_path.st_size))
        available_capacity = self._free_space_provider(self._workspace_root)
        if available_capacity < required_capacity:
            raise InsufficientArtifactCapacityError(
                required_bytes=required_capacity,
                available_bytes=available_capacity,
            )
        attempt_root = self._staging_root / attempt_id.value
        attempt_root.mkdir(parents=True, exist_ok=True)
        temporary = attempt_root / f"{artifact_id.value}.partial"
        digest = hashlib.sha256()
        copied = 0

        try:
            with source.open("rb") as reader, temporary.open("xb") as writer:
                opened_before = os.fstat(reader.fileno())
                while block := reader.read(self._chunk_size):
                    writer.write(block)
                    digest.update(block)
                    copied += len(block)
                writer.flush()
                os.fsync(writer.fileno())
                opened_after = os.fstat(reader.fileno())

            if self._after_copy_hook is not None:
                self._after_copy_hook(source)
            after_path = source.stat()
            stable = (
                _stat_identity(before_path)
                == _stat_identity(opened_before)
                == _stat_identity(opened_after)
                == _stat_identity(after_path)
            )
            if not stable or copied != int(before_path.st_size):
                raise UnstableSourceError("source changed while capture was in progress")

            sha256 = digest.hexdigest()
            object_directory = self._objects_root / artifact_id.value
            object_directory.mkdir(parents=True, exist_ok=True)
            final_path = object_directory / f"{sha256}.payload"
            if final_path.exists():
                existing_size, existing_hash = self._hash_file(final_path)
                if existing_size != copied or existing_hash != sha256:
                    raise ManagedPayloadIntegrityError("managed publish collision")
                temporary.unlink(missing_ok=True)
            else:
                os.replace(temporary, final_path)
                try:
                    final_path.chmod(0o444)
                except OSError:
                    pass

            return PublishedManagedPayload(
                managed_handle=final_path.relative_to(self._workspace_root).as_posix(),
                size_bytes=copied,
                sha256=sha256,
                source_locator=self.source_locator(source),
                source_size_bytes=int(before_path.st_size),
                source_modified_ns=int(before_path.st_mtime_ns),
                source_file_identity=f"{before_path.st_dev}:{before_path.st_ino}",
            )
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    def verify(self, managed_handle: str) -> tuple[int | None, str | None]:
        path = self._resolve_managed_handle(managed_handle)
        if not path.is_file():
            return None, None
        return self._hash_file(path)

    def read_small_payload(self, managed_handle: str, *, max_bytes: int) -> bytes:
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        path = self._resolve_managed_handle(managed_handle)
        if not path.is_file():
            raise FileNotFoundError("managed payload is missing")
        if path.stat().st_size > max_bytes:
            raise ValueError("managed payload exceeds the bounded read limit")
        return path.read_bytes()

    def source_locator(self, source_path: Path) -> str:
        source = source_path.resolve(strict=True)
        if source.is_relative_to(self._workspace_root):
            return f"workspace://{source.relative_to(self._workspace_root).as_posix()}"
        return f"external-file://{source.name}"

    def _resolve_managed_handle(self, managed_handle: str) -> Path:
        raw = Path(managed_handle)
        if raw.is_absolute() or ".." in raw.parts:
            raise ValueError("managed handle must be a safe Workspace-relative path")
        resolved = (self._workspace_root / raw).resolve()
        if not resolved.is_relative_to(self._objects_root.resolve()):
            raise ValueError("managed handle escaped the objects store")
        return resolved

    def _hash_file(self, path: Path) -> tuple[int, str]:
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as reader:
            while block := reader.read(self._chunk_size):
                digest.update(block)
                size += len(block)
        return size, digest.hexdigest()
