"""Bind Founder Acceptance to the active release and a G0-G6-ready exact payload."""

from __future__ import annotations

from stata_research_agent.application.release_candidate import ReleaseCandidateState

from .release_candidate_store import SqliteReleaseCandidateStore
from .release_control_store import SqliteReleaseControlStore


class ActiveInstalledCandidateVerifier:
    def __init__(
        self,
        candidates: SqliteReleaseCandidateStore,
        releases: SqliteReleaseControlStore,
    ) -> None:
        self._candidates = candidates
        self._releases = releases

    def is_verified_active_candidate(self, candidate_sha256: str) -> bool:
        candidate = self._candidates.candidate_for_payload(candidate_sha256)
        if candidate is None:
            return False
        release_id, release_manifest_sha256, state = candidate
        if state != ReleaseCandidateState.READY_FOR_FOUNDER:
            return False
        if not self._releases.is_successfully_active(release_id):
            return False
        release = self._releases.load_release(release_id)
        return bool(release is not None and release.manifest_sha256 == release_manifest_sha256)
