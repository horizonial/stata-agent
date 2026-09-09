# Context & Memory V2 — Phase 2

Status: implementation contract

This phase adds optional model-assisted compaction and high-signal memory intake without
weakening the deterministic V2 foundation. The feature is conservative by default: opening the
application or completing a turn must not silently cause an extra remote model call.

## 1. Non-negotiable boundaries

- The ledger remains canonical. A summary or memory can never create research evidence.
- Deterministic compaction remains the default and the fail-safe fallback.
- Model-extracted memories are candidates, never active memory, until explicitly accepted.
- Only same-workspace active memory can be injected into context.
- Raw tool results, secrets, live estimates, coefficients, p-values and citations are ineligible
  for memory extraction.
- All model input is bounded, sanitized, and treated as untrusted data inside a fixed prompt.
- Provider failures do not fail the user's completed chat turn.
- Every extraction/summary attempt is idempotent and auditable without storing chain-of-thought.

## 2. Model-assisted compaction

Add `harness/summary_service.py`:

```python
@dataclass(frozen=True)
class SummarySource:
    source_id: str
    role: str
    text: str

@dataclass(frozen=True)
class CompactionSummaryRequest:
    idea_id: str
    from_seq: int
    to_seq: int
    previous_summary: dict
    deterministic_summary: dict
    sources: tuple[SummarySource, ...]

class CompactionSummaryProvider(Protocol):
    def summarize(self, request: CompactionSummaryRequest) -> Mapping[str, object]: ...

class ChatCompactionSummaryProvider:
    # wraps an existing provider.chat(..., json_mode=True)
    ...

def validate_model_summary(request, candidate) -> dict: ...
```

The provider returns only this bounded shape:

```json
{
  "objective": {"text": "...", "source_ids": ["seq:1"]},
  "constraints": [{"text": "...", "source_ids": ["seq:2"]}],
  "decisions": [{"text": "...", "source_ids": ["seq:3"]}],
  "open_items": [{"text": "...", "source_ids": ["seq:4"]}]
}
```

Validation rules:

- Every source id must exist in the request and be eligible for that category.
- Objective/constraints/preferences require user-message or approved-decision provenance.
- No unknown keys, blank/oversized text, secret-like text, or evidence assertions.
- At most 1 objective, 12 constraints, 12 decisions and 12 open items; total output <= 8,000 chars.
- The deterministic `research_state` and `evidence_refs` are always retained and cannot be
  replaced by the model.
- Invalid output raises `SummaryValidationError`; the caller persists the deterministic summary.

Extend `compact(..., summarizer=None)` without breaking existing callers. It first computes the
deterministic summary, then optionally asks the summarizer to augment the narrative fields. The
boundary payload records `summary_mode=deterministic|model_validated|model_fallback`, provider
name, prompt version and a source digest, but not the prompt body. Original events remain intact.

The agent-loop overflow path may pass a summarizer from `ToolContext`, but still retries the
provider at most once. A summary-provider failure must not trigger a second summary attempt.

## 3. Memory intake pipeline

Add `memory/pipeline.py`:

```python
@dataclass(frozen=True)
class MemoryExtractionRequest:
    idea_id: str
    workspace_id: str
    from_seq: int
    to_seq: int
    sources: tuple[MemorySource, ...]
    fingerprint: str

class MemoryExtractionProvider(Protocol):
    def extract(self, request: MemoryExtractionRequest) -> Sequence[Mapping[str, object]]: ...

class ChatMemoryExtractionProvider:
    ...

@dataclass(frozen=True)
class MemoryExtractionResult:
    status: str                 # completed|noop|failed|denied
    candidate_ids: tuple[str, ...]
    fingerprint: str
    error_code: str | None = None

class MemoryExtractionPipeline:
    def run_once(self, *, store, memory, idea_id, workspace_id, provider=None,
                 privacy_mode="local_strict", upto_seq=None) -> MemoryExtractionResult: ...
```

Eligibility is deliberately narrow:

