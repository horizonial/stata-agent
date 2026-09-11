"""Deterministic, fail-closed verification for coefficient result runs.

The verifier is deliberately independent from the provider, Stata transport and
the event store.  It consumes the canonical :class:`RunRecord` projection and
returns a stable report which can be used by both the toolkit and the evidence
signer.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator, model_validator

from ..domain.models import RunRecord

RESULT_VERIFICATION_SCHEMA_VERSION = 1
SUPPORTED_ESTIMATORS = frozenset({"regress", "reghdfe"})
SUPPORTED_VCE = frozenset({"ols", "robust", "cluster"})
SUPPORTED_STATS = frozenset({"coef", "se", "N", "r2"})

# This order is part of the public audit contract.  Do not reorder without a
# schema-version change and corresponding migration/compatibility work.
CHECK_ORDER = (
    "run_exists",
    "run_succeeded",
    "contract_supported",
    "provenance_trusted",
    "artifact_hash_matches",
    "required_stats_present",
    "target_term_matches",
    "estimator_matches",
    "dependent_variable_matches",
    "vce_matches",
    "cluster_variables_match",
    "fixed_effects_match",
)

_TERM_RE = re.compile(r"^[A-Za-z0-9_]+(?:[.#][A-Za-z0-9_]+)*$")
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ResultContract(BaseModel):
    """Versioned declaration of the model result a run is allowed to report."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: StrictInt = 1
    target_term: StrictStr
    estimator: StrictStr
    dependent_variable: StrictStr
    vce: StrictStr = "ols"
    cluster_variables: list[StrictStr] = Field(default_factory=list)
    fixed_effects: list[StrictStr] = Field(default_factory=list)
    required_stats: list[StrictStr] = Field(default_factory=lambda: ["coef", "N"])

    @field_validator("target_term")
    @classmethod
    def _target_term(cls, value: str) -> str:
        value = value.strip()
        if not value or len(value) > 256 or not _TERM_RE.fullmatch(value):
            raise ValueError("target_term 必须是无空白的保守 Stata term")
        return value

    @field_validator("estimator", "vce")
    @classmethod
    def _lower_token(cls, value: str) -> str:
        value = value.strip().lower()
        if not value:
            raise ValueError("字段不能为空")
        return value

    @field_validator("dependent_variable")
    @classmethod
    def _dependent_variable(cls, value: str) -> str:
        value = value.strip()
        if not value or len(value) > 128 or not _NAME_RE.fullmatch(value):
            raise ValueError("dependent_variable 必须是 Stata 变量名")
        return value

    @field_validator("cluster_variables", "fixed_effects")
    @classmethod
    def _names(cls, values: list[str]) -> list[str]:
        if len(values) > 64:
            raise ValueError("变量列表过长")
        cleaned: list[str] = []
        for value in values:
            if not isinstance(value, str):
                raise ValueError("变量列表只能包含字符串")
            value = value.strip()
            if not value or not _NAME_RE.fullmatch(value):
                raise ValueError("变量列表含非法 Stata 变量名")
            if value in cleaned:
                raise ValueError("变量列表不能重复")
            cleaned.append(value)
        return cleaned

    @field_validator("required_stats")
    @classmethod
    def _stats(cls, values: list[str]) -> list[str]:
        if not values:
            raise ValueError("required_stats 不能为空")
        cleaned: list[str] = []
        for value in values:
            if not isinstance(value, str) or value not in SUPPORTED_STATS:
                raise ValueError(f"不支持的 required stat: {value!r}")
            if value in cleaned:
                raise ValueError("required_stats 不能重复")
            cleaned.append(value)
        if "coef" not in cleaned or "N" not in cleaned:
            raise ValueError("required_stats 至少需要 coef 和 N")
        return cleaned

    @field_validator("schema_version")
    @classmethod
    def _schema(cls, value: int) -> int:
        if value != RESULT_VERIFICATION_SCHEMA_VERSION:
            raise ValueError(f"不支持的 result contract schema_version: {value!r}")
        return value

    @field_validator("estimator")
    @classmethod
    def _estimator(cls, value: str) -> str:
        if value not in SUPPORTED_ESTIMATORS:
            raise ValueError(f"不支持的 estimator: {value!r}")
        return value

    @field_validator("vce")
    @classmethod
    def _vce(cls, value: str) -> str:
        if value not in SUPPORTED_VCE:
            raise ValueError(f"不支持的 vce: {value!r}")
        return value

    @model_validator(mode="after")
    def _cross_fields(self) -> "ResultContract":
        if self.vce == "cluster" and not self.cluster_variables:
            raise ValueError("cluster VCE 必须声明 cluster_variables")
        if self.vce != "cluster" and self.cluster_variables:
            raise ValueError("非 cluster VCE 不得声明 cluster_variables")
        if self.estimator == "regress" and self.fixed_effects:
            raise ValueError("regress 不支持 fixed_effects；请使用 reghdfe")
        return self


