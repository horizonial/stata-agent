"""Fixed-code disposable Turn Worker process; stdin/stdout are IPC-only."""

from __future__ import annotations

import os
import sys
from uuid import uuid4

from .ipc_contract import (
    AgentIpcMessage,
    CompletionProposal,
    EvaluationProposal,
    LifecycleEvent,
    ModelInvocationProposal,
    PlanProposal,
    ResponseDelta,
    ToolProposal,
    WaitingProposal,
    WorkerAdvance,
    WorkerBootstrap,
    WorkerModelOutput,
    WorkerShutdown,
    encode_ndjson_line,
    parse_worker_input_line,
)

MAX_IPC_LINE_BYTES = 1024 * 1024
SENSITIVE_ENV_MARKERS = ("API_KEY", "TOKEN", "SECRET", "CREDENTIAL", "PASSWORD")


def _read_line() -> str | None:
    raw = sys.stdin.buffer.readline(MAX_IPC_LINE_BYTES + 1)
    if not raw:
        return None
    if len(raw) > MAX_IPC_LINE_BYTES or not raw.endswith(b"\n"):
        raise ValueError("worker IPC frame exceeds limit or is not newline terminated")
    return raw[:-1].decode("utf-8", errors="strict")


def _emit(event: LifecycleEvent) -> None:
    sys.stdout.buffer.write(encode_ndjson_line(event).encode("utf-8"))
    sys.stdout.buffer.flush()


def _emit_message(message: AgentIpcMessage) -> None:
    # All emitted proposal classes are part of AgentIpcMessage; keeping this helper local
    # prevents the Worker from importing any main-service authority module.
    sys.stdout.buffer.write(encode_ndjson_line(message).encode("utf-8"))
    sys.stdout.buffer.flush()


def _sensitive_environment_is_absent() -> bool:
    return not any(
        marker in name.upper() for name in os.environ for marker in SENSITIVE_ENV_MARKERS
    )