- Include user messages, approval notes and explicit rejection/modification notes.
- Exclude assistant prose, tool arguments/results, RAG text and unsigned research assertions.
- Ignore greetings, short acknowledgements, one-off task parameters and temporary status.
- A candidate must be one of `preference`, `constraint`, `decision`, `rejection`, `procedure` and
  carry one or more eligible source ids from the request.
- Provider output always enters `MemoryStore.add_candidate(..., confidence="inferred")`.
- Existing direct approval-note writes remain active explicit memories and are not duplicated.

Idempotency and audit events:

- Fingerprint = workspace + covered sequence range + prompt version + source-content digest.
- Write `memory.extraction.requested`, then exactly one terminal event:
  `memory.extraction.completed`, `.noop`, `.failed`, or `.denied`.
- Before provider I/O, scan terminal events for the fingerprint. If found, return the recorded
  result without another call.
- Bound each run to 32 source items / 12,000 source characters / 8 candidates.
- Provider/parsing/validation failure records a stable error code, never raw secrets or response.

Add public MemoryStore helpers `candidates(workspace_id=...)`, `candidate(id)` if necessary; keep
the JSON schema and old APIs backward compatible.

## 4. Runtime and review integration

Configuration:

- `STATA_AGENT_COMPACTION_SUMMARY=deterministic|provider`, default `deterministic`.
- `STATA_AGENT_MEMORY_EXTRACTION=off|provider`, default `off`.
- `provider` mode is rejected under `local_strict`; under `mixed_sanitized`, sanitize before I/O.
- No new provider instance or API key path: adapters wrap the already selected provider.

Runtime behavior:

- Successful, non-cancelled turns may schedule extraction only when explicitly enabled.
- Chat response delivery does not wait for extraction. Use a bounded single-worker executor.
- Append the requested event before scheduling. A startup/resume hook may process a requested job
  lacking a terminal event; duplicate candidate fingerprints remain harmless.
- Shutdown stops accepting jobs and waits only a short bounded interval.

Review API:

- `GET /api/memory/candidates?ws=...` returns same-workspace candidates with source ids.
- `POST /api/memory/candidates/{id}/accept?ws=...` activates it and records the reviewer event.
- `POST /api/memory/candidates/{id}/reject?ws=...` rejects it and records the reviewer event.
- Cross-workspace ids return 404; raw provider responses are never returned.

Remove the unused UI `_classify_intent` / keyword router and its isolated tests. The primary loop
already handles conversational intent through normal response/tool selection, while policy,
enabled tools and skills enforce action boundaries. This avoids an extra classifier model call and
keeps one routing path.

## 5. Acceptance criteria

1. Existing deterministic compaction output is unchanged when no summarizer is supplied.
2. Valid model summaries preserve deterministic state/evidence fields and carry source ids.
3. Invalid, injected, oversized or evidence-like summaries fall back without corrupting a boundary.
4. The same extraction fingerprint never performs provider I/O twice.
5. Extracted items remain invisible to context until accepted.
6. Tool output and assistant-only claims cannot become memory candidates.
7. Secrets/live metrics/evidence assertions are rejected before persistence.
8. Candidate review is workspace-isolated and ledgered.
9. Defaults make no extra remote calls; privacy denial is explicit and auditable.
10. Cancellation/shutdown/provider failure cannot fail an already completed chat turn.
11. Full tests, Ruff, Mypy, product eval, branch coverage threshold and wheel build pass.

## 6. Work packages

- **Summary core:** owns `harness/summary_service.py`, the optional summarizer hook in
  `harness/compaction.py`, and focused summary tests. No UI or memory edits.
- **Memory pipeline:** owns `memory/pipeline.py`, minimal candidate-read helpers in `memstore.py`,
  memory exports, event constants and focused pipeline tests. No UI or agent-loop edits.
- **Runtime integration:** after the public contracts are available, owns `agent_loop.py`,
  `toolkit.py`, `ui.py`, config/docs and integration tests. It wraps the selected provider,
  enforces privacy/features, schedules extraction, exposes review APIs and removes dead intent code.

