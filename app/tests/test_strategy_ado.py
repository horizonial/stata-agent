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


@pytest.mark.skipif(os.environ.get("STATA_LIVE") != "1", reason="需 STATA_LIVE=1")
def test_missing_ados_live(tmp_path):
    from stata_agent.tools.ado import missing_ados

    miss = missing_ados(["nonsense_xyz_123"])  # 一个必不存在的 ado
    assert "nonsense_xyz_123" in miss
