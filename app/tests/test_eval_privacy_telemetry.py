"""E：eval harness 检查 / 隐私三档边界 / 两本账遥测。"""

from __future__ import annotations

import pytest

from stata_agent.eval.checks import run
from stata_agent.harness.telemetry import Telemetry
from stata_agent.privacy.modes import (
    PrivacyViolation,
    assert_fallback_allowed,
    fallback_allowed,
    remote_llm_allowed,
)


def test_eval_checks_all_pass():
    report = run()
    assert report["ok"] is True, report


def test_privacy_fallback_boundaries():
    assert fallback_allowed("approved_remote", "approved_remote")
    assert fallback_allowed("approved_remote", "mixed_sanitized")   # 收严允许
    assert not fallback_allowed("local_strict", "approved_remote")  # 放松禁止
    with pytest.raises(PrivacyViolation):
        assert_fallback_allowed("local_strict", "mixed_sanitized")
    # local_strict 不许远端模型处理研究内容；approved/mixed 允许
    assert not remote_llm_allowed("local_strict", "deepseek")
    assert remote_llm_allowed("approved_remote", "deepseek")
    assert remote_llm_allowed("local_strict", "local")   # 本地模型总是可以


def test_telemetry_two_ledger(tmp_path):
    t = Telemetry(tmp_path / "telemetry.jsonl")
    t.record(kind="llm.propose", ms=120.5, tokens=320, idea="i1", event_seq=3)
    t.record(kind="stata.run", ms=15.0)
    t.record(kind="llm.propose", ms=90.0)
    m = t.metrics()
    assert m["events"] == 3
    assert m["by_kind"]["llm.propose"] == 2
    assert m["total_ms"] >= 220
