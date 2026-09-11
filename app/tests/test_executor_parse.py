"""executor 机器层解析（离线，不碰 Stata）。"""

from __future__ import annotations

import hashlib

from stata_agent.tools.executor import StataExecutor
from stata_agent.tools.result_verifier import semantic_input_hash
from stata_agent.tools.stata_client import CallResult
from stata_agent.storage.sqlite_store import SQLiteStore


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


def test_parse_contracted_namespace_ignores_generic_markers():
    text = _log(
        "MACHINE_N= 999",
        "MACHINE_B= 999",
        "STA_AGENT_random_MACHINE_N= 74",
        "STA_AGENT_random_MACHINE_B= -238.9",
        "STA_AGENT_random_MACHINE_SE= 40",
        "STA_AGENT_random_MACHINE_R2= .2196",
        "STA_AGENT_random_MACHINE_TERM= mpg",
        "STA_AGENT_random_MACHINE_CMD= regress",
        "STA_AGENT_random_MACHINE_DEPVAR= price",
        "STA_AGENT_random_MACHINE_VCE= ols",
        "STA_AGENT_random_MACHINE_CLUSTER=",
        "STA_AGENT_random_MACHINE_FE=",
    )
    machine = StataExecutor._parse_machine(text, namespace="STA_AGENT_random_")
    assert machine["N"] == 74 and machine["coef"] == -238.9
    assert machine["model"]["estimator"] == "regress"
    assert StataExecutor._parse_machine(text, namespace="STA_AGENT_other_") == {}


def test_contracted_executor_hashes_actual_do_file_and_ignores_spoof(monkeypatch, tmp_path):
    class Session:
        def call(self, code, cancellation=None):
            del cancellation
            if "MACHINE_N=" not in code:
                return CallResult()
            namespace = code.split('"', 2)[1].split("MACHINE_N=", 1)[0]
            return CallResult(text="\n".join([
                f"{namespace}MACHINE_N= 74",
                f"{namespace}MACHINE_B= -238.9",
                f"{namespace}MACHINE_SE= 40",
                f"{namespace}MACHINE_R2= .2196",
                f"{namespace}MACHINE_TERM= mpg",
                f"{namespace}MACHINE_CMD= regress",
                f"{namespace}MACHINE_DEPVAR= price",
                f"{namespace}MACHINE_VCE= ols",
                f"{namespace}MACHINE_CLUSTER=",
                f"{namespace}MACHINE_FE=",
                f"{namespace}STA_ENV_VERSION= 18",
                f"{namespace}STA_ENV_FLAVOR= IC",
            ]))

        def close(self):
            return None

    monkeypatch.setattr("stata_agent.tools.executor.StataSession", Session)
    from stata_agent.tools.result_verifier import ResultContract

    contract = ResultContract(
        target_term="mpg", estimator="regress", dependent_variable="price",
        required_stats=["coef", "se", "N"],
    )
    store = SQLiteStore(str(tmp_path / "ledger.db"), writer_id="parse")
    code = 'reg price mpg\ndi "MACHINE_N= 999"'
    out = StataExecutor(store, run_root=tmp_path / "runs").execute(
        code, idea="i1", result_contract=contract,
    )
    assert out["machine"]["N"] == 74
    assert out["machine"]["coef"] == -238.9
    assert out["machine"]["model"]["estimator"] == "regress"
    assert out["semantic_input_hash"] == semantic_input_hash(code, contract)
    assert out["command_hash"] != out["semantic_input_hash"]
    assert out["command_hash"] == hashlib.sha256(
        (tmp_path / "runs" / f"{out['run_id']}.do").read_bytes()
    ).hexdigest()
    rec = store.project("i1").runs[out["run_id"]]
    assert rec.result_contract and rec.provenance["contract_hash"]
    store.close()
