"""Offline contract tests for the opt-in real-Stata acceptance doctor."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from stata_agent import stata_doctor as doctor
from stata_agent.events.schema import (
    ACTOR_ORCH,
    EVENT_RUN_REQ,
    EVENT_RUN_SUCCEEDED,
    EVENT_TOOL_CALL,
    EVENT_TOOL_RESULT,
    Event,
)
from stata_agent.tools.executor import auto_regress_script, sha


@dataclass
class _Result:
    text: str = ""
    is_error: bool = False
    rc: int | None = None


class _Client:
    def __init__(self, tools: list[str] | None = None) -> None:
        self.tools = tools or ["stata_run", "stata_get_results"]

    def list_tools(self) -> list[str]:
        return list(self.tools)


class _Session:
    def __init__(self, *, fail_after: int | None = None) -> None:
        self.calls: list[str] = []
        self.value = 0
        self.closed = False
        self.fail_after = fail_after

    def call(self, code: str) -> _Result:
        self.calls.append(code)
        if self.fail_after is not None and len(self.calls) > self.fail_after:
            raise RuntimeError("synthetic session failure")
        if "DOCTOR_ENGINE" in code:
            return _Result(doctor._ENGINE_MARKER)
        if code.startswith("scalar __stata_agent_doctor = 0"):
            self.value = 0
            return _Result()
        if code.startswith("scalar __stata_agent_doctor = __stata_agent_doctor + 1"):
            self.value += 1
            return _Result()
        if code.startswith("di") and doctor._SESSION_MARKER in code:
            return _Result(f"{doctor._SESSION_MARKER}{self.value}")
        return _Result()

    def close(self) -> None:
        self.closed = True


class _Store:
    def __init__(self, events: list[Event]) -> None:
        self.events = events
        self.closed = False

    def scan(self, idea: str):
        del idea
        return iter(self.events)

    def close(self) -> None:
        self.closed = True


class _Executor:
    def __init__(self, store: _Store, root: Path) -> None:
        self.store = store
        self.root = root
        self.closed = False

    def execute(self, script: str, *, idea: str) -> dict:
        run_id = "doctor-run"
        operation_id = "doctor-op"
        do_file = self.root / "runs" / f"{run_id}.do"
        do_file.parent.mkdir(parents=True, exist_ok=True)
        do_file.write_text(script, encoding="utf-8")
        self.store.events.extend(
            [
                Event(
                    idea_id=idea,
                    event_type=EVENT_RUN_REQ,
                    actor=ACTOR_ORCH,
                    source=ACTOR_ORCH,
                    operation_id=operation_id,
                    payload={"run_id": run_id},
                ),
                Event(
                    idea_id=idea,
                    event_type=EVENT_TOOL_CALL,
                    actor=ACTOR_ORCH,
                    source=ACTOR_ORCH,
                    operation_id=operation_id,
                    payload={"run_id": run_id, "call_id": "call-1"},
                ),
                Event(
                    idea_id=idea,
                    event_type=EVENT_TOOL_RESULT,
                    actor=ACTOR_ORCH,
                    source=ACTOR_ORCH,
                    operation_id=operation_id,
                    payload={"run_id": run_id, "call_id": "call-1", "rc": 0},
                ),
                Event(
                    idea_id=idea,
                    event_type=EVENT_RUN_SUCCEEDED,
                    actor=ACTOR_ORCH,
                    source=ACTOR_ORCH,
                    operation_id=operation_id,
                    payload={
                        "run_id": run_id,
                        "machine": {"N": 74, "coef": -1.0, "r2": 0.2},
                        "provenance": {
                            "kind": "real",
                            "executor": "stata-mcp",
                            "attested": True,
                            "do_file": str(do_file),
                            "command_hash": sha(script),
                            "env_sig": {
                                "stata_version": "18.0",
                                "stata_flavor": "MP",
                            },
                        },
                    },
                ),
            ]
        )
        return {
            "run_id": run_id,
            "machine": {"N": 74, "coef": -1.0, "r2": 0.2},
            "env": {"stata_version": "18.0", "stata_flavor": "MP"},
            "do_file": str(do_file),
            "command_hash": sha(script),
        }

    def close(self) -> None:
        self.closed = True


def _fake_acceptance(
    *,
    session: _Session | None = None,
    client: _Client | None = None,
):
    active_session = session or _Session()
    active_client = client or _Client()
    stores: list[_Store] = []
    executors: list[_Executor] = []

    def store_factory(_root: Path) -> _Store:
        store = _Store([])
        stores.append(store)
        return store

    def executor_factory(store: _Store, root: Path) -> _Executor:
        executor = _Executor(store, root)
        executors.append(executor)
        return executor

    report = doctor.run_acceptance(
        iterations=3,
        client_factory=lambda: active_client,
        session_factory=lambda: active_session,
        store_factory=store_factory,
        executor_factory=executor_factory,
    )
    return report, active_session, stores, executors


def test_report_schema_and_full_fake_pipeline() -> None:
    report, session, stores, executors = _fake_acceptance()

    assert report.ok is True
    data = report.as_dict()
    assert data["schema_version"] == 1
    assert data["summary"] == {"total": 5, "passed": 5, "failed": 0, "not_run": 0}
    assert [check["name"] for check in data["checks"]] == list(doctor.CHECK_ORDER)
    json.dumps(data, ensure_ascii=False, sort_keys=True)
    assert session.closed is True
    assert stores[0].closed is True
    assert executors[0].closed is True


def test_failed_tool_discovery_short_circuits_without_session() -> None:
    session_created = False

    def session_factory() -> _Session:
        nonlocal session_created
        session_created = True
        return _Session()

    report = doctor.run_acceptance(
        client_factory=lambda: _Client(["other_tool"]),
        session_factory=session_factory,
        iterations=1,
    )

    assert report.ok is False
    assert session_created is False
    data = report.as_dict()
    assert data["checks"][0]["status"] == "failed"
    assert data["checks"][0]["code"] == "tool_missing"
    assert all(check["status"] == "not_run" for check in data["checks"][1:])


def test_engine_failure_short_circuits_and_closes_session() -> None:
    session = _Session(fail_after=0)
    report, active, stores, executors = _fake_acceptance(session=session)

    assert report.ok is False
    assert active.closed is True
    assert stores == []
    assert executors == []
    data = report.as_dict()
    assert data["checks"][1]["status"] == "failed"
    assert all(check["status"] == "not_run" for check in data["checks"][2:])


def test_sanitize_detail_removes_paths_and_secrets() -> None:
    raw = (
        r"serial number: 3851871377 C:\Users\user\stata-mcp "
        "api_key=sk-test-secret-123 password: hunter2"
    )
    safe = doctor.sanitize_detail(raw)

    assert "3851871377" not in safe
    assert r"C:\Users\user" not in safe
    assert "sk-test-secret-123" not in safe
    assert "hunter2" not in safe
    assert "[redacted]" in safe
    assert "[path]" in safe


def test_report_sanitizes_metric_values() -> None:
    checks = tuple(
        doctor.CheckResult(
            name,
            "passed",
            metrics={"diagnostic": r"C:\Users\user\private\run.do", "nested": ["token=secret"]},
        )
        for name in doctor.CHECK_ORDER
    )
    encoded = json.dumps(doctor.AcceptanceReport(checks).as_dict(), ensure_ascii=False)
    assert "C:\\Users\\user" not in encoded
    assert "secret" not in encoded


def test_report_rejects_wrong_order_and_passed_failure_code() -> None:
    with pytest.raises(ValueError):
        doctor.AcceptanceReport(
            tuple(doctor.CheckResult(name, "passed") for name in reversed(doctor.CHECK_ORDER))
        )
    with pytest.raises(ValueError):
        doctor.CheckResult("engine", "passed", code="unexpected")


def test_cli_invalid_iterations_returns_usage_code(capsys) -> None:
    assert doctor.main(["--json", "--iterations", "0"]) == doctor.EXIT_USAGE
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "configuration_invalid"


def test_cli_invalid_argument_json_is_machine_readable(capsys) -> None:
    assert doctor.main(["--json", "--iterations", "not-an-int"]) == doctor.EXIT_USAGE
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "configuration_invalid"


def test_cli_prints_stable_json_for_success(monkeypatch, capsys) -> None:
    report = doctor.AcceptanceReport(
        tuple(doctor.CheckResult(name, "passed") for name in doctor.CHECK_ORDER)
    )
    monkeypatch.setattr(doctor, "run_acceptance", lambda *, iterations: report)

    assert doctor.main(["--json", "--iterations", "1"]) == doctor.EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["summary"]["passed"] == 5


def test_cli_prints_sanitized_human_failure_and_exit_one(monkeypatch, capsys) -> None:
    checks = (
        doctor.CheckResult(
            "mcp_tools",
            "failed",
            code="tool_missing",
            detail=r"serial number: 123 C:\Users\user\stata-mcp",
        ),
        *(doctor.CheckResult(name, "not_run", code="blocked", detail="blocked")
          for name in doctor.CHECK_ORDER[1:]),
    )
    report = doctor.AcceptanceReport(checks)
    monkeypatch.setattr(doctor, "run_acceptance", lambda *, iterations: report)

    assert doctor.main(["--iterations", "1"]) == doctor.EXIT_CHECK_FAILED
    output = capsys.readouterr().out
    assert "FAILED mcp_tools (tool_missing)" in output
    assert "123" not in output
    assert r"C:\Users\user" not in output


def test_failure_codes_are_stable_for_common_runtime_errors() -> None:
    assert doctor._failure_code(ModuleNotFoundError("No module named 'mcp'")) == "mcp_unavailable"
    assert doctor._failure_code(TimeoutError("timed out")) == "transport_timeout"
    assert doctor._failure_code(RuntimeError("license unavailable")) == "engine_unavailable"
    assert doctor._failure_code(doctor.DoctorProtocolError("bad marker")) == "protocol_invalid"


def test_executor_failure_closes_store_and_executor() -> None:
    active_session = _Session()
    stores: list[_Store] = []
    executors: list[_Executor] = []

    def store_factory(_root: Path) -> _Store:
        store = _Store([])
        stores.append(store)
        return store

    class FailingExecutor(_Executor):
        def execute(self, script: str, *, idea: str) -> dict:
            del script, idea
            raise RuntimeError("synthetic executor failure")

    def executor_factory(store: _Store, root: Path) -> _Executor:
        executor = FailingExecutor(store, root)
        executors.append(executor)
        return executor

    report = doctor.run_acceptance(
        iterations=1,
        client_factory=_Client,
        session_factory=lambda: active_session,
        store_factory=store_factory,
        executor_factory=executor_factory,
    )
    assert report.ok is False
    assert report.checks[3].status == "failed"
    assert report.checks[4].status == "not_run"
    assert stores[0].closed is True
    assert executors[0].closed is True


def test_ledger_rejects_missing_result_and_provenance() -> None:
    report, _session, stores, executors = _fake_acceptance()
    assert report.ok is True
    events = stores[0].events
    events[:] = [event for event in events if event.event_type != EVENT_TOOL_RESULT]
    # Re-run the verifier with a deliberately incomplete chain; the public
    # acceptance path must fail the ledger stage rather than trust the result.
    run_output = {
        "run_id": "doctor-run",
        "do_file": str(executors[0].root / "runs" / "doctor-run.do"),
        "command_hash": sha(auto_regress_script()),
    }
    with pytest.raises(doctor.DoctorCheckError):
        doctor._verify_ledger(stores[0], "__stata_agent_doctor__", run_output)


def test_ledger_rejects_unattested_terminal() -> None:
    report, _session, stores, executors = _fake_acceptance()
    assert report.ok is True
    terminal = stores[0].events[-1]
    terminal.payload["provenance"]["attested"] = False
    output = {
        "run_id": "doctor-run",
        "do_file": str(executors[0].root / "runs" / "doctor-run.do"),
        "command_hash": sha(auto_regress_script()),
    }
    with pytest.raises(doctor.DoctorCheckError, match="attested"):
        doctor._verify_ledger(stores[0], "__stata_agent_doctor__", output)
