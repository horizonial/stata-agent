"""Application orchestration for the first formal Stata Result Profile."""

from __future__ import annotations

from stata_research_agent.domain.identifiers import (
    CommandId,
    EnvironmentSnapshotId,
    EstimationSampleManifestId,
    ResearchCommandInstanceId,
    ResultCandidateId,
    ResultCapturePointId,
    ResultCaptureSnapshotId,
    ResultContractId,
    ResultElementId,
    ResultId,
    ResultQualificationReportId,
    ResultSourceLocatorId,
    RunId,
    TrustedDerivationReceiptId,
)
from stata_research_agent.domain.result_profile import (
    GenericStataResultProfile,
    Ivregress2slsResultProfile,
    LogitResultProfile,
    QualificationVerdict,
    ReghdfeResultProfile,
    RegisteredResultProfile,
    RegressProfileEvaluation,
    RegressResultProfile,
)

from .ports.identity import IdentityGenerator
from .ports.result_profile import ResultProfileRepository
from .result_profile import (
    PreparedElementIdentity,
    PromoteStataResultCommand,
    QualifyRegressResultCommand,
    RegisterGenericStataResultProfileCommand,
    RegisterRegressProfileCommand,
    RegressOperationFacts,
    ResultPromotionIdentity,
    ResultQualificationOutcome,
)


