"""Offline release-entrypoint coverage.

These are deliberately small smoke tests around command boundaries.  They do
not exercise the runtime/UX paths owned by the other product worktrees.
"""

from __future__ import annotations

import io
import json
import os
import runpy
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from stata_agent import cli
from stata_agent.eval import runner
from stata_agent.tools import ado


def test_eval_cli_failure_exit_codes_and_filter(monkeypatch, capsys) -> None:
    """A selected scenario runs, scenario failures return 1, setup returns 2."""

    assert runner.main(["--json", "--scenario", "L1_TOOL_ROUTING"]) == 0
    selected = json.loads(capsys.readouterr().out)
    assert selected["summary"] == {"total": 1, "passed": 1, "failed": 0}

    monkeypatch.setitem(
        runner.SCENARIOS,
        "L1_TOOL_ROUTING",
        lambda: {"unexpected": True},
    )
    assert runner.main(["--json", "--scenario", "L1_TOOL_ROUTING"]) == 1
    failed = json.loads(capsys.readouterr().out)
    assert failed["summary"] == {"total": 1, "passed": 0, "failed": 1}
    assert failed["scenarios"]["L1_TOOL_ROUTING"]["ok"] is False

    monkeypatch.setattr(
        runner,
        "load_golden",
        lambda: (_ for _ in ()).throw(FileNotFoundError("golden missing")),
    )
    assert runner.main(["--json"]) == 2
    setup_failed = json.loads(capsys.readouterr().out)
    assert setup_failed["summary"] == {"total": 0, "passed": 0, "failed": 0}
    assert setup_failed["error"] == "FileNotFoundError"


def test_eval_runner_golden_and_diff_failure_paths(tmp_path, monkeypatch) -> None:
    """Malformed goldens and mismatches fail deterministically, not silently."""

    assert runner._diff({"a": 1}, {"a": 2}) == ["a: expected 1, got 2"]
    assert runner._diff({"a": 1}, {"b": 2}) == [
        "a: missing",
        "b: unexpected",
    ]
    assert runner._diff([1], [2], "root") == [
        "root: expected [1], got [2]"
    ]
    assert runner._diff(1, 2, "root") == ["root: expected 1, got 2"]

    missing = tmp_path / "missing.json"
    monkeypatch.setattr(runner, "_golden_candidates", lambda: (missing,))
    with pytest.raises(FileNotFoundError):
        runner.load_golden()

    malformed = tmp_path / "malformed.json"
    malformed.write_text(json.dumps({"schema_version": 2}), encoding="utf-8")
    monkeypatch.setattr(runner, "_golden_candidates", lambda: (malformed,))
    with pytest.raises(ValueError, match="schema_version"):
        runner.load_golden()

    no_scenarios = tmp_path / "no-scenarios.json"
    no_scenarios.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
    monkeypatch.setattr(runner, "_golden_candidates", lambda: (no_scenarios,))
    with pytest.raises(ValueError, match="scenarios object"):
        runner.load_golden()


def test_eval_runner_unknown_missing_and_exception_paths(monkeypatch) -> None:
    """Unknown selections and per-scenario failures are surfaced in reports."""

    with pytest.raises(ValueError, match="未知 scenario"):
        runner.run_product_eval(["NOT_A_SCENARIO"])

    monkeypatch.setattr(
        runner,
        "load_golden",
        lambda: {"schema_version": 1, "scenarios": {}},
    )
    missing = runner.run_product_eval(["L1_TOOL_ROUTING"])
    assert missing["ok"] is False
    assert missing["scenarios"]["L1_TOOL_ROUTING"]["failures"] == [
        "ValueError: scenario execution failed"
    ]

    def explode() -> dict:
        raise RuntimeError("synthetic")

    monkeypatch.setitem(runner.SCENARIOS, "L1_TOOL_ROUTING", explode)
    failed = runner.run_product_eval(["L1_TOOL_ROUTING"])
    assert failed["ok"] is False
    assert failed["scenarios"]["L1_TOOL_ROUTING"]["failures"] == [
        "RuntimeError: scenario execution failed"
    ]


def test_eval_cli_human_output_and_setup_error(monkeypatch, capsys) -> None:
    """Human output remains useful while setup errors retain exit code 2."""

    assert runner.main(["--scenario", "L1_TOOL_ROUTING"]) == 0
    output = capsys.readouterr().out
    assert "product-eval: 1/1 scenarios passed" in output
    assert "PASS L1_TOOL_ROUTING" in output

    monkeypatch.setattr(
        runner,
        "load_golden",
        lambda: (_ for _ in ()).throw(FileNotFoundError("golden missing")),
    )
    assert runner.main([]) == 2
    assert "product-eval setup failed" in capsys.readouterr().err


def test_eval_l4_false_draft_observation_branch(monkeypatch) -> None:
    """A missing generated draft is observable as a failed scenario field."""

    monkeypatch.setattr(runner.Path, "is_file", lambda _path: False)
    observed = runner.SCENARIOS["L4_FAKE_TO_DRAFT"]()
    assert observed["draft_written"] is False
    assert observed["draft_has_claim"] is False


