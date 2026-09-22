"""Deterministic Tool Broker slice around the external Stata runtime."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import cast

from stata_research_agent.domain.identifiers import (
    ArtifactCandidateId,
    ArtifactCapturePlanId,
    ArtifactId,
    ArtifactLocationId,
    ArtifactPromotionId,
    ArtifactStateObservationId,
    CommandId,
    CompletionManifestId,
    ExecutableSourceId,
    OperationAttemptId,
    OperationId,
    TurnId,
)
from stata_research_agent.domain.stata_execution import (
    StataArtifactOutputRequest,
    StataRuntimeResult,
)

from .ports.artifact_data import ManagedArtifactStore
from .ports.completion_manifest import CompletionManifestStore, IsolatedCompletion
from .ports.identity import IdentityGenerator
from .ports.release_activation import IrreversibilityGuard, NoopIrreversibilityGuard
from .ports.stata_operation import StataOperationRepository
from .ports.stata_runtime import StataRuntime
from .release_activation import IrreversibleCapability
from .sensitive_output import SensitiveOutputGate, SensitiveOutputGateUnavailable
from .stata_operation import (
    ExecuteStataCommand,
    FormalSessionDataBinding,
    PlannedArtifactCandidate,
    PublishedArtifactCandidate,
    StataOperationHandle,
    StataOperationOutcome,
)


class StataOperationService:
    def __init__(
        self,
        repository: StataOperationRepository,
        runtime: StataRuntime,
        identities: IdentityGenerator,
        completion_store: CompletionManifestStore,
        managed_store: ManagedArtifactStore | None = None,
        sensitive_output_gate: SensitiveOutputGate | None = None,
        irreversibility_guard: IrreversibilityGuard | None = None,
    ) -> None:
        self._repository = repository
        self._runtime = runtime
        self._identities = identities
        self._completion_store = completion_store
        self._managed_store = managed_store
        self._sensitive_output_gate = sensitive_output_gate or SensitiveOutputGate()
        self._irreversibility_guard = irreversibility_guard or NoopIrreversibilityGuard()

    async def execute(self, command: ExecuteStataCommand) -> StataOperationOutcome:
        planned_candidates = tuple(
            PlannedArtifactCandidate(
                self._identities.new(ArtifactCandidateId),
                self._identities.new(ArtifactId),
                self._identities.new(ArtifactPromotionId),
                self._identities.new(ArtifactStateObservationId),
                self._identities.new(ArtifactLocationId),
                expectation,
            )
            for expectation in command.expected_outputs
        )
        operation_id = (
            command.admitted_operation_id
            if command.admitted_operation_id is not None
            else self._identities.new(OperationId)
        )
        self._irreversibility_guard.before(
            IrreversibleCapability.TOOL_HANDOFF,
            reference=operation_id.value,
        )
        handle = self._repository.handoff(
            command,
            handoff_command_id=self._identities.new(CommandId),
            operation_id=operation_id,
            attempt_id=self._identities.new(OperationAttemptId),
            capture_plan_id=self._identities.new(ArtifactCapturePlanId),
            planned_candidates=planned_candidates,
            executable_source_id=self._identities.new(ExecutableSourceId),
        )
        # A durable handoff from an earlier process is never re-executed automatically.
        if handle.replayed:
            return self._repository.existing_outcome(command, handle)
        try:
            execution_code = command.code.replace(
                "<ATTEMPT_STAGING>",
                f".stata-agent/staging/{handle.attempt_id.value}",
            )
            result = await self._runtime.execute(
                session_id=command.session_id,
                code=execution_code,
                timeout_seconds=command.timeout_seconds,
                operation_attempt_id=handle.attempt_id.value,
                artifact_outputs=tuple(
                    StataArtifactOutputRequest(
                        output.output_slot,
                        output.relative_staging_path,
                        output.artifact_kind,
                        output.media_type,
                        output.required,
                    )
                    for output in command.expected_outputs
                ),
            )
            result = self._sanitize_runtime_result(result)
        except SensitiveOutputGateUnavailable:
            try:
                await self._runtime.close_session(
                    session_id=command.session_id,
                    reason="Sensitive Output Gate unavailable after committed handoff",
                )
            finally:
                return self._repository.mark_transport_unknown(
                    command,
                    handle,
                    error_kind="CREDENTIAL_OUTPUT_GATE_UNAVAILABLE",
                    error_detail="Sensitive Output Gate unavailable after committed handoff",
                )
        except Exception as error:
            try:
                await self._runtime.close_session(
                    session_id=command.session_id,
                    reason="transport incomplete after committed handoff",
                )
            finally:
                return self._repository.mark_transport_unknown(
                    command,
                    handle,
                    error_kind=type(error).__name__,
                    error_detail=str(error)[:500],
                )

        manifest_id = self._identities.new(CompletionManifestId)
        isolated_completion = self._completion_store.publish(
            manifest_id=manifest_id,
            handle=handle,
            result=result,
        )
        published_candidates = self._publish_candidates(handle, result, isolated_completion)
        outcome = self._repository.finalize(
            command,
            handle,
            result,
            manifest_id=manifest_id,
            published_candidates=published_candidates,
        )
        if result.receipt.execution_status.is_uncertain:
            await self._runtime.close_session(
                session_id=command.session_id,
                reason=f"tainted after {result.receipt.execution_status.value}",
            )
        return outcome

    def _sanitize_runtime_result(self, result: StataRuntimeResult) -> StataRuntimeResult:
        text = self._sensitive_output_gate.inspect_text("stata.raw_output", result.text)
        structured = None
        if result.structured is not None:
            inspected = self._sensitive_output_gate.inspect_json(
                "stata.structured_output", result.structured
            )
            structured = cast(dict[str, object], inspected.safe_value)
        runtime_environment = self._sensitive_output_gate.inspect_json(
            "stata.runtime_environment", result.receipt.runtime_environment
        )
        supervision = self._sensitive_output_gate.inspect_json(
            "stata.supervision_proof", result.receipt.supervision_proof
        )
        receipt = replace(
            result.receipt,
            runtime_environment=cast(dict[str, object], runtime_environment.safe_value),
            supervision_proof=cast(dict[str, object], supervision.safe_value),
        )
        return replace(
            result,
            text=cast(str, text.safe_value),
            structured=structured,
            receipt=receipt,
        )

    def reconcile(
        self,
        command: ExecuteStataCommand,
        *,
        authorization_command_id: CommandId,
        authorized_by_turn_id: TurnId,
    ) -> StataOperationOutcome:
        """Explicitly finalize a verified completion without re-running Stata."""

        planned_candidates = tuple(
            PlannedArtifactCandidate(
                self._identities.new(ArtifactCandidateId),
                self._identities.new(ArtifactId),
                self._identities.new(ArtifactPromotionId),
                self._identities.new(ArtifactStateObservationId),
                self._identities.new(ArtifactLocationId),
                expectation,
            )
            for expectation in command.expected_outputs
        )
        handle = self._repository.handoff(
            command,
            handoff_command_id=self._identities.new(CommandId),
            operation_id=(
                command.admitted_operation_id
                if command.admitted_operation_id is not None
                else self._identities.new(OperationId)
            ),
            attempt_id=self._identities.new(OperationAttemptId),
            capture_plan_id=self._identities.new(ArtifactCapturePlanId),
            planned_candidates=planned_candidates,
            executable_source_id=self._identities.new(ExecutableSourceId),
        )
        if not handle.replayed:
            raise RuntimeError("reconciliation requires an existing committed handoff")
        if authorized_by_turn_id == command.requested_by_turn_id:
            raise RuntimeError("reconciliation must be authorized by a successor Turn")
        existing = self._repository.existing_outcome(command, handle)
        if existing.manifest_id is not None:
            return existing
        self._repository.authorize_reconciliation(
            command,
            handle,
            authorization_command_id=authorization_command_id,
            authorized_by_turn_id=authorized_by_turn_id,
        )
        recovered = self._completion_store.load_attempt(handle.attempt_id.value)
        if (
            recovered.operation_id != handle.operation_id.value
            or recovered.attempt_id != handle.attempt_id.value
            or recovered.result.receipt.session_id != command.session_id
        ):
            raise RuntimeError("Completion Manifest does not match the operation handoff")
        isolated = self._completion_store.publish(
            manifest_id=recovered.manifest_id,
            handle=handle,
            result=recovered.result,
        )
        published = self._publish_candidates(handle, recovered.result, isolated)
        return self._repository.finalize(
            command,
            handle,
            recovered.result,
            manifest_id=recovered.manifest_id,
            published_candidates=published,
        )

    def session_data_binding(self, operation_id: OperationId) -> FormalSessionDataBinding:
        return self._repository.session_data_binding(operation_id)

    def _publish_candidates(
        self,
        handle: StataOperationHandle,
        result: StataRuntimeResult,
        isolated_completion: IsolatedCompletion,
    ) -> tuple[PublishedArtifactCandidate, ...]:
        if not result.artifacts:
            missing_required = [
                planned.expectation.output_slot
                for planned in handle.planned_candidates
                if planned.expectation.required
            ]
            if missing_required and result.receipt.execution_status.value == "succeeded":
                raise RuntimeError(
                    f"required Artifact outputs were not captured: {missing_required}"
                )
            return ()
        if self._managed_store is None:
            raise RuntimeError("Artifact output requires a managed Artifact Store")

        planned_by_slot = {
            planned.expectation.output_slot: planned for planned in handle.planned_candidates
        }
        published: list[PublishedArtifactCandidate] = []
        observed_by_slot = {
            artifact.output_slot: artifact for artifact in isolated_completion.artifacts
        }
        observed_slots: set[str] = set()
        for output in result.artifacts:
            planned = planned_by_slot.get(output.output_slot)
            if planned is None:
                raise RuntimeError(f"unexpected Artifact output slot: {output.output_slot}")
            expectation = planned.expectation
            if (
                output.relative_staging_path != expectation.relative_staging_path
                or output.artifact_kind != expectation.artifact_kind
                or output.media_type != expectation.media_type
            ):
                raise RuntimeError(f"Artifact output contract mismatch: {output.output_slot}")
            if output.output_slot in observed_slots:
                raise RuntimeError(f"duplicate Artifact output slot: {output.output_slot}")
            if (
                self._sensitive_output_gate.assert_clean_file(
                    "stata.artifact_candidate", Path(output.source_path)
                ).verdict
                != "safe"
            ):
                raise RuntimeError("CREDENTIAL_OUTPUT_BLOCKED")
            observed_slots.add(output.output_slot)
            payload = self._managed_store.publish_candidate(
                Path(output.source_path),
                artifact_id=planned.artifact_id,
                attempt_id=handle.attempt_id,
            )
            observed = observed_by_slot.get(output.output_slot)
            if (
                observed is None
                or observed.size_bytes != payload.size_bytes
                or observed.sha256 != payload.sha256
            ):
                raise RuntimeError(
                    f"Artifact changed after Completion Manifest: {output.output_slot}"
                )
            published.append(
                PublishedArtifactCandidate(
                    candidate_id=planned.candidate_id,
                    artifact_id=planned.artifact_id,
                    promotion_id=planned.promotion_id,
                    state_observation_id=planned.state_observation_id,
                    location_id=planned.location_id,
                    output_slot=output.output_slot,
                    relative_staging_path=output.relative_staging_path,
                    artifact_kind=output.artifact_kind,
                    media_type=output.media_type,
                    producer_locator=output.producer_locator,
                    expected=output.expected,
                    managed_handle=payload.managed_handle,
                    size_bytes=payload.size_bytes,
                    sha256=payload.sha256,
                )
            )
        missing_required_slots = {
            planned.expectation.output_slot
            for planned in handle.planned_candidates
            if planned.expectation.required
        } - observed_slots
        if missing_required_slots and result.receipt.execution_status.value == "succeeded":
            raise RuntimeError(
                f"required Artifact outputs were not captured: {sorted(missing_required_slots)}"
            )
        return tuple(published)
