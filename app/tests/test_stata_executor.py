"""切片 3 live 验收（需真 Stata + stata-mcp）：L-R = rc0 + 机器层 + env_sig + 可复现 + 事件链。

默认跳过；本机跑： STATA_LIVE=1 python -m pytest tests/test_stata_executor.py -q
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from stata_agent.storage.sqlite_store import SQLiteStore
from stata_agent.tools.executor import StataExecutor, auto_regress_script
from stata_agent.tools.result_verifier import ResultContract, verify_run_record

pytestmark = pytest.mark.skipif(
    os.environ.get("STATA_LIVE") != "1",
    reason="需 STATA_LIVE=1 且本机有 stata-mcp + Stata",
)


def test_real_regression_machine_env_and_events(tmp_path):
    store = SQLiteStore(str(tmp_path / "l.db"), writer_id="a")
    ex = StataExecutor(store, run_root=tmp_path / "runs")
    out = ex.execute(auto_regress_script())

    m = out["machine"]
    assert m["N"] == 74                      # sysuse auto 固定样本
    assert m["coef"] < 0                     # price vs mpg 显著为负（已知稳定结果）
    assert 0.1 < m["r2"] < 0.5               # auto: r2≈0.2196
    assert "stata_version" in out["env"]

    proj = store.project("i1")
    run_recs = list(proj.runs.values())
    assert run_recs and run_recs[-1].status == "succeeded"
    prov = run_recs[-1].provenance
    assert prov["command_hash"] and "env_sig" in prov
    assert Path(prov["do_file"]).exists()    # 可复现 do-file
    assert prov["do_file"].endswith(".do")
    store.close()


def test_failed_script_records_run_failed(tmp_path):
    store = SQLiteStore(str(tmp_path / "l.db"), writer_id="a")
    ex = StataExecutor(store, run_root=tmp_path / "runs")
    bad = "di 1 +\n"  # 语法错误
    with pytest.raises(Exception):
        ex.execute(bad + "sysuse auto, clear\ndi 2")
    proj = store.project("i1")
    # 失败也要留痕（run.failed），不许静默
    assert any(rec.status == "failed" for rec in proj.runs.values())
    store.close()


def test_real_contracted_regression_is_verified(tmp_path):
    store = SQLiteStore(str(tmp_path / "contracted.db"), writer_id="contracted")
    ex = StataExecutor(store, run_root=tmp_path / "runs")
    contract = ResultContract(
        target_term="mpg",
        estimator="regress",
        dependent_variable="price",
        vce="ols",
        required_stats=["coef", "se", "N"],
    )
    out = ex.execute(
        "sysuse auto, clear\nreg price mpg",
        result_contract=contract,
    )
    assert out["machine"]["N"] == 74
    assert out["machine"]["model"] == {
        "target_term": "mpg",
        "estimator": "regress",
        "dependent_variable": "price",
        "vce": "ols",
        "cluster_variables": "",
        "fixed_effects": "",
    }
    report = verify_run_record(store.project("i1").runs[out["run_id"]])
    assert report.evidence_ready
    store.close()


def test_real_reghdfe_contract_is_verified_when_ado_is_available(tmp_path):
    from stata_agent.tools.ado import missing_ados

    if missing_ados(["reghdfe"]):
        pytest.skip("reghdfe ado 不可用")
    store = SQLiteStore(str(tmp_path / "reghdfe.db"), writer_id="reghdfe")
    ex = StataExecutor(store, run_root=tmp_path / "runs-reghdfe")
    contract = ResultContract(
        target_term="mpg",
        estimator="reghdfe",
        dependent_variable="price",
        vce="robust",
        cluster_variables=[],
        fixed_effects=["foreign"],
        required_stats=["coef", "se", "N"],
    )
    out = ex.execute(
        "sysuse auto, clear\nreghdfe price mpg, absorb(foreign) vce(robust)",
        result_contract=contract,
        require_ados=["reghdfe"],
    )
    assert out["machine"]["N"] == 74
    assert out["machine"]["model"]["estimator"] == "reghdfe"
    assert out["machine"]["model"]["fixed_effects"] == "foreign"
    assert verify_run_record(store.project("i1").runs[out["run_id"]]).evidence_ready
    store.close()
