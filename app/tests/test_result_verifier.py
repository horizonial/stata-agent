"""Deterministic result-contract and verification tests."""

from __future__ import annotations

import hashlib
import json

import pytest

from stata_agent.domain.models import RunRecord
from stata_agent.tools.result_verifier import (
    CHECK_ORDER,
    ResultContract,
    canonical_contract_hash,
    canonical_machine_hash,
    semantic_input_hash,
    verify_run_record,
)


def _contract(**updates):
    data = {
        "target_term": "mpg",
        "estimator": "regress",
        "dependent_variable": "price",
        "vce": "ols",
        "required_stats": ["coef", "se", "N"],
    }
    data.update(updates)
    return ResultContract.model_validate(data)


def test_contract_validation_and_canonical_hash_are_stable():
    first = _contract(estimator="reghdfe", cluster_variables=["firm"], vce="cluster",
                      fixed_effects=["year", "firm"])
    second = _contract(estimator="reghdfe", cluster_variables=["firm"], vce="cluster",
                       fixed_effects=["firm", "year"])
    assert canonical_contract_hash(first) == canonical_contract_hash(second)
    assert semantic_input_hash("reg price mpg", first) == semantic_input_hash("reg price mpg", second)
    with pytest.raises(ValueError):
        _contract(estimator="ivregress")
    with pytest.raises(ValueError):
        _contract(target_term="mpg bad")
    with pytest.raises(ValueError):
        _contract(vce="cluster")
    with pytest.raises(ValueError):
        _contract(required_stats=["coef"])
    with pytest.raises(ValueError):
        _contract(schema_version=True)
    with pytest.raises(ValueError):
        _contract(target_term=123)


def test_verification_report_has_fixed_order_and_fail_closed(tmp_path):
    do_file = tmp_path / "run.do"
    do_file.write_text("reg price mpg\n", encoding="utf-8")
    machine = {
        "coef": -238.9,
        "se": 40.0,
        "N": 74,
        "model": {
            "target_term": "mpg",
            "estimator": "regress",
            "dependent_variable": "price",
            "vce": "ols",
            "cluster_variables": [],
            "fixed_effects": [],
        },
    }
    contract = _contract()
    command_hash = hashlib.sha256(do_file.read_bytes()).hexdigest()
    record = RunRecord(
        run_id="r1",
        semantic_input_hash=semantic_input_hash("reg price mpg", contract),
        status="succeeded",
        result_contract=contract.model_dump(),
        machine=machine,
        provenance={
            "kind": "test",
            "executor": "fake",
            "test_only": True,
            "do_file": str(do_file),
            "command_hash": command_hash,
            "contract_hash": canonical_contract_hash(contract),
            "semantic_input_hash": semantic_input_hash("reg price mpg", contract),
            "machine_hash": canonical_machine_hash(machine),
            "data_signature": "fake",
            "env_sig": {"stata_version": "fake", "stata_flavor": "fake"},
        },
    )
    report = verify_run_record(record)
    assert report.evidence_ready
    assert [item.check_id for item in report.checks] == list(CHECK_ORDER)
    assert all(item.passed for item in report.checks)
    assert json.dumps(report.model_dump(), allow_nan=False)

    failed = verify_run_record(record.model_copy(update={"result_contract": None}))
    assert not failed.evidence_ready
    assert failed.check("contract_supported").code == "contract_missing"
    assert len(failed.checks) == 12


def test_machine_hash_rejects_non_finite_values():
    with pytest.raises(ValueError):
        canonical_machine_hash({"coef": float("nan")})


def test_non_finite_machine_is_a_reported_failure_not_an_exception(tmp_path):
    contract = _contract()
    record = RunRecord(
        run_id="r-nan", status="succeeded", result_contract=contract.model_dump(),
        machine={"coef": float("nan"), "N": 10}, provenance={},
    )
    report = verify_run_record(record)
    assert not report.evidence_ready
    assert report.check("required_stats_present").code == "required_stats_missing:coef,se"


def test_verifier_reports_metadata_and_stat_failures_without_cascading_exceptions(tmp_path):
    contract = _contract()
    do_file = tmp_path / "run.do"
    do_file.write_text("reg price mpg\n", encoding="utf-8")
    machine = {
        "coef": -1.0,
        "N": 10,
        "model": {
            "target_term": "other",
            "estimator": "reghdfe",
            "dependent_variable": "income",
            "vce": "cluster",
            "cluster_variables": ["firm"],
            "fixed_effects": ["firm", "year"],
        },
    }
    record = RunRecord(
        run_id="r2", status="succeeded", semantic_input_hash=semantic_input_hash("x", contract),
        result_contract=contract.model_dump(), machine=machine,
        provenance={
            "kind": "test", "executor": "fake", "test_only": True,
            "do_file": str(do_file), "command_hash": hashlib.sha256(do_file.read_bytes()).hexdigest(),
            "semantic_input_hash": semantic_input_hash("x", contract),
            "contract_hash": canonical_contract_hash(contract),
            "machine_hash": canonical_machine_hash(machine), "data_signature": "fake",
            "env_sig": {"stata_version": "fake", "stata_flavor": "fake"},
        },
    )
    report = verify_run_record(record)
    assert not report.evidence_ready
    assert report.check("required_stats_present").code == "required_stats_missing:se"
    assert report.check("target_term_matches").code == "target_term_mismatch"
    assert report.check("estimator_matches").code == "estimator_mismatch"
    assert report.check("dependent_variable_matches").code == "dependent_variable_mismatch"
    assert report.check("vce_matches").code == "vce_mismatch"
    assert report.check("cluster_variables_match").code == "cluster_variables_mismatch"
    assert report.check("fixed_effects_match").code == "fixed_effects_mismatch"
