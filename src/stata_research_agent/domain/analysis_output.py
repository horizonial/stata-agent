"""Closed classification vocabulary for exploratory Python/Shell output."""

from enum import StrEnum


class AnalysisOutputKind(StrEnum):
    VISUAL = "visual"
    SCALAR = "scalar"
    TABLE = "table"
    TEST = "test"
    CUSTOM = "custom"
    REGRESSION = "regression"
    UNKNOWN = "unknown"

    @property
    def document_eligible(self) -> bool:
        # Python/Shell regression output remains exploratory until a later, exact user
        # confirmation, but the method family itself is not a permanent adoption ban.
        return self is not AnalysisOutputKind.UNKNOWN
