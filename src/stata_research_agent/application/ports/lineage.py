"""Consistent-read Evidence lineage query port."""

from __future__ import annotations

from typing import Protocol

from stata_research_agent.application.lineage import (
    LineageSelector,
    UnifiedEvidenceLineage,
)


class EvidenceLineageQuery(Protocol):
    def load(self, selector: LineageSelector) -> UnifiedEvidenceLineage: ...
