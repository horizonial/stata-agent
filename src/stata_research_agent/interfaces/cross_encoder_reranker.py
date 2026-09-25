"""Optional local cross-encoder reranker adapter for the evidence pipeline."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from threading import Lock
from typing import Protocol

from stata_research_agent.application.retrieval_pipeline import (
    DeterministicEvidenceReranker,
    RerankAssessment,
    RerankCandidate,
)


class _CrossEncoderModel(Protocol):
    def predict(
        self,
        sentences: Sequence[tuple[str, str]],
        *,
        batch_size: int,
        show_progress_bar: bool,
    ) -> object: ...


class Qwen3CausalRerankerModel:
    """Qwen3 yes/no causal scorer following the model card's ranking contract."""

    def __init__(
        self,
        *,
        model_name: str,
        model_revision: str,
        cache_folder: str,
        device: str,
        max_length: int,
        instruction: str = (
            "Given a research literature query, retrieve passages that provide "
            "directly relevant evidence"
        ),
    ) -> None:
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        self._torch = torch
        self._device = device
        self._max_length = max_length
        self._instruction = instruction
        self._tokenizer = AutoTokenizer.from_pretrained(
            model_name,
            revision=model_revision,
            cache_dir=cache_folder,
            padding_side="left",
        )
        if self._tokenizer.pad_token_id is None:
            self._tokenizer.pad_token = self._tokenizer.eos_token
        dtype = torch.float16 if device.startswith("cuda") else torch.float32
        self._model = AutoModelForCausalLM.from_pretrained(
            model_name,
            revision=model_revision,
            cache_dir=cache_folder,
            dtype=dtype,
        ).to(device)
        self._model.eval()
        self._false_token_id = self._tokenizer.convert_tokens_to_ids("no")
        self._true_token_id = self._tokenizer.convert_tokens_to_ids("yes")
        prefix = (
            '<|im_start|>system\nJudge whether the Document meets the requirements '
            'based on the Query and the Instruct provided. Note that the answer can '
            'only be "yes" or "no".<|im_end|>\n<|im_start|>user\n'
        )
        suffix = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"
        self._prefix_tokens = self._tokenizer.encode(prefix, add_special_tokens=False)
        self._suffix_tokens = self._tokenizer.encode(suffix, add_special_tokens=False)

    def predict(
        self,
        sentences: Sequence[tuple[str, str]],
        *,
        batch_size: int,
        show_progress_bar: bool,
    ) -> list[float]:
        del show_progress_bar
        scores: list[float] = []
        for start in range(0, len(sentences), batch_size):
            batch = sentences[start : start + batch_size]
            texts = [
                f"<Instruct>: {self._instruction}\n<Query>: {query}\n<Document>: {document}"
                for query, document in batch
            ]
            inputs = self._tokenizer(
                texts,
                padding=False,
                truncation=True,
                return_attention_mask=False,
                max_length=(
                    self._max_length
                    - len(self._prefix_tokens)
                    - len(self._suffix_tokens)
                ),
            )
            for index, token_ids in enumerate(inputs["input_ids"]):
                inputs["input_ids"][index] = (
                    self._prefix_tokens + token_ids + self._suffix_tokens
                )
            padded = self._tokenizer.pad(inputs, padding=True, return_tensors="pt")
            padded = {key: value.to(self._device) for key, value in padded.items()}
            with self._torch.no_grad():
                logits = self._model(**padded).logits[:, -1, :]
                differences = (
                    logits[:, self._true_token_id] - logits[:, self._false_token_id]
                )
            scores.extend(float(value) for value in differences.float().cpu().tolist())
        return scores


def _as_scores(raw: object) -> tuple[float, ...]:
    if hasattr(raw, "tolist"):
        raw = raw.tolist()
    if not isinstance(raw, (list, tuple)):
        raw = [raw]
    scores: list[float] = []
    for value in raw:
        if isinstance(value, (list, tuple)):
            if len(value) != 1:
                raise ValueError("cross-encoder returned a non-scalar score")
            value = value[0]
        scores.append(float(value))
    return tuple(scores)


def _sigmoid(value: float) -> float:
    if value >= 0:
        return 1.0 / (1.0 + math.exp(-value))
    exponential = math.exp(value)
    return exponential / (1.0 + exponential)


