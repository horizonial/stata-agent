"""Pinned stdio MCP adapter for the real local ``stata-mcp`` executor."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from stata_research_agent.domain.stata_execution import (
    StataArtifactOutput,
    StataArtifactOutputRequest,
    StataExecutionReceipt,
    StataExecutionStatus,
    StataRuntimeResult,
    StataSessionCloseResult,
)


class StataRuntimeContractError(RuntimeError):
    """The pinned executor did not satisfy the reviewed MCP contract."""


@dataclass(slots=True)
class _TransportRequest:
    operation: Callable[[ClientSession], Awaitable[Any]]
    future: asyncio.Future[Any]


class StdioStataRuntime:
    REQUIRED_TOOLS = frozenset(
        {
            "stata_executor_capabilities",
            "stata_session_open",
            "stata_run",
            "stata_session_status",
            "stata_session_close",
            "stata_task_status",
        }
    )

    def __init__(
        self,
        *,
        python_executable: Path,
        mcp_source_root: Path | None,
        working_directory: Path,
        stata_home: Path,
    ) -> None:
        self._python_executable = python_executable.resolve()
        self._mcp_source_root = mcp_source_root.resolve() if mcp_source_root is not None else None
        self._working_directory = working_directory.resolve()
        self._stata_home = stata_home.resolve()
        self._owner_task: asyncio.Task[None] | None = None
        self._request_queue: asyncio.Queue[_TransportRequest | None] | None = None
        self._lifecycle_lock = asyncio.Lock()
        self._session_working_directories: dict[str, Path] = {}

    async def __aenter__(self) -> StdioStataRuntime:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self._owner_task is not None and not self._owner_task.done():
                return
            if not self._python_executable.is_file():
                raise StataRuntimeContractError(
                    f"pinned MCP Python does not exist: {self._python_executable}"
                )
            if self._mcp_source_root is not None and not self._mcp_source_root.is_dir():
                raise StataRuntimeContractError(
                    f"pinned MCP source root does not exist: {self._mcp_source_root}"
                )
            self._working_directory.mkdir(parents=True, exist_ok=True)
            loop = asyncio.get_running_loop()
            queue: asyncio.Queue[_TransportRequest | None] = asyncio.Queue()
            ready: asyncio.Future[None] = loop.create_future()
            owner = asyncio.create_task(
                self._serve_transport(queue, ready),
                name=f"stata-mcp-transport:{self._working_directory.name}",
            )
            self._request_queue = queue
            self._owner_task = owner
            try:
                await ready
            except BaseException:
                try:
                    await owner
                finally:
                    self._owner_task = None
                    self._request_queue = None
                raise

    async def close(self) -> None:
        async with self._lifecycle_lock:
            owner = self._owner_task
            if owner is None:
                return
            if not owner.done():
                owner.cancel()
            try:
                await owner
            except asyncio.CancelledError:
                pass
            finally:
                self._owner_task = None
                self._request_queue = None
                self._session_working_directories.clear()

    async def _serve_transport(
        self,
        queue: asyncio.Queue[_TransportRequest | None],
        ready: asyncio.Future[None],
    ) -> None:
        """Own MCP async contexts for their entire lifetime in one asyncio Task."""

        failure: BaseException | None = None
        active: dict[asyncio.Task[None], _TransportRequest] = {}
        stack = AsyncExitStack()
        try:
            parameters = StdioServerParameters(
                command=str(self._python_executable),
                args=self._server_arguments(),
                env=self._server_environment(),
                cwd=self._working_directory,
            )
            streams = await stack.enter_async_context(stdio_client(parameters))
            session = await stack.enter_async_context(ClientSession(*streams))
            initialized = await session.initialize()
            if initialized.server_info.name != "stata-mcp":
                raise StataRuntimeContractError(
                    f"unexpected MCP server: {initialized.server_info.name!r}"
                )
            listed = await session.list_tools()
            available = {tool.name for tool in listed.tools}
            missing = sorted(self.REQUIRED_TOOLS - available)
            if missing:
                raise StataRuntimeContractError(
                    "stata-mcp is missing required tools: " + ", ".join(missing)
                )
            capabilities_result = await session.call_tool(
                "stata_executor_capabilities", {}, read_timeout_seconds=10
            )
            capabilities = capabilities_result.structured_content
            if capabilities_result.is_error or not isinstance(capabilities, Mapping):
                raise StataRuntimeContractError(
                    "stata-mcp returned no executor capability contract"
                )
            if capabilities.get("schema_version") != ("stata.executor-capabilities/v1alpha1"):
                raise StataRuntimeContractError("unsupported Stata executor capability schema")
            if capabilities.get("different_sessions_parallel") is not True:
                raise StataRuntimeContractError(
                    "Stata executor does not advertise cross-session parallelism"
                )
            if capabilities.get("same_session_serial") is not True:
                raise StataRuntimeContractError(
                    "Stata executor does not guarantee same-session serialization"
                )
            ready.set_result(None)

            async def run_request(request: _TransportRequest) -> None:
                if request.future.cancelled():
                    return
                try:
                    value = await request.operation(session)
                except asyncio.CancelledError:
                    raise
                except BaseException as error:
                    if not request.future.done():
                        request.future.set_exception(error)
                else:
                    if not request.future.done():
                        request.future.set_result(value)

            while True:
                request = await queue.get()
                if request is None:
                    break
                if request.future.cancelled():
                    continue
                task = asyncio.create_task(run_request(request))
                active[task] = request
                task.add_done_callback(active.pop)
        except BaseException as error:
            failure = error
            if not ready.done():
                ready.set_exception(error)
            raise
        finally:
            terminal = (
                StataRuntimeContractError("stdio Stata runtime transport was interrupted")
                if isinstance(failure, asyncio.CancelledError)
                else failure or StataRuntimeContractError("stdio Stata runtime transport closed")
            )
            # Stop all calls before closing MCP/stdio contexts.  The owner Task remains the
            # only task that enters and exits those contexts; child Tasks merely issue requests.
            active_snapshot = tuple(active.items())
            for task, request in active_snapshot:
                if not request.future.done():
                    request.future.set_exception(terminal)
                task.cancel()
            if active_snapshot:
                await asyncio.gather(
                    *(task for task, _ in active_snapshot),
                    return_exceptions=True,
                )
            while not queue.empty():
                pending = queue.get_nowait()
                if pending is not None and not pending.future.done():
                    pending.future.set_exception(terminal)
            await stack.aclose()

    def _server_environment(self) -> dict[str, str]:
        """Build an exact import environment for source or installed-package mode.

        Development tests may inject one reviewed source root.  A bundled release
        passes ``None`` and imports the locked wheel from its own Python runtime;
        inherited PYTHONPATH must not be able to substitute another adapter.
        """
        environment = {
            key: value for key, value in os.environ.items() if key.upper() != "PYTHONPATH"
        }
        environment["STATA_HOME"] = str(self._stata_home)
        environment["PYTHONNOUSERSITE"] = "1"
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        if self._mcp_source_root is not None:
            environment["PYTHONPATH"] = str(self._mcp_source_root)
        return environment

    def _server_arguments(self) -> list[str]:
        if self._mcp_source_root is None:
            return ["-I", "-B", "-m", "stata_mcp.server"]
        return ["-P", "-B", "-m", "stata_mcp.server"]

    async def _submit(self, operation: Callable[[ClientSession], Awaitable[Any]]) -> Any:
        await self.start()
        owner = self._owner_task
        queue = self._request_queue
        if owner is None or queue is None:
            raise StataRuntimeContractError("stdio Stata runtime has not been started")
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        await queue.put(_TransportRequest(operation, future))
        done, _ = await asyncio.wait((future, owner), return_when=asyncio.FIRST_COMPLETED)
        if future in done:
            return await future
        await owner
        raise StataRuntimeContractError("stdio Stata runtime transport stopped")

    async def configure_session_workspace(
        self, *, session_id: str, working_directory: Path
    ) -> None:
        """Bind one MCP-managed Stata session to a directory below the host root."""

        target = working_directory.resolve()
        try:
            target.relative_to(self._working_directory)
        except ValueError as error:
            raise StataRuntimeContractError(
                "Session working directory escapes the runtime host root"
            ) from error
        existing = self._session_working_directories.get(session_id)
        if existing is not None:
            if existing != target:
                raise StataRuntimeContractError(
                    "Stata session is already bound to another working directory"
                )
            return
        target.mkdir(parents=True, exist_ok=True)
        relative = target.relative_to(self._working_directory)
        relative_text = relative.as_posix() if relative.parts else "."

        async def call(session: ClientSession) -> Any:
            return await session.call_tool(
                "stata_session_open",
                {
                    "session_id": session_id,
                    "working_directory": relative_text,
                },
                read_timeout_seconds=40.0,
            )

        result = await self._submit(call)
        structured = result.structured_content
        if result.is_error or not isinstance(structured, Mapping):
            raise StataRuntimeContractError(
                "Failed to configure the Stata Execution Scope working directory"
            )
        if structured.get("schema_version") != "stata.session-open/v1alpha1":
            raise StataRuntimeContractError("unsupported Stata session-open schema")
        opened_path = Path(self._required_string(structured, "canonical_working_directory"))
        if opened_path.resolve() != target:
            raise StataRuntimeContractError("Stata session opened in a different working directory")
        self._session_working_directories[session_id] = target

    async def execute(
        self,
        *,
        session_id: str,
        code: str,
        timeout_seconds: float,
        operation_attempt_id: str | None = None,
        artifact_outputs: tuple[StataArtifactOutputRequest, ...] = (),
    ) -> StataRuntimeResult:
        return await self._execute_direct(
            session_id=session_id,
            code=code,
            timeout_seconds=timeout_seconds,
            operation_attempt_id=operation_attempt_id,
            artifact_outputs=artifact_outputs,
        )

    async def _execute_direct(
        self,
        *,
        session_id: str,
        code: str,
        timeout_seconds: float,
        operation_attempt_id: str | None = None,
        artifact_outputs: tuple[StataArtifactOutputRequest, ...] = (),
    ) -> StataRuntimeResult:
        execution_root = self._session_working_directories.get(session_id, self._working_directory)
        arguments: dict[str, Any] = {
            "session_id": session_id,
            "code": code,
            "timeout_seconds": timeout_seconds,
            # MCP stdio request handling is intentionally conservative/serial.  Background
            # submission lets the MCP supervisor return immediately, while its TaskRunner runs
            # different Stata sessions concurrently and status polling retrieves the same
            # execution receipt and Artifact contract.
            "background": True,
        }
        if operation_attempt_id is not None:
            arguments["operation_attempt_id"] = operation_attempt_id
        if artifact_outputs:
            for output in artifact_outputs:
                relative = output.relative_staging_path
                if operation_attempt_id is not None:
                    relative = f".stata-agent/staging/{operation_attempt_id}/{relative}"
                target = (execution_root / relative).resolve()
                try:
                    target.relative_to(execution_root)
                except ValueError as error:
                    raise StataRuntimeContractError(
                        "Artifact staging path escapes the runtime working directory"
                    ) from error
                if target.exists():
                    raise StataRuntimeContractError(
                        f"Artifact staging output already exists: {output.output_slot}"
                    )
                target.parent.mkdir(parents=True, exist_ok=True)
            arguments["artifact_outputs"] = [
                {
                    "output_slot": output.output_slot,
                    "relative_staging_path": (
                        Path(".stata-agent")
                        / "staging"
                        / operation_attempt_id
                        / output.relative_staging_path
                        if operation_attempt_id is not None
                        else Path(output.relative_staging_path)
                    ).as_posix(),
                    "artifact_kind": output.artifact_kind,
                    "media_type": output.media_type,
                    "required": output.required,
                }
                for output in artifact_outputs
            ]

        async def call(session: ClientSession) -> Any:
            return await session.call_tool(
                "stata_run",
                arguments,
                read_timeout_seconds=15.0,
            )

        submission = await self._submit(call)
        submission_meta = dict(submission.meta or {})
        job_id = submission_meta.get("job_id")
        if submission.is_error or not isinstance(job_id, str) or not job_id:
            raise StataRuntimeContractError("stata-mcp did not accept the background job")

        deadline = asyncio.get_running_loop().time() + timeout_seconds + 15.0
        while True:
            if asyncio.get_running_loop().time() >= deadline:
                raise StataRuntimeContractError(
                    "stata-mcp background job did not reach a terminal state"
                )
            await asyncio.sleep(0.05)

            async def status_call(session: ClientSession) -> Any:
                return await session.call_tool(
                    "stata_task_status",
                    {"job_id": job_id},
                    read_timeout_seconds=15.0,
                )

            status_result = await self._submit(status_call)
            status_meta = dict(status_result.meta or {})
            if status_meta.get("status") == "running":
                continue
            return self._parse_runtime_result(status_result, artifact_outputs)

    def _parse_runtime_result(
        self,
        result: Any,
        artifact_outputs: tuple[StataArtifactOutputRequest, ...],
    ) -> StataRuntimeResult:
        meta = dict(result.meta or {})
        if meta.get("envelope_schema_version") != "stata-mcp.envelope/v1":
            raise StataRuntimeContractError("unsupported or missing MCP envelope schema")
        receipt = self._parse_receipt(meta.get("execution_receipt"))
        text = "\n".join(
            str(getattr(item, "text", ""))
            for item in result.content
            if getattr(item, "type", None) == "text"
        )
        structured = result.structured_content
        if structured is not None and not isinstance(structured, Mapping):
            raise StataRuntimeContractError("MCP structured content must be an object")
        artifact_status = meta.get("artifact_output_contract_status")
        if (
            artifact_outputs
            and artifact_status != "complete"
            and receipt.execution_status is StataExecutionStatus.SUCCEEDED
        ):
            diagnostic = " ".join(text.strip().split())[:300]
            raise StataRuntimeContractError(
                "MCP did not complete the declared Artifact output contract "
                f"(status={artifact_status!r}, execution={receipt.execution_status.value}, "
                f"rc={receipt.rc}, output={diagnostic!r})"
            )
        raw_outputs = meta.get("artifact_outputs", [])
        if not isinstance(raw_outputs, list):
            raise StataRuntimeContractError("MCP Artifact outputs must be an array")
        logical_paths = {
            output.output_slot: output.relative_staging_path for output in artifact_outputs
        }
        parsed_outputs = tuple(
            self._parse_artifact_output(item, logical_paths=logical_paths) for item in raw_outputs
        )
        return StataRuntimeResult(
            envelope_schema_version="stata-mcp.envelope/v1",
            text=text,
            structured=dict(structured) if structured is not None else None,
            receipt=receipt,
            is_error=bool(result.is_error),
            artifacts=parsed_outputs,
        )

    async def close_session(self, *, session_id: str, reason: str) -> StataSessionCloseResult:
        async def call(session: ClientSession) -> Any:
            return await session.call_tool(
                "stata_session_close",
                {"session_id": session_id, "reason": reason},
                read_timeout_seconds=15,
            )

        result = await self._submit(call)
        structured = result.structured_content
        if result.is_error or not isinstance(structured, Mapping):
            raise StataRuntimeContractError("stata_session_close returned no control receipt")
        if structured.get("schema_version") != "stata.session-control/v1alpha1":
            raise StataRuntimeContractError("unsupported session control schema")
        detail = structured.get("detail")
        if not isinstance(detail, Mapping):
            raise StataRuntimeContractError("session close detail must be an object")
        closed = StataSessionCloseResult(
            schema_version="stata.session-control/v1alpha1",
            executor_instance_id=self._required_string(structured, "executor_instance_id"),
            session_id=self._required_string(structured, "session_id"),
            closed=bool(detail.get("closed")),
            detail=dict(detail),
        )
        self._session_working_directories.pop(session_id, None)
        return closed

    @classmethod
    def _parse_receipt(cls, raw: Any) -> StataExecutionReceipt:
        if not isinstance(raw, Mapping):
            raise StataRuntimeContractError("missing execution receipt")
        if raw.get("schema_version") != "stata.execution-receipt/v1alpha1":
            raise StataRuntimeContractError("unsupported execution receipt schema")
        try:
            status = StataExecutionStatus(str(raw["execution_status"]))
        except (KeyError, ValueError) as exc:
            raise StataRuntimeContractError("unknown execution status") from exc
        generation = raw.get("session_generation")
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
            raise StataRuntimeContractError("invalid session generation")
        rc = raw.get("rc")
        if isinstance(rc, bool) or not isinstance(rc, int):
            raise StataRuntimeContractError("invalid Stata rc")
        runtime_environment = raw.get("runtime_environment")
        supervision_proof = raw.get("supervision_proof")
        if not isinstance(runtime_environment, Mapping) or not isinstance(
            supervision_proof, Mapping
        ):
            raise StataRuntimeContractError("invalid runtime/supervision facts")
        return StataExecutionReceipt(
            schema_version="stata.execution-receipt/v1alpha1",
            executor_instance_id=cls._required_string(raw, "executor_instance_id"),
            session_id=cls._required_string(raw, "session_id"),
            session_generation=generation,
            exec_seq=raw.get("exec_seq") if isinstance(raw.get("exec_seq"), int) else None,
            execution_status=status,
            rc=rc,
            raw_output_status=cls._required_string(raw, "raw_output_status"),
            structured_result_status=cls._required_string(raw, "structured_result_status"),
            command_hash=(
                str(raw["command_hash"]) if raw.get("command_hash") is not None else None
            ),
            data_signature=(
                str(raw["data_signature"]) if raw.get("data_signature") is not None else None
            ),
            session_reset=bool(raw.get("session_reset")),
            runtime_environment=dict(runtime_environment),
            supervision_proof=dict(supervision_proof),
        )

    @staticmethod
    def _required_string(source: Mapping[str, Any], key: str) -> str:
        value = source.get(key)
        if not isinstance(value, str) or not value:
            raise StataRuntimeContractError(f"missing or invalid {key}")
        return value

    @classmethod
    def _parse_artifact_output(
        cls, raw: Any, *, logical_paths: Mapping[str, str]
    ) -> StataArtifactOutput:
        if not isinstance(raw, Mapping):
            raise StataRuntimeContractError("invalid MCP Artifact output")
        output_slot = cls._required_string(raw, "output_slot")
        try:
            relative_path = logical_paths[output_slot]
        except KeyError as error:
            raise StataRuntimeContractError("MCP returned an undeclared Artifact slot") from error
        return StataArtifactOutput(
            output_slot,
            cls._required_string(raw, "source_path"),
            relative_path,
            cls._required_string(raw, "artifact_kind"),
            cls._required_string(raw, "media_type"),
            cls._required_string(raw, "producer_locator"),
            bool(raw.get("expected", True)),
        )
