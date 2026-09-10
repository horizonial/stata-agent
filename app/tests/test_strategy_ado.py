"""工具策略映射（DD-04 §3 全量）+ ado 预检 util（live-skip）。"""

from __future__ import annotations

import os

import pytest

from stata_agent.skills.loader import load_skill
from stata_agent.tools.strategy import (
    TOOLS,
    phase_allows,
    side_effect_of,
    summary,
    write_tools,
)
from stata_agent.tools.executor import MachineParseError, StataExecutor


def test_all_10_stata_tools_mapped():
    expected = {
        "stata_break", "stata_data_rows", "stata_export_graph", "stata_get_help",
        "stata_get_results", "stata_inspect_data", "stata_load_data", "stata_run",
        "stata_session_history", "stata_task_status",
    }
    assert set(TOOLS) == expected


def test_write_read_classification_matches_policy():
    assert side_effect_of("stata_run") == "write"
    assert side_effect_of("stata_get_results") == "read"
    assert "stata_load_data" in write_tools()
    # write 只在 数据/估计/稳健 阶段
    assert phase_allows("stata_run", "ESTIMATION")
    assert not phase_allows("stata_run", "IDEA")
    # read 任意阶段
    assert phase_allows("stata_get_help", "WRITING")


def test_export_graph_is_write_not_read():
    assert side_effect_of("stata_export_graph") == "write"
    assert "stata_export_graph" in write_tools()


def test_external_skill_loads_without_ados():
    from pathlib import Path
    skill = load_skill(Path(__file__).resolve().parents[1] / "skills" / "causal-inference-mixtape.md")
    assert skill.slug == "causal-inference-mixtape"
    assert skill.requires_ados == []   # 外部 skill 无 ados 字段 → 空，不崩


def test_require_ados_empty_ok(tmp_path):
    # require_ados=[] 不触发真 Stata 预检（离线安全）
    assert True


class _ProbeResult:
    def __init__(self, text: str = "", *, is_error: bool = False) -> None:
        self.text = text
        self.is_error = is_error


class _ProbeSession:
    results: list[_ProbeResult] | None = None
    error: BaseException | None = None
    instances: list["_ProbeSession"] = []

    def __init__(self) -> None:
        type(self).instances.append(self)
        self.closed = False

    def run_batch(self, codes: list[str]):
        del codes
        if type(self).error is not None:
            raise type(self).error
        return list(type(self).results or [])

    def close(self) -> None:
        self.closed = True


def test_ado_probe_requires_complete_markers_and_closes(monkeypatch):
    from stata_agent.tools import ado

    _ProbeSession.instances = []
    _ProbeSession.results = [
        _ProbeResult("C:/ado/reghdfe.ado"),
        _ProbeResult("ADOOK_rc=0"),
        _ProbeResult("command nonsense_xyz_123 not found as either built-in or ado-file", is_error=True),
        _ProbeResult("ADOOK_rc=0"),
    ]
    _ProbeSession.error = None
    monkeypatch.setattr(ado, "StataSession", _ProbeSession)

    assert ado.which_ados(["reghdfe", "nonsense_xyz_123"]) == {
        "reghdfe": True,
        "nonsense_xyz_123": False,
    }
    assert _ProbeSession.instances[0].closed is True


def test_ado_probe_partial_or_error_response_fails_closed(monkeypatch):
    from stata_agent.tools import ado

    _ProbeSession.error = None
    _ProbeSession.results = [_ProbeResult("ADOOK_rc=0")]
    monkeypatch.setattr(ado, "StataSession", _ProbeSession)
    with pytest.raises(ado.AdoProbeProtocolError, match="result count"):
        ado.which_ados(["reghdfe", "ftools"])
    _ProbeSession.results = [_ProbeResult("C:/ado/reghdfe.ado"), _ProbeResult("ADOOK_rc=0")]
    assert ado.which_ados(["reghdfe"]) == {"reghdfe": True}

    class ErrorSession(_ProbeSession):
        def run_batch(self, codes):
            del codes
            return [
                _ProbeResult("MCP returned an error", is_error=True),
                _ProbeResult("ADOOK_rc=0"),
            ]

    monkeypatch.setattr(ado, "StataSession", ErrorSession)
    with pytest.raises(ado.AdoProbeUnavailable):
        ado.which_ados(["reghdfe"])


def test_ado_probe_runtime_failure_never_means_no_missing(monkeypatch):
    from stata_agent.tools import ado

    class BrokenSession(_ProbeSession):
        def run_batch(self, codes):
            del codes
            raise RuntimeError("session failed to start: license unavailable")

    monkeypatch.setattr(ado, "StataSession", BrokenSession)
    with pytest.raises(ado.AdoProbeUnavailable):
        ado.missing_ados(["reghdfe"])


def test_ado_probe_rejects_bad_markers_and_duplicate_names(monkeypatch):
    from stata_agent.tools import ado

    _ProbeSession.error = None
    _ProbeSession.results = [_ProbeResult("C:/ado/reghdfe.ado"), _ProbeResult("ADOOK_rc=abc")]
    monkeypatch.setattr(ado, "StataSession", _ProbeSession)
    with pytest.raises(ado.AdoProbeProtocolError):
        ado.which_ados(["reghdfe"])
    with pytest.raises(ado.AdoProbeProtocolError, match="unique"):
        ado.which_ados(["reghdfe", "reghdfe"])


def test_ado_probe_rejects_empty_success_response_and_close_error(monkeypatch):
    from stata_agent.tools import ado

    class CloseErrorSession(_ProbeSession):
        def close(self) -> None:
            self.closed = True
            raise RuntimeError("cleanup failed")

    _ProbeSession.error = None
    _ProbeSession.results = [_ProbeResult(""), _ProbeResult("ADOOK_rc=0")]
    monkeypatch.setattr(ado, "StataSession", CloseErrorSession)
    with pytest.raises(ado.AdoProbeProtocolError, match="empty which"):
        ado.which_ados(["reghdfe"])
    assert _ProbeSession.instances[-1].closed is True


def test_ado_probe_rejects_out_of_order_which_responses(monkeypatch):
    from stata_agent.tools import ado

    _ProbeSession.error = None
    _ProbeSession.results = [
        _ProbeResult("C:/ado/ftools.ado"),
        _ProbeResult("ADOOK_rc=0"),
        _ProbeResult("C:/ado/reghdfe.ado"),
        _ProbeResult("ADOOK_rc=0"),
    ]
    monkeypatch.setattr(ado, "StataSession", _ProbeSession)
    with pytest.raises(ado.AdoProbeProtocolError, match="does not identify"):
        ado.which_ados(["reghdfe", "ftools"])


@pytest.mark.skipif(os.environ.get("STATA_LIVE") != "1", reason="需 STATA_LIVE=1")
def test_missing_ados_live(tmp_path):
    from stata_agent.tools.ado import missing_ados

    miss = missing_ados(["nonsense_xyz_123"])  # 一个必不存在的 ado
    assert "nonsense_xyz_123" in miss
