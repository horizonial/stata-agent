"""Orchestration for read-only release probing and guarded activation."""

from .ports.release_activation import (
    ActivationProbe,
    ReleaseBundleVerifier,
    ReleaseControlRepository,
)
from .release_activation import (
    ActivationAttempt,
    ActivationReport,
    ActivationStartOutcome,
    IrreversibleCapability,
    ReleaseActivationError,
    VerifiedRelease,
)


class ReleaseActivationService:
    def __init__(
        self,
        repository: ReleaseControlRepository,
        verifier: ReleaseBundleVerifier,
        probe: ActivationProbe,
    ) -> None:
        self._repository = repository
        self._verifier = verifier
        self._probe = probe

    def stage(self, version_directory: str) -> VerifiedRelease:
        release = self._verifier.verify(version_directory)
        self._repository.register_pending(release)
        return release

    def start(self, candidate_release_id: str) -> ActivationStartOutcome:
        release = self._repository.load_release(candidate_release_id)
        if release is None:
            raise ReleaseActivationError("candidate release is not registered")
        attempt = self._repository.begin_attempt(candidate_release_id)
        passed, codes = self._probe.run(release)
        if not passed:
            failed = self._repository.fail(attempt.activation_attempt_id, "activation_probe_failed")
            return ActivationStartOutcome(failed, codes)
        started = self._repository.mark_starting_safe(attempt.activation_attempt_id)
        return ActivationStartOutcome(started, codes)

    def runtime_ready(self, activation_attempt_id: str) -> ActivationAttempt:
        return self._repository.mark_active_reversible(activation_attempt_id)

    def complete(self, activation_attempt_id: str) -> ActivationAttempt:
        return self._repository.succeed(activation_attempt_id)

    def fail(self, activation_attempt_id: str, reason_code: str) -> ActivationAttempt:
        return self._repository.fail(activation_attempt_id, reason_code)

    def rollback(self, activation_attempt_id: str) -> ActivationReport:
        attempt = self._repository.load_attempt(activation_attempt_id)
        verified = False
        if attempt.previous_release_id is not None:
            previous = self._repository.load_release(attempt.previous_release_id)
            if previous is not None:
                try:
                    observed = self._verifier.verify(previous.version_directory)
                    verified = (
                        observed.release_id == previous.release_id
                        and observed.manifest_sha256 == previous.manifest_sha256
                    )
                except (OSError, ValueError, ReleaseActivationError):
                    verified = False
        return self._repository.rollback(
            activation_attempt_id,
            previous_release_verified=verified,
        )


class ReleaseIrreversibilityGuard:
    def __init__(self, repository: ReleaseControlRepository) -> None:
        self._repository = repository

    def before(
        self,
        capability: IrreversibleCapability,
        *,
        reference: str,
    ) -> None:
        if not reference or len(reference) > 256 or "\n" in reference or "\r" in reference:
            raise ReleaseActivationError("irreversibility reference is invalid")
        self._repository.mark_current_irreversible(capability, reference)
