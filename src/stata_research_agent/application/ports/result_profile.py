"""Persistence port for registered Result Profile qualification."""

from __future__ import annotations

from typing import Protocol

from stata_research_agent.application.result_profile import (
    PromoteStataResultCommand,
    QualifyRegressResultCommand,
    RegisterGenericStataResultProfileCommand,
    RegisterRegressProfileCommand,
    RegressOperationFacts,
    ResultPromotionIdentity,
    ResultQualificationOutcome,
)
from stata_research_agent.domain.result_profile import (
    RegisteredResultProfile,
    RegressProfileEvaluation,
)


class ResultProfileRepository(Protocol):
    def register_profile(
        self,
        command: RegisterRegressProfileCommand | RegisterGenericStataResultProfileCommand,
        profile: RegisteredResultProfile,
    ) -> None: ...

    def load_operation(self, operation_id: str) -> RegressOperationFacts: ...

    def commit_qualification(
        self,
        command: QualifyRegressResultCommand | PromoteStataResultCommand,
        profile: RegisteredResultProfile,
        facts: RegressOperationFacts,
        evaluation: RegressProfileEvaluation,
        identities: ResultPromotionIdentity,
    ) -> ResultQualificationOutcome: ...