class RegisteredResultProfileService:
    def __init__(
        self,
        repository: ResultProfileRepository,
        identities: IdentityGenerator,
        profiles: tuple[RegisteredResultProfile, ...] | None = None,
    ) -> None:
        self._repository = repository
        self._identities = identities
        self._generic_profile = GenericStataResultProfile()
        registered = profiles or (
            RegressResultProfile(),
            LogitResultProfile(),
            ReghdfeResultProfile(),
            Ivregress2slsResultProfile(),
        )
        self._profiles = {profile.command_family: profile for profile in registered}

    def register_generic_profile(self, command: RegisterGenericStataResultProfileCommand) -> None:
        self._repository.register_profile(command, self._generic_profile)

    def promote(self, command: PromoteStataResultCommand) -> ResultQualificationOutcome:
        facts = self._repository.load_operation(command.operation_id.value)
        evaluation = self._generic_profile.evaluate(
            facts.structured,
            selected_source_keys=command.selected_source_keys,
        )
        evaluation = self._apply_authority_gates(facts, evaluation)
        identities = self._promotion_identities(evaluation)
        return self._repository.commit_qualification(
            command, self._generic_profile, facts, evaluation, identities
        )

    def register_builtin_profile(self, command: RegisterRegressProfileCommand) -> None:
        for index, profile in enumerate(self._profiles.values()):
            profile_command = (
                command
                if index == 0
                else RegisterRegressProfileCommand(
                    self._identities.new(CommandId), command.requested_by_turn_id
                )
            )
            self._repository.register_profile(profile_command, profile)

    def qualify(self, command: QualifyRegressResultCommand) -> ResultQualificationOutcome:
        profile = self._profiles.get(command.estimator)
        if profile is None:
            raise ValueError("Result Profile is not registered")
        facts = self._repository.load_operation(command.operation_id.value)
        if isinstance(profile, LogitResultProfile):
            evaluation = profile.evaluate(
                facts.structured,
                expected_dependent_variable=command.expected_dependent_variable,
                expected_terms=command.expected_terms,
                expected_vce=command.expected_vce,
            )
        elif isinstance(profile, ReghdfeResultProfile):
            evaluation = profile.evaluate(
                facts.structured,
                expected_dependent_variable=command.expected_dependent_variable,
                expected_terms=command.expected_terms,
                expected_absorbed_effects=command.expected_absorbed_effects,
                expected_cluster_variables=command.expected_cluster_variables,
                expected_vce=command.expected_vce,
            )
        elif isinstance(profile, Ivregress2slsResultProfile):
            evaluation = profile.evaluate(
                facts.structured,
                expected_dependent_variable=command.expected_dependent_variable,
                expected_terms=command.expected_terms,
                expected_endogenous_variables=(command.expected_endogenous_variables),
                expected_included_exogenous_variables=(
                    command.expected_included_exogenous_variables
                ),
                expected_excluded_instruments=(command.expected_excluded_instruments),
                expected_vce=command.expected_vce,
            )
        elif isinstance(profile, RegressResultProfile):
            evaluation = profile.evaluate(
                facts.structured,
                expected_dependent_variable=command.expected_dependent_variable,
                expected_terms=command.expected_terms,
            )
        else:
            raise TypeError("generic Result Profile is not a legacy qualifier")
        evaluation = self._apply_authority_gates(facts, evaluation)
        identities = self._promotion_identities(evaluation)
        return self._repository.commit_qualification(
            command, profile, facts, evaluation, identities
        )

    def _promotion_identities(
        self, evaluation: RegressProfileEvaluation
    ) -> ResultPromotionIdentity:
        element_identities = tuple(
            PreparedElementIdentity(
                self._identities.new(ResultElementId),
                self._identities.new(ResultSourceLocatorId),
                tuple(
                    self._identities.new(ResultSourceLocatorId) for _ in element.primitive_locators
                ),
                (
                    self._identities.new(TrustedDerivationReceiptId)
                    if element.derivation_profile_id is not None
                    else None
                ),
            )
            for element in evaluation.elements
        )
        return ResultPromotionIdentity(
            self._identities.new(EnvironmentSnapshotId),
            self._identities.new(RunId),
            self._identities.new(ResearchCommandInstanceId),
            self._identities.new(ResultContractId),
            self._identities.new(ResultCapturePointId),
            self._identities.new(ResultCaptureSnapshotId),
            self._identities.new(EstimationSampleManifestId),
            self._identities.new(ResultCandidateId),
            self._identities.new(ResultQualificationReportId),
            self._identities.new(ResultId),
            element_identities,
        )

    @staticmethod
    def _apply_authority_gates(
        facts: RegressOperationFacts, evaluation: RegressProfileEvaluation
    ) -> RegressProfileEvaluation:
        if facts.structured_result_status != "complete":
            return RegressProfileEvaluation(
                QualificationVerdict.UNKNOWN,
                ("STRUCTURED_RESULT_INCOMPLETE",),
                (),
                None,
            )
        if facts.structured is not None:
            provenance = facts.structured.get("provenance")
            if (
                not isinstance(facts.structured.get("cmdline"), str)
                or not str(facts.structured["cmdline"]).strip()
                or not isinstance(provenance, dict)
                or provenance.get("command_hash") != facts.receipt.get("command_hash")
                or provenance.get("data_signature") != facts.receipt.get("data_signature")
                or provenance.get("exec_seq") != facts.receipt.get("exec_seq")
            ):
                return RegressProfileEvaluation(
                    QualificationVerdict.UNKNOWN,
                    ("EXECUTION_SOURCE_MISMATCH",),
                    (),
                    None,
                )
        runtime_environment = facts.receipt.get("runtime_environment")
        if (
            not isinstance(runtime_environment, dict)
            or str(runtime_environment.get("stata_version")) != "18"
        ):
            return RegressProfileEvaluation(
                QualificationVerdict.UNKNOWN,
                ("PROFILE_ENVIRONMENT_UNSUPPORTED",),
                (),
                None,
            )
        if not facts.input_is_currently_verified:
            return RegressProfileEvaluation(
                QualificationVerdict.UNKNOWN,
                ("FORMAL_INPUT_VERIFICATION_INVALID",),
                (),
                None,
            )
        if (
            facts.source_data_state_operation_id is None
            or not facts.expected_data_state_token
            or facts.expected_session_generation != facts.receipt.get("session_generation")
        ):
            return RegressProfileEvaluation(
                QualificationVerdict.UNKNOWN,
                ("SAMPLE_SOURCE_MISMATCH",),
                (),
                None,
            )
        if facts.execution_purpose == "formal_post_estimation":
            if (
                facts.source_execution_purpose != "formal_estimation"
                or not facts.source_has_formal_result
            ):
                return RegressProfileEvaluation(
                    QualificationVerdict.UNKNOWN,
                    ("POST_ESTIMATION_SOURCE_RUN_MISSING",),
                    (),
                    None,
                )
            if (
                facts.source_exec_seq is None
                or facts.receipt.get("exec_seq") != facts.source_exec_seq + 1
                or facts.receipt.get("data_signature") != facts.expected_data_state_token
            ):
                return RegressProfileEvaluation(
                    QualificationVerdict.UNKNOWN,
                    ("POST_ESTIMATION_STATE_MISMATCH",),
                    (),
                    None,
                )
            if any(
                str(element.locator.get("locator_type", "")).upper() != "R_SCALAR"
                for element in evaluation.elements
            ):
                return RegressProfileEvaluation(
                    QualificationVerdict.REJECTED,
                    ("POST_ESTIMATION_SELECTION_NOT_RETURN_STATE",),
                    (),
                    None,
                )
        return evaluation
