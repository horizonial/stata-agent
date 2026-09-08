"""executor 机器层解析（离线，不碰 Stata）。"""

from __future__ import annotations

from stata_agent.tools.executor import StataExecutor


def _log(*lines):
    """executor 输出含命令 echo 与 marker 行；marker 在行首（值=行首 token 后到行尾）。"""
    return "\n".join(lines)


def test_parse_machine_full():
    text = _log(
        "MACHINE_N= 788",
        "MACHINE_R2= 0.008091",
        "MACHINE_B= 2.809943",
        "MACHINE_SE= 1.293107",
    )
    m = StataExecutor.parse_machine(text)
    assert m["N"] == 788 and m["r2"] == 0.008091
    assert abs(m["coef"] - 2.809943) < 1e-6 and abs(m["se"] - 1.293107) < 1e-6


def test_parse_machine_non_estimation_empty():
    # describe/list 之类无 e(N)，di 输出 "." → 空 dict，不 raise
    assert StataExecutor.parse_machine("MACHINE_N= .\nMACHINE_R2= .") == {}
    assert StataExecutor.parse_machine("") == {}


def test_parse_machine_partial():
    assert StataExecutor.parse_machine("MACHINE_N= 74") == {"N": 74}
    assert StataExecutor.parse_machine("MACHINE_N= 74\nMACHINE_R2= 0.22") == {"N": 74, "r2": 0.22}
    # coef/r2 为 '.' 时忽略（非估计后的残余行）
    assert StataExecutor.parse_machine("MACHINE_N= 74\nMACHINE_B= .\nMACHINE_R2= .") == {"N": 74}


def test_parse_env():
    assert StataExecutor.parse_env("STA_ENV version= 18\nSTA_ENV flavor= IC") == {
        "stata_version": "18", "stata_flavor": "IC"}
