"""Deterministic, provenance-preserving compilation of model context.

The compiler decides what a model may see for one Step.  It does not own research
facts: every item points back to an authoritative source and every omission or
compression is recorded as a build decision on the Step Context Manifest.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Protocol

from stata_research_agent.domain.identifiers import TurnId

from .model_gateway import ContextBuildDecisionCandidate, ContextItemCandidate


class ContextBudgetExceeded(ValueError):
    """Raised when mandatory context cannot fit without silent information loss."""


class ContextPrivacyBoundary(ValueError):
    """Raised when mandatory local-only context would have to leave the machine."""


@dataclass(frozen=True, slots=True)
class ContextSourceCandidate:
    item: ContextItemCandidate
    priority: int
    inclusion_reason: str
    mandatory: bool = False
    summarizable: bool = False
    group_key: str | None = None
    recency: int = 0

    def __post_init__(self) -> None:
        if self.priority not in {0, 1, 2, 3}:
            raise ValueError("context priority must be P0, P1, P2, or P3")
        if self.mandatory and self.priority != 0:
            raise ValueError("mandatory context must be P0")
        if not self.inclusion_reason.strip():
            raise ValueError("context inclusion reason is required")


class ContextAuthorityReader(Protocol):
    def collect(self, turn_id: TurnId) -> tuple[ContextSourceCandidate, ...]: ...


@dataclass(frozen=True, slots=True)
class CompiledStepContext:
    items: tuple[ContextItemCandidate, ...]
    decisions: tuple[ContextBuildDecisionCandidate, ...]
    estimated_input_tokens: int


class ContextCompiler:
    """Select context under a fixed budget without hiding important omissions."""

    def __init__(
        self,
        authority: ContextAuthorityReader,
        *,
        input_token_budget: int = 96_000,
        summary_token_target: int = 1_000,
    ) -> None:
        if input_token_budget < 1 or summary_token_target < 64:
            raise ValueError("invalid Context Compiler budget")
        self._authority = authority
        self._budget = input_token_budget
        self._summary_target = summary_token_target

    def compile(
        self,
        turn_id: TurnId,
        *,
        static_items: tuple[ContextItemCandidate, ...] = (),
        transient_items: tuple[ContextItemCandidate, ...] = (),
        remote_provider: bool = True,
    ) -> CompiledStepContext:
        candidates = [*self._authority.collect(turn_id)]
        candidates.extend(self._wrap_static(item) for item in static_items)
        candidates.extend(self._wrap_transient(item) for item in transient_items)
        candidates = self._deduplicate(candidates)

        # Selection happens by priority, while the original sequence remains the tie-breaker.
        indexed = list(enumerate(candidates))
        indexed.sort(key=lambda pair: (pair[1].priority, -pair[1].recency, pair[0]))
        grouped: list[list[tuple[int, ContextSourceCandidate]]] = []
        group_positions: dict[str, int] = {}
        for entry in indexed:
            candidate = entry[1]
            key = candidate.group_key or f"single:{entry[0]}"
            position = group_positions.get(key)
            if position is None:
                group_positions[key] = len(grouped)
                grouped.append([entry])
            else:
                grouped[position].append(entry)

        included: list[tuple[int, ContextItemCandidate]] = []
        decisions: list[ContextBuildDecisionCandidate] = []
        used = 0
        for group in grouped:
            group.sort(key=lambda pair: pair[0])
            sources = [candidate for _, candidate in group]
            if remote_provider and any(
                source.item.remote_transmission_class == "local_only" for source in sources
            ):
                if any(source.mandatory for source in sources):
                    raise ContextPrivacyBoundary(
                        "mandatory local-only context cannot be sent to a remote provider"
                    )
                decisions.extend(
                    self._decision(source, "excluded", "provider_policy") for source in sources
                )
                continue

            group_tokens = sum(self._estimate_tokens(source.item.content) for source in sources)
            if used + group_tokens <= self._budget:
                included.extend((ordinal, source.item) for ordinal, source in group)
                used += group_tokens
                decisions.extend(
                    self._decision(source, "included", source.inclusion_reason)
                    for source in sources
                )
                continue

            if any(source.mandatory for source in sources):
                raise ContextBudgetExceeded(
                    "mandatory P0 context exceeds the configured model-input budget"
                )

            summarized_group = self._summarize_group(group, self._budget - used)
            if summarized_group:
                for ordinal, source, item in summarized_group:
                    included.append((ordinal, item))
                    used += self._estimate_tokens(item.content)
                    decisions.append(
                        self._decision(
                            source,
                            "summarized",
                            "token_budget",
                            {"replacement_item_kind": item.item_kind},
                        )
                    )
                continue

            decisions.extend(
                self._decision(source, "excluded", "token_budget") for source in sources
            )

        # Model semantics use a stable order independent from selection traversal.
        included.sort(key=lambda pair: pair[0])
        return CompiledStepContext(
            tuple(item for _, item in included),
            tuple(decisions),
            used,
        )

    def _summarize_group(
        self,
        group: list[tuple[int, ContextSourceCandidate]],
        remaining_tokens: int,
    ) -> list[tuple[int, ContextSourceCandidate, ContextItemCandidate]]:
        if remaining_tokens < 64 or not all(source.summarizable for _, source in group):
            return []
        per_item = max(64, min(self._summary_target, remaining_tokens // len(group)))
        result: list[tuple[int, ContextSourceCandidate, ContextItemCandidate]] = []
        for ordinal, source in group:
            summary = self._extractive_summary(source.item.content, per_item)
            item = replace(
                source.item,
                item_kind="context_summary",
                source_revision=f"{source.item.source_revision}:summary-v1",
                content=summary,
            )
            if self._estimate_tokens(item.content) > remaining_tokens:
                return []
            remaining_tokens -= self._estimate_tokens(item.content)
            result.append((ordinal, source, item))
        return result

    @staticmethod
    def _wrap_static(item: ContextItemCandidate) -> ContextSourceCandidate:
        mandatory = item.item_kind == "user_message"
        return ContextSourceCandidate(
            item,
            0 if mandatory else 1,
            "turn_baseline" if mandatory else "workspace_relevance",
            mandatory=mandatory,
            summarizable=not mandatory,
        )

    @staticmethod
    def _wrap_transient(item: ContextItemCandidate) -> ContextSourceCandidate:
        return ContextSourceCandidate(item, 0, "runtime_feedback", mandatory=True, recency=10**9)

    @staticmethod
    def _deduplicate(
        candidates: list[ContextSourceCandidate],
    ) -> list[ContextSourceCandidate]:
        chosen: dict[tuple[str, str, str], ContextSourceCandidate] = {}
        order: list[tuple[str, str, str]] = []
        for candidate in candidates:
            item = candidate.item
            key = (
                item.item_kind,
                item.source_object_type,
                item.source_object_id,
            )
            prior = chosen.get(key)
            if prior is None:
                order.append(key)
                chosen[key] = candidate
            elif (candidate.mandatory, -candidate.priority, candidate.recency) > (
                prior.mandatory,
                -prior.priority,
                prior.recency,
            ):
                chosen[key] = candidate
        return [chosen[key] for key in order]

    @staticmethod
    def _decision(
        source: ContextSourceCandidate,
        kind: str,
        reason: str,
        extra: dict[str, object] | None = None,
    ) -> ContextBuildDecisionCandidate:
        detail: dict[str, object] = {
            "source_revision": source.item.source_revision,
            "priority": f"P{source.priority}",
            "mandatory": source.mandatory,
            "estimated_tokens": ContextCompiler._estimate_tokens(source.item.content),
        }
        if source.group_key is not None:
            detail["group_key"] = source.group_key
        if extra:
            detail.update(extra)
        return ContextBuildDecisionCandidate(
            kind,
            source.item.source_object_type,
            source.item.source_object_id,
            reason,
            detail,
        )

    @staticmethod
    def _estimate_tokens(content: str) -> int:
        return max(1, (len(content.encode("utf-8")) + 3) // 4 + 12)

    @staticmethod
    def _extractive_summary(content: str, token_target: int) -> str:
        byte_target = max(192, token_target * 4)
        encoded = content.encode("utf-8")
        if len(encoded) <= byte_target:
            return content
        marker = b"\n...[deterministic context compression; original remains authoritative]...\n"
        allowance = max(64, byte_target - len(marker))
        head = encoded[: allowance * 2 // 3].decode("utf-8", errors="ignore")
        tail = encoded[-allowance // 3 :].decode("utf-8", errors="ignore")
        return head + marker.decode("utf-8") + tail


class EmptyContextAuthorityReader:
    def collect(self, turn_id: TurnId) -> tuple[ContextSourceCandidate, ...]:
        del turn_id
        return ()