class CrossEncoderEvidenceReranker:
    """Rerank only the strongest first-stage candidates with a pinned local model.

    Candidates outside ``top_n`` retain a deliberately small deterministic score so
    the diversity selector can still fill a sparse result without letting an
    unscored candidate outrank a cross-encoder-scored candidate.
    """

    def __init__(
        self,
        *,
        model_name: str,
        model_revision: str,
        cache_folder: str,
        device: str = "cpu",
        batch_size: int = 8,
        max_length: int = 1024,
        top_n: int = 32,
        trust_remote_code: bool = False,
        lazy_load: bool = False,
        model: _CrossEncoderModel | None = None,
    ) -> None:
        if not model_name or not model_revision:
            raise ValueError("cross-encoder model identity is required")
        if batch_size < 1 or max_length < 32 or not 1 <= top_n <= 96:
            raise ValueError("cross-encoder runtime configuration is invalid")
        self._batch_size = batch_size
        self._top_n = top_n
        self._fallback = DeterministicEvidenceReranker()
        self._model_lock = Lock()
        self._model_configuration = {
            "model_name": model_name,
            "revision": model_revision,
            "cache_folder": cache_folder,
            "device": device,
            "max_length": max_length,
            "trust_remote_code": trust_remote_code,
        }
        self._policy_revision = (
            f"cross-encoder/{model_name}@{model_revision}:top-{top_n}:max-{max_length}"
        )
        self._model = model
        if self._model is None and not lazy_load:
            self._model = self._load_model()

    def _load_model(self) -> _CrossEncoderModel:
        if self._model is not None:
            return self._model
        with self._model_lock:
            if self._model is not None:
                return self._model
            from sentence_transformers import CrossEncoder

            self._model = CrossEncoder(
                self._model_configuration["model_name"],
                revision=self._model_configuration["revision"],
                cache_folder=self._model_configuration["cache_folder"],
                device=self._model_configuration["device"],
                max_length=self._model_configuration["max_length"],
                trust_remote_code=self._model_configuration["trust_remote_code"],
            )
            return self._model

    @property
    def policy_revision(self) -> str:
        return self._policy_revision

    def rerank(
        self,
        original_query: str,
        objective: str,
        candidates: tuple[RerankCandidate, ...],
    ) -> tuple[RerankAssessment, ...]:
        if not candidates:
            return ()
        ordered_candidates = tuple(
            sorted(candidates, key=lambda item: (-item.retrieval_score, item.node_id))
        )
        scored_candidates = ordered_candidates[: self._top_n]
        query = original_query.strip()
        if objective.strip() and objective.strip().casefold() != query.casefold():
            query = f"{query}\nResearch objective: {objective.strip()}"
        pairs = tuple((query, candidate.content) for candidate in scored_candidates)
        model = self._load_model()
        with self._model_lock:
            raw_scores = _as_scores(
                model.predict(
                    pairs,
                    batch_size=self._batch_size,
                    show_progress_bar=False,
                )
            )
        if len(raw_scores) != len(scored_candidates):
            raise ValueError("cross-encoder score count does not match candidates")

        assessments: list[RerankAssessment] = []
        for candidate, raw_score in zip(scored_candidates, raw_scores, strict=True):
            score = _sigmoid(raw_score)
            if score >= 0.70:
                label = "direct_support"
            elif score >= 0.40:
                label = "partial_support"
            elif score >= 0.15:
                label = "background"
            else:
                label = "low_relevance"
            assessments.append(
                RerankAssessment(
                    candidate.node_id,
                    score,
                    label,
                    ("cross_encoder_score",),
                )
            )

        fallback = self._fallback.rerank(
            original_query,
            objective,
            ordered_candidates[self._top_n :],
        )
        assessments.extend(
            RerankAssessment(
                item.node_id,
                min(0.05, item.score * 0.05),
                "low_relevance",
                ("outside_cross_encoder_top_n",),
            )
            for item in fallback
        )
        return tuple(sorted(assessments, key=lambda item: (-item.score, item.node_id)))


