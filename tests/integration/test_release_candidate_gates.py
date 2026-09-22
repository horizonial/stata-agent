"""D-233 immutable RC, fresh evidence, sequential G0-G7, and signing gate."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from stata_research_agent.application.release_activation import VerifiedRelease
from stata_research_agent.application.release_candidate import (
    ApplicableReleaseTest,
    FounderAcceptanceInput,
    FounderDecision,
    ReleaseCandidateDefinition,
    ReleaseCandidateError,
    ReleaseCandidateState,
    ReleaseGate,
    ReleaseTestOutcome,
    ReleaseTestResultInput,
)
from stata_research_agent.interfaces.gated_release_signing import (
    GatedReleaseSigningService,
)
from stata_research_agent.persistence.installed_candidate_verifier import (
    ActiveInstalledCandidateVerifier,
)
from stata_research_agent.persistence.release_candidate_store import (
    SqliteReleaseCandidateStore,
)
from stata_research_agent.persistence.release_control_store import (
    SqliteReleaseControlStore,
)
from stata_research_agent.persistence.release_signing_store import (
    SqliteReleaseSigningStore,
)


def fingerprint(case: str) -> str:
    value = sum(case.encode()) % 16
    return format(value, "x") * 64


def cases() -> tuple[ApplicableReleaseTest, ...]:
    ordinary = tuple(
        ApplicableReleaseTest(
            f"case-{gate.value.lower()}",
            gate,
            fingerprint(f"case-{gate.value.lower()}"),
        )
        for gate in tuple(ReleaseGate)[:-1]
    )
    return ordinary + (
        ApplicableReleaseTest("founder-auto", ReleaseGate.G7, fingerprint("founder-auto")),
        ApplicableReleaseTest(
            "founder-controlled", ReleaseGate.G7, fingerprint("founder-controlled")
        ),
        ApplicableReleaseTest("clean-installer", ReleaseGate.G7, fingerprint("clean-installer")),
    )


def candidate(release_id: str = "release-1", payload: str = "a") -> ReleaseCandidateDefinition:
    return ReleaseCandidateDefinition(
        release_id,
        payload * 64,
        payload * 64,
        "c" * 64,
        "d" * 64,
        "e" * 64,
        "signed-source-revision",
        cases(),
    )


def result(
    release_id: str,
    case: ApplicableReleaseTest,
    outcome: ReleaseTestOutcome = ReleaseTestOutcome.PASSED,
    *,
    result_id: str | None = None,
    observed_fingerprint: str | None = None,
) -> ReleaseTestResultInput:
    return ReleaseTestResultInput(
        result_id or f"result-{release_id}-{case.test_case_id}-{outcome.value.lower()}",
        release_id,
        case.test_case_id,
        outcome,
        observed_fingerprint or case.applicability_fingerprint,
        "f" * 64,
        "1" * 64,
    )


def pass_g0_to_g6(store: SqliteReleaseCandidateStore, release_id: str) -> None:
    by_gate = {case.gate: case for case in cases() if case.gate is not ReleaseGate.G7}
    for gate in tuple(ReleaseGate)[:-1]:
        store.record_result(result(release_id, by_gate[gate]))
        decision = store.evaluate_gate(release_id, gate)
        assert decision.passed


def activate_release(
    store: SqliteReleaseControlStore, definition: ReleaseCandidateDefinition
) -> None:
    store.register_pending(
        VerifiedRelease(
            definition.release_id,
            "0.1.0-rc1",
            "build-1",
            "publisher-key-1",
            definition.release_manifest_sha256,
            f"versions/{definition.release_id}",
            "StataResearchAgent.exe",
            1,
            1,
            26,
            26,
            26,
            26,
            "1.0.0",
        )
    )
    attempt = store.begin_attempt(definition.release_id)
    store.mark_starting_safe(attempt.activation_attempt_id)
    store.mark_active_reversible(attempt.activation_attempt_id)
    store.succeed(attempt.activation_attempt_id)


def test_g0_g7_founder_binding_and_signing_use_one_exact_candidate(tmp_path: Path) -> None:
    definition = candidate()
    candidates = SqliteReleaseCandidateStore(tmp_path / "release-candidates.sqlite3")
    controls = SqliteReleaseControlStore(tmp_path / "release-control.sqlite3")
    signing = SqliteReleaseSigningStore(tmp_path / "release-signing.sqlite3")
    candidates.create(definition)
    pass_g0_to_g6(candidates, definition.release_id)
    assert candidates.state(definition.release_id) is ReleaseCandidateState.READY_FOR_FOUNDER

    verifier = ActiveInstalledCandidateVerifier(candidates, controls)
    assert not verifier.is_verified_active_candidate(definition.unsigned_payload_sha256)
    activate_release(controls, definition)
    assert verifier.is_verified_active_candidate(definition.unsigned_payload_sha256)

    candidates.record_founder_acceptance(
        FounderAcceptanceInput(
            "founder-accept-auto",
            definition.release_id,
            "founder-auto",
            "founder.autonomous.auto",
            1,
            "2" * 64,
            "3" * 64,
            FounderDecision.ACCEPT,
            "Automatic journey is acceptable for this exact candidate.",
        )
    )
    candidates.record_founder_acceptance(
        FounderAcceptanceInput(
            "founder-accept-controlled",
            definition.release_id,
            "founder-controlled",
            "founder.controlled.auto",
            1,
            "4" * 64,
            "5" * 64,
            FounderDecision.ACCEPT,
            "Controlled journey preserves researcher control.",
        )
    )
    installer = next(case for case in cases() if case.test_case_id == "clean-installer")
    candidates.record_result(result(definition.release_id, installer))
    assert candidates.evaluate_gate(definition.release_id, ReleaseGate.G7).passed
    assert candidates.state(definition.release_id) is ReleaseCandidateState.APPROVED_FOR_SIGNING

    attempt = GatedReleaseSigningService(candidates, signing).request_signing(
        definition.release_id, "sign-request-release-1"
    )
    assert attempt.release_id == definition.release_id
    assert attempt.unsigned_payload_sha256 == definition.unsigned_payload_sha256


def test_failure_stale_or_open_evidence_rejects_candidate_permanently(
    tmp_path: Path,
) -> None:
    store = SqliteReleaseCandidateStore(tmp_path / "release-candidates.sqlite3")
    failed = candidate("release-failed", "6")
    store.create(failed)
    g0 = next(case for case in cases() if case.gate is ReleaseGate.G0)
    store.record_result(result(failed.release_id, g0, ReleaseTestOutcome.FAILED))
    store.record_result(
        result(
            failed.release_id,
            g0,
            ReleaseTestOutcome.PASSED,
            result_id="result-later-pass-cannot-erase-failure",
        )
    )
    decision = store.evaluate_gate(failed.release_id, ReleaseGate.G0)
    assert not decision.passed
    assert any("FAILED" in reason for reason in decision.reason_codes)
    assert store.state(failed.release_id) is ReleaseCandidateState.REJECTED
    with pytest.raises(ReleaseCandidateError, match="terminal"):
        store.record_result(
            result(
                failed.release_id,
                g0,
                result_id="result-after-terminal-rejection",
            )
        )

    stale = candidate("release-stale", "7")
    store.create(stale)
    store.record_result(
        result(
            stale.release_id,
            g0,
            observed_fingerprint="8" * 64,
        )
    )
    stale_decision = store.evaluate_gate(stale.release_id, ReleaseGate.G0)
    assert stale_decision.reason_codes == ("case-g0:STALE_EVIDENCE",)
    assert store.state(stale.release_id) is ReleaseCandidateState.REJECTED

    unopened = candidate("release-open", "9")
    store.create(unopened)
    open_decision = store.evaluate_gate(unopened.release_id, ReleaseGate.G0)
    assert open_decision.reason_codes == ("case-g0:OPEN_NOT_RUN",)


def test_candidate_applicable_set_results_and_gate_history_are_immutable(
    tmp_path: Path,
) -> None:
    path = tmp_path / "release-candidates.sqlite3"
    store = SqliteReleaseCandidateStore(path)
    definition = candidate()
    store.create(definition)
    with pytest.raises(ReleaseCandidateError, match="rebound"):
        store.create(candidate(payload="0"))
    with pytest.raises(ReleaseCandidateError, match="G0-G7 order"):
        store.evaluate_gate(definition.release_id, ReleaseGate.G1)

    g0 = next(case for case in cases() if case.gate is ReleaseGate.G0)
    store.record_result(result(definition.release_id, g0))
    store.evaluate_gate(definition.release_id, ReleaseGate.G0)
    with pytest.raises(ReleaseCandidateError, match="decided gate"):
        store.record_result(
            result(definition.release_id, g0, result_id="result-after-gate-decision")
        )

    connection = sqlite3.connect(path)
    try:
        for sql in (
            "UPDATE release_candidates SET source_revision = 'rewritten'",
            "DELETE FROM release_applicable_tests",
            "UPDATE release_test_results SET outcome = 'FAILED'",
            "DELETE FROM release_gate_decisions",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                connection.execute(sql)
    finally:
        connection.close()


def test_founder_rejection_is_terminal_and_receipt_identity_cannot_rebind(
    tmp_path: Path,
) -> None:
    store = SqliteReleaseCandidateStore(tmp_path / "release-candidates.sqlite3")
    definition = candidate()
    store.create(definition)
    pass_g0_to_g6(store, definition.release_id)
    receipt = FounderAcceptanceInput(
        "founder-reject-auto",
        definition.release_id,
        "founder-auto",
        "founder.autonomous.auto",
        1,
        "2" * 64,
        "3" * 64,
        FounderDecision.REJECT,
        "P1 product issue found.",
        ("issue-1",),
    )
    store.record_founder_acceptance(receipt)
    assert store.state(definition.release_id) is ReleaseCandidateState.REJECTED
    with pytest.raises(ReleaseCandidateError, match="not ready"):
        store.record_founder_acceptance(receipt)