class CheckResult(BaseModel):
    """One stable verification decision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    check_id: str
    passed: bool
    code: str
    detail: str = ""

    @property
    def ok(self) -> bool:
        """Compatibility alias useful to callers reading a report."""

        return self.passed

    @property
    def name(self) -> str:
        return self.check_id

    @property
    def status(self) -> str:
        return "passed" if self.passed else "failed"

    @property
    def message(self) -> str:
        return self.detail


class VerificationReport(BaseModel):
    """Versioned, safe-to-log result verification output."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = RESULT_VERIFICATION_SCHEMA_VERSION
    evidence_ready: bool = False
    checks: list[CheckResult] = Field(default_factory=list)
    contract_hash: str | None = None
    machine_hash: str | None = None

    @property
    def ok(self) -> bool:
        return self.evidence_ready

    @property
    def failed_codes(self) -> list[str]:
        return [check.code for check in self.checks if not check.passed]

    def check(self, check_id: str) -> CheckResult:
        for item in self.checks:
            if item.check_id == check_id:
                return item
        raise KeyError(check_id)

    def as_dict(self) -> dict[str, Any]:
        """Stable tool-facing representation (without raw code/output)."""

        return self.model_dump(mode="json")


def _canonical(value: Any) -> str:
    # Keep the historical ledger/writer JSON representation (sorted keys and
    # UTF-8, with the standard separators) so existing machine_hash values
    # remain consumable without a migration.
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def canonical_contract(contract: ResultContract | Mapping[str, Any]) -> dict[str, Any]:
    parsed = contract if isinstance(contract, ResultContract) else ResultContract.model_validate(contract)
    data = parsed.model_dump(mode="json")
    # Cluster and FE lists describe sets.  Sorting makes semantically identical
    # contracts hash identically while retaining the original model for display.
    data["cluster_variables"] = sorted(data["cluster_variables"])
    data["fixed_effects"] = sorted(data["fixed_effects"])
    data["required_stats"] = sorted(data["required_stats"], key=lambda x: (x != "N", x))
    return data


def canonical_contract_hash(contract: ResultContract | Mapping[str, Any]) -> str:
    return canonical_hash(canonical_contract(contract))


def canonical_machine_hash(machine: Mapping[str, Any]) -> str:
    return canonical_hash(dict(machine))


def _safe_machine_hash(machine: Mapping[str, Any]) -> str | None:
    try:
        return canonical_machine_hash(machine)
    except (TypeError, ValueError):
        return None


def semantic_input_hash(code: str, contract: ResultContract | Mapping[str, Any]) -> str:
    """Hash the user code and canonical contract, excluding executor markers."""

    return canonical_hash({"code": code, "result_contract": canonical_contract(contract)})


