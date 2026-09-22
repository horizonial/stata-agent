"""Workspace-scoped execution resources for V0.1 cross-Workspace parallelism."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from stata_research_agent.application.ports.execution_scope import (
    AuthorizedExecutionScope,
    ExecutionScopeAuthority,
)
from stata_research_agent.application.ports.stata_runtime import StataRuntime
from stata_research_agent.domain.identifiers import ExecutionScopeId, TurnId, WorkspaceId
from stata_research_agent.domain.stata_execution import (
    StataArtifactOutputRequest,
    StataRuntimeResult,
    StataSessionCloseResult,
)


class WorkspaceExecutionContractError(RuntimeError):
    """An execution request does not belong to the selected Workspace/Scope."""


class ManagedStataRuntime(StataRuntime, Protocol):
    async def start(self) -> None: ...

    async def close(self) -> None: ...

    async def configure_session_workspace(
        self, *, session_id: str, working_directory: Path
    ) -> None: ...


class ManagedStataRuntimeFactory(Protocol):
    def __call__(self, working_directory: Path) -> ManagedStataRuntime: ...


@dataclass(frozen=True, slots=True)
class ExecutionScopeBinding:
    workspace_id: WorkspaceId
    execution_scope_id: ExecutionScopeId
    session_id: str
    workspace_root: Path
    working_directory: Path


@dataclass(slots=True)
class _RuntimeEntry:
    binding: ExecutionScopeBinding
    runtime: ManagedStataRuntime
    start_lock: asyncio.Lock
    operation_lock: asyncio.Lock
    started: bool = False
    tainted: bool = False


class WorkspaceExecutionPool:
    """Own one host supervisor and one isolated Stata session per Execution Scope.

    The per-entry operation lock is only a resource safety net. Authoritative write ownership
    remains the per-Workspace SQLite write lane; this pool neither grants nor releases it.
    Different Workspace keys use different locks, Stata worker processes, sessions, and working
    directories.  The shared MCP host is only a supervisor/control plane; it admits concurrent
    requests and the executor serializes per session while allowing different sessions to run in
    parallel.  A Stata worker failure therefore taints only its owning scope.
    """

    def __init__(self, runtime_factory: ManagedStataRuntimeFactory) -> None:
        self._runtime_factory = runtime_factory
        self._entries: dict[tuple[str, str], _RuntimeEntry] = {}
        self._entries_lock = asyncio.Lock()
        self._host_root: Path | None = None
        self._runtime: ManagedStataRuntime | None = None

    def runtime_for_active_write_turn(
        self,
        authority: ExecutionScopeAuthority,
        turn_id: TurnId,
    ) -> WorkspaceScopedStataRuntime:
        """Resolve runtime resources only for the authoritative write-lane owner."""

        try:
            authorized = authority.active_write_scope(turn_id)
        except ValueError as error:
            raise WorkspaceExecutionContractError(
                "Turn does not own the selected Workspace write lane"
            ) from error
        return WorkspaceScopedStataRuntime(
            self,
            self._binding(authorized),
            authority=authority,
            active_write_turn_id=turn_id,
        )

    def _binding(
        self,
        authorized: AuthorizedExecutionScope,
    ) -> ExecutionScopeBinding:
        identity = (
            f"{authorized.workspace_id.value}\0{authorized.execution_scope_id.value}"
        ).encode()
        digest = hashlib.sha256(identity).hexdigest()[:32]
        workspace_root = authorized.workspace_root.resolve()
        host_root = workspace_root.parent
        if self._host_root is None:
            self._host_root = host_root
        elif host_root != self._host_root:
            raise WorkspaceExecutionContractError(
                "Workspace does not belong to this execution host"
            )
        working_directory = (workspace_root / ".runtime" / "scopes" / f"scope-{digest}").resolve()
        if not working_directory.is_relative_to(workspace_root):
            raise WorkspaceExecutionContractError(
                "Execution Scope working directory escaped its Workspace"
            )
        return ExecutionScopeBinding(
            workspace_id=authorized.workspace_id,
            execution_scope_id=authorized.execution_scope_id,
            session_id=f"stata_{digest}",
            workspace_root=workspace_root,
            working_directory=working_directory,
        )

    async def _entry(self, binding: ExecutionScopeBinding) -> _RuntimeEntry:
        key = (binding.workspace_id.value, binding.execution_scope_id.value)
        async with self._entries_lock:
            entry = self._entries.get(key)
            if entry is None or entry.tainted:
                if self._runtime is None:
                    if self._host_root is None:
                        raise WorkspaceExecutionContractError(
                            "Execution host root is not initialized"
                        )
                    self._runtime = self._runtime_factory(self._host_root)
                entry = _RuntimeEntry(
                    binding=binding,
                    runtime=self._runtime,
                    start_lock=asyncio.Lock(),
                    operation_lock=asyncio.Lock(),
                )
                self._entries[key] = entry
        async with entry.start_lock:
            if entry.started:
                return entry
            try:
                await entry.runtime.start()
                await entry.runtime.configure_session_workspace(
                    session_id=binding.session_id,
                    working_directory=binding.working_directory,
                )
            except BaseException:
                await self._discard(entry)
                raise
            entry.started = True
        return entry

    async def _discard(self, entry: _RuntimeEntry) -> None:
        entry.tainted = True
        key = (
            entry.binding.workspace_id.value,
            entry.binding.execution_scope_id.value,
        )
        async with self._entries_lock:
            if self._entries.get(key) is entry:
                del self._entries[key]
        if entry.started:
            try:
                await entry.runtime.close_session(
                    session_id=entry.binding.session_id,
                    reason="execution scope tainted",
                )
            except Exception:
                # The shared supervisor may itself be unavailable.  The authoritative
                # Operation has already been classified by the caller; cleanup remains
                # best-effort and must not turn this into a cross-Workspace close.
                pass

    async def _execute(
        self,
        binding: ExecutionScopeBinding,
        *,
        code: str,
        timeout_seconds: float,
        operation_attempt_id: str | None,
        artifact_outputs: tuple[StataArtifactOutputRequest, ...],
    ) -> StataRuntimeResult:
        while True:
            entry = await self._entry(binding)
            async with entry.operation_lock:
                if entry.tainted:
                    continue
                try:
                    result = await entry.runtime.execute(
                        session_id=binding.session_id,
                        code=code,
                        timeout_seconds=timeout_seconds,
                        operation_attempt_id=operation_attempt_id,
                        artifact_outputs=artifact_outputs,
                    )
                except BaseException:
                    await self._discard(entry)
                    raise
                if result.receipt.execution_status.is_uncertain:
                    await self._discard(entry)
                return result

    async def _close_session(
        self, binding: ExecutionScopeBinding, *, reason: str
    ) -> StataSessionCloseResult:
        key = (binding.workspace_id.value, binding.execution_scope_id.value)
        async with self._entries_lock:
            entry = self._entries.get(key)
            runtime = self._runtime
        if entry is None:
            if runtime is None:
                raise WorkspaceExecutionContractError(
                    "Execution Scope has no active Stata supervisor"
                )
            # Session close is idempotent and the MCP control tool never creates a session.
            # This path is used after an abort already removed the scope from the pool.
            return await runtime.close_session(
                session_id=binding.session_id,
                reason=reason,
            )
        async with entry.operation_lock:
            if entry.tainted:
                return await entry.runtime.close_session(
                    session_id=binding.session_id,
                    reason=reason,
                )
            return await entry.runtime.close_session(
                session_id=binding.session_id,
                reason=reason,
            )

    async def abort_scope(
        self, binding: ExecutionScopeBinding, *, reason: str
    ) -> StataSessionCloseResult | None:
        """Force-close one scope without waiting for its in-flight operation lock."""

        key = (binding.workspace_id.value, binding.execution_scope_id.value)
        async with self._entries_lock:
            entry = self._entries.pop(key, None)
        if entry is None:
            return None
        entry.tainted = True
        if not entry.started:
            return None
        return await entry.runtime.close_session(
            session_id=binding.session_id,
            reason=reason,
        )

    async def close_scope(self, binding: ExecutionScopeBinding) -> None:
        key = (binding.workspace_id.value, binding.execution_scope_id.value)
        async with self._entries_lock:
            entry = self._entries.pop(key, None)
        if entry is None:
            return
        async with entry.operation_lock:
            entry.tainted = True
            if entry.started:
                try:
                    await entry.runtime.close_session(
                        session_id=binding.session_id,
                        reason="execution scope closed",
                    )
                except Exception:
                    pass

    async def close(self) -> None:
        async with self._entries_lock:
            entries = tuple(self._entries.values())
            self._entries.clear()
        for entry in entries:
            entry.tainted = True
        await asyncio.gather(
            *(
                entry.runtime.close_session(
                    session_id=entry.binding.session_id,
                    reason="execution pool closed",
                )
                for entry in entries
                if entry.started
            ),
            return_exceptions=True,
        )
        runtime = self._runtime
        self._runtime = None
        if runtime is not None:
            await runtime.close()


class WorkspaceScopedStataRuntime(StataRuntime):
    """StataRuntime adapter pinned to one exact Workspace Execution Scope."""

    def __init__(
        self,
        pool: WorkspaceExecutionPool,
        binding: ExecutionScopeBinding,
        *,
        authority: ExecutionScopeAuthority,
        active_write_turn_id: TurnId,
    ) -> None:
        self._pool = pool
        self.binding = binding
        self._authority = authority
        self._active_write_turn_id = active_write_turn_id
        self._abort_requested = False

    @property
    def session_id(self) -> str:
        return self.binding.session_id

    @property
    def working_directory(self) -> Path:
        return self.binding.working_directory

    def _validate_session(self, session_id: str) -> None:
        if session_id != self.binding.session_id:
            raise WorkspaceExecutionContractError(
                "Stata session identity does not match the selected Execution Scope"
            )

    def _revalidate_write_lane(self) -> None:
        try:
            current = self._authority.active_write_scope(self._active_write_turn_id)
        except ValueError as error:
            raise WorkspaceExecutionContractError(
                "Turn no longer owns the Workspace write lane"
            ) from error
        if (
            current.workspace_id != self.binding.workspace_id
            or current.execution_scope_id != self.binding.execution_scope_id
            or current.workspace_root.resolve() != self.binding.workspace_root
        ):
            raise WorkspaceExecutionContractError(
                "Execution Scope grant changed before Stata handoff"
            )

    async def prepare(self) -> None:
        """Start this scope's transport without executing a research command."""

        self._revalidate_write_lane()
        await self._pool._entry(self.binding)

    async def execute(
        self,
        *,
        session_id: str,
        code: str,
        timeout_seconds: float,
        operation_attempt_id: str | None = None,
        artifact_outputs: tuple[StataArtifactOutputRequest, ...] = (),
    ) -> StataRuntimeResult:
        self._validate_session(session_id)
        self._revalidate_write_lane()
        result = await self._pool._execute(
            self.binding,
            code=code,
            timeout_seconds=timeout_seconds,
            operation_attempt_id=operation_attempt_id,
            artifact_outputs=artifact_outputs,
        )
        if self._abort_requested and result.receipt.execution_status.is_uncertain:
            proof = dict(result.receipt.supervision_proof)
            proof["scope_abort_requested"] = True
            result = replace(
                result,
                receipt=replace(result.receipt, supervision_proof=proof),
            )
        return result

    async def close_session(self, *, session_id: str, reason: str) -> StataSessionCloseResult:
        self._validate_session(session_id)
        self._revalidate_write_lane()
        return await self._pool._close_session(self.binding, reason=reason)

    async def close_scope(self) -> None:
        await self._pool.close_scope(self.binding)

    async def abort_scope(self, *, reason: str) -> StataSessionCloseResult | None:
        """Terminate only this Workspace scope, including an in-flight Stata worker."""

        self._abort_requested = True
        return await self._pool.abort_scope(self.binding, reason=reason)
