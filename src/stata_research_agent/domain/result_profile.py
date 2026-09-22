"""Pure qualification rules for the first registered Stata Result Profile."""

from __future__ import annotations

import hashlib
import math
import struct
from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any


class QualificationVerdict(StrEnum):
    QUALIFIED = "qualified"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class PreparedElement:
    semantic_key: str
    statistic_kind: str
    value: float
    authority: str
    locator: Mapping[str, Any]
    primitive_locators: tuple[Mapping[str, Any], ...] = ()
    derivation_profile_id: str | None = None
    derivation_parameters: Mapping[str, Any] | None = None

    @property
    def binary64_bits(self) -> str:
        return struct.pack(">d", self.value).hex()

    @property
    def decimal_text(self) -> str:
        return repr(self.value)


@dataclass(frozen=True, slots=True)
class RegressProfileEvaluation:
    verdict: QualificationVerdict
    findings: tuple[str, ...]
    elements: tuple[PreparedElement, ...]
    sample: Mapping[str, Any] | None


class RegressResultProfile:
    command_family = "regress"
    profile_id = "linear_model.regress.v1"
    version = 1
    snapshot_schema_version = "stata.regress.snapshot/v1"
    extractor_implementation_hash = (
        "e23ec60a1ed6dda223fe8729d3f7a89a39706f506914b04f309f605264dd89f8"
    )

    def evaluate(
        self,
        structured: Mapping[str, Any] | None,
        *,
        expected_dependent_variable: str,
        expected_terms: tuple[str, ...],
    ) -> RegressProfileEvaluation:
        if structured is None:
            return self._unknown("STRUCTURED_RESULT_MISSING")
        capability = structured.get("result_profile_capability")
        if not isinstance(capability, Mapping) or (
            capability.get("result_profile_id") != self.profile_id
            or capability.get("profile_version") != self.version
            or capability.get("snapshot_schema_version") != self.snapshot_schema_version
            or capability.get("extractor_contract_hash") != self.extractor_implementation_hash
        ):
            return self._unknown("RESULT_PROFILE_CAPABILITY_MISMATCH")
        command = structured.get("cmd")
        if command != "regress":
            return RegressProfileEvaluation(
                QualificationVerdict.REJECTED,
                ("COMMAND_FAMILY_MISMATCH",),
                (),
                None,
            )
        if structured.get("depvar") != expected_dependent_variable:
            return RegressProfileEvaluation(
                QualificationVerdict.REJECTED,
                ("DEPENDENT_VARIABLE_MISMATCH",),
                (),
                None,
            )
        coefs = structured.get("coefs")
        source_map = structured.get("stored_result_source_map")
        sample = structured.get("estimation_sample_manifest")
        scalars = structured.get("scalars")
        if not isinstance(coefs, list) or not isinstance(source_map, Mapping):
            return self._unknown("SOURCE_INCOMPLETE")
        if (
            source_map.get("schema_version") != "stata.stored-result-source-map/v1alpha1"
            or source_map.get("command_type") != "regress"
        ):
            return self._unknown("SOURCE_MAP_SCHEMA_MISMATCH")
        if not isinstance(sample, Mapping) or not isinstance(scalars, Mapping):
            return self._unknown("SOURCE_INCOMPLETE")
        scalar_sources = source_map.get("scalars")
        term_sources = source_map.get("terms")
        if not isinstance(scalar_sources, Mapping) or not isinstance(term_sources, list):
            return self._unknown("SOURCE_INCOMPLETE")
        required_scalars = {"N": "e(N)", "r2": "e(r2)", "df_r": "e(df_r)"}
        for key, name in required_scalars.items():
            locator = scalar_sources.get(key)
            if not isinstance(locator, Mapping) or locator.get("name") != name:
                return self._unknown(f"REQUIRED_STAT_MISSING:{key}")

        source_by_term = {
            str(item.get("display_key")): item for item in term_sources if isinstance(item, Mapping)
        }
        coefficient_by_term = {
            str(item.get("var")): item for item in coefs if isinstance(item, Mapping)
        }
        if any(term not in coefficient_by_term for term in expected_terms):
            return RegressProfileEvaluation(
                QualificationVerdict.REJECTED,
                ("TARGET_TERM_MISSING",),
                (),
                sample,
            )
        try:
            n_value = self._finite(structured["N"])
            r2_value = self._finite(structured["r2"])
            df_r_value = self._finite(scalars["df_r"])
        except (KeyError, TypeError, ValueError):
            return self._unknown("REQUIRED_STAT_MISSING")
        mask_hex = sample.get("mask_hex")
        mask_sha256 = sample.get("mask_sha256")
        try:
            observed_mask_hash = hashlib.sha256(bytes.fromhex(str(mask_hex))).hexdigest()
        except ValueError:
            return self._unknown("ESTIMATION_SAMPLE_MANIFEST_INVALID")
        if (
            sample.get("source") != "e(sample)"
            or sample.get("included_count") != int(n_value)
            or not isinstance(mask_sha256, str)
            or mask_sha256 != observed_mask_hash
        ):
            return self._unknown("ESTIMATION_SAMPLE_COUNT_MISMATCH")

        elements: list[PreparedElement] = [
            PreparedElement(
                "model.N",
                "sample_size",
                n_value,
                "direct_stored",
                dict(scalar_sources["N"]),
            ),
            PreparedElement(
                "model.r2",
                "r_squared",
                r2_value,
                "direct_stored",
                dict(scalar_sources["r2"]),
            ),
        ]
        df_locator = dict(scalar_sources["df_r"])
        for term_name, term in coefficient_by_term.items():
            source = source_by_term.get(term_name)
            if not isinstance(source, Mapping):
                return self._unknown(f"SOURCE_INCOMPLETE:{term_name}")
            coef_locator = source.get("coefficient")
            variance_locator = source.get("variance")
            if (
                not isinstance(coef_locator, Mapping)
                or coef_locator.get("matrix") != "e(b)"
                or not isinstance(variance_locator, Mapping)
                or variance_locator.get("matrix") != "e(V)"
            ):
                return self._unknown(f"SOURCE_INCOMPLETE:{term_name}")
            try:
                values = {
                    "coefficient": self._finite(term["coef"]),
                    "se": self._finite(term["se"]),
                    "t": self._finite(term["t"]),
                    "p": self._finite(term["p"]),
                }
                ci = term["ci"]
                if not isinstance(ci, list) or len(ci) != 2:
                    raise ValueError
                values["ci95.lower"] = self._finite(ci[0])
                values["ci95.upper"] = self._finite(ci[1])
            except (KeyError, TypeError, ValueError):
                return self._unknown(f"TERM_FIELD_MISSING:{term_name}")
            elements.append(
                PreparedElement(
                    f"term.{term_name}.coefficient",
                    "coefficient",
                    values["coefficient"],
                    "direct_stored",
                    dict(coef_locator),
                )
            )
            derivations = {
                "se": ("standard_error", (dict(variance_locator),)),
                "t": (
                    "t_statistic",
                    (dict(coef_locator), dict(variance_locator), df_locator),
                ),
                "p": (
                    "p_value",
                    (dict(coef_locator), dict(variance_locator), df_locator),
                ),
                "ci95.lower": (
                    "confidence_interval",
                    (dict(coef_locator), dict(variance_locator), df_locator),
                ),
                "ci95.upper": (
                    "confidence_interval",
                    (dict(coef_locator), dict(variance_locator), df_locator),
                ),
            }
            for suffix, (profile, primitives) in derivations.items():
                elements.append(
                    PreparedElement(
                        f"term.{term_name}.{suffix}",
                        suffix.replace("ci95.", "confidence_interval_"),
                        values[suffix],
                        "trusted_stata_derived",
                        {"locator_type": "TRUSTED_STATA_DERIVATION_RECEIPT"},
                        primitives,
                        f"stata.regress.{profile}",
                        {"df_r": df_r_value, "confidence_level": 0.95},
                    )
                )
        return RegressProfileEvaluation(
            QualificationVerdict.QUALIFIED,
            (),
            tuple(elements),
            sample,
        )

    @staticmethod
    def _finite(value: Any) -> float:
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("formal finite result element is not finite")
        return number

    @staticmethod
    def _unknown(finding: str) -> RegressProfileEvaluation:
        return RegressProfileEvaluation(QualificationVerdict.UNKNOWN, (finding,), (), None)


