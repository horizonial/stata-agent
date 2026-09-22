"""D-232 exact candidate binding and no blind signing retry."""

from pathlib import Path

import pytest

from stata_research_agent.application.release_signing import (
    ApprovedReleaseCandidate,
    ReleaseSigningError,
    SigningAttemptState,
)
from stata_research_agent.persistence.release_signing_store import (
    SqliteReleaseSigningStore,
)


def candidate(payload: str = "a") -> ApprovedReleaseCandidate:
    return ApprovedReleaseCandidate(
        "release-1",
        payload * 64,
        "b" * 64,
        "c" * 64,
        "d" * 64,
    )


def test_release_identity_is_immutable_and_signing_request_is_idempotent(
    tmp_path: Path,
) -> None:
    store = SqliteReleaseSigningStore(tmp_path / "control" / "release.sqlite3")
    store.approve(candidate())
    store.approve(candidate())
    with pytest.raises(ReleaseSigningError, match="cannot be rebound"):
        store.approve(candidate("e"))

    first = store.request("release-1", "sign-request-1")
    assert first == store.request("release-1", "sign-request-1")
    completed = store.complete(
        first.signing_attempt_id,
        state=SigningAttemptState.COMPLETED,
        signed_payload_sha256="f" * 64,
    )
    assert completed.state == SigningAttemptState.COMPLETED


def test_unknown_signing_outcome_blocks_blind_retry(tmp_path: Path) -> None:
    store = SqliteReleaseSigningStore(tmp_path / "control" / "release.sqlite3")
    store.approve(candidate())
    first = store.request("release-1", "sign-request-1")
    store.complete(
        first.signing_attempt_id,
        state=SigningAttemptState.OUTCOME_UNKNOWN,
        failure_code="signer_timeout",
    )
    with pytest.raises(ReleaseSigningError, match="blind retry"):
        store.request("release-1", "sign-request-2")


def test_signing_failure_code_is_safe_structured_metadata(tmp_path: Path) -> None:
    store = SqliteReleaseSigningStore(tmp_path / "control" / "release.sqlite3")
    store.approve(candidate())
    attempt = store.request("release-1", "sign-request-1")
    with pytest.raises(ReleaseSigningError, match="single-line"):
        store.complete(
            attempt.signing_attempt_id,
            state=SigningAttemptState.FAILED,
            failure_code="signer failed\nsecret-shaped free-form diagnostic",
        )
