"""Opt-in, fail-closed acceptance checks for the real Stata runtime.

The doctor is a release/operator adapter.  It deliberately does not participate
in the agent loop or in the UI request path.  All checks use the existing
`stata-mcp` client/session and `StataExecutor` contracts, while the report
contains only stable, non-sensitive fields.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import os
import re
import sys
from collections.abc import Callable, Iterable, Mapping
from contextlib import redirect_stderr
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

SCHEMA_VERSION = 1
EXIT_OK = 0
EXIT_CHECK_FAILED = 1
EXIT_USAGE = 2

CHECK_ORDER = (
    "mcp_tools",
    "engine",
    "persistent_session",
    "executor",
    "ledger",
)
CHECK_STATUSES = frozenset({"passed", "failed", "not_run"})

_MAX_ITERATIONS = 200
_ENGINE_MARKER = "STATA_AGENT_DOCTOR_ENGINE=ok"
_SESSION_MARKER = "STATA_AGENT_DOCTOR_SESSION="
_SECRET_RE = re.compile(
    r"(?ix)"
    r"(?:api[_-]?key|access[_-]?token|bearer|password|secret|token|"
    r"serial(?:\s+number)?|license(?:\s+key)?)"
    r"\s*[:=]?\s*[^\s,;]+"
)
_WINDOWS_PATH_RE = re.compile(r"(?i)(?:[a-z]:\\|\\\\)[^\r\n\t,;]+")
_POSIX_PATH_RE = re.compile(r"(?<![\w])/(?:[^/\r\n\t,;]+/)+[^\r\n\t,;]*")


class DoctorError(RuntimeError):
    """Base error for a failed, but otherwise valid, runtime check."""

    code = "runtime_unavailable"

    def __init__(self, detail: str = "") -> None:
        self.detail = str(detail or self.code)
        super().__init__(self.detail)


class DoctorProtocolError(DoctorError):
    code = "protocol_invalid"


class DoctorToolMissing(DoctorError):
    code = "tool_missing"


class DoctorConfigurationError(DoctorError):
    code = "configuration_invalid"


class DoctorCheckError(DoctorError):
    """An invariant failed after the external capability responded."""

    code = "check_failed"


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str
    code: str | None = None
    detail: str | None = None
    metrics: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.name not in CHECK_ORDER:
            raise ValueError(f"unknown doctor check: {self.name}")
        if self.status not in CHECK_STATUSES:
            raise ValueError(f"invalid doctor check status: {self.status}")
        if self.status == "passed" and self.code is not None:
            raise ValueError("passed check cannot carry a failure code")
        if self.status != "passed" and not self.code:
            raise ValueError("failed/not_run check requires a stable failure code")

    def as_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "name": self.name,
            "status": self.status,
        }
        if self.code is not None:
            data["code"] = self.code
        if self.detail:
            data["detail"] = sanitize_detail(self.detail)
        if self.metrics:
            data["metrics"] = _safe_metrics(self.metrics)
        return data


@dataclass(frozen=True)
class AcceptanceReport:
    checks: tuple[CheckResult, ...]
    error: dict[str, str] | None = None

    def __post_init__(self) -> None:
        names = tuple(check.name for check in self.checks)
        if names != CHECK_ORDER:
            raise ValueError(f"doctor checks must follow {CHECK_ORDER!r}")

    @property
    def ok(self) -> bool:
        return self.error is None and all(check.status == "passed" for check in self.checks)

    def as_dict(self) -> dict[str, Any]:
        counts = {
            status: sum(check.status == status for check in self.checks)
            for status in ("passed", "failed", "not_run")
        }
        data: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "ok": self.ok,
            "summary": {
                "total": len(self.checks),
                **counts,
            },
            "checks": [check.as_dict() for check in self.checks],
        }
        if self.error is not None:
            data["error"] = {
                "code": self.error["code"],
                "detail": sanitize_detail(self.error.get("detail", "")),
            }
        return data


def sanitize_detail(detail: Any, *, limit: int = 280) -> str:
    """Return bounded diagnostics without secrets, license ids, or local paths."""

    text = str(detail or "").replace("\x00", " ").replace("\r", " ")
    text = _SECRET_RE.sub("[redacted]", text)
    text = _WINDOWS_PATH_RE.sub("[path]", text)
    text = _POSIX_PATH_RE.sub("[path]", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit] or "runtime check failed"


def _safe_metrics(value: Any, *, depth: int = 0) -> Any:
    """Keep diagnostic metrics JSON-safe without allowing path/secret leakage."""

    if depth > 4:
        return sanitize_detail(value, limit=128)
    if isinstance(value, Mapping):
        return {
            sanitize_detail(key, limit=64): _safe_metrics(child, depth=depth + 1)
            for key, child in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_safe_metrics(child, depth=depth + 1) for child in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return sanitize_detail(value, limit=128)
    return sanitize_detail(value, limit=128)


def _failure_code(error: BaseException, default: str = "runtime_unavailable") -> str:
    explicit = getattr(error, "code", None)
    if isinstance(error, DoctorError) and explicit not in (None, DoctorError.code):
        return str(explicit)
    if isinstance(error, (TimeoutError, asyncio.TimeoutError)):
        return "transport_timeout"
    if isinstance(error, (ImportError, ModuleNotFoundError)):
        return "mcp_unavailable"
    if isinstance(error, (FileNotFoundError, ConnectionError, BrokenPipeError, EOFError, OSError)):
        return "mcp_unavailable"
    text = str(error).lower()
    if any(token in text for token in ("license", "pystata", "engine init", "session failed to start")):
        return "engine_unavailable"
    if any(token in text for token in ("protocol", "marker", "json", "response")):
        return "protocol_invalid"
    if isinstance(error, DoctorError):
        return error.code
    return default


def _check_failure(name: str, error: BaseException) -> CheckResult:
    return CheckResult(
        name=name,
        status="failed",
        code=_failure_code(error),
        detail=sanitize_detail(error),
    )


def _not_run(name: str, reason: str) -> CheckResult:
    return CheckResult(name=name, status="not_run", code="blocked", detail=reason)


def _await_sync(value: Any) -> Any:
    if inspect.isawaitable(value):
        async def resolve() -> Any:
            return await value

        return asyncio.run(resolve())
    return value


def _close_quiet(resource: Any) -> None:
    close = getattr(resource, "close", None)
    if not callable(close):
        return
    try:
        _await_sync(close())
    except Exception:  # noqa: BLE001 - cleanup must not mask the check outcome
        pass


def _result_rc(result: Any) -> int | None:
    rc = getattr(result, "rc", None)
    if callable(rc):
        rc = rc()
    if rc is None:
        return None
    try:
        return int(rc)
    except (TypeError, ValueError) as error:
        raise DoctorProtocolError("Stata response rc is not an integer") from error


def _result_text(result: Any) -> str:
    return str(getattr(result, "text", "") or "")


def _ensure_result(result: Any, *, marker: str | None = None) -> str:
    if result is None:
        raise DoctorProtocolError("Stata returned no response")
    if bool(getattr(result, "is_error", False)):
        raise DoctorError(_result_text(result) or "Stata MCP returned an error")
    rc = _result_rc(result)
    if rc is not None and rc != 0:
        raise DoctorError(f"Stata returned non-zero rc={rc}")
    text = _result_text(result)
    if marker is not None and marker not in text:
        raise DoctorProtocolError(f"missing response marker: {marker}")
    return text


def _default_client() -> Any:
    from .tools.stata_client import StataClient

    return StataClient()


def _default_session(*, timeout: float = 60.0) -> Any:
    from .tools.stata_client import StataSession

    return StataSession(timeout=timeout)


def _default_store(root: Path) -> Any:
    from .storage.sqlite_store import SQLiteStore

    return SQLiteStore(str(root / "ledger.sqlite3"), writer_id="stata-doctor")


def _default_executor(store: Any, root: Path) -> Any:
    from .tools.executor import StataExecutor

    return StataExecutor(store, run_root=root / "runs", share_session=False)


def _check_tools(client_factory: Callable[[], Any]) -> CheckResult:
    client: Any | None = None
    try:
        client = client_factory()
        tools = _await_sync(client.list_tools())
        if isinstance(tools, (str, bytes)) or not isinstance(tools, Iterable):
            raise DoctorProtocolError("MCP tool listing is not a sequence")
        names = {str(tool) for tool in tools}
        if "stata_run" not in names:
            raise DoctorToolMissing("MCP server does not expose stata_run")
        return CheckResult(
            "mcp_tools",
            "passed",
            metrics={"tool_count": len(names), "has_stata_run": True},
        )
    except Exception as error:  # noqa: BLE001 - normalize external runtime failures
        return _check_failure("mcp_tools", error)
    finally:
        _close_quiet(client)


def _check_engine(session: Any) -> CheckResult:
    try:
        text = _ensure_result(
            session.call(f'di "{_ENGINE_MARKER}"'),
            marker=_ENGINE_MARKER,
        )
        return CheckResult(
            "engine",
            "passed",
            metrics={"marker": _ENGINE_MARKER, "response_bytes": len(text.encode("utf-8"))},
        )
    except Exception as error:  # noqa: BLE001 - normalize external runtime failures
        return _check_failure("engine", error)


def _check_persistent_session(session: Any, iterations: int) -> CheckResult:
    try:
        _ensure_result(session.call("scalar __stata_agent_doctor = 0"))
        for _ in range(iterations):
            _ensure_result(session.call("scalar __stata_agent_doctor = __stata_agent_doctor + 1"))
        text = _ensure_result(
            session.call(f'di "{_SESSION_MARKER}" scalar(__stata_agent_doctor)'),
            marker=_SESSION_MARKER,
        )
        match = re.search(rf"{re.escape(_SESSION_MARKER)}\s*([0-9]+)", text)
        if match is None:
            raise DoctorProtocolError("persistent session value is not an integer")
        observed = int(match.group(1))
        if observed != iterations:
            raise DoctorCheckError(
                f"persistent session expected {iterations}, observed {observed}"
            )
        return CheckResult(
            "persistent_session",
            "passed",
            metrics={"iterations": iterations, "observed": observed},
        )
    except Exception as error:  # noqa: BLE001 - normalize external runtime failures
        return _check_failure("persistent_session", error)


def _verify_executor_result(output: Mapping[str, Any], script: str) -> dict[str, Any]:
    machine = output.get("machine")
    env = output.get("env")
    if not isinstance(machine, Mapping) or machine.get("N") != 74:
        raise DoctorCheckError("auto regression returned unexpected N")
    coef = machine.get("coef")
    r2 = machine.get("r2")
    if not isinstance(coef, (int, float)) or coef >= 0:
        raise DoctorCheckError("auto regression coefficient sign mismatch")
    if not isinstance(r2, (int, float)) or not 0.1 < r2 < 0.5:
        raise DoctorCheckError("auto regression R2 outside expected range")
    if not isinstance(env, Mapping) or not env.get("stata_version") or not env.get("stata_flavor"):
        raise DoctorCheckError("missing Stata environment fingerprint")
    do_file = output.get("do_file")
    if not isinstance(do_file, str) or not do_file or not Path(do_file).is_file():
        raise DoctorCheckError("reproducible do-file is missing")
    command_hash = output.get("command_hash")
    from .tools.executor import sha

    if command_hash != sha(script):
        raise DoctorCheckError("executor command hash mismatch")
    return {
        "N": int(machine["N"]),
        "coef_negative": True,
        "r2": float(r2),
        "stata_version": str(env["stata_version"]),
        "stata_flavor": str(env["stata_flavor"]),
    }


def _verify_ledger(store: Any, idea: str, output: Mapping[str, Any]) -> dict[str, Any]:
    from .events.schema import (
        EVENT_RUN_FAILED,
        EVENT_RUN_REQ,
        EVENT_RUN_SUCCEEDED,
        EVENT_RUN_UNCERTAIN,
        EVENT_TOOL_CALL,
        EVENT_TOOL_RESULT,
    )

    run_id = str(output.get("run_id") or "")
    if not run_id:
        raise DoctorCheckError("executor output has no run_id")
    events = [
        event
        for event in store.scan(idea)
        if event.payload.get("run_id") == run_id
    ]
    if not events:
        raise DoctorCheckError("ledger has no events for executor run")
    operation_ids = {event.operation_id for event in events}
    if len(operation_ids) != 1:
        raise DoctorCheckError("executor run spans multiple operation ids")
    if not operation_ids or None in operation_ids:
        raise DoctorCheckError("executor run has no operation id")
    event_types = [event.event_type for event in events]
    terminal_types = {EVENT_RUN_SUCCEEDED, EVENT_RUN_FAILED, EVENT_RUN_UNCERTAIN}
    if event_types[0] != EVENT_RUN_REQ or event_types[-1] != EVENT_RUN_SUCCEEDED:
        raise DoctorCheckError("run event chain is not requested-to-succeeded")
    if any(event_type not in {EVENT_RUN_REQ, EVENT_TOOL_CALL, EVENT_TOOL_RESULT, *terminal_types}
           for event_type in event_types):
        raise DoctorCheckError("run event chain contains an unsupported event")
    if any(event_type in terminal_types for event_type in event_types[:-1]):
        raise DoctorCheckError("run event chain has a premature terminal")
    if event_types.count(EVENT_RUN_REQ) != 1:
        raise DoctorCheckError("run must have exactly one requested event")
    call_count = event_types.count(EVENT_TOOL_CALL)
    result_count = event_types.count(EVENT_TOOL_RESULT)
    if call_count < 1 or call_count != result_count:
        raise DoctorCheckError("tool call/result closure is incomplete")
    open_calls: set[str] = set()
    seen_calls: set[str] = set()
    for event in events:
        if event.event_type not in {EVENT_TOOL_CALL, EVENT_TOOL_RESULT}:
            continue
        call_id = event.payload.get("call_id")
        if not isinstance(call_id, str) or not call_id:
            raise DoctorCheckError("tool event has no call_id")
        if event.event_type == EVENT_TOOL_CALL:
            if call_id in seen_calls:
                raise DoctorCheckError("tool call id is duplicated")
            seen_calls.add(call_id)
            open_calls.add(call_id)
        elif call_id not in open_calls:
            raise DoctorCheckError("tool result has no preceding call")
        else:
            open_calls.remove(call_id)
    if open_calls:
        raise DoctorCheckError("tool call/result closure is incomplete")
    terminal = events[-1]
    provenance = terminal.payload.get("provenance")
    terminal_machine = terminal.payload.get("machine")
    expected_machine = output.get("machine")
    if (
        not isinstance(terminal_machine, Mapping)
        or terminal_machine != expected_machine
        or not isinstance(provenance, Mapping)
        or not provenance.get("attested")
        or provenance.get("kind") != "real"
        or provenance.get("executor") != "stata-mcp"
        or provenance.get("do_file") != output.get("do_file")
        or provenance.get("command_hash") != output.get("command_hash")
        or not isinstance(provenance.get("env_sig"), Mapping)
        or provenance.get("env_sig") != output.get("env")
    ):
        raise DoctorCheckError("succeeded run lacks attested provenance")
    return {
        "event_count": len(events),
        "tool_calls": call_count,
        "terminal": EVENT_RUN_SUCCEEDED,
        "provenance_attested": True,
    }


def run_acceptance(
    *,
    iterations: int = 20,
    client_factory: Callable[[], Any] | None = None,
    session_factory: Callable[[], Any] | None = None,
    store_factory: Callable[[Path], Any] | None = None,
    executor_factory: Callable[[Any, Path], Any] | None = None,
) -> AcceptanceReport:
    """Run the acceptance checks while containing third-party stderr output."""

    # stata-mcp writes a startup banner (including license metadata) to stderr.
    # Keep that process diagnostic out of the stable CLI contract; failures are
    # represented by the sanitized check details instead.
    with open(os.devnull, "w", encoding="utf-8") as sink, redirect_stderr(sink):
        return _run_acceptance(
            iterations=iterations,
            client_factory=client_factory,
            session_factory=session_factory,
            store_factory=store_factory,
            executor_factory=executor_factory,
        )


def _run_acceptance(
    *,
    iterations: int = 20,
    client_factory: Callable[[], Any] | None = None,
    session_factory: Callable[[], Any] | None = None,
    store_factory: Callable[[Path], Any] | None = None,
    executor_factory: Callable[[Any, Path], Any] | None = None,
) -> AcceptanceReport:
    """Run the ordered live checks in an isolated temporary workspace."""

    if not isinstance(iterations, int) or isinstance(iterations, bool):
        raise DoctorConfigurationError("iterations must be an integer")
    if not 1 <= iterations <= _MAX_ITERATIONS:
        raise DoctorConfigurationError(
            f"iterations must be between 1 and {_MAX_ITERATIONS}"
        )

    client_factory = client_factory or _default_client
    session_factory = session_factory or (lambda: _default_session())
    store_factory = store_factory or _default_store
    executor_factory = executor_factory or _default_executor

    checks: list[CheckResult] = []
    tool_check = _check_tools(client_factory)
    checks.append(tool_check)
    if tool_check.status != "passed":
        checks.extend(
            _not_run(name, "blocked by mcp_tools")
            for name in CHECK_ORDER[1:]
        )
        return AcceptanceReport(tuple(checks))

    session: Any | None = None
    try:
        try:
            session = session_factory()
        except Exception as error:  # noqa: BLE001 - normalize startup failures
            engine_check = _check_failure("engine", error)
            checks.append(engine_check)
            checks.extend(
                _not_run(name, "blocked by engine")
                for name in CHECK_ORDER[2:]
            )
            return AcceptanceReport(tuple(checks))

        engine_check = _check_engine(session)
        checks.append(engine_check)
        if engine_check.status != "passed":
            checks.extend(
                _not_run(name, "blocked by engine")
                for name in CHECK_ORDER[2:]
            )
            return AcceptanceReport(tuple(checks))

        session_check = _check_persistent_session(session, iterations)
        checks.append(session_check)
        if session_check.status != "passed":
            checks.extend(
                _not_run(name, "blocked by persistent_session")
                for name in CHECK_ORDER[3:]
            )
            return AcceptanceReport(tuple(checks))
    finally:
        _close_quiet(session)

    script: str | None = None
    store: Any | None = None
    executor: Any | None = None
    with TemporaryDirectory(prefix="stata-agent-doctor-") as temporary:
        root = Path(temporary)
        try:
            try:
                from .tools.executor import auto_regress_script

                script = auto_regress_script()
                store = store_factory(root)
                executor = executor_factory(store, root)
                output = executor.execute(script, idea="__stata_agent_doctor__")
                metrics = _verify_executor_result(output, script)
                checks.append(CheckResult("executor", "passed", metrics=metrics))
            except Exception as error:  # noqa: BLE001 - normalize execution failures
                checks.append(_check_failure("executor", error))
                checks.append(_not_run("ledger", "blocked by executor"))
                return AcceptanceReport(tuple(checks))

            try:
                assert store is not None
                assert script is not None
                ledger_metrics = _verify_ledger(
                    store,
                    "__stata_agent_doctor__",
                    output,
                )
                checks.append(CheckResult("ledger", "passed", metrics=ledger_metrics))
            except Exception as error:  # noqa: BLE001 - ledger is a release gate
                checks.append(_check_failure("ledger", error))
        finally:
            _close_quiet(executor)
            _close_quiet(store)

    return AcceptanceReport(tuple(checks))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stata-agent-stata-check",
        description="运行隔离的 Windows 真 Stata / stata-mcp 发布验收",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="只输出机器可读 JSON",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=20,
        help=f"持久会话迭代次数（1-{_MAX_ITERATIONS}，默认 20）",
    )
    return parser


def _print_report(report: AcceptanceReport, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(report.as_dict(), ensure_ascii=False, sort_keys=True))
        return
    data = report.as_dict()
    summary = data["summary"]
    print(
        "stata-agent-stata-check: "
        f"{summary['passed']}/{summary['total']} checks passed"
    )
    for check in data["checks"]:
        status = str(check["status"]).upper()
        suffix = ""
        if check.get("code"):
            suffix = f" ({check['code']})"
        detail = f": {check['detail']}" if check.get("detail") else ""
        print(f"  {status} {check['name']}{suffix}{detail}")


def _print_error(error: BaseException, *, as_json: bool) -> None:
    detail = sanitize_detail(error)
    if as_json:
        print(
            json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "ok": False,
                    "summary": {"total": 0, "passed": 0, "failed": 0, "not_run": 0},
                    "checks": [],
                    "error": {
                        "code": _failure_code(error, "internal_error"),
                        "detail": detail,
                    },
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    else:
        print(f"stata-agent-stata-check failed: {detail}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    raw_args = list(sys.argv[1:] if argv is None else argv)
    try:
        args = parser.parse_args(raw_args)
    except SystemExit as error:
        code = error.code if isinstance(error.code, int) else EXIT_USAGE
        if code != 0 and "--json" in raw_args:
            _print_error(
                DoctorConfigurationError("invalid command arguments"),
                as_json=True,
            )
        return int(code)
    if not 1 <= args.iterations <= _MAX_ITERATIONS:
        configuration_error = DoctorConfigurationError(
            f"iterations must be between 1 and {_MAX_ITERATIONS}"
        )
        _print_error(configuration_error, as_json=args.json)
        return EXIT_USAGE
    try:
        report = run_acceptance(iterations=args.iterations)
    except KeyboardInterrupt:
        _print_error(DoctorError("interrupted"), as_json=args.json)
        return EXIT_CHECK_FAILED
    except DoctorConfigurationError as configuration_error:
        _print_error(configuration_error, as_json=args.json)
        return EXIT_USAGE
    except Exception as setup_error:  # noqa: BLE001 - CLI must have a stable setup exit
        _print_error(setup_error, as_json=args.json)
        return EXIT_USAGE
    _print_report(report, as_json=args.json)
    return EXIT_OK if report.ok else EXIT_CHECK_FAILED


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "AcceptanceReport",
    "CHECK_ORDER",
    "CheckResult",
    "DoctorCheckError",
    "DoctorConfigurationError",
    "DoctorError",
    "DoctorProtocolError",
    "DoctorToolMissing",
    "EXIT_CHECK_FAILED",
    "EXIT_OK",
    "EXIT_USAGE",
    "main",
    "run_acceptance",
    "sanitize_detail",
]
