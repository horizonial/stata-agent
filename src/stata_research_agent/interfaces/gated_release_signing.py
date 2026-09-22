"""Signing coordinator that only consumes a G0-G7-approved exact candidate."""

from __future__ import annotations

from stata_research_agent.application.release_signing import SigningAttempt
from stata_research_agent.persistence.release_candidate_store import (
    SqliteReleaseCandidateStore,
)
from stata_research_agent.persistence.release_signing_store import SqliteReleaseSigningStore


class GatedReleaseSigningService:
    def __init__(
        self,
        candidates: SqliteReleaseCandidateStore,
        signing: SqliteReleaseSigningStore,
    ) -> None:
        self._candidates = candidates
        self._signing = signing

    def request_signing(self, release_id: str, request_id: str) -> SigningAttempt:
        approved = self._candidates.approved_signing_candidate(release_id)
        self._signing.approve(approved)
        return self._signing.request(release_id, request_id)
