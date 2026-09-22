"""Ports for release activation and its irreversible side-effect guard."""

from __future__ import annotations

from typing import Protocol

from ..release_activation import (
    ActivationAttempt,
    ActivationReport,
    IrreversibleCapability,
    VerifiedRelease,
)


class ReleaseControlRepository(Protocol):
    def register_pending(self, release: VerifiedRelease) -> int: ...

    def load_release(self, release_id: str) -> VerifiedRelease | None: ...

    def begin_attempt(self, candidate_release_id: str) -> ActivationAttempt: ...

    def mark_starting_safe(self, activation_attempt_id: str) -> ActivationAttempt: ...

    def mark_active_reversible(self, activation_attempt_id: str) -> ActivationAttempt: ...

    def succeed(self, activation_attempt_id: str) -> ActivationAttempt: ...

    def fail(self, activation_attempt_id: str, reason_code: str) -> ActivationAttempt: ...

    def mark_current_irreversible(
        self,
        capability: IrreversibleCapability,
        reference: str,
    ) -> ActivationAttempt | None: ...

    def rollback(
        self,
        activation_attempt_id: str,
        *,
        previous_release_verified: bool,
    ) -> ActivationReport: ...

    def load_attempt(self, activation_attempt_id: str) -> ActivationAttempt: ...


class ReleaseBundleVerifier(Protocol):
    def verify(self, version_directory: str) -> VerifiedRelease: ...


class ActivationProbe(Protocol):
    def run(self, release: VerifiedRelease) -> tuple[bool, tuple[str, ...]]: ...


class IrreversibilityGuard(Protocol):
    def before(
        self,
        capability: IrreversibleCapability,
        *,
        reference: str,
    ) -> None: ...


class NoopIrreversibilityGuard:
    def before(
        self,
        capability: IrreversibleCapability,
        *,
        reference: str,
    ) -> None:
        del capability, reference