class LogitResultProfile:
    """Qualification contract for Stata's single-equation binary logit model."""

    command_family = "logit"
    profile_id = "binary_model.logit.v1"
    version = 1
    snapshot_schema_version = "stata.logit.snapshot/v1"
    extractor_implementation_hash = (
        "9ddf205980051b2ad7a02c23b21f4f9ea56c12a1422a393a64ef3f01f4573156"
    )

    def evaluate(
        self,
        structured: Mapping[str, Any] | None,
        *,
        expected_dependent_variable: str,
        expected_terms: tuple[str, ...],
        expected_vce: str,
    ) -> RegressProfileEvaluation:
        if structured is None:
            return RegressResultProfile._unknown("STRUCTURED_RESULT_MISSING")
        capability = structured.get("result_profile_capability")
        if not isinstance(capability, Mapping) or (
            capability.get("result_profile_id") != self.profile_id
            or capability.get("profile_version") != self.version
            or capability.get("snapshot_schema_version") != self.snapshot_schema_version
            or capability.get("extractor_contract_hash") != self.extractor_implementation_hash
        ):
            return RegressResultProfile._unknown("RESULT_PROFILE_CAPABILITY_MISMATCH")
        if structured.get("cmd") != self.command_family:
            return RegressProfileEvaluation(
                QualificationVerdict.REJECTED,
                ("COMMAND_FAMILY_MISMATCH",),
                (),
                None,
            )
        if structured.get("depvar") != expected_dependent_variable:
            return RegressProfileEvaluation(
                QualificationVerdict.REJECTED,
                ("DEPENDENT_VARIABLE_MISMATCH",),
                (),
                None,
            )
        if structured.get("vce") != expected_vce:
            return RegressProfileEvaluation(
                QualificationVerdict.REJECTED,
                ("VCE_MISMATCH",),
                (),
                None,
            )
        coefs = structured.get("coefs")
        source_map = structured.get("stored_result_source_map")
        sample = structured.get("estimation_sample_manifest")
        scalars = structured.get("scalars")
        if (
            not isinstance(coefs, list)
            or not isinstance(source_map, Mapping)
            or not isinstance(sample, Mapping)
            or not isinstance(scalars, Mapping)
        ):
            return RegressResultProfile._unknown("SOURCE_INCOMPLETE")
        if (
            source_map.get("schema_version") != "stata.stored-result-source-map/v1alpha1"
            or source_map.get("command_type") != self.command_family
        ):
            return RegressResultProfile._unknown("SOURCE_MAP_SCHEMA_MISMATCH")
        scalar_sources = source_map.get("scalars")
        macro_sources = source_map.get("macros")
        term_sources = source_map.get("terms")
        if (
            not isinstance(scalar_sources, Mapping)
            or not isinstance(macro_sources, Mapping)
            or not isinstance(term_sources, list)
        ):
            return RegressResultProfile._unknown("SOURCE_INCOMPLETE")
        for key, name in {"N": "e(N)", "r2_p": "e(r2_p)"}.items():
            locator = scalar_sources.get(key)
            if not isinstance(locator, Mapping) or locator.get("name") != name:
                return RegressResultProfile._unknown(f"REQUIRED_STAT_MISSING:{key}")
        vce_source = macro_sources.get("vce")
        if not isinstance(vce_source, Mapping) or vce_source.get("name") != "e(vce)":
            return RegressResultProfile._unknown("VCE_SOURCE_MISSING")

        source_by_term = {
            str(item.get("display_key")): item for item in term_sources if isinstance(item, Mapping)
        }
        coefficient_by_term = {
            str(item.get("var")): item for item in coefs if isinstance(item, Mapping)
        }
        if any(term not in coefficient_by_term for term in expected_terms):
            return RegressProfileEvaluation(
                QualificationVerdict.REJECTED,
                ("TARGET_TERM_MISSING",),
                (),
                sample,
            )
        try:
            n_value = RegressResultProfile._finite(structured["N"])
            r2_p_value = RegressResultProfile._finite(structured["r2_p"])
        except (KeyError, TypeError, ValueError):
            return RegressResultProfile._unknown("REQUIRED_STAT_MISSING")
        try:
            observed_mask_hash = hashlib.sha256(
                bytes.fromhex(str(sample.get("mask_hex")))
            ).hexdigest()
        except ValueError:
            return RegressResultProfile._unknown("ESTIMATION_SAMPLE_MANIFEST_INVALID")
        if (
            sample.get("source") != "e(sample)"
            or sample.get("included_count") != int(n_value)
            or sample.get("mask_sha256") != observed_mask_hash
        ):
            return RegressResultProfile._unknown("ESTIMATION_SAMPLE_COUNT_MISMATCH")

        elements: list[PreparedElement] = [
            PreparedElement(
                "model.N",
                "sample_size",
                n_value,
                "direct_stored",
                dict(scalar_sources["N"]),
            ),
            PreparedElement(
                "model.r2_p",
                "pseudo_r_squared",
                r2_p_value,
                "direct_stored",
                dict(scalar_sources["r2_p"]),
            ),
        ]
        for term_name, term in coefficient_by_term.items():
            source = source_by_term.get(term_name)
            if not isinstance(source, Mapping):
                return RegressResultProfile._unknown(f"SOURCE_INCOMPLETE:{term_name}")
            coef_locator = source.get("coefficient")
            variance_locator = source.get("variance")
            if (
                not isinstance(coef_locator, Mapping)
                or coef_locator.get("matrix") != "e(b)"
                or not isinstance(variance_locator, Mapping)
                or variance_locator.get("matrix") != "e(V)"
            ):
                return RegressResultProfile._unknown(f"SOURCE_INCOMPLETE:{term_name}")
            try:
                values = {
                    "coefficient": RegressResultProfile._finite(term["coef"]),
                    "se": RegressResultProfile._finite(term["se"]),
                    "z": RegressResultProfile._finite(term["t"]),
                    "p": RegressResultProfile._finite(term["p"]),
                }
                ci = term["ci"]
                if not isinstance(ci, list) or len(ci) != 2:
                    raise ValueError
                values["ci95.lower"] = RegressResultProfile._finite(ci[0])
                values["ci95.upper"] = RegressResultProfile._finite(ci[1])
            except (KeyError, TypeError, ValueError):
                return RegressResultProfile._unknown(f"TERM_FIELD_MISSING:{term_name}")
            elements.append(
                PreparedElement(
                    f"term.{term_name}.coefficient",
                    "coefficient",
                    values["coefficient"],
                    "direct_stored",
                    dict(coef_locator),
                )
            )
            derivations = {
                "se": ("standard_error", (dict(variance_locator),)),
                "z": (
                    "z_statistic",
                    (dict(coef_locator), dict(variance_locator)),
                ),
                "p": (
                    "p_value",
                    (dict(coef_locator), dict(variance_locator)),
                ),
                "ci95.lower": (
                    "confidence_interval",
                    (dict(coef_locator), dict(variance_locator)),
                ),
                "ci95.upper": (
                    "confidence_interval",
                    (dict(coef_locator), dict(variance_locator)),
                ),
            }
            for suffix, (profile, primitives) in derivations.items():
                elements.append(
                    PreparedElement(
                        f"term.{term_name}.{suffix}",
                        suffix.replace("ci95.", "confidence_interval_"),
                        values[suffix],
                        "trusted_stata_derived",
                        {"locator_type": "TRUSTED_STATA_DERIVATION_RECEIPT"},
                        primitives,
                        f"stata.logit.{profile}",
                        {"distribution": "normal", "confidence_level": 0.95},
                    )
                )
        return RegressProfileEvaluation(QualificationVerdict.QUALIFIED, (), tuple(elements), sample)


