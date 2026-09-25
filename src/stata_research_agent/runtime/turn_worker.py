"""Main-service supervisor for one disposable stdio Turn Worker."""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from typing import Literal
from uuid import uuid4

from stata_research_agent.application.diagnostic_tracing import DiagnosticTracer

from .ipc_contract import (
    AgentIpcMessage,
    LifecycleEvent,
    ModelInvocationProposal,
    WorkerAdvance,
    WorkerBootstrap,
    WorkerBootstrapContext,
    WorkerCompletion,
    WorkerEvaluation,
    WorkerModelOutput,
    WorkerPlan,
    WorkerShutdown,
    WorkerToolCall,
    WorkerWaiting,
    encode_worker_input_line,
    parse_ndjson_line,
)

MAX_IPC_LINE_BYTES = 1024 * 1024


class TurnWorkerProtocolError(RuntimeError):
    pass


class TurnWorkerProcess:
    def __init__(
        self,
        python_executable: Path,
        *,
        tracer: DiagnosticTracer | None = None,
    ) -> None:
        self._python_executable = python_executable.resolve()
        self._tracer = tracer
        self._process: asyncio.subprocess.Process | None = None
        self._temporary: tempfile.TemporaryDirectory[str] | None = None
        self._bootstrap: WorkerBootstrap | None = None

    @property
    def pid(self) -> int | None:
        return None if self._process is None else self._process.pid

    @property
    def returncode(self) -> int | None:
        return None if self._process is None else self._process.returncode

    @property
    def worker_session_id(self) -> str | None:
        return None if self._bootstrap is None else self._bootstrap.worker_session_id

    async def start(
        self,
        *,
        turn_id: str,
        context_revision: int,
        context: WorkerBootstrapContext,
        timeout_seconds: float = 10,
    ) -> LifecycleEvent:
        if self._tracer is None:
            return await self._start(
                turn_id=turn_id,
                context_revision=context_revision,
                context=context,
                timeout_seconds=timeout_seconds,
            )
        async with self._tracer.span(
            "worker.start",
            kind="worker",
            component="turn_worker",
            turn_id=turn_id,
            domain_ref_type="turn",
            domain_ref_id=turn_id,
        ) as span:
            event = await self._start(
                turn_id=turn_id,
                context_revision=context_revision,
                context=context,
                timeout_seconds=timeout_seconds,
            )
            if self.worker_session_id is not None:
                span.set_domain_ref("worker_session", self.worker_session_id)
            return event

    async def _start(
        self,
        *,
        turn_id: str,
        context_revision: int,
        context: WorkerBootstrapContext,
        timeout_seconds: float,
    ) -> LifecycleEvent:
        if self._process is not None:
            raise RuntimeError("Turn Worker process has already been started")
        self._temporary = tempfile.TemporaryDirectory(
            prefix="stata-agent-worker-", ignore_cleanup_errors=True
        )
        worker_session_id = f"worker_{uuid4()}"
        self._bootstrap = WorkerBootstrap(
            protocol_version="2",
            message_type="worker_bootstrap",
            message_id=f"ipc_{uuid4()}",
            worker_session_id=worker_session_id,
            turn_id=turn_id,
            context_revision=context_revision,
            context=context,
        )
        self._process = await asyncio.create_subprocess_exec(
            str(self._python_executable),
            *self._worker_arguments(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._temporary.name,
            env=self._isolated_environment(Path(self._temporary.name)),
            creationflags=(0x08000000 if os.name == "nt" else 0),
        )
        try:
            await self._send(self._bootstrap)
            event = await self.receive(timeout_seconds=timeout_seconds)
        except BaseException:
            await self.terminate()
            raise
        if not isinstance(event, LifecycleEvent) or event.event != "worker_ready":
            await self.terminate()
            raise TurnWorkerProtocolError("Turn Worker did not produce worker_ready")
        if (
            event.worker_session_id != worker_session_id
            or event.turn_id != turn_id
            or event.context_revision != context_revision
        ):
            await self.terminate()
            raise TurnWorkerProtocolError("Turn Worker ready event mismatched bootstrap identity")
        return event

    async def stop(self, *, timeout_seconds: float = 10) -> LifecycleEvent:
        if self._tracer is None:
            return await self._stop(timeout_seconds=timeout_seconds)
        async with self._tracer.span(
            "worker.stop",
            kind="worker",
            component="turn_worker",
            turn_id=None if self._bootstrap is None else self._bootstrap.turn_id,
            domain_ref_type="worker_session",
            domain_ref_id=self.worker_session_id,
        ):
            return await self._stop(timeout_seconds=timeout_seconds)

    async def _stop(self, *, timeout_seconds: float) -> LifecycleEvent:
        bootstrap = self._require_bootstrap()
        await self._send(
            WorkerShutdown(
                protocol_version="2",
                message_type="worker_shutdown",
                message_id=f"ipc_{uuid4()}",
                worker_session_id=bootstrap.worker_session_id,
                turn_id=bootstrap.turn_id,
                context_revision=bootstrap.context_revision,
            )
        )
        event = await self.receive(timeout_seconds=timeout_seconds)
        if not isinstance(event, LifecycleEvent) or event.event != "worker_stopping":
            raise TurnWorkerProtocolError("Turn Worker did not acknowledge shutdown")
        process = self._require_process()
        await asyncio.wait_for(process.wait(), timeout=timeout_seconds)
        await self._close_pipes()
        self._cleanup_temporary()
        return event

    async def process_model_output(
        self,
        *,
        step_ordinal: int,
        text: str,
        plan: WorkerPlan | None = None,
        tool_calls: tuple[WorkerToolCall, ...] = (),
        evaluation: WorkerEvaluation | None = None,
        waiting: WorkerWaiting | None = None,
        completion: WorkerCompletion | None = None,
        timeout_seconds: float = 10,
    ) -> tuple[AgentIpcMessage, ...]:
        if self._tracer is None:
            return await self._process_model_output(
                step_ordinal=step_ordinal,
                text=text,
                plan=plan,
                tool_calls=tool_calls,
                evaluation=evaluation,
                waiting=waiting,
                completion=completion,
                timeout_seconds=timeout_seconds,
            )
        async with self._tracer.span(
            "worker.process_model_output",
            kind="worker",
            component="turn_worker",
            turn_id=None if self._bootstrap is None else self._bootstrap.turn_id,
            domain_ref_type="step_ordinal",
            domain_ref_id=str(step_ordinal),
        ):
            return await self._process_model_output(
                step_ordinal=step_ordinal,
                text=text,
                plan=plan,
                tool_calls=tool_calls,
                evaluation=evaluation,
                waiting=waiting,
                completion=completion,
                timeout_seconds=timeout_seconds,
            )

    async def _process_model_output(
        self,
        *,
        step_ordinal: int,
        text: str,
        plan: WorkerPlan | None,
        tool_calls: tuple[WorkerToolCall, ...],
        evaluation: WorkerEvaluation | None,
        waiting: WorkerWaiting | None,
        completion: WorkerCompletion | None,
        timeout_seconds: float,
    ) -> tuple[AgentIpcMessage, ...]:
        bootstrap = self._require_bootstrap()
        await self._send(
            WorkerModelOutput(
                protocol_version="2",
                message_type="worker_model_output",
                message_id=f"ipc_{uuid4()}",
                worker_session_id=bootstrap.worker_session_id,
                turn_id=bootstrap.turn_id,
                context_revision=bootstrap.context_revision,
                step_ordinal=step_ordinal,
                text=text,
                plan=plan,
                tool_calls=tool_calls,
                evaluation=evaluation,
                waiting=waiting,
                completion=completion,
            )
        )
        messages: list[AgentIpcMessage] = []
        while True:
            message = await self.receive(timeout_seconds=timeout_seconds)
            if isinstance(message, LifecycleEvent):
                if message.event != "step_output_processed":
                    raise TurnWorkerProtocolError(
                        f"unexpected Worker lifecycle event: {message.event}"
                    )
                return tuple(messages)
            messages.append(message)

    async def request_model_invocation(
        self,
        *,
        step_ordinal: int,
        trigger: Literal[
            "initial", "tool_results_committed", "stop_guard_continue", "driver_feedback"
        ],
        remaining_step_budget: int,
        remaining_tool_budget: int,
        timeout_seconds: float = 10,
    ) -> ModelInvocationProposal:
        if self._tracer is None:
            return await self._request_model_invocation(
                step_ordinal=step_ordinal,
                trigger=trigger,
                remaining_step_budget=remaining_step_budget,
                remaining_tool_budget=remaining_tool_budget,
                timeout_seconds=timeout_seconds,
            )
        async with self._tracer.span(
            "worker.request_model_invocation",
            kind="worker",
            component="turn_worker",
            turn_id=None if self._bootstrap is None else self._bootstrap.turn_id,
            domain_ref_type="step_ordinal",
            domain_ref_id=str(step_ordinal),
        ):
            return await self._request_model_invocation(
                step_ordinal=step_ordinal,
                trigger=trigger,
                remaining_step_budget=remaining_step_budget,
                remaining_tool_budget=remaining_tool_budget,
                timeout_seconds=timeout_seconds,
            )

    async def _request_model_invocation(
        self,
        *,
        step_ordinal: int,
        trigger: Literal[
            "initial", "tool_results_committed", "stop_guard_continue", "driver_feedback"
        ],
        remaining_step_budget: int,
        remaining_tool_budget: int,
        timeout_seconds: float,
    ) -> ModelInvocationProposal:
        bootstrap = self._require_bootstrap()
        await self._send(
            WorkerAdvance(
                protocol_version="2",
                message_type="worker_advance",
                message_id=f"ipc_{uuid4()}",
                worker_session_id=bootstrap.worker_session_id,
                turn_id=bootstrap.turn_id,
                context_revision=bootstrap.context_revision,
                step_ordinal=step_ordinal,
                trigger=trigger,
                remaining_step_budget=remaining_step_budget,
                remaining_tool_budget=remaining_tool_budget,
            )
        )
        proposal = await self.receive(timeout_seconds=timeout_seconds)
        if not isinstance(proposal, ModelInvocationProposal):
            raise TurnWorkerProtocolError("Turn Worker did not request a model invocation")
        if proposal.step_ordinal != step_ordinal:
            raise TurnWorkerProtocolError("Turn Worker requested an unexpected Step ordinal")
        return proposal

    async def receive(self, *, timeout_seconds: float = 10) -> AgentIpcMessage:
        process = self._require_process()
        if process.stdout is None:
            raise TurnWorkerProtocolError("Turn Worker stdout is unavailable")
        raw = await asyncio.wait_for(process.stdout.readline(), timeout=timeout_seconds)
        if not raw:
            detail = await self._stderr_tail()
            raise TurnWorkerProtocolError(f"Turn Worker exited without an IPC message: {detail}")
        if len(raw) > MAX_IPC_LINE_BYTES or not raw.endswith(b"\n"):
            raise TurnWorkerProtocolError("Turn Worker emitted an invalid IPC frame")
        return parse_ndjson_line(raw[:-1].decode("utf-8", errors="strict"))

    async def terminate(self, *, timeout_seconds: float = 5) -> None:
        process = self._process
        if process is None:
            self._cleanup_temporary()
            return
        if process.returncode is None:
            process.kill()
            await asyncio.wait_for(process.wait(), timeout=timeout_seconds)
        await self._close_pipes()
        self._cleanup_temporary()

    async def _send(
        self, message: WorkerBootstrap | WorkerAdvance | WorkerModelOutput | WorkerShutdown
    ) -> None:
        process = self._require_process()
        if process.stdin is None or process.returncode is not None:
            raise TurnWorkerProtocolError("Turn Worker stdin is unavailable")
        process.stdin.write(encode_worker_input_line(message).encode("utf-8"))
        await process.stdin.drain()

    async def _stderr_tail(self) -> str:
        process = self._require_process()
        if process.stderr is None:
            return "stderr unavailable"
        raw = await process.stderr.read(4096)
        return raw.decode("utf-8", errors="replace")[-2000:]

    async def _close_pipes(self) -> None:
        process = self._require_process()
        if process.stdin is not None and not process.stdin.is_closing():
            process.stdin.close()
            await process.stdin.wait_closed()
        if process.stdout is not None:
            await process.stdout.read()
        if process.stderr is not None:
            await process.stderr.read()

    def _require_process(self) -> asyncio.subprocess.Process:
        if self._process is None:
            raise RuntimeError("Turn Worker process has not been started")
        return self._process

    def _require_bootstrap(self) -> WorkerBootstrap:
        if self._bootstrap is None:
            raise RuntimeError("Turn Worker bootstrap is unavailable")
        return self._bootstrap

    def _cleanup_temporary(self) -> None:
        if self._temporary is not None:
            self._temporary.cleanup()
            self._temporary = None

    @staticmethod
    def _isolated_environment(temporary: Path) -> dict[str, str]:
        allowed = {
            key: value
            for key, value in os.environ.items()
            if key.upper() in {"SYSTEMROOT", "WINDIR"}
        }
        allowed.update(
            {
                "TEMP": str(temporary),
                "TMP": str(temporary),
                "PYTHONNOUSERSITE": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONUTF8": "1",
            }
        )
        return allowed

    @staticmethod
    def _worker_arguments() -> tuple[str, ...]:
        return ("-I", "-B", "-m", "stata_research_agent.runtime.worker_process")
