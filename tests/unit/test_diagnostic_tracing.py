"""Non-authoritative spans correlate runtime boundaries without changing control flow."""

import asyncio
from pathlib import Path

import pytest

from stata_research_agent.application.diagnostic_service import DiagnosticService
from stata_research_agent.application.diagnostic_tracing import DiagnosticTracer
from stata_research_agent.application.diagnostics import default_diagnostic_registry
from stata_research_agent.application.sensitive_output import SensitiveOutputGate
from stata_research_agent.interfaces.filesystem_diagnostics import FilesystemDiagnosticSink


def tracer_at(root: Path) -> tuple[DiagnosticTracer, FilesystemDiagnosticSink]:
    sink = FilesystemDiagnosticSink(root)
    service = DiagnosticService(
        sink,
        default_diagnostic_registry(),
        SensitiveOutputGate(),
        release_id="test",
        build_id="test",
        instance_id="instance_test",
    )
    return DiagnosticTracer(service), sink


def test_nested_spans_share_trace_and_preserve_parent_domain_links(tmp_path: Path) -> None:
    tracer, sink = tracer_at(tmp_path / "diagnostics")

    with tracer.span(
        "agent.turn",
        component="turn_runner",
        workspace_ref="ws_test",
        turn_id="turn_test",
        domain_ref_type="turn",
        domain_ref_id="turn_test",
    ) as root:
        with tracer.span(
            "tool.execute",
            kind="tool",
            component="stata.run",
            operation_id="operation_test",
            domain_ref_type="operation",
            domain_ref_id="operation_test",
        ) as child:
            child.set_status("succeeded")

    events = sink.read_through(sink.snapshot_end())
    assert [event.safe_attributes["span_name"] for event in events] == [
        "tool.execute",
        "agent.turn",
    ]
    child_event, root_event = events
    assert child_event.trace_id == root_event.trace_id == root.context.trace_id
    assert child_event.parent_span_id == root_event.span_id
    assert root_event.parent_span_id is None
    assert child_event.workspace_ref == "ws_test"
    assert child_event.turn_id == "turn_test"
    assert child_event.operation_id == "operation_test"
    assert child_event.safe_attributes["status_code"] == "succeeded"
    assert child_event.duration_ms is not None and child_event.duration_ms >= 0


def test_span_records_safe_error_type_and_never_swallows_application_error(
    tmp_path: Path,
) -> None:
    tracer, sink = tracer_at(tmp_path / "diagnostics")

    with pytest.raises(LookupError, match="application failure"):
        with tracer.span(
            "worker.advance",
            kind="worker",
            component="turn_worker",
            domain_ref_type="turn",
            domain_ref_id="turn_test",
        ):
            raise LookupError("application failure")

    (event,) = sink.read_through(sink.snapshot_end())
    assert event.safe_code == "SPAN_FAILED"
    assert event.safe_attributes["status_code"] == "error"
    assert event.safe_attributes["error_type"] == "LookupError"
    assert "application failure" not in str(event.to_payload())


def test_parallel_workspace_tasks_keep_independent_trace_contexts(tmp_path: Path) -> None:
    tracer, sink = tracer_at(tmp_path / "diagnostics")

    async def run(workspace_ref: str, turn_id: str) -> None:
        async with tracer.span(
            "agent.turn",
            component="turn_runner",
            workspace_ref=workspace_ref,
            turn_id=turn_id,
            domain_ref_type="turn",
            domain_ref_id=turn_id,
        ):
            await asyncio.sleep(0)
            async with tracer.span(
                "worker.advance",
                kind="worker",
                component="turn_worker",
                domain_ref_type="turn",
                domain_ref_id=turn_id,
            ):
                await asyncio.sleep(0)

    async def scenario() -> None:
        await asyncio.gather(run("ws_a", "turn_a"), run("ws_b", "turn_b"))

    asyncio.run(scenario())

    events = sink.read_through(sink.snapshot_end())
    traces: dict[str, list[tuple[str | None, str | None]]] = {}
    for event in events:
        assert event.trace_id is not None
        traces.setdefault(event.trace_id, []).append((event.workspace_ref, event.turn_id))
    assert len(traces) == 2
    assert {event.workspace_ref for event in events} == {"ws_a", "ws_b"}
    for trace_events in traces.values():
        assert len(trace_events) == 2
        assert len({workspace_ref for workspace_ref, _turn_id in trace_events}) == 1
        assert len({turn_id for _workspace_ref, turn_id in trace_events}) == 1
