"""Authority ports for read-only recovery assessment and safe convergence."""

from typing import Protocol

from stata_research_agent.application.recovery import (
    RecoverOperationCommand,
    RecoveryAssessment,
    RecoveryOutcome,
)
from stata_research_agent.domain.identifiers import OperationId, RecoveryReportId


class RecoveryAssessmentSource(Protocol):
    def assess(self, operation_id: OperationId) -> RecoveryAssessment: ...


class RecoveryRepository(Protocol):
    def record(
        self,
        command: RecoverOperationCommand,
        assessment: RecoveryAssessment,
        report_id: RecoveryReportId,
    ) -> RecoveryOutcome: ...