@dataclass(frozen=True, slots=True)
class AdaptiveRerankPolicy:
    """Cheap query-time policy deciding whether neural ranking is worth its cost."""

    force: bool | None = None
    minimum_candidates: int = 4
    maximum_normalized_top_margin: float = 0.12
    maximum_deterministic_confidence: float = 0.52
    maximum_abstention_confidence: float = 0.18
    minimum_rank_disagreement: int = 6
    minimum_complex_query_tokens: int = 7

    @property
    def revision(self) -> str:
        force = "auto" if self.force is None else ("always" if self.force else "never")
        return (
            f"adaptive-rerank/v1:{force}:margin-{self.maximum_normalized_top_margin:g}:"
            f"confidence-{self.maximum_deterministic_confidence:g}:"
            f"abstain-{self.maximum_abstention_confidence:g}:"
            f"disagreement-{self.minimum_rank_disagreement}:"
            f"complexity-{self.minimum_complex_query_tokens}"
        )

    def decide(
        self,
        original_query: str,
        candidates: tuple[RerankCandidate, ...],
        deterministic: tuple[RerankAssessment, ...],
    ) -> tuple[bool, tuple[str, ...]]:
        if self.force is not None:
            return self.force, ("forced_neural" if self.force else "forced_deterministic",)
        if len(candidates) < self.minimum_candidates:
            return False, ("too_few_candidates",)

        ordered = tuple(
            sorted(candidates, key=lambda item: (-item.retrieval_score, item.node_id))
        )
        top = ordered[0].retrieval_score
        second = ordered[1].retrieval_score
        normalized_margin = (top - second) / max(abs(top), 1e-9)
        narrow_margin = normalized_margin <= self.maximum_normalized_top_margin

        top_deterministic = max((item.score for item in deterministic), default=0.0)
        if top_deterministic <= self.maximum_abstention_confidence:
            return False, ("insufficient_evidence_fast_path",)
        low_confidence = top_deterministic <= self.maximum_deterministic_confidence
        query_tokens = original_query.split()
        complex_query = len(query_tokens) >= self.minimum_complex_query_tokens or any(
            marker in original_query.casefold()
            for marker in (
                " compare ",
                " versus ",
                " mechanism",
                " causal",
                " explain",
                " why ",
                " how ",
                "区别",
                "比较",
                "机制",
                "因果",
                "为什么",
                "如何",
            )
        )
        rank_disagreement = False
        for candidate in ordered[:8]:
            if candidate.lexical_rank is None or candidate.dense_rank is None:
                continue
            if abs(candidate.lexical_rank - candidate.dense_rank) >= self.minimum_rank_disagreement:
                rank_disagreement = True
                break
        reasons: list[str] = []
        if narrow_margin:
            reasons.append("narrow_first_stage_margin")
        if low_confidence:
            reasons.append("low_deterministic_confidence")
        if rank_disagreement:
            reasons.append("lexical_dense_disagreement")
        if complex_query:
            reasons.append("complex_query")
        triggered = complex_query and narrow_margin and (
            rank_disagreement or low_confidence
        )
        if not triggered:
            reasons.append("neural_rerank_not_cost_effective")
        return triggered, tuple(reasons)


@dataclass(frozen=True, slots=True)
class RerankFusionWeights:
    neural: float = 0.55
    deterministic: float = 0.25
    retrieval: float = 0.20

    def __post_init__(self) -> None:
        values = (self.neural, self.deterministic, self.retrieval)
        if any(value < 0 for value in values) or not math.isclose(sum(values), 1.0):
            raise ValueError("rerank fusion weights must be non-negative and sum to one")

    @property
    def revision(self) -> str:
        return f"n{self.neural:g}-d{self.deterministic:g}-r{self.retrieval:g}"


