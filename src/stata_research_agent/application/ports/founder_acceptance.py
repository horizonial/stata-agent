"""Ports for binding Founder Acceptance to an installed exact candidate."""

from __future__ import annotations

from typing import Protocol


class InstalledCandidateVerifier(Protocol):
    def is_verified_active_candidate(self, candidate_sha256: str) -> bool: ...