class ReghdfeResultProfile:
    """Qualification contract for the pinned reghdfe stored-result surface."""

    profile_id = "linear_model.reghdfe.v1"
    version = 1
    command_family = "reghdfe"
    snapshot_schema_version = "stata.reghdfe.snapshot/v1"
    extractor_implementation_hash = (
        "51e0365a980a58091ee0569140526e5f28612da151eb68642de8943ad9facaf0"
    )

    def evaluate(
        self,
        structured: Mapping[str, Any] | None,
        *,
        expected_dependent_variable: str,
        expected_terms: tuple[str, ...],
        expected_absorbed_effects: tuple[str, ...],
        expected_cluster_variables: tuple[str, ...],
        expected_vce: str,
    ) -> RegressProfileEvaluation:
        if structured is None:
            return RegressResultProfile._unknown("STRUCTURED_RESULT_MISSING")
        capability = structured.get("result_profile_capability")
        if not isinstance(capability, Mapping) or (
            capability.get("result_profile_id") != self.profile_id
            or capability.get("profile_version") != self.version
            or capability.get("snapshot_schema_version") != self.snapshot_schema_version
            or capability.get("extractor_contract_hash") != self.extractor_implementation_hash
        ):
            return RegressResultProfile._unknown("RESULT_PROFILE_CAPABILITY_MISMATCH")
        if structured.get("cmd") != self.command_family:
            return RegressProfileEvaluation(
                QualificationVerdict.REJECTED,
                ("COMMAND_FAMILY_MISMATCH",),
                (),
                None,
            )
        absorbed = structured.get("absorbed_effects")
        clusters = structured.get("cluster_variables")
        environment = structured.get("profile_environment")
        if not isinstance(absorbed, list) or not isinstance(clusters, list):
            return RegressResultProfile._unknown("HDFE_STRUCTURE_MISSING")
        if tuple(str(value) for value in absorbed) != expected_absorbed_effects:
            return RegressProfileEvaluation(
                QualificationVerdict.REJECTED,
                ("ABSORBED_EFFECTS_MISMATCH",),
                (),
                None,
            )
        if tuple(str(value) for value in clusters) != expected_cluster_variables:
            return RegressProfileEvaluation(
                QualificationVerdict.REJECTED,
                ("CLUSTER_VARIABLES_MISMATCH",),
                (),
                None,
            )
        if str(structured.get("vce", "")) != expected_vce:
            return RegressProfileEvaluation(
                QualificationVerdict.REJECTED,
                ("VCE_MISMATCH",),
                (),
                None,
            )
        if (
            not isinstance(environment, Mapping)
            or environment.get("dependency") != "reghdfe"
            or not isinstance(environment.get("path"), str)
            or not isinstance(environment.get("version"), str)
        ):
            return RegressResultProfile._unknown("PROFILE_ENVIRONMENT_MISSING")
        source_map = structured.get("stored_result_source_map")
        if not isinstance(source_map, Mapping):
            return RegressResultProfile._unknown("SOURCE_INCOMPLETE")
        scalar_sources = source_map.get("scalars")
        macro_sources = source_map.get("macros")
        if not isinstance(scalar_sources, Mapping) or not isinstance(macro_sources, Mapping):
            return RegressResultProfile._unknown("SOURCE_INCOMPLETE")
        within_locator = scalar_sources.get("r2_within")
        absorb_locator = macro_sources.get("absvars")
        cluster_locator = macro_sources.get("clustvar")
        if (
            not isinstance(within_locator, Mapping)
            or within_locator.get("name") != "e(r2_within)"
            or not isinstance(absorb_locator, Mapping)
            or absorb_locator.get("name") != "e(absvars)"
            or not isinstance(cluster_locator, Mapping)
            or cluster_locator.get("name") != "e(clustvar)"
        ):
            return RegressResultProfile._unknown("HDFE_SOURCE_MAP_MISMATCH")

        adapted_source = dict(source_map)
        adapted_source["command_type"] = "regress"
        adapted = dict(structured)
        adapted["cmd"] = "regress"
        adapted["result_profile_capability"] = {
            "result_profile_id": RegressResultProfile.profile_id,
            "profile_version": RegressResultProfile.version,
            "snapshot_schema_version": RegressResultProfile.snapshot_schema_version,
            "extractor_contract_hash": (RegressResultProfile.extractor_implementation_hash),
        }
        adapted["stored_result_source_map"] = adapted_source
        base = RegressResultProfile().evaluate(
            adapted,
            expected_dependent_variable=expected_dependent_variable,
            expected_terms=expected_terms,
        )
        if base.verdict is not QualificationVerdict.QUALIFIED:
            return base
        try:
            within_value = RegressResultProfile._finite(structured["r2_within"])
        except (KeyError, TypeError, ValueError):
            return RegressResultProfile._unknown("REQUIRED_STAT_MISSING:r2_within")
        elements = tuple(
            replace(
                element,
                derivation_profile_id=(
                    element.derivation_profile_id.replace("stata.regress.", "stata.reghdfe.")
                    if element.derivation_profile_id is not None
                    else None
                ),
            )
            for element in base.elements
        ) + (
            PreparedElement(
                "model.r2_within",
                "within_r_squared",
                within_value,
                "direct_stored",
                dict(within_locator),
            ),
        )
        return RegressProfileEvaluation(
            QualificationVerdict.QUALIFIED,
            (),
            elements,
            base.sample,
        )