def main() -> int:
    bootstrap: WorkerBootstrap | None = None
    active_step_ordinal: int | None = None
    next_step_ordinal = 1
    awaiting_model_output = False
    remaining_step_budget = 0
    remaining_tool_budget = 0
    try:
        first = _read_line()
        if first is None:
            return 2
        parsed = parse_worker_input_line(first)
        if not isinstance(parsed, WorkerBootstrap):
            raise ValueError("first worker IPC message must be worker_bootstrap")
        bootstrap = parsed
        remaining_step_budget = bootstrap.context.remaining_step_budget
        remaining_tool_budget = bootstrap.context.remaining_tool_budget
        if not _sensitive_environment_is_absent():
            raise RuntimeError("sensitive environment capability reached Turn Worker")
        _emit(
            LifecycleEvent(
                protocol_version="2",
                message_type="lifecycle_event",
                message_id=f"ipc_{uuid4()}",
                worker_session_id=bootstrap.worker_session_id,
                turn_id=bootstrap.turn_id,
                context_revision=bootstrap.context_revision,
                event="worker_ready",
                safe_code="CAPABILITY_ISOLATED",
            )
        )
        while True:
            line = _read_line()
            if line is None:
                return 0
            incoming = parse_worker_input_line(line)
            if (
                incoming.worker_session_id != bootstrap.worker_session_id
                or incoming.turn_id != bootstrap.turn_id
                or incoming.context_revision != bootstrap.context_revision
            ):
                raise ValueError("worker input target does not match active bootstrap")
            if isinstance(incoming, WorkerAdvance):
                if awaiting_model_output:
                    raise ValueError("Worker cannot advance while a model output is pending")
                if incoming.step_ordinal != next_step_ordinal:
                    raise ValueError("Worker Step ordinal is not contiguous")
                if incoming.remaining_step_budget > remaining_step_budget:
                    raise ValueError("Worker Step budget cannot increase")
                if incoming.remaining_tool_budget > remaining_tool_budget:
                    raise ValueError("Worker Tool budget cannot increase")
                if incoming.step_ordinal == 1 and incoming.trigger != "initial":
                    raise ValueError("first Worker Step requires the initial trigger")
                if incoming.step_ordinal > 1 and incoming.trigger == "initial":
                    raise ValueError("only the first Worker Step may use the initial trigger")
                active_step_ordinal = incoming.step_ordinal
                remaining_step_budget = incoming.remaining_step_budget
                remaining_tool_budget = incoming.remaining_tool_budget
                awaiting_model_output = True
                _emit_message(
                    ModelInvocationProposal(
                        protocol_version="2",
                        message_type="model_invocation_proposal",
                        message_id=f"ipc_{uuid4()}",
                        turn_id=bootstrap.turn_id,
                        context_revision=bootstrap.context_revision,
                        step_ordinal=incoming.step_ordinal,
                        trigger=incoming.trigger,
                        remaining_step_budget=remaining_step_budget,
                        remaining_tool_budget=remaining_tool_budget,
                    )
                )
                continue
            if isinstance(incoming, WorkerModelOutput):
                if not awaiting_model_output or incoming.step_ordinal != active_step_ordinal:
                    raise ValueError("model output was not requested for the active Worker Step")
                if len(incoming.tool_calls) > remaining_tool_budget:
                    raise ValueError("model output exceeds the remaining Worker Tool budget")
                if incoming.text:
                    _emit_message(
                        ResponseDelta(
                            protocol_version="2",
                            message_type="response_delta",
                            message_id=f"ipc_{uuid4()}",
                            turn_id=bootstrap.turn_id,
                            context_revision=bootstrap.context_revision,
                            text=incoming.text,
                        )
                    )
                if incoming.plan is not None:
                    _emit_message(
                        PlanProposal(
                            protocol_version="2",
                            message_type="plan_proposal",
                            message_id=f"ipc_{uuid4()}",
                            turn_id=bootstrap.turn_id,
                            context_revision=bootstrap.context_revision,
                            summary=incoming.plan.summary,
                            structured_plan=incoming.plan.structured_plan,
                        )
                    )
                for call in incoming.tool_calls:
                    _emit_message(
                        ToolProposal(
                            protocol_version="2",
                            message_type="tool_proposal",
                            message_id=f"ipc_{uuid4()}",
                            turn_id=bootstrap.turn_id,
                            context_revision=bootstrap.context_revision,
                            call_ordinal=call.call_ordinal,
                            tool_name=call.tool_name,
                            arguments=call.arguments,
                        )
                    )
                if incoming.evaluation is not None:
                    _emit_message(
                        EvaluationProposal(
                            protocol_version="2",
                            message_type="evaluation_proposal",
                            message_id=f"ipc_{uuid4()}",
                            turn_id=bootstrap.turn_id,
                            context_revision=bootstrap.context_revision,
                            verdict=incoming.evaluation.verdict,
                            findings=incoming.evaluation.findings,
                        )
                    )
                if incoming.waiting is not None:
                    _emit_message(
                        WaitingProposal(
                            protocol_version="2",
                            message_type="waiting_proposal",
                            message_id=f"ipc_{uuid4()}",
                            turn_id=bootstrap.turn_id,
                            context_revision=bootstrap.context_revision,
                            reason=incoming.waiting.reason,
                            prompt=incoming.waiting.prompt,
                        )
                    )
                if incoming.completion is not None:
                    _emit_message(
                        CompletionProposal(
                            protocol_version="2",
                            message_type="completion_proposal",
                            message_id=f"ipc_{uuid4()}",
                            turn_id=bootstrap.turn_id,
                            context_revision=bootstrap.context_revision,
                            disposition=incoming.completion.disposition,
                            summary=incoming.completion.summary,
                        )
                    )
                _emit(
                    LifecycleEvent(
                        protocol_version="2",
                        message_type="lifecycle_event",
                        message_id=f"ipc_{uuid4()}",
                        worker_session_id=bootstrap.worker_session_id,
                        turn_id=bootstrap.turn_id,
                        context_revision=bootstrap.context_revision,
                        event="step_output_processed",
                        safe_code="OUTPUT_VALIDATED",
                    )
                )
                remaining_step_budget -= 1
                next_step_ordinal += 1
                active_step_ordinal = None
                awaiting_model_output = False
                continue
            if not isinstance(incoming, WorkerShutdown):
                raise ValueError("unsupported worker control message")
            _emit(
                LifecycleEvent(
                    protocol_version="2",
                    message_type="lifecycle_event",
                    message_id=f"ipc_{uuid4()}",
                    worker_session_id=bootstrap.worker_session_id,
                    turn_id=bootstrap.turn_id,
                    context_revision=bootstrap.context_revision,
                    event="worker_stopping",
                    safe_code="REQUESTED_SHUTDOWN",
                )
            )
            return 0
    except Exception as error:
        if bootstrap is not None:
            _emit(
                LifecycleEvent(
                    protocol_version="2",
                    message_type="lifecycle_event",
                    message_id=f"ipc_{uuid4()}",
                    worker_session_id=bootstrap.worker_session_id,
                    turn_id=bootstrap.turn_id,
                    context_revision=bootstrap.context_revision,
                    event="worker_failed",
                    safe_code=type(error).__name__.upper(),
                )
            )
        print(f"Turn Worker failed: {type(error).__name__}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
