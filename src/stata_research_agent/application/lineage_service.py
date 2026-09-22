"""Application entrypoint for path-neutral, consistent Evidence lineage."""

from __future__ import annotations

from .lineage import LineageSelector, UnifiedEvidenceLineage
from .ports.lineage import EvidenceLineageQuery


class EvidenceLineageService:
    def __init__(self, query: EvidenceLineageQuery) -> None:
        self._query = query

    def load(self, selector: LineageSelector) -> UnifiedEvidenceLineage:
        return self._query.load(selector)
