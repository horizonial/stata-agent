"""Open, contract-driven Tool Call normalization, planning, and JIT admission."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

from stata_research_agent.domain.identifiers import (
    BudgetUsageId,
    CanonicalArgumentsSnapshotId,
    DispatchPlanEntryId,
    DispatchPlanId,
    OperationId,
    RawArgumentsSnapshotId,
    ResourceClaimId,
    ToolAdmissionId,
    ToolCallId,
    ToolContractId,
    ToolResultId,
)

from .ports.identity import IdentityGenerator
from .ports.tool_broker import ToolBrokerRepository
from .tool_broker import (
    AdmitToolCallCommand,
    CreateDispatchPlanCommand,
    DispatchPlanIdentity,
    DispatchPlanOutcome,
    ExecutorExceptionOutcome,
    PreparedToolCall,
    RecordExecutorExceptionCommand,
    RecordToolAdmissionBlockedCommand,
    RegisteredToolContract,
    RegisterToolContractCommand,
    ResolvedResourceClaim,
    ToolAdmissionBlockedOutcome,
    ToolAdmissionIdentity,
    ToolAdmissionOutcome,
    ToolContractDefinition,
)

_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_-]*(?:\.[a-z][a-z0-9_-]*)+$")
_REASON_CODE = re.compile(r"^[a-z][a-z0-9_]{2,127}$")


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class ToolBrokerService:
    def __init__(self, repository: ToolBrokerRepository, identities: IdentityGenerator) -> None:
        self._repository = repository
        self._identities = identities

    def register_contract(self, command: RegisterToolContractCommand) -> RegisteredToolContract:
        definition = command.definition
        if _TOOL_NAME.fullmatch(definition.tool_name) is None:
            raise ValueError("tool name must use the open dotted tool namespace")
        if definition.default_timeout_seconds <= 0:
            raise ValueError("default timeout must be positive")
        if definition.max_timeout_seconds < definition.default_timeout_seconds:
            raise ValueError("max timeout cannot be lower than default timeout")
        if definition.max_output_bytes < 1:
            raise ValueError("max output size must be positive")
        if (
            definition.execution_owner == "local_runtime"
            and definition.operation_kind in {"python.execute", "shell.execute"}
            and definition.execution_isolation != "sandboxed_staged_execution"
        ):
            raise ValueError("local Python/Shell Tool Contracts require staged OS isolation")
        contract_json = self._contract_json(definition)
        return self._repository.register_contract(
            command,
            self._identities.new(ToolContractId),
            _hash(contract_json),
        )

    def create_dispatch_plan(self, command: CreateDispatchPlanCommand) -> DispatchPlanOutcome:
        if not command.calls:
            raise ValueError("a Dispatch Plan requires at least one proposed Tool Call")
        prepared: list[PreparedToolCall] = []
        current_batch = 0
        batch_claims: list[ResolvedResourceClaim] = []
        batch_parallel = True
        for ordinal, proposal in enumerate(command.calls, start=1):
            tool_call_id = self._identities.new(ToolCallId)
            raw_id = self._identities.new(RawArgumentsSnapshotId)
            contract = self._repository.load_contract(proposal.requested_tool_name)
            rejected_reason = ""
            arguments: dict[str, Any] | None = None
            diff: dict[str, Any] = {}
            if contract is None:
                rejected_reason = "tool_contract_not_registered"
            else:
                try:
                    parsed = json.loads(proposal.raw_arguments_text)
                    if not isinstance(parsed, dict):
                        raise ValueError("arguments must be a JSON object")
                    arguments, diff = self._normalize_arguments(
                        parsed, contract.definition.input_schema
                    )
                except (json.JSONDecodeError, ValueError, TypeError):
                    rejected_reason = "invalid_tool_arguments"

            if contract is None or arguments is None:
                prepared.append(
                    PreparedToolCall(
                        tool_call_id,
                        raw_id,
                        None,
                        self._identities.new(ToolResultId),
                        ordinal,
                        proposal,
                        contract,
                        None,
                        None,
                        None,
                        "rejected",
                        rejected_reason,
                        None,
                        (),
                        None,
                        False,
                        False,
                    )
                )
                continue

            arguments_json = _canonical_json(arguments)
            claims = self._resolve_claims(contract.definition, arguments)
            parallel = contract.definition.concurrency_class == "parallel_safe"
            if (
                current_batch == 0
                or not parallel
                or not batch_parallel
                or self._has_conflict(batch_claims, [claim for _, claim in claims])
            ):
                current_batch += 1
                batch_claims = []
                batch_parallel = parallel
            batch_claims.extend(claim for _, claim in claims)
            prepared.append(
                PreparedToolCall(
                    tool_call_id,
                    raw_id,
                    self._identities.new(CanonicalArgumentsSnapshotId),
                    None,
                    ordinal,
                    proposal,
                    contract,
                    arguments_json,
                    _hash(arguments_json),
                    _canonical_json(diff),
                    "scheduled",
                    "preflight_passed",
                    self._identities.new(DispatchPlanEntryId),
                    claims,
                    current_batch,
                    not parallel,
                    not parallel,
                )
            )
            if not parallel:
                batch_parallel = False

        dependency_json = _canonical_json(command.dependency_snapshot)
        return self._repository.commit_dispatch_plan(
            command,
            DispatchPlanIdentity(self._identities.new(DispatchPlanId)),
            tuple(prepared),
            dependency_json,
            _hash(dependency_json),
        )

    def admit(self, command: AdmitToolCallCommand) -> ToolAdmissionOutcome:
        dependency_json = _canonical_json(command.current_dependency_snapshot)
        return self._repository.admit(
            command,
            ToolAdmissionIdentity(
                self._identities.new(ToolAdmissionId),
                self._identities.new(OperationId),
                self._identities.new(BudgetUsageId),
            ),
            dependency_json,
            _hash(dependency_json),
        )

    def record_executor_exception(
        self, command: RecordExecutorExceptionCommand
    ) -> ExecutorExceptionOutcome:
        if not command.error_kind.strip():
            raise ValueError("executor exception kind is required")
        return self._repository.record_executor_exception(command)

    def record_admission_blocked(
        self, command: RecordToolAdmissionBlockedCommand
    ) -> ToolAdmissionBlockedOutcome:
        if _REASON_CODE.fullmatch(command.reason_code) is None:
            raise ValueError("admission reason code must be a stable lowercase token")
        return self._repository.record_admission_blocked(command)

    @staticmethod
    def _contract_json(definition: ToolContractDefinition) -> str:
        return _canonical_json(
            {
                "tool_name": definition.tool_name,
                "tool_version": definition.tool_version,
                "display_name": definition.display_name,
                "operation_kind": definition.operation_kind,
                "input_schema": dict(definition.input_schema),
                "output_schema": dict(definition.output_schema),
                "execution_owner": definition.execution_owner,
                "effect_class": definition.effect_class,
                "concurrency_class": definition.concurrency_class,
                "replay_class": definition.replay_class,
                "confirmation_policy": definition.confirmation_policy,
                "pause_behavior": definition.pause_behavior,
                "default_timeout_seconds": definition.default_timeout_seconds,
                "max_timeout_seconds": definition.max_timeout_seconds,
                "max_output_bytes": definition.max_output_bytes,
                "resource_claim_templates": [
                    {
                        "key_template": item.key_template,
                        "access_mode": item.access_mode,
                        "identity_argument": item.identity_argument,
                    }
                    for item in definition.resource_claim_templates
                ],
                "execution_isolation": definition.execution_isolation,
            }
        )

    @staticmethod
    def _normalize_arguments(
        arguments: dict[str, Any], schema: Mapping[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if schema.get("type") != "object":
            raise ValueError("V0.1 Tool Contract input schema must be an object")
        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            raise ValueError("schema properties must be an object")
        required = schema.get("required", [])
        if not isinstance(required, list) or not all(isinstance(item, str) for item in required):
            raise ValueError("schema required must be a string array")
        missing = [name for name in required if name not in arguments]
        if missing:
            raise ValueError("required tool arguments are missing")
        if schema.get("additionalProperties", True) is False:
            unknown = set(arguments) - set(properties)
            if unknown:
                raise ValueError("unknown tool arguments are forbidden")
        normalized = dict(arguments)
        defaults: dict[str, Any] = {}
        for name, property_schema in properties.items():
            if not isinstance(property_schema, dict):
                raise ValueError("property schema must be an object")
            if name not in normalized and "default" in property_schema:
                normalized[name] = property_schema["default"]
                defaults[name] = property_schema["default"]
            if name in normalized:
                ToolBrokerService._validate_json_type(normalized[name], property_schema.get("type"))
        return normalized, {"deterministic_defaults": defaults}

    @staticmethod
    def _validate_json_type(value: Any, expected: Any) -> None:
        expected_types: dict[str, type[Any] | tuple[type[Any], ...]] = {
            "string": str,
            "number": (int, float),
            "integer": int,
            "boolean": bool,
            "object": dict,
            "array": list,
        }
        if expected is None:
            return
        wanted = expected_types.get(str(expected))
        if wanted is None or not isinstance(value, wanted):
            raise ValueError("tool argument type does not match schema")
        if expected in {"number", "integer"} and isinstance(value, bool):
            raise ValueError("boolean is not a JSON number for Tool Contract validation")

    def _resolve_claims(
        self, definition: ToolContractDefinition, arguments: Mapping[str, Any]
    ) -> tuple[tuple[ResourceClaimId, ResolvedResourceClaim], ...]:
        claims: list[tuple[ResourceClaimId, ResolvedResourceClaim]] = []
        for template in definition.resource_claim_templates:
            try:
                key = template.key_template.format_map(arguments)
            except (KeyError, ValueError) as error:
                raise ValueError("resource claim could not be resolved") from error
            identity = (
                str(arguments[template.identity_argument])
                if template.identity_argument is not None
                else "stable"
            )
            claims.append(
                (
                    self._identities.new(ResourceClaimId),
                    ResolvedResourceClaim(key, template.access_mode, identity),
                )
            )
        if not claims and definition.concurrency_class != "parallel_safe":
            raise ValueError("non-parallel Tool Contract requires an explicit resource claim")
        return tuple(claims)

    @staticmethod
    def _has_conflict(
        existing: list[ResolvedResourceClaim], incoming: list[ResolvedResourceClaim]
    ) -> bool:
        for left in existing:
            for right in incoming:
                if left.resource_key != right.resource_key:
                    continue
                if left.access_mode != "read" or right.access_mode != "read":
                    return True
        return False