def validate_contract(contract: ResultContract | Mapping[str, Any] | None) -> ResultContract | None:
    if contract is None:
        return None
    parsed = contract if isinstance(contract, ResultContract) else ResultContract.model_validate(contract)
    return parsed


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _tokens(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [item for item in re.split(r"[\s,]+", value.strip()) if item]
    return []


def _metadata(machine: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in ("model", "metadata"):
        nested = machine.get(key)
        if isinstance(nested, Mapping):
            out.update(nested)
    for key in (
        "target_term", "term", "estimator", "cmd", "dependent_variable", "depvar",
        "vce", "cluster_variables", "clustvar", "fixed_effects", "absvars",
    ):
        if key in machine:
            out[key] = machine[key]
    return out


def trusted_provenance_kind(provenance: Mapping[str, Any] | None) -> str:
    """Validate the executor attestation shape and return ``real``/``test``.

    This function intentionally does not trust a caller-supplied boolean.  The
    do-file path and command hash are checked separately by the artifact check.
    """

    if not isinstance(provenance, Mapping):
        raise ValueError("provenance_missing")
    kind = provenance.get("kind")
    if not isinstance(provenance.get("command_hash"), str) or not provenance["command_hash"]:
        raise ValueError("command_hash_missing")
    if not isinstance(provenance.get("do_file"), str) or not provenance["do_file"]:
        raise ValueError("do_file_missing")
    if "data_signature" not in provenance:
        raise ValueError("data_signature_missing")
    env_sig = provenance.get("env_sig")
    if not isinstance(env_sig, Mapping) or not env_sig:
        raise ValueError("env_sig_missing")
    if kind == "real":
        if provenance.get("executor") != "stata-mcp" or provenance.get("attested") is not True:
            raise ValueError("real_attestation_missing")
        if not {"stata_version", "stata_flavor"}.issubset(env_sig):
            raise ValueError("env_sig_incomplete")
    elif kind == "test":
        if provenance.get("executor") != "fake" or provenance.get("test_only") is not True:
            raise ValueError("test_attestation_missing")
    else:
        raise ValueError("provenance_kind_unknown")
    return str(kind)


def _check(check_id: str, passed: bool, code: str, detail: str = "") -> CheckResult:
    return CheckResult(check_id=check_id, passed=passed, code=code, detail=detail[:240])


def verify_run_record(
    record: RunRecord | None,
    *,
    expected_contract: ResultContract | Mapping[str, Any] | None = None,
) -> VerificationReport:
    """Run all fixed checks against one canonical run projection."""

    checks: list[CheckResult] = []
    exists = record is not None
    checks.append(_check("run_exists", exists, "ok" if exists else "run_missing"))
    succeeded = bool(record is not None and record.status == "succeeded")
    checks.append(_check("run_succeeded", succeeded, "ok" if succeeded else "run_not_succeeded"))

    contract: ResultContract | None = None
    contract_error = "contract_missing"
    contract_hash: str | None = None
    if record is not None and record.result_contract is not None:
        try:
            contract = validate_contract(record.result_contract)
            assert contract is not None
            contract_hash = canonical_contract_hash(contract)
            if expected_contract is not None and canonical_contract_hash(expected_contract) != contract_hash:
                contract = None
                contract_error = "contract_argument_mismatch"
            else:
                contract_error = "ok"
        except Exception as exc:  # validation output must remain deterministic/safe
            contract_error = f"contract_invalid:{type(exc).__name__}"
    checks.append(_check("contract_supported", contract is not None, contract_error))

    provenance = record.provenance if record is not None else {}
    provenance_error = "ok"
    provenance_kind: str | None = None
    try:
        provenance_kind = trusted_provenance_kind(provenance)
        if contract is not None:
            if provenance.get("contract_hash") != contract_hash:
                raise ValueError("contract_hash_mismatch")
            expected_machine_hash = _safe_machine_hash(record.machine)  # type: ignore[union-attr]
            if expected_machine_hash is None:
                raise ValueError("machine_hash_invalid")
            if provenance.get("machine_hash") != expected_machine_hash:
                raise ValueError("machine_hash_mismatch")
            if provenance.get("semantic_input_hash") != record.semantic_input_hash:  # type: ignore[union-attr]
                raise ValueError("semantic_hash_mismatch")
    except Exception as exc:
        provenance_error = str(exc) or "provenance_invalid"
    checks.append(_check("provenance_trusted", provenance_kind is not None and provenance_error == "ok", provenance_error))

    artifact_ok = False
    artifact_code = "artifact_unavailable"
    if record is not None:
        path = provenance.get("do_file")
        if isinstance(path, str) and Path(path).is_file():
            try:
                digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
                artifact_ok = digest == provenance.get("command_hash")
                artifact_code = "ok" if artifact_ok else "command_hash_mismatch"
            except OSError:
                artifact_code = "artifact_read_error"
        elif isinstance(path, str) and path.startswith("fake:"):
            artifact_code = "fake_artifact_missing"
    checks.append(_check("artifact_hash_matches", artifact_ok, artifact_code))

    machine = record.machine if record is not None and isinstance(record.machine, Mapping) else {}
    required_ok = False
    required_code = "contract_missing"
    if contract is not None:
        missing = [stat for stat in contract.required_stats if not _finite(machine.get(stat))]
        required_ok = not missing
        required_code = "ok" if required_ok else "required_stats_missing:" + ",".join(missing)
    checks.append(_check("required_stats_present", required_ok, required_code))

    meta = _metadata(machine)
    target = meta.get("target_term", meta.get("term"))
    target_ok = contract is not None and isinstance(target, str) and target.strip() == contract.target_term
    checks.append(_check("target_term_matches", target_ok, "ok" if target_ok else "target_term_mismatch"))

    actual_estimator = meta.get("estimator", meta.get("cmd"))
    estimator_ok = contract is not None and isinstance(actual_estimator, str) and actual_estimator.strip().lower() == contract.estimator
    checks.append(_check("estimator_matches", estimator_ok, "ok" if estimator_ok else "estimator_mismatch"))

    actual_depvar = meta.get("dependent_variable", meta.get("depvar"))
    depvar_ok = contract is not None and isinstance(actual_depvar, str) and actual_depvar.strip() == contract.dependent_variable
    checks.append(_check("dependent_variable_matches", depvar_ok, "ok" if depvar_ok else "dependent_variable_mismatch"))

    actual_vce = meta.get("vce")
    vce_ok = contract is not None and isinstance(actual_vce, str) and actual_vce.strip().lower() == contract.vce
    checks.append(_check("vce_matches", vce_ok, "ok" if vce_ok else "vce_mismatch"))

    actual_clusters = sorted(_tokens(meta.get("cluster_variables", meta.get("clustvar"))))
    cluster_ok = contract is not None and actual_clusters == sorted(contract.cluster_variables)
    checks.append(_check("cluster_variables_match", cluster_ok, "ok" if cluster_ok else "cluster_variables_mismatch"))

    actual_fe = sorted(_tokens(meta.get("fixed_effects", meta.get("absvars"))))
    fe_ok = contract is not None and actual_fe == sorted(contract.fixed_effects)
    checks.append(_check("fixed_effects_match", fe_ok, "ok" if fe_ok else "fixed_effects_mismatch"))

    ready = all(item.passed for item in checks)
    return VerificationReport(
        evidence_ready=ready,
        checks=checks,
        contract_hash=contract_hash,
        machine_hash=_safe_machine_hash(machine) if machine else None,
    )


# Short aliases for callers that prefer the verb-first spelling.
verify_result = verify_run_record

__all__ = [
    "CHECK_ORDER",
    "CheckResult",
    "RESULT_VERIFICATION_SCHEMA_VERSION",
    "ResultContract",
    "SUPPORTED_ESTIMATORS",
    "SUPPORTED_STATS",
    "SUPPORTED_VCE",
    "VerificationReport",
    "canonical_contract",
    "canonical_contract_hash",
    "canonical_hash",
    "canonical_machine_hash",
    "semantic_input_hash",
    "trusted_provenance_kind",
    "validate_contract",
    "verify_result",
    "verify_run_record",
]
