"""Authority port for Analysis Output classification and user adoption."""

from typing import Protocol

from stata_research_agent.application.analysis_output import (
    AdoptAnalysisOutputCommand,
    AdoptedAnalysisOutput,
    AnalysisAdoptionIdentity,
    AnalysisOutputIdentity,
    ClassifiedAnalysisOutput,
    ClassifyAnalysisOutputCommand,
)


class AnalysisOutputRepository(Protocol):
    def classify(
        self,
        command: ClassifyAnalysisOutputCommand,
        identity: AnalysisOutputIdentity,
    ) -> ClassifiedAnalysisOutput: ...

    def adopt(
        self,
        command: AdoptAnalysisOutputCommand,
        identity: AnalysisAdoptionIdentity,
    ) -> AdoptedAnalysisOutput: ...