class Ivregress2slsResultProfile:
    profile_id = "linear_model.ivregress_2sls.v1"
    version = 1
    command_family = "ivregress"
    snapshot_schema_version = "stata.ivregress-2sls.snapshot/v1"
    extractor_implementation_hash = (
        "74bd652b4211599bcb01ef55fe4cd2db1555c4d1da7eebf70a9f3820ea10fac6"
    )

    def evaluate(
        self,
        structured: Mapping[str, Any] | None,
        *,
        expected_dependent_variable: str,
        expected_terms: tuple[str, ...],
        expected_endogenous_variables: tuple[str, ...],
        expected_included_exogenous_variables: tuple[str, ...],
        expected_excluded_instruments: tuple[str, ...],
        expected_vce: str,
    ) -> RegressProfileEvaluation:
        if structured is None:
            return RegressResultProfile._unknown("STRUCTURED_RESULT_MISSING")
        capability = structured.get("result_profile_capability")
        if not isinstance(capability, Mapping) or (
            capability.get("result_profile_id") != self.profile_id
            or capability.get("profile_version") != self.version
            or capability.get("snapshot_schema_version") != self.snapshot_schema_version
            or capability.get("extractor_contract_hash") != self.extractor_implementation_hash
        ):
            return RegressResultProfile._unknown("RESULT_PROFILE_CAPABILITY_MISMATCH")
        if structured.get("cmd") != self.command_family:
            return RegressProfileEvaluation(
                QualificationVerdict.REJECTED,
                ("COMMAND_FAMILY_MISMATCH",),
                (),
                None,
            )
        if structured.get("depvar") != expected_dependent_variable:
            return RegressProfileEvaluation(
                QualificationVerdict.REJECTED,
                ("DEPENDENT_VARIABLE_MISMATCH",),
                (),
                None,
            )
        expected_lists = (
            ("endogenous_variables", expected_endogenous_variables),
            (
                "included_exogenous_variables",
                expected_included_exogenous_variables,
            ),
            ("excluded_instruments", expected_excluded_instruments),
        )
        for key, expected in expected_lists:
            observed = structured.get(key)
            if not isinstance(observed, list) or tuple(map(str, observed)) != expected:
                return RegressProfileEvaluation(
                    QualificationVerdict.REJECTED,
                    (f"{key.upper()}_MISMATCH",),
                    (),
                    None,
                )
        if structured.get("iv_estimator") != "2sls":
            return RegressProfileEvaluation(
                QualificationVerdict.REJECTED,
                ("IV_ESTIMATOR_MISMATCH",),
                (),
                None,
            )
        if str(structured.get("vce", "")) != expected_vce:
            return RegressProfileEvaluation(
                QualificationVerdict.REJECTED,
                ("VCE_MISMATCH",),
                (),
                None,
            )
        coefs = structured.get("coefs")
        source_map = structured.get("stored_result_source_map")
        sample = structured.get("estimation_sample_manifest")
        scalars = structured.get("scalars")
        first_stage = structured.get("first_stage")
        if (
            not isinstance(coefs, list)
            or not isinstance(source_map, Mapping)
            or not isinstance(sample, Mapping)
            or not isinstance(scalars, Mapping)
            or not isinstance(first_stage, list)
        ):
            return RegressResultProfile._unknown("SOURCE_INCOMPLETE")
        if (
            source_map.get("schema_version") != "stata.stored-result-source-map/v1alpha1"
            or source_map.get("command_type") != self.command_family
        ):
            return RegressResultProfile._unknown("SOURCE_MAP_SCHEMA_MISMATCH")
        scalar_sources = source_map.get("scalars")
        macro_sources = source_map.get("macros")
        term_sources = source_map.get("terms")
        if (
            not isinstance(scalar_sources, Mapping)
            or not isinstance(macro_sources, Mapping)
            or not isinstance(term_sources, list)
        ):
            return RegressResultProfile._unknown("SOURCE_INCOMPLETE")
        for key, name in {"N": "e(N)", "r2": "e(r2)"}.items():
            locator = scalar_sources.get(key)
            if not isinstance(locator, Mapping) or locator.get("name") != name:
                return RegressResultProfile._unknown(f"REQUIRED_STAT_MISSING:{key}")
        for key, name in {
            "estimator": "e(estimator)",
            "endog": "e(endog)",
            "exog": "e(exog)",
            "exogr": "e(exogr)",
        }.items():
            locator = macro_sources.get(key)
            if not isinstance(locator, Mapping) or locator.get("name") != name:
                return RegressResultProfile._unknown(f"IV_SOURCE_MISSING:{key}")
        try:
            n_value = RegressResultProfile._finite(structured["N"])
            r2_value = RegressResultProfile._finite(structured["r2"])
        except (KeyError, TypeError, ValueError):
            return RegressResultProfile._unknown("REQUIRED_STAT_MISSING")
        try:
            observed_mask_hash = hashlib.sha256(
                bytes.fromhex(str(sample.get("mask_hex")))
            ).hexdigest()
        except ValueError:
            return RegressResultProfile._unknown("ESTIMATION_SAMPLE_MANIFEST_INVALID")
        if (
            sample.get("source") != "e(sample)"
            or sample.get("included_count") != int(n_value)
            or sample.get("mask_sha256") != observed_mask_hash
        ):
            return RegressResultProfile._unknown("ESTIMATION_SAMPLE_COUNT_MISMATCH")

        source_by_term = {
            str(item.get("display_key")): item for item in term_sources if isinstance(item, Mapping)
        }
        coefficient_by_term = {
            str(item.get("var")): item for item in coefs if isinstance(item, Mapping)
        }
        if any(term not in coefficient_by_term for term in expected_terms):
            return RegressProfileEvaluation(
                QualificationVerdict.REJECTED,
                ("TARGET_TERM_MISSING",),
                (),
                sample,
            )
        elements: list[PreparedElement] = [
            PreparedElement(
                "model.N", "sample_size", n_value, "direct_stored", dict(scalar_sources["N"])
            ),
            PreparedElement(
                "model.r2", "r_squared", r2_value, "direct_stored", dict(scalar_sources["r2"])
            ),
        ]
        for term_name, term in coefficient_by_term.items():
            source = source_by_term.get(term_name)
            if not isinstance(source, Mapping):
                return RegressResultProfile._unknown(f"SOURCE_INCOMPLETE:{term_name}")
            coef_locator = source.get("coefficient")
            variance_locator = source.get("variance")
            if not isinstance(coef_locator, Mapping) or not isinstance(variance_locator, Mapping):
                return RegressResultProfile._unknown(f"SOURCE_INCOMPLETE:{term_name}")
            try:
                ci = term["ci"]
                if not isinstance(ci, list) or len(ci) != 2:
                    raise ValueError
                values = {
                    "coefficient": RegressResultProfile._finite(term["coef"]),
                    "se": RegressResultProfile._finite(term["se"]),
                    "z": RegressResultProfile._finite(term["t"]),
                    "p": RegressResultProfile._finite(term["p"]),
                    "ci95.lower": RegressResultProfile._finite(ci[0]),
                    "ci95.upper": RegressResultProfile._finite(ci[1]),
                }
            except (KeyError, TypeError, ValueError):
                return RegressResultProfile._unknown(f"TERM_FIELD_MISSING:{term_name}")
            elements.append(
                PreparedElement(
                    f"term.{term_name}.coefficient",
                    "coefficient",
                    values["coefficient"],
                    "direct_stored",
                    dict(coef_locator),
                )
            )
            derivations = {
                "se": "standard_error",
                "z": "z_statistic",
                "p": "p_value",
                "ci95.lower": "confidence_interval",
                "ci95.upper": "confidence_interval",
            }
            for suffix, derivation in derivations.items():
                semantic_suffix = "t" if suffix == "z" else suffix
                elements.append(
                    PreparedElement(
                        f"term.{term_name}.{semantic_suffix}",
                        suffix.replace("ci95.", "confidence_interval_"),
                        values[suffix],
                        "trusted_stata_derived",
                        {"locator_type": "TRUSTED_STATA_DERIVATION_RECEIPT"},
                        (dict(coef_locator), dict(variance_locator)),
                        f"stata.ivregress_2sls.{derivation}",
                        {"distribution": "normal", "confidence_level": 0.95},
                    )
                )

        stages = {
            str(item.get("endogenous_variable")): item
            for item in first_stage
            if isinstance(item, Mapping)
        }
        if set(stages) != set(expected_endogenous_variables):
            return RegressResultProfile._unknown("FIRST_STAGE_SET_MISMATCH")
        for variable in expected_endogenous_variables:
            stage = stages[variable]
            statistics = stage.get("statistics")
            locator = stage.get("source")
            if not isinstance(statistics, Mapping) or not isinstance(locator, Mapping):
                return RegressResultProfile._unknown("FIRST_STAGE_SOURCE_MISSING")
            for key, kind in (
                ("partial_r2", "partial_r_squared"),
                ("f_statistic", "f_statistic"),
                ("p_value", "p_value"),
            ):
                try:
                    value = RegressResultProfile._finite(statistics[key])
                except (KeyError, TypeError, ValueError):
                    return RegressResultProfile._unknown(
                        f"FIRST_STAGE_STAT_MISSING:{variable}:{key}"
                    )
                elements.append(
                    PreparedElement(
                        f"iv.first_stage.{variable}.{key}",
                        kind,
                        value,
                        "direct_stored",
                        {
                            **dict(locator),
                            "locator_type": "R_MATRIX_CELL",
                            "column_semantic": key,
                        },
                    )
                )
        return RegressProfileEvaluation(QualificationVerdict.QUALIFIED, (), tuple(elements), sample)