def test_eval_module_entrypoint(monkeypatch, capsys) -> None:
    """Cover the installed ``python -m stata_agent.eval`` dispatcher."""

    monkeypatch.setattr(
        runner.sys,
        "argv",
        ["stata-agent-eval", "--json", "--scenario", "L1_TOOL_ROUTING"],
    )
    with pytest.raises(SystemExit) as raised:
        runpy.run_module("stata_agent.eval", run_name="__main__")
    assert raised.value.code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is True
    assert output["summary"] == {"total": 1, "passed": 1, "failed": 0}


def test_stata_doctor_module_help_entrypoint() -> None:
    """The module help path is import-safe and never starts stata-mcp."""

    app_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(app_root / "src") + os.pathsep + env.get("PYTHONPATH", "")
    completed = subprocess.run(
        [sys.executable, "-m", "stata_agent.stata_doctor", "--help"],
        cwd=app_root,
        env=env,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0
    assert "stata-agent-stata-check" in completed.stdout.decode("utf-8", errors="replace")


def test_stata_doctor_console_script_is_declared() -> None:
    """The wheel-facing console target stays aligned with the module main."""

    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8")
    assert 'stata-agent-stata-check = "stata_agent.stata_doctor:main"' in text


def test_legacy_cli_empty_stdin_closes_store(tmp_path, monkeypatch, capsys) -> None:
    """EOF before the first line still creates and cleanly closes the ledger."""

    db = tmp_path / "empty.sqlite3"
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO())
    assert cli.main(["--db", str(db), "--idea", "release-empty"]) == 0
    assert "已落 0 条事件" in capsys.readouterr().out
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0


def test_legacy_cli_mock_and_eof_close(tmp_path, monkeypatch, capsys) -> None:
    """One mock turn is persisted, then a real EOF exits through the close path."""

    db = tmp_path / "mock.sqlite3"
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("研究问题\n第二轮\n"))
    assert cli.main(["--db", str(db), "--idea", "release-mock"]) == 0
    output = capsys.readouterr().out
    assert "agent>" in output
    with sqlite3.connect(db) as connection:
        event_count = connection.execute(
            "SELECT COUNT(*) FROM events WHERE idea_id=?", ("release-mock",)
        ).fetchone()[0]
        assert event_count == 3


def test_legacy_cli_module_entrypoint(tmp_path, monkeypatch, capsys) -> None:
    """The legacy module dispatcher exits with the CLI's return code."""

    db = tmp_path / "module.sqlite3"
    monkeypatch.setattr(cli.sys, "argv", ["stata-agent", "--db", str(db)])
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO())
    with pytest.raises(SystemExit) as raised:
        runpy.run_module("stata_agent.cli", run_name="__main__")
    assert raised.value.code == 0
    assert "已落 0 条事件" in capsys.readouterr().out


def test_legacy_cli_mock_script_exhaustion(tmp_path, monkeypatch, capsys) -> None:
    """The old CLI's replay-exhausted branch remains a clean, offline stop."""

    db = tmp_path / "exhausted.sqlite3"
    monkeypatch.setattr(cli, "_provider", lambda: cli.MockReplayProvider([]))
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("研究问题\n"))
    assert cli.main(["--db", str(db), "--idea", "release-exhausted"]) == 0
    assert "mock 剧本用尽" in capsys.readouterr().out


class _AdoResult:
    def __init__(self, text: str, *, is_error: bool = False) -> None:
        self.text = text
        self.is_error = is_error


class _AdoSession:
    instances: list["_AdoSession"] = []
    responses: list[_AdoResult] = []
    error: BaseException | None = None

    def __init__(self) -> None:
        self.codes: list[str] = []
        self.closed = False
        type(self).instances.append(self)

    def run_batch(self, codes: list[str]) -> list[_AdoResult]:
        self.codes = list(codes)
        if type(self).error is not None:
            raise type(self).error
        return list(type(self).responses)

    def close(self) -> None:
        self.closed = True


def test_ado_success_failure_and_empty_paths(monkeypatch) -> None:
    """Ado probes parse installed/missing statuses and always close sessions."""

    _AdoSession.instances = []
    _AdoSession.responses = [
        _AdoResult("C:/ado/reghdfe.ado"),
        _AdoResult("ADOOK_rc=0"),
        _AdoResult("command ftools not found as either built-in or ado-file", is_error=True),
        _AdoResult("ADOOK_rc=0"),
    ]
    _AdoSession.error = None
    monkeypatch.setattr(ado, "StataSession", _AdoSession)
    assert ado.which_ados(["reghdfe", "ftools"]) == {
        "reghdfe": True,
        "ftools": False,
    }
    assert len(_AdoSession.instances) == 1
    session = _AdoSession.instances[0]
    assert session.closed is True
    assert session.codes == [
        "which reghdfe",
        'di "ADOOK_rc=" _rc',
        "which ftools",
        'di "ADOOK_rc=" _rc',
    ]

    assert ado.which_ados([]) == {}
    monkeypatch.setattr(ado, "which_ados", lambda names: {name: name == "ok" for name in names})
    assert ado.missing_ados(["ok", "missing"]) == ["missing"]


def test_ado_transport_failure_still_closes(monkeypatch) -> None:
    """Transport errors are surfaced; cleanup is not swallowed."""

    _AdoSession.instances = []
    _AdoSession.responses = []
    _AdoSession.error = RuntimeError("transport unavailable")
    monkeypatch.setattr(ado, "StataSession", _AdoSession)
    with pytest.raises(RuntimeError, match="transport unavailable"):
        ado.which_ados(["reghdfe"])
    assert _AdoSession.instances[0].closed is True