class AdaptiveFusionEvidenceReranker:
    """Fuse model semantics with stable first-stage signals and degrade safely.

    Neural scores are converted to within-query rank percentiles before fusion. This
    avoids treating logits from unrelated reranker families as calibrated
    probabilities while still letting the model dominate the ordering decision.
    """

    def __init__(
        self,
        neural: CrossEncoderEvidenceReranker,
        *,
        policy: AdaptiveRerankPolicy | None = None,
        weights: RerankFusionWeights | None = None,
    ) -> None:
        self._neural = neural
        self._deterministic = DeterministicEvidenceReranker()
        self._policy = policy or AdaptiveRerankPolicy()
        self._weights = weights or RerankFusionWeights()
        self._query_count = 0
        self._trigger_count = 0
        self._fallback_count = 0

    @property
    def policy_revision(self) -> str:
        return (
            f"adaptive-fusion-reranker/v1:{self._weights.revision}:"
            f"{self._policy.revision}:{self._neural.policy_revision}"
        )

    @property
    def diagnostics_snapshot(self) -> dict[str, int | float]:
        return {
            "query_count": self._query_count,
            "trigger_count": self._trigger_count,
            "trigger_rate": (
                0.0 if self._query_count == 0 else self._trigger_count / self._query_count
            ),
            "fallback_count": self._fallback_count,
        }

    def rerank(
        self,
        original_query: str,
        objective: str,
        candidates: tuple[RerankCandidate, ...],
    ) -> tuple[RerankAssessment, ...]:
        self._query_count += 1
        deterministic = self._deterministic.rerank(original_query, objective, candidates)
        triggered, schedule_reasons = self._policy.decide(
            original_query, candidates, deterministic
        )
        if not triggered:
            return tuple(
                RerankAssessment(
                    item.node_id,
                    item.score,
                    item.relevance_label,
                    item.reason_codes + schedule_reasons + ("deterministic_fast_path",),
                )
                for item in deterministic
            )

        self._trigger_count += 1
        try:
            neural = self._neural.rerank(original_query, objective, candidates)
        except Exception:
            self._fallback_count += 1
            return tuple(
                RerankAssessment(
                    item.node_id,
                    item.score,
                    item.relevance_label,
                    item.reason_codes
                    + schedule_reasons
                    + ("neural_failure_deterministic_fallback",),
                )
                for item in deterministic
            )

        deterministic_by_id = {item.node_id: item for item in deterministic}
        candidate_by_id = {item.node_id: item for item in candidates}
        model_scored = tuple(
            item for item in neural if "outside_cross_encoder_top_n" not in item.reason_codes
        )
        neural_rank = _rank_percentiles(model_scored)
        retrieval_rank = _candidate_rank_percentiles(candidates)
        assessments: list[RerankAssessment] = []
        for neural_item in neural:
            deterministic_item = deterministic_by_id[neural_item.node_id]
            candidate = candidate_by_id[neural_item.node_id]
            if neural_item.node_id not in neural_rank:
                score = deterministic_item.score
                reasons = deterministic_item.reason_codes + ("outside_neural_fusion_top_n",)
            else:
                calibrated_neural = 0.7 * neural_rank[neural_item.node_id] + 0.3 * neural_item.score
                score = (
                    self._weights.neural * calibrated_neural
                    + self._weights.deterministic * deterministic_item.score
                    + self._weights.retrieval * retrieval_rank[candidate.node_id]
                )
                reasons = (
                    "neural_rank_fusion",
                    "deterministic_relevance_signal",
                    "first_stage_rank_prior",
                )
            assessments.append(
                RerankAssessment(
                    neural_item.node_id,
                    min(1.0, max(0.0, score)),
                    _label_for_score(score),
                    reasons + schedule_reasons,
                )
            )
        return tuple(sorted(assessments, key=lambda item: (-item.score, item.node_id)))


def _rank_percentiles(
    assessments: tuple[RerankAssessment, ...],
) -> dict[str, float]:
    ordered = tuple(sorted(assessments, key=lambda item: (-item.score, item.node_id)))
    denominator = max(1, len(ordered) - 1)
    return {
        item.node_id: 1.0 - (index / denominator)
        for index, item in enumerate(ordered)
    }


def _candidate_rank_percentiles(
    candidates: tuple[RerankCandidate, ...],
) -> dict[str, float]:
    ordered = tuple(
        sorted(candidates, key=lambda item: (-item.retrieval_score, item.node_id))
    )
    denominator = max(1, len(ordered) - 1)
    return {
        item.node_id: 1.0 - (index / denominator)
        for index, item in enumerate(ordered)
    }


def _label_for_score(score: float) -> str:
    if score >= 0.70:
        return "direct_support"
    if score >= 0.40:
        return "partial_support"
    if score >= 0.15:
        return "background"
    return "low_relevance"
