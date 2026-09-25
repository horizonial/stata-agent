"""Runtime composition of the bounded Agent loop over inward application services."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal, cast

from stata_research_agent.application.context_budget import DynamicContextBudgetPolicy
from stata_research_agent.application.context_compiler import (
    CompiledStepContext,
    ContextCompiler,
    EmptyContextAuthorityReader,
)
from stata_research_agent.application.diagnostic_tracing import DiagnosticTracer
from stata_research_agent.application.evaluation import (
    DecideNaturalStopCommand,
    EvaluationDimension,
    EvaluationFindingCandidate,
    EvaluationVerdict,
    RecordEvaluationCommand,
    RecordObligationStateCommand,
    StopGuardOutcome,
)
from stata_research_agent.application.evaluation_service import RuntimeEvaluationService
from stata_research_agent.application.model_gateway import (
    ContextItemCandidate,
    StartModelStepCommand,
)
from stata_research_agent.application.model_gateway_service import ModelGatewayService
from stata_research_agent.application.ports.identity import IdentityGenerator
from stata_research_agent.application.sensitive_output import (
    SensitiveOutputGate,
    SensitiveOutputGateUnavailable,
)
from stata_research_agent.application.tool_broker import (
    AdmitToolCallCommand,
    CreateDispatchPlanCommand,
    RawToolProposal,
    RecordExecutorExceptionCommand,
    RecordToolAdmissionBlockedCommand,
    ToolAdmissionBlockedError,
    ToolAdmissionOutcome,
)
from stata_research_agent.application.tool_broker_service import ToolBrokerService
from stata_research_agent.application.turn_driver import (
    AdmittedToolExecutor,
    JsonObject,
    RetrievalSessionFinalizer,
    ToolExecutionRequest,
    ToolExecutionResult,
    TurnDriverConfig,
    TurnDriverOutcome,
    TurnRuntimeBudgetLedger,
)
from stata_research_agent.application.turn_interaction import OpenWaitingCommand
from stata_research_agent.application.turn_interaction_service import TurnInteractionService
from stata_research_agent.domain.identifiers import (
    CommandId,
    ResearchPathId,
    StepId,
    ToolCallId,
    TurnId,
)
from stata_research_agent.domain.status import (
    StopGuardDecision,
    TerminalDisposition,
    WaitReason,
)

from .ipc_contract import (
    CompletionProposal,
    EvaluationProposal,
    PlanProposal,
    ToolProposal,
    WaitingProposal,
    WorkerBootstrapContext,
    WorkerCompletion,
    WorkerEvaluation,
    WorkerPlan,
    WorkerToolCall,
    WorkerWaiting,
)
from .model_evaluation_coordinator import ModelEvaluationCoordinator
from .plan_coordinator import ResearchPlanCoordinator
from .turn_worker import TurnWorkerProcess


class AgentTurnDriver:
    """Run model → Worker proposals → Broker → executor → Stop Guard."""

    def __init__(
        self,
        gateway: ModelGatewayService,
        broker: ToolBrokerService,
        evaluator: RuntimeEvaluationService,
        executor: AdmittedToolExecutor,
        identities: IdentityGenerator,
        worker_python: Path,
        sensitive_output_gate: SensitiveOutputGate | None = None,
        context_compiler: ContextCompiler | None = None,
        plan_coordinator: ResearchPlanCoordinator | None = None,
        turn_interactions: TurnInteractionService | None = None,
        model_evaluation: ModelEvaluationCoordinator | None = None,
        retrieval_finalizer: RetrievalSessionFinalizer | None = None,
        monotonic_clock: Callable[[], float] = time.monotonic,
        runtime_budget_ledger: TurnRuntimeBudgetLedger | None = None,
        tracer: DiagnosticTracer | None = None,
        context_budget_policy: DynamicContextBudgetPolicy | None = None,
    ) -> None:
        self._gateway = gateway
        self._broker = broker
        self._evaluator = evaluator
        self._executor = executor
        self._identities = identities
        self._worker_python = worker_python
        self._sensitive_output_gate = sensitive_output_gate or SensitiveOutputGate()
        self._context_compiler = context_compiler or ContextCompiler(EmptyContextAuthorityReader())
        self._has_authoritative_context = context_compiler is not None
        self._plan_coordinator = plan_coordinator
        self._turn_interactions = turn_interactions
        self._model_evaluation = model_evaluation
        self._retrieval_finalizer = retrieval_finalizer
        self._monotonic_clock = monotonic_clock
        self._runtime_budget_ledger = runtime_budget_ledger
        self._tracer = tracer
        self._context_budget_policy = context_budget_policy or DynamicContextBudgetPolicy()

    async def run(self, config: TurnDriverConfig) -> TurnDriverOutcome:
        if (
            config.max_loop_steps < 1
            or config.max_tool_admissions < 1
            or config.max_wall_clock_seconds <= 0
            or not config.runtime_policy_revision.strip()
        ):
            raise ValueError(
                "Turn Driver loop, Tool, and wall-clock limits must be positive and "
                "runtime policy revision must be non-empty"
            )
        segment_started = self._monotonic_clock()
        available_seconds = config.max_wall_clock_seconds
        if self._runtime_budget_ledger is not None:
            available_seconds = self._runtime_budget_ledger.remaining_seconds(
                config.turn_id,
                policy_revision=config.runtime_policy_revision,
                configured_max_seconds=config.max_wall_clock_seconds,
            )
        if available_seconds <= 0:
            return self._deadline_outcome(config.turn_id, 0, 0)
        deadline = segment_started + available_seconds
        worker = TurnWorkerProcess(self._worker_python, tracer=self._tracer)
        transient_items: list[ContextItemCandidate] = []
        tool_executions = 0
        executed_steps = 0
        loop_ordinal = 0
        next_trigger: Literal[
            "initial", "tool_results_committed", "stop_guard_continue", "driver_feedback"
        ] = "initial"
        await worker.start(
            turn_id=config.turn_id.value,
            context_revision=config.initial_turn_revision,
            context=WorkerBootstrapContext(
                workspace_id=config.workspace_id,
                execution_scope_id=config.execution_scope_id,
                research_path_id=config.research_path_id,
                research_state_revision=0,
                tool_names=tuple(
                    str(schema["name"]) for schema in config.model.tool_schemas if "name" in schema
                ),
                remaining_step_budget=config.max_loop_steps,
                remaining_tool_budget=config.max_tool_admissions,
            ),
        )
        try:
            while executed_steps < config.max_loop_steps:
                if self._deadline_reached(deadline):
                    return self._deadline_outcome(config.turn_id, executed_steps, tool_executions)
                loop_ordinal += 1
                remaining_steps = config.max_loop_steps - executed_steps
                remaining_tools = config.max_tool_admissions - tool_executions
                model_request = await worker.request_model_invocation(
                    step_ordinal=loop_ordinal,
                    trigger=next_trigger,
                    remaining_step_budget=remaining_steps,
                    remaining_tool_budget=remaining_tools,
                )
                compiled, input_token_limit = self._compile_context(
                    config,
                    tuple(transient_items),
                    remaining_step_budget=model_request.remaining_step_budget,
                    remaining_tool_budget=model_request.remaining_tool_budget,
                )
                step = await self._gateway.execute_step(
                    self._step_command(
                        config,
                        compiled,
                        input_token_limit=input_token_limit,
                        remaining_step_budget=model_request.remaining_step_budget,
                        remaining_tool_budget=model_request.remaining_tool_budget,
                    )
                )
                executed_steps += 1
                if self._deadline_reached(deadline):
                    return self._deadline_outcome(config.turn_id, executed_steps, tool_executions)
                if step.status != "completed" or step.output is None:
                    return TurnDriverOutcome(
                        config.turn_id,
                        step.status,
                        executed_steps,
                        tool_executions,
                        None,
                        step.failure_code,
                    )
                messages = await worker.process_model_output(
                    step_ordinal=loop_ordinal,
                    **self._parse_output(step.output),
                )
                tools = tuple(message for message in messages if isinstance(message, ToolProposal))
                plan_proposal = next(
                    (message for message in messages if isinstance(message, PlanProposal)),
                    None,
                )
                waiting = next(
                    (message for message in messages if isinstance(message, WaitingProposal)),
                    None,
                )
                completion = next(
                    (message for message in messages if isinstance(message, CompletionProposal)),
                    None,
                )
                evaluation = next(
                    (message for message in messages if isinstance(message, EvaluationProposal)),
                    None,
                )
                research_path_id = ResearchPathId(config.research_path_id)
                if waiting is not None:
                    if tools or completion is not None:
                        raise ValueError(
                            "Waiting Proposal cannot be combined with Tool or Completion proposals"
                        )
                    if self._turn_interactions is None:
                        raise RuntimeError("Waiting Proposal requires a TurnInteractionService")
                    self._turn_interactions.open_waiting(
                        OpenWaitingCommand(
                            self._identities.new(CommandId),
                            config.turn_id,
                            config.initial_turn_revision,
                            WaitReason(waiting.reason),
                            waiting.prompt,
                        )
                    )
                    return TurnDriverOutcome(
                        config.turn_id, "waiting", executed_steps, tool_executions, None
                    )
                if plan_proposal is not None and self._plan_coordinator is not None:
                    self._plan_coordinator.commit_proposal(
                        config.turn_id, research_path_id, plan_proposal
                    )
                if tools:
                    if self._plan_coordinator is not None:
                        self._plan_coordinator.ensure_minimal_formal_plan(
                            config.turn_id, research_path_id, tools
                        )
                        tools = self._plan_coordinator.bind_formal_tools(research_path_id, tools)
                    assert step.assistant_output_id is not None
                    plan = self._broker.create_dispatch_plan(
                        CreateDispatchPlanCommand(
                            self._identities.new(CommandId),
                            step.assistant_output_id,
                            config.model.tool_catalog_revision,
                            config.dependency_snapshot,
                            tuple(self._raw_proposal(item) for item in tools),
                        )
                    )
                    if len(plan.scheduled_call_ids) != len(tools):
                        transient_items.append(
                            ContextItemCandidate(
                                "tool_result",
                                "dispatch_plan",
                                plan.dispatch_plan_id.value,
                                str(plan.plan_revision),
                                "remote_allowed",
                                "One or more proposed tools were rejected by the Tool Broker.",
                                "tool_output_untrusted",
                            )
                        )
                        next_trigger = "driver_feedback"
                        continue
                    proposal_by_ordinal = {
                        ordinal: proposal for ordinal, proposal in enumerate(tools, start=1)
                    }
                    batches: dict[int, list[tuple[ToolCallId, ToolProposal]]] = {}
                    for scheduled in plan.scheduled_calls:
                        batches.setdefault(scheduled.execution_batch_ordinal, []).append(
                            (
                                scheduled.tool_call_id,
                                proposal_by_ordinal[scheduled.call_ordinal],
                            )
                        )
                    for batch_ordinal in sorted(batches):
                        if self._deadline_reached(deadline):
                            return self._deadline_outcome(
                                config.turn_id, executed_steps, tool_executions
                            )
                        admitted: list[tuple[ToolAdmissionOutcome, ToolProposal]] = []
                        for tool_call_id, proposal in batches[batch_ordinal]:
                            if self._deadline_reached(deadline):
                                return self._deadline_outcome(
                                    config.turn_id, executed_steps, tool_executions
                                )
                            try:
                                admission = self._broker.admit(
                                    AdmitToolCallCommand(
                                        self._identities.new(CommandId),
                                        tool_call_id,
                                        config.initial_turn_revision,
                                        "admission-v0.1",
                                        config.allowed_effect_classes,
                                        config.dependency_snapshot,
                                        config.max_tool_admissions,
                                    )
                                )
                            except ValueError as error:
                                reason_code = (
                                    error.reason_code
                                    if isinstance(error, ToolAdmissionBlockedError)
                                    else "admission_validation_error"
                                )
                                self._broker.record_admission_blocked(
                                    RecordToolAdmissionBlockedCommand(
                                        self._identities.new(CommandId),
                                        config.turn_id,
                                        tool_call_id,
                                        reason_code,
                                    )
                                )
                                transient_items.append(
                                    ContextItemCandidate(
                                        "tool_admission_feedback",
                                        "tool_call",
                                        tool_call_id.value,
                                        "rejected",
                                        "remote_allowed",
                                        (
                                            "Tool Admission rejected: "
                                            f"{reason_code}. "
                                            "Revise the call or choose a different next action; "
                                            "do not repeat it unchanged."
                                        ),
                                        "tool_output_untrusted",
                                    )
                                )
                                continue
                            admitted.append((admission, proposal))
                        results = await asyncio.gather(
                            *(
                                self._execute_admitted(
                                    config.turn_id,
                                    admission,
                                    proposal,
                                    self._remaining_time(deadline),
                                )
                                for admission, proposal in admitted
                            ),
                            return_exceptions=True,
                        )
                        for (admission, proposal), raw_result in zip(
                            admitted, results, strict=True
                        ):
                            tool_executions += 1
                            if isinstance(raw_result, BaseException):
                                error_detail = self._safe_executor_error_detail(raw_result)
                                self._broker.record_executor_exception(
                                    RecordExecutorExceptionCommand(
                                        self._identities.new(CommandId),
                                        config.turn_id,
                                        admission.tool_call_id,
                                        admission.operation_id,
                                        type(raw_result).__name__,
                                        error_detail,
                                    )
                                )
                                transient_items.append(
                                    ContextItemCandidate(
                                        "tool_result",
                                        "tool_call",
                                        admission.tool_call_id.value,
                                        "failed",
                                        "remote_allowed",
                                        (
                                            "Tool execution failed with "
                                            f"{type(raw_result).__name__}: {error_detail}. "
                                            "Revise the call according to this validation "
                                            "failure; do not repeat it unchanged."
                                        ),
                                        "tool_output_untrusted",
                                    )
                                )
                                continue
                            result = self._inspect_tool_result(raw_result)
                            if not self._has_authoritative_context:
                                transient_items.append(
                                    ContextItemCandidate(
                                        "tool_result",
                                        "tool_call",
                                        admission.tool_call_id.value,
                                        str(result.success).lower(),
                                        "remote_allowed",
                                        result.context_text,
                                        "tool_output_untrusted",
                                    )
                                )
                            if result.success:
                                self._satisfy_tool_obligations(
                                    config, proposal, admission.tool_call_id
                                )
                    next_trigger = "tool_results_committed"
                    continue
                if completion is None:
                    transient_items.append(
                        ContextItemCandidate(
                            "driver_feedback",
                            "turn",
                            config.turn_id.value,
                            str(loop_ordinal),
                            "remote_allowed",
                            "No Tool Call or Completion Proposal was produced. Choose one.",
                        )
                    )
                    next_trigger = "driver_feedback"
                    continue
                if self._retrieval_finalizer is not None:
                    self._retrieval_finalizer.conclude_open_sessions(config.turn_id)
                stop_step_id = step.step_id
                stop_evaluation: EvaluationProposal | WorkerEvaluation | None = evaluation
                independent = False
                if (
                    self._model_evaluation is not None
                    and not self._deadline_reached(deadline)
                    and executed_steps < config.max_loop_steps
                ):
                    # An independent Evaluator is an optional extra Model Invocation. If the
                    # research model used the final Step to propose completion, no Step budget
                    # remains for that extra call. The already-produced proposal must still go
                    # through the deterministic Stop Guard instead of being discarded as
                    # budget_exhausted. If the Guard rejects it, the ordinary loop boundary
                    # below reports exhaustion because no further semantic Step is permitted.
                    evaluator_context = ContextItemCandidate(
                        "evaluation_checkpoint",
                        "turn",
                        config.turn_id.value,
                        str(loop_ordinal),
                        "remote_allowed",
                        self._evaluation_request(completion, evaluation),
                    )
                    evaluation_remaining_steps = config.max_loop_steps - executed_steps
                    evaluation_remaining_tools = config.max_tool_admissions - tool_executions
                    compiled_evaluation, evaluation_input_limit = self._compile_context(
                        config,
                        tuple((*transient_items, evaluator_context)),
                        remaining_step_budget=evaluation_remaining_steps,
                        remaining_tool_budget=evaluation_remaining_tools,
                    )
                    evaluated = await self._model_evaluation.evaluate(
                        self._step_command(
                            config,
                            compiled_evaluation,
                            input_token_limit=evaluation_input_limit,
                            remaining_step_budget=evaluation_remaining_steps,
                            remaining_tool_budget=evaluation_remaining_tools,
                        )
                    )
                    executed_steps += 1
                    stop_step_id = evaluated.step_id
                    stop_evaluation = evaluated.evaluation or WorkerEvaluation(
                        verdict="warn",
                        findings=("quality_concern",),
                    )
                    independent = True
                stop = self._evaluate_stop(
                    config,
                    stop_step_id,
                    completion,
                    stop_evaluation,
                    independent=independent,
                )
                if stop.decision is StopGuardDecision.TERMINATE:
                    return TurnDriverOutcome(
                        config.turn_id,
                        stop.turn_status,
                        executed_steps,
                        tool_executions,
                        stop,
                    )
                if stop.decision is StopGuardDecision.WAIT:
                    return TurnDriverOutcome(
                        config.turn_id, "waiting", executed_steps, tool_executions, stop
                    )
                transient_items.append(
                    ContextItemCandidate(
                        "stop_guard_feedback",
                        "stop_guard_decision",
                        stop.decision_id.value,
                        str(stop.commit_revision.value),
                        "remote_allowed",
                        f"Natural stop rejected: {stop.reason_code}; blockers={stop.blockers}",
                    )
                )
                next_trigger = "stop_guard_continue"
            return TurnDriverOutcome(
                config.turn_id,
                "budget_exhausted",
                config.max_loop_steps,
                tool_executions,
                None,
            )
        finally:
            if worker.returncode is None:
                await worker.stop()
            if self._runtime_budget_ledger is not None:
                elapsed = min(
                    available_seconds,
                    max(0.0, self._monotonic_clock() - segment_started),
                )
                self._runtime_budget_ledger.consume_seconds(config.turn_id, elapsed)

    def _satisfy_tool_obligations(
        self, config: TurnDriverConfig, proposal: ToolProposal, tool_call_id: ToolCallId
    ) -> None:
        for obligation_id in config.obligation_by_tool_name.get(proposal.tool_name, ()):
            self._evaluator.record_obligation_state(
                RecordObligationStateCommand(
                    self._identities.new(CommandId),
                    config.turn_id,
                    obligation_id,
                    "satisfied",
                    (f"tool_call:{tool_call_id.value}",),
                    "system_fact",
                    "Registered tool completed successfully.",
                )
            )

    def _evaluate_stop(
        self,
        config: TurnDriverConfig,
        step_id: StepId,
        completion: CompletionProposal,
        evaluation: EvaluationProposal | WorkerEvaluation | None,
        *,
        independent: bool = False,
    ) -> StopGuardOutcome:
        codes = (
            ("completion_ready",)
            if evaluation is None or not evaluation.findings
            else evaluation.findings
        )
        verdict = (
            EvaluationVerdict.PASS if evaluation is None else EvaluationVerdict(evaluation.verdict)
        )
        report = self._evaluator.record_evaluation(
            RecordEvaluationCommand(
                self._identities.new(CommandId),
                config.turn_id,
                config.initial_turn_revision,
                step_id,
                "natural_stop_readiness",
                EvaluationDimension.COMPLETION,
                "worker_completion_proposal",
                "turn",
                config.turn_id.value,
                str(config.initial_turn_revision),
                (),
                ("goal_coverage", "runtime_state"),
                ("hidden_candidates", "unadopted_paths"),
                "model" if independent else "rule",
                "research-evaluator-v1" if independent else "completion-rule-v1",
                verdict,
                tuple(
                    EvaluationFindingCandidate(
                        code,
                        "warn"
                        if code in {"quality_concern", "research_semantic_ambiguity"}
                        else "info",
                        completion.summary,
                        "medium" if verdict is EvaluationVerdict.UNKNOWN else "high",
                    )
                    for code in codes
                ),
                ("research decision unresolved",) if verdict is EvaluationVerdict.UNKNOWN else (),
            )
        )
        return self._evaluator.decide_natural_stop(
            DecideNaturalStopCommand(
                self._identities.new(CommandId),
                config.turn_id,
                config.initial_turn_revision,
                report.evaluation_report_id,
                TerminalDisposition(completion.disposition),
            )
        )

    def _evaluation_request(
        self,
        completion: CompletionProposal,
        evaluation: EvaluationProposal | None,
    ) -> str:
        self_assessment = (
            "none"
            if evaluation is None
            else json.dumps(
                {
                    "verdict": evaluation.verdict,
                    "findings": list(evaluation.findings),
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        return (
            self._evaluation_system_prompt()
            + " Inspect the authoritative context, current Plan, executed tools, Results, "
            "Evidence, completion obligations, and unresolved user decisions. "
            f"Candidate completion={completion.disposition}:{completion.summary}; "
            f"acting-agent self-assessment={self_assessment}."
        )

    @staticmethod
    def _evaluation_system_prompt() -> str:
        return (
            "You are the independent research checkpoint evaluator, not the acting research "
            "Agent. "
            "You have no tools and must not continue the research. Return exactly a JSON object "
            "with text, an empty tool_calls array, and evaluation; never return plan, waiting, "
            "completion, or a non-empty tool_calls array. Use only registered "
            "finding codes: completion_ready, quality_concern, plan_deviation, "
            "required_evidence_missing, research_semantic_ambiguity, loop_no_progress. "
            "Use pass/completion_ready when required obligations and provenance gates are "
            "satisfied. Use warn/quality_concern for disclosed limitations or optional further "
            "work; those do not require user confirmation. Use research_semantic_ambiguity only "
            "when a specific consequential research choice truly requires the user's decision."
        )

    async def _execute_admitted(
        self,
        turn_id: TurnId,
        admission: ToolAdmissionOutcome,
        proposal: ToolProposal,
        remaining_time_seconds: float,
    ) -> ToolExecutionResult:
        return await self._executor.execute(
            ToolExecutionRequest(
                turn_id,
                admission.tool_call_id,
                admission.operation_id,
                proposal.tool_name,
                proposal.arguments,
                remaining_time_seconds,
            )
        )

    def _deadline_reached(self, deadline: float) -> bool:
        return self._monotonic_clock() >= deadline

    def _remaining_time(self, deadline: float) -> float:
        return max(0.0, deadline - self._monotonic_clock())

    @staticmethod
    def _deadline_outcome(
        turn_id: TurnId, executed_steps: int, tool_executions: int
    ) -> TurnDriverOutcome:
        return TurnDriverOutcome(
            turn_id,
            "runtime_deadline_exceeded",
            executed_steps,
            tool_executions,
            None,
        )

    def _inspect_tool_result(self, result: ToolExecutionResult) -> ToolExecutionResult:
        try:
            inspected_result = self._sensitive_output_gate.inspect_text(
                "tool.agent_context", result.context_text
            )
        except SensitiveOutputGateUnavailable:
            inspected_result = None
        if inspected_result is None or inspected_result.verdict != "safe":
            return ToolExecutionResult(
                False,
                "CREDENTIAL_OUTPUT_GATE_UNAVAILABLE"
                if inspected_result is None
                else "CREDENTIAL_OUTPUT_BLOCKED",
            )
        return result

    def _safe_executor_error_detail(self, error: BaseException) -> str:
        """Return bounded, redacted validation feedback without leaking arbitrary failures."""

        if not isinstance(error, (ValueError, RuntimeError)):
            return type(error).__name__
        candidate = str(error).strip()[:500] or type(error).__name__
        try:
            inspected = self._sensitive_output_gate.inspect_text("tool.executor_error", candidate)
        except SensitiveOutputGateUnavailable:
            return type(error).__name__
        if inspected.verdict not in {"safe", "redacted"}:
            return type(error).__name__
        return str(inspected.safe_value)

    def _step_command(
        self,
        config: TurnDriverConfig,
        compiled: CompiledStepContext,
        *,
        input_token_limit: int,
        remaining_step_budget: int,
        remaining_tool_budget: int,
    ) -> StartModelStepCommand:
        model = config.model
        return StartModelStepCommand(
            self._identities.new(CommandId),
            config.turn_id,
            config.initial_turn_revision,
            model.system_prompt_revision,
            model.system_prompt,
            model.main_skill_name,
            model.main_skill_revision,
            model.main_skill_content,
            model.tool_catalog_revision,
            model.tool_schemas,
            compiled.items,
            compiled.decisions,
            model.permission_policy_revision,
            model.permissions,
            model.model_policy_revision,
            model.provider_profile,
            model.provider_kind,
            model.model_name,
            model.endpoint,
            model.credential_ref,
            model.provider_policy,
            model.remote_provider,
            input_token_limit=input_token_limit,
            remaining_step_budget=config.max_loop_steps,
            remaining_tool_budget=config.max_tool_admissions,
            current_remaining_step_budget=remaining_step_budget,
            current_remaining_tool_budget=remaining_tool_budget,
        )

    def _compile_context(
        self,
        config: TurnDriverConfig,
        transient_items: tuple[ContextItemCandidate, ...],
        *,
        remaining_step_budget: int,
        remaining_tool_budget: int,
    ) -> tuple[CompiledStepContext, int]:
        allocation = self._context_budget_policy.allocate(
            config.model,
            remaining_step_budget=remaining_step_budget,
            remaining_tool_budget=remaining_tool_budget,
        )
        compiled = self._context_compiler.compile(
            config.turn_id,
            static_items=config.initial_context,
            transient_items=transient_items,
            remote_provider=config.model.remote_provider,
            input_token_budget=allocation.context_item_budget_tokens,
        )
        return (
            replace(
                compiled,
                decisions=(*compiled.decisions, allocation.as_build_decision()),
            ),
            allocation.hard_input_token_limit,
        )

    @staticmethod
    def _raw_proposal(proposal: ToolProposal) -> RawToolProposal:
        return RawToolProposal(
            proposal.tool_name,
            json.dumps(
                proposal.arguments,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
            proposal.message_id,
        )

    @staticmethod
    def _parse_output(output: JsonObject) -> dict[str, Any]:
        raw_plan = output.get("plan")
        plan = None
        if isinstance(raw_plan, Mapping):
            structured = raw_plan.get("structured_plan", {})
            if not isinstance(structured, dict):
                raise ValueError("model plan structured_plan must be an object")
            plan = WorkerPlan(summary=str(raw_plan.get("summary", "")), structured_plan=structured)
        raw_calls = output.get("tool_calls", [])
        if not isinstance(raw_calls, list):
            raise ValueError("model tool_calls must be an array")
        calls: list[WorkerToolCall] = []
        for ordinal, raw_call in enumerate(raw_calls, start=1):
            if not isinstance(raw_call, Mapping):
                raise ValueError("model Tool Call must be an object")
            arguments = raw_call.get("arguments", {})
            if not isinstance(arguments, dict):
                raise ValueError("model Tool Call arguments must be an object")
            calls.append(
                WorkerToolCall(
                    call_ordinal=ordinal,
                    tool_name=str(raw_call.get("name", "")),
                    arguments=arguments,
                )
            )
        evaluation = AgentTurnDriver._parse_evaluation(output.get("evaluation"))
        waiting = AgentTurnDriver._parse_waiting(output.get("waiting"))
        completion = AgentTurnDriver._parse_completion(output.get("completion"))
        return {
            "text": str(output.get("text", "")),
            "plan": plan,
            "tool_calls": tuple(calls),
            "evaluation": evaluation,
            "waiting": waiting,
            "completion": completion,
        }

    @staticmethod
    def _parse_waiting(raw: Any) -> WorkerWaiting | None:
        if not isinstance(raw, Mapping):
            return None
        reason = str(raw.get("reason", "user_input"))
        if reason not in {"user_input", "user_confirmation", "external_resolution"}:
            raise ValueError("invalid model waiting reason")
        return WorkerWaiting(
            reason=cast(Literal["user_input", "user_confirmation", "external_resolution"], reason),
            prompt=str(raw.get("prompt", "")),
        )

    @staticmethod
    def _parse_evaluation(raw: Any) -> WorkerEvaluation | None:
        if not isinstance(raw, Mapping):
            return None
        verdict = str(raw.get("verdict", "pass"))
        if verdict not in {"pass", "warn", "fail", "unknown"}:
            raise ValueError("invalid model evaluation verdict")
        findings = raw.get("findings", [])
        if not isinstance(findings, list) or not all(isinstance(value, str) for value in findings):
            raise ValueError("model evaluation findings must be an array of codes")
        return WorkerEvaluation(
            verdict=cast(Literal["pass", "warn", "fail", "unknown"], verdict),
            findings=tuple(findings),
        )

    @staticmethod
    def _parse_completion(raw: Any) -> WorkerCompletion | None:
        if not isinstance(raw, Mapping):
            return None
        disposition = str(raw.get("disposition", "succeed"))
        if disposition not in {"succeed", "partial", "pause", "fail"}:
            raise ValueError("invalid model completion disposition")
        return WorkerCompletion(
            disposition=cast(Literal["succeed", "partial", "pause", "fail"], disposition),
            summary=str(raw.get("summary", "")),
        )