class GenericStataResultProfile:
    """Method-neutral promotion of Agent-selected values returned by Stata.

    The profile understands only the generic MCP source catalog.  It deliberately does not
    branch on ``cmd`` or interpret estimator semantics.
    """

    profile_id = "stata.generic-result.v1"
    version = 1
    command_family = "stata-returned-result"
    snapshot_schema_version = "stata.generic-result.snapshot/v1"
    extractor_implementation_hash = (
        "320d520badfe130782be6f85832f8e797687259d671172e49a2a5d12ed62d7e0"
    )

    def evaluate(
        self,
        structured: Mapping[str, Any] | None,
        *,
        selected_source_keys: tuple[str, ...],
    ) -> RegressProfileEvaluation:
        if structured is None:
            return RegressResultProfile._unknown("STRUCTURED_RESULT_MISSING")
        capability = structured.get("result_source_capability")
        if not isinstance(capability, Mapping) or (
            capability.get("capability_id") != "stata.generic-result-source.v1"
            or capability.get("capability_version") != self.version
            or capability.get("snapshot_schema_version") != self.snapshot_schema_version
            or capability.get("extractor_contract_hash") != self.extractor_implementation_hash
        ):
            return RegressResultProfile._unknown("GENERIC_RESULT_SOURCE_CAPABILITY_MISMATCH")
        catalog = structured.get("result_catalog")
        if not isinstance(catalog, Mapping) or (
            catalog.get("schema_version") != "stata.result-catalog/v1"
        ):
            return RegressResultProfile._unknown("RESULT_CATALOG_MISSING")
        raw_elements = catalog.get("elements")
        if not isinstance(raw_elements, list):
            return RegressResultProfile._unknown("RESULT_CATALOG_MISSING")
        by_key: dict[str, Mapping[str, Any]] = {}
        for item in raw_elements:
            if not isinstance(item, Mapping):
                return RegressResultProfile._unknown("RESULT_CATALOG_INVALID")
            source_key = item.get("source_key")
            if not isinstance(source_key, str) or not source_key or source_key in by_key:
                return RegressResultProfile._unknown("RESULT_CATALOG_INVALID")
            by_key[source_key] = item
        if not selected_source_keys or len(selected_source_keys) != len(set(selected_source_keys)):
            return RegressProfileEvaluation(
                QualificationVerdict.REJECTED,
                ("RESULT_SELECTION_INVALID",),
                (),
                None,
            )
        missing = [key for key in selected_source_keys if key not in by_key]
        if missing:
            return RegressProfileEvaluation(
                QualificationVerdict.REJECTED,
                tuple(f"RESULT_SOURCE_KEY_MISSING:{key}" for key in missing),
                (),
                None,
            )

        prepared: list[PreparedElement] = []
        for source_key in selected_source_keys:
            item = by_key[source_key]
            locator = item.get("locator")
            primitives = item.get("primitive_locators", [])
            statistic_kind = item.get("statistic_kind")
            if (
                not isinstance(locator, Mapping)
                or not isinstance(primitives, list)
                or not all(isinstance(value, Mapping) for value in primitives)
                or not isinstance(statistic_kind, str)
                or not statistic_kind
            ):
                return RegressResultProfile._unknown(f"RESULT_SOURCE_INVALID:{source_key}")
            try:
                value = RegressResultProfile._finite(item["value"])
            except (KeyError, TypeError, ValueError):
                return RegressResultProfile._unknown(f"RESULT_SOURCE_INVALID:{source_key}")
            derived = str(locator.get("locator_type", "")).lower() == (
                "trusted_stata_derivation_receipt"
            )
            prepared.append(
                PreparedElement(
                    source_key,
                    statistic_kind,
                    value,
                    "trusted_stata_derived" if derived else "direct_stored",
                    dict(locator),
                    tuple(dict(primitive) for primitive in primitives),
                    "stata.generic-returned-value.v1" if derived else None,
                    {"source_key": source_key} if derived else None,
                )
            )

        sample = structured.get("estimation_sample_manifest")
        if sample is not None and not isinstance(sample, Mapping):
            return RegressResultProfile._unknown("ESTIMATION_SAMPLE_MANIFEST_INVALID")
        return RegressProfileEvaluation(
            QualificationVerdict.QUALIFIED,
            (),
            tuple(prepared),
            sample,
        )


RegisteredResultProfile = (
    RegressResultProfile
    | LogitResultProfile
    | ReghdfeResultProfile
    | Ivregress2slsResultProfile
    | GenericStataResultProfile
)
