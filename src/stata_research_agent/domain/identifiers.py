"""Runtime-distinct opaque identifiers for authoritative domain objects."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Self

from .errors import DomainValidationError


@dataclass(frozen=True, slots=True)
class OpaqueId:
    """A non-empty opaque value whose concrete class carries its domain type.

    The value deliberately does not encode database location or filesystem path.  Equality is
    class-sensitive because dataclass equality requires the same concrete class.
    """

    value: str
    prefix: ClassVar[str] = ""

    def __post_init__(self) -> None:
        if not isinstance(self.value, str):
            raise DomainValidationError("opaque ID must be a string")
        if not self.value or self.value != self.value.strip():
            raise DomainValidationError("opaque ID must be non-empty and have no edge whitespace")
        if len(self.value) > 128:
            raise DomainValidationError("opaque ID cannot exceed 128 characters")
        if any(character.isspace() or ord(character) < 32 for character in self.value):
            raise DomainValidationError("opaque ID cannot contain whitespace or control characters")
        if not self.prefix or not self.value.startswith(self.prefix):
            raise DomainValidationError(
                f"{type(self).__name__} must start with the type prefix {self.prefix!r}"
            )
        if len(self.value) == len(self.prefix):
            raise DomainValidationError("opaque ID must contain a value after its type prefix")

    def __str__(self) -> str:
        return self.value

    def to_primitive(self) -> str:
        """Return the stable wire/storage primitive without leaking an internal row shape."""

        return self.value

    @classmethod
    def from_primitive(cls, value: str) -> Self:
        return cls(value)


@dataclass(frozen=True, slots=True)
class WorkspaceId(OpaqueId):
    prefix = "ws_"


@dataclass(frozen=True, slots=True)
class ConversationId(OpaqueId):
    prefix = "conv_"


@dataclass(frozen=True, slots=True)
class MessageId(OpaqueId):
    prefix = "msg_"


@dataclass(frozen=True, slots=True)
class TurnId(OpaqueId):
    prefix = "turn_"


@dataclass(frozen=True, slots=True)
class TurnOutcomeFeedbackId(OpaqueId):
    prefix = "turnfeedback_"


@dataclass(frozen=True, slots=True)
class ResearchPathId(OpaqueId):
    prefix = "path_"


@dataclass(frozen=True, slots=True)
class ExecutionScopeId(OpaqueId):
    prefix = "scope_"


@dataclass(frozen=True, slots=True)
class StepId(OpaqueId):
    prefix = "step_"


@dataclass(frozen=True, slots=True)
class ModelInvocationId(OpaqueId):
    prefix = "modelinv_"


@dataclass(frozen=True, slots=True)
class ProviderAttemptId(OpaqueId):
    prefix = "providerattempt_"


@dataclass(frozen=True, slots=True)
class TurnContextBaselineId(OpaqueId):
    prefix = "contextbase_"


@dataclass(frozen=True, slots=True)
class ContextManifestId(OpaqueId):
    prefix = "contextmanifest_"


@dataclass(frozen=True, slots=True)
class ContextItemId(OpaqueId):
    prefix = "contextitem_"


@dataclass(frozen=True, slots=True)
class ContextBuildDecisionId(OpaqueId):
    prefix = "contextdecision_"


@dataclass(frozen=True, slots=True)
class MemoryItemId(OpaqueId):
    prefix = "memoryitem_"


@dataclass(frozen=True, slots=True)
class MemoryRevisionId(OpaqueId):
    prefix = "memoryrev_"


@dataclass(frozen=True, slots=True)
class MemoryRevisionSourceId(OpaqueId):
    prefix = "memorysource_"


@dataclass(frozen=True, slots=True)
class MemoryStateHistoryId(OpaqueId):
    prefix = "memorystate_"


@dataclass(frozen=True, slots=True)
class MemoryEpisodeId(OpaqueId):
    prefix = "memoryepisode_"


@dataclass(frozen=True, slots=True)
class MemoryMaintenanceJobId(OpaqueId):
    prefix = "memoryjob_"


@dataclass(frozen=True, slots=True)
class MemoryProviderAttemptId(OpaqueId):
    prefix = "memoryattempt_"


@dataclass(frozen=True, slots=True)
class MemoryCandidateId(OpaqueId):
    prefix = "memorycandidate_"


@dataclass(frozen=True, slots=True)
class MemoryCompactionCheckpointId(OpaqueId):
    prefix = "memorycheckpoint_"


@dataclass(frozen=True, slots=True)
class MemoryRetentionHistoryId(OpaqueId):
    prefix = "memoryretention_"


@dataclass(frozen=True, slots=True)
class SkillEvolutionCandidateId(OpaqueId):
    prefix = "skillcandidate_"


@dataclass(frozen=True, slots=True)
class SkillEvolutionStateHistoryId(OpaqueId):
    prefix = "skillstate_"


@dataclass(frozen=True, slots=True)
class SkillActivationManifestId(OpaqueId):
    prefix = "skillactivation_"


@dataclass(frozen=True, slots=True)
class SkillVersionId(OpaqueId):
    prefix = "skillversion_"


@dataclass(frozen=True, slots=True)
class SkillAdoptionHistoryId(OpaqueId):
    prefix = "skilladoption_"


@dataclass(frozen=True, slots=True)
class SkillPublicationManifestId(OpaqueId):
    prefix = "skillpublication_"


@dataclass(frozen=True, slots=True)
class SkillEvaluationRunId(OpaqueId):
    prefix = "skilleval_"


@dataclass(frozen=True, slots=True)
class SkillEvaluationAttemptId(OpaqueId):
    prefix = "skillevalattempt_"


@dataclass(frozen=True, slots=True)
class SkillImprovementProposalId(OpaqueId):
    prefix = "skillproposal_"


@dataclass(frozen=True, slots=True)
class SkillChangeCandidateId(OpaqueId):
    prefix = "skillchange_"


@dataclass(frozen=True, slots=True)
class SkillChangeStateHistoryId(OpaqueId):
    prefix = "skillchangestate_"


@dataclass(frozen=True, slots=True)
class KnowledgeDocumentId(OpaqueId):
    prefix = "knowledgedoc_"


@dataclass(frozen=True, slots=True)
class KnowledgeDocumentRevisionId(OpaqueId):
    prefix = "knowledgerev_"


@dataclass(frozen=True, slots=True)
class KnowledgeChunkId(OpaqueId):
    prefix = "knowledgechunk_"


@dataclass(frozen=True, slots=True)
class KnowledgeIndexRunId(OpaqueId):
    prefix = "knowledgeindex_"


@dataclass(frozen=True, slots=True)
class KnowledgeSourceId(OpaqueId):
    prefix = "knowledgesource_"


@dataclass(frozen=True, slots=True)
class KnowledgeSourceRevisionId(OpaqueId):
    prefix = "knowledgesrcrev_"


@dataclass(frozen=True, slots=True)
class KnowledgeParseRevisionId(OpaqueId):
    prefix = "knowledgeparse_"


@dataclass(frozen=True, slots=True)
class KnowledgeNodeId(OpaqueId):
    prefix = "knowledgenode_"


@dataclass(frozen=True, slots=True)
class KnowledgeEdgeId(OpaqueId):
    prefix = "knowledgeedge_"


@dataclass(frozen=True, slots=True)
class KnowledgeRetrievalSessionId(OpaqueId):
    prefix = "retrievalsession_"


@dataclass(frozen=True, slots=True)
class KnowledgeRetrievalHopId(OpaqueId):
    prefix = "retrievalhop_"


@dataclass(frozen=True, slots=True)
class KnowledgeEmbeddingIndexRevisionId(OpaqueId):
    prefix = "knowledgeembedidx_"


@dataclass(frozen=True, slots=True)
class ModelInputSnapshotId(OpaqueId):
    prefix = "modelinput_"


@dataclass(frozen=True, slots=True)
class ProviderRequestSnapshotId(OpaqueId):
    prefix = "providerrequest_"


@dataclass(frozen=True, slots=True)
class OutboundMaterialRecordId(OpaqueId):
    prefix = "outboundmaterial_"


@dataclass(frozen=True, slots=True)
class AssistantOutputId(OpaqueId):
    prefix = "assistantout_"


@dataclass(frozen=True, slots=True)
class PermissionSnapshotId(OpaqueId):
    prefix = "permissionsnap_"


@dataclass(frozen=True, slots=True)
class ModelPolicySnapshotId(OpaqueId):
    prefix = "modelpolicy_"


@dataclass(frozen=True, slots=True)
class ToolCallId(OpaqueId):
    prefix = "toolcall_"


@dataclass(frozen=True, slots=True)
class ToolContractId(OpaqueId):
    prefix = "toolcontract_"


@dataclass(frozen=True, slots=True)
class RawArgumentsSnapshotId(OpaqueId):
    prefix = "argsraw_"


@dataclass(frozen=True, slots=True)
class CanonicalArgumentsSnapshotId(OpaqueId):
    prefix = "argscanonical_"


@dataclass(frozen=True, slots=True)
class DispatchPlanId(OpaqueId):
    prefix = "dispatchplan_"


@dataclass(frozen=True, slots=True)
class DispatchPlanEntryId(OpaqueId):
    prefix = "dispatchentry_"


@dataclass(frozen=True, slots=True)
class ResourceClaimId(OpaqueId):
    prefix = "resourceclaim_"


@dataclass(frozen=True, slots=True)
class ToolAdmissionId(OpaqueId):
    prefix = "admission_"


@dataclass(frozen=True, slots=True)
class ToolResultId(OpaqueId):
    prefix = "toolresult_"


@dataclass(frozen=True, slots=True)
class OperationId(OpaqueId):
    prefix = "op_"


@dataclass(frozen=True, slots=True)
class OperationAttemptId(OpaqueId):
    prefix = "attempt_"


@dataclass(frozen=True, slots=True)
class RunId(OpaqueId):
    prefix = "run_"


@dataclass(frozen=True, slots=True)
class ResearchCommandInstanceId(OpaqueId):
    prefix = "researchcmd_"


@dataclass(frozen=True, slots=True)
class ResultCapturePointId(OpaqueId):
    prefix = "capturepoint_"


@dataclass(frozen=True, slots=True)
class ResultCaptureSnapshotId(OpaqueId):
    prefix = "snapshot_"


@dataclass(frozen=True, slots=True)
class ResultContractId(OpaqueId):
    prefix = "resultcontract_"


@dataclass(frozen=True, slots=True)
class ResultCandidateId(OpaqueId):
    prefix = "resultcandidate_"


@dataclass(frozen=True, slots=True)
class ResultQualificationReportId(OpaqueId):
    prefix = "qualification_"


@dataclass(frozen=True, slots=True)
class ResultId(OpaqueId):
    prefix = "result_"


@dataclass(frozen=True, slots=True)
class ResultElementId(OpaqueId):
    prefix = "element_"


@dataclass(frozen=True, slots=True)
class ResultSourceLocatorId(OpaqueId):
    prefix = "locator_"


@dataclass(frozen=True, slots=True)
class TrustedDerivationReceiptId(OpaqueId):
    prefix = "derivation_"


@dataclass(frozen=True, slots=True)
class EstimationSampleManifestId(OpaqueId):
    prefix = "sample_"


@dataclass(frozen=True, slots=True)
class ExecutableSourceId(OpaqueId):
    prefix = "execsource_"


@dataclass(frozen=True, slots=True)
class EvidenceRecordId(OpaqueId):
    prefix = "evidence_"


@dataclass(frozen=True, slots=True)
class EvidenceIssuanceReceiptId(OpaqueId):
    prefix = "issuance_"


@dataclass(frozen=True, slots=True)
class EvidencePresentationUseId(OpaqueId):
    prefix = "presentationuse_"


@dataclass(frozen=True, slots=True)
class EvidenceRenderReceiptId(OpaqueId):
    prefix = "renderreceipt_"


@dataclass(frozen=True, slots=True)
class EvidenceRenderBindingId(OpaqueId):
    prefix = "renderbinding_"


@dataclass(frozen=True, slots=True)
class FormatRuleSnapshotId(OpaqueId):
    prefix = "formatrule_"


@dataclass(frozen=True, slots=True)
class FormalResultBlockId(OpaqueId):
    prefix = "formalblock_"


@dataclass(frozen=True, slots=True)
class NumericCoverageManifestId(OpaqueId):
    prefix = "coverage_"


@dataclass(frozen=True, slots=True)
class NumericOccurrenceId(OpaqueId):
    prefix = "occurrence_"


@dataclass(frozen=True, slots=True)
class TableExportInputManifestId(OpaqueId):
    prefix = "tableinput_"


@dataclass(frozen=True, slots=True)
class TableExportManifestId(OpaqueId):
    prefix = "tableexport_"


@dataclass(frozen=True, slots=True)
class TableRenderReceiptId(OpaqueId):
    prefix = "tablerender_"


@dataclass(frozen=True, slots=True)
class TableCellEvidenceUseId(OpaqueId):
    prefix = "tablecelluse_"


@dataclass(frozen=True, slots=True)
class TableCoverageManifestId(OpaqueId):
    prefix = "tablecoverage_"


@dataclass(frozen=True, slots=True)
class DocumentManifestId(OpaqueId):
    prefix = "docmanifest_"


@dataclass(frozen=True, slots=True)
class DocumentParseReceiptId(OpaqueId):
    prefix = "docparse_"


@dataclass(frozen=True, slots=True)
class DeliveryGateReportId(OpaqueId):
    prefix = "deliverygate_"


@dataclass(frozen=True, slots=True)
class DocumentSlotId(OpaqueId):
    prefix = "docslot_"


@dataclass(frozen=True, slots=True)
class ArtifactId(OpaqueId):
    prefix = "artifact_"


@dataclass(frozen=True, slots=True)
class FileObservationId(OpaqueId):
    prefix = "fileobs_"


@dataclass(frozen=True, slots=True)
class ArtifactStateObservationId(OpaqueId):
    prefix = "artifactstate_"


@dataclass(frozen=True, slots=True)
class ArtifactVerificationReceiptId(OpaqueId):
    prefix = "artifactverify_"


@dataclass(frozen=True, slots=True)
class ArtifactLocationId(OpaqueId):
    prefix = "artifactloc_"


@dataclass(frozen=True, slots=True)
class DataVersionId(OpaqueId):
    prefix = "data_"


@dataclass(frozen=True, slots=True)
class PlanRevisionId(OpaqueId):
    prefix = "planrev_"


@dataclass(frozen=True, slots=True)
class DocumentId(OpaqueId):
    prefix = "doc_"


@dataclass(frozen=True, slots=True)
class DocumentRevisionId(OpaqueId):
    prefix = "docrev_"


@dataclass(frozen=True, slots=True)
class CompletionContractId(OpaqueId):
    prefix = "contract_"


@dataclass(frozen=True, slots=True)
class CompletionContractRevisionId(OpaqueId):
    prefix = "contractrev_"


@dataclass(frozen=True, slots=True)
class CommandId(OpaqueId):
    prefix = "cmd_"


@dataclass(frozen=True, slots=True)
class JournalEntryId(OpaqueId):
    prefix = "journal_"


@dataclass(frozen=True, slots=True)
class OutboxEntryId(OpaqueId):
    prefix = "outbox_"


@dataclass(frozen=True, slots=True)
class SkillSnapshotId(OpaqueId):
    prefix = "skillsnap_"


@dataclass(frozen=True, slots=True)
class PlanId(OpaqueId):
    prefix = "plan_"


@dataclass(frozen=True, slots=True)
class PlanNodeId(OpaqueId):
    prefix = "plannode_"


@dataclass(frozen=True, slots=True)
class PathBranchManifestId(OpaqueId):
    prefix = "pathbranch_"


@dataclass(frozen=True, slots=True)
class PathDataSlotId(OpaqueId):
    prefix = "dataslot_"


@dataclass(frozen=True, slots=True)
class ResultSlotId(OpaqueId):
    prefix = "resultslot_"


@dataclass(frozen=True, slots=True)
class EvidenceUseId(OpaqueId):
    prefix = "evidenceuse_"


@dataclass(frozen=True, slots=True)
class EvidenceValidationReceiptId(OpaqueId):
    prefix = "evidencevalidation_"


@dataclass(frozen=True, slots=True)
class AnalysisOutputId(OpaqueId):
    prefix = "analysisout_"


@dataclass(frozen=True, slots=True)
class AnalysisOutputAdoptionId(OpaqueId):
    prefix = "analysisadopt_"


@dataclass(frozen=True, slots=True)
class AnalysisOutputClassificationId(OpaqueId):
    prefix = "analysisclass_"


@dataclass(frozen=True, slots=True)
class AnalysisDocumentEligibilityId(OpaqueId):
    prefix = "analysiseligibility_"


@dataclass(frozen=True, slots=True)
class DocumentRevisionViewPolicyId(OpaqueId):
    prefix = "docviewpolicy_"


@dataclass(frozen=True, slots=True)
class DocumentReturnReportId(OpaqueId):
    prefix = "docreturn_"


@dataclass(frozen=True, slots=True)
class DocumentDiffId(OpaqueId):
    prefix = "docdiff_"


@dataclass(frozen=True, slots=True)
class DocumentMergeReceiptId(OpaqueId):
    prefix = "docmerge_"


@dataclass(frozen=True, slots=True)
class EnvironmentSnapshotId(OpaqueId):
    prefix = "envsnap_"


@dataclass(frozen=True, slots=True)
class CompletionManifestId(OpaqueId):
    prefix = "manifest_"


@dataclass(frozen=True, slots=True)
class ArtifactCapturePlanId(OpaqueId):
    prefix = "captureplan_"


@dataclass(frozen=True, slots=True)
class ArtifactCandidateId(OpaqueId):
    prefix = "candidate_"


@dataclass(frozen=True, slots=True)
class ArtifactPromotionId(OpaqueId):
    prefix = "promotion_"


@dataclass(frozen=True, slots=True)
class EvaluationReportId(OpaqueId):
    prefix = "eval_"


@dataclass(frozen=True, slots=True)
class WaitingRequestId(OpaqueId):
    prefix = "waiting_"


@dataclass(frozen=True, slots=True)
class WaitingAnswerId(OpaqueId):
    prefix = "waitinganswer_"


@dataclass(frozen=True, slots=True)
class PauseIntentId(OpaqueId):
    prefix = "pauseintent_"


@dataclass(frozen=True, slots=True)
class BudgetPolicySnapshotId(OpaqueId):
    prefix = "budgetpolicy_"


@dataclass(frozen=True, slots=True)
class BudgetUsageId(OpaqueId):
    prefix = "budgetusage_"


@dataclass(frozen=True, slots=True)
class TurnContinuationId(OpaqueId):
    prefix = "turncontinuation_"


@dataclass(frozen=True, slots=True)
class RecoveryReportId(OpaqueId):
    prefix = "recovery_"


@dataclass(frozen=True, slots=True)
class CompletionObligationId(OpaqueId):
    prefix = "obligation_"


@dataclass(frozen=True, slots=True)
class ObligationObservationId(OpaqueId):
    prefix = "observation_"


@dataclass(frozen=True, slots=True)
class EvaluationPolicySnapshotId(OpaqueId):
    prefix = "evalpolicy_"


@dataclass(frozen=True, slots=True)
class EvidenceScopeManifestId(OpaqueId):
    prefix = "evalscope_"


@dataclass(frozen=True, slots=True)
class EvaluationRequestId(OpaqueId):
    prefix = "evalrequest_"


@dataclass(frozen=True, slots=True)
class EvaluationFindingId(OpaqueId):
    prefix = "evalfinding_"


@dataclass(frozen=True, slots=True)
class GoalCoverageId(OpaqueId):
    prefix = "goalcoverage_"


@dataclass(frozen=True, slots=True)
class StopGuardDecisionRecordId(OpaqueId):
    prefix = "stopguard_"
