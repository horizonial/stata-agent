"""Explicit user outcome signals captured from ordinary product use.

Feedback is not an objective research-quality score.  It records what the user decided about a
real Turn so operational evaluation can distinguish technical success from a useful outcome.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from stata_research_agent.domain.identifiers import CommandId, TurnId, TurnOutcomeFeedbackId
from stata_research_agent.domain.revisions import WorkspaceRevision


class OutcomeDisposition(StrEnum):
    ACCEPTED = "accepted"
    NEEDS_REVISION = "needs_revision"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class OutcomeRating:
    dimension: str
    score: int

    def __post_init__(self) -> None:
        if not self.dimension.strip():
            raise ValueError("rating dimension is required")
        if not 1 <= self.score <= 5:
            raise ValueError("rating score must be between 1 and 5")


@dataclass(frozen=True, slots=True)
class RecordTurnOutcomeFeedbackCommand:
    command_id: CommandId
    turn_id: TurnId
    disposition: OutcomeDisposition
    ratings: tuple[OutcomeRating, ...] = ()
    issue_codes: tuple[str, ...] = ()
    comment: str = ""
    policy_revision: str = "turn-outcome-feedback/v1"

    def __post_init__(self) -> None:
        dimensions = [rating.dimension for rating in self.ratings]
        if len(dimensions) != len(set(dimensions)):
            raise ValueError("rating dimensions must be unique")
        if any(not code.strip() for code in self.issue_codes):
            raise ValueError("issue codes must be non-empty")
        if not self.policy_revision.strip():
            raise ValueError("feedback policy revision is required")
        if len(self.comment) > 4000:
            raise ValueError("feedback comment cannot exceed 4000 characters")


@dataclass(frozen=True, slots=True)
class TurnOutcomeFeedbackOutcome:
    feedback_id: TurnOutcomeFeedbackId
    turn_id: TurnId
    disposition: OutcomeDisposition
    commit_revision: WorkspaceRevision
    replayed: bool
