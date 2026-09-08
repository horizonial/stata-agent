# Context & Memory V2

Status: implementation contract

## 1. Problem

The canonical research ledger is durable, but the primary agent loop currently sends only the
last 16 chat messages and globally ranked memory. This loses long-running decisions, mixes
projects, ignores token pressure, and gives operators no audit record of what the model saw.
The existing compactor is append-only, but it repeatedly summarizes the full ledger into a
shallow counter summary and is not used by the primary loop.

V2 keeps three concepts deliberately separate:

1. **Ledger** — immutable facts and actions; the only source of research truth.
2. **Context projection** — a bounded, deterministic view built for one model request.
3. **Memory** — durable user/project constraints and reusable working knowledge; never evidence.

This follows the strongest common pattern in the local Codex, Pi, and Claude Code studies:
project context from canonical history, append a compaction boundary instead of deleting history,
preserve a recent raw tail, and use staged/high-signal memory extraction with provenance.

## 2. Context assembly contract

Add `harness/context_assembler.py` with these public types:

```python
@dataclass(frozen=True)
class ContextBudget:
    max_input_tokens: int = 16_000
    reserve_output_tokens: int = 4_000
    recent_tail_tokens: int = 4_000
    memory_tokens: int = 1_500
    chars_per_token: int = 4

@dataclass(frozen=True)
class ContextManifestItem:
    layer: str
    source_ids: tuple[str, ...]
    estimated_tokens: int
    truncated: bool = False

@dataclass
class AssembledContext:
    messages: list[dict]
    manifest: list[ContextManifestItem]
    estimated_tokens: int
    compacted_through_seq: int | None

class ContextAssembler:
    def assemble(self, *, store, ctx, user_text, system, skills=(), budget=None) -> AssembledContext: ...
```

The projection order is fixed:

1. system/safety instructions and selected skills;
2. deterministic current research-state projection;
3. latest valid compaction summary, if any;
4. query-relevant, same-project memory;
5. recent complete conversation/tool-interaction tail after the boundary;
6. the current user message exactly once.

The assembler works within `max_input_tokens - reserve_output_tokens`. It allocates the recent
tail and memory their own caps, estimates tokens deterministically, drops lowest-priority old
items first, and never truncates between an assistant tool call and its results. A single item
may be character-truncated only as a last resort and must be marked in the manifest. The current
user message and system safety instructions are never silently dropped. If they alone exceed the
budget, raise `ContextBudgetExceeded` before calling the provider.

The loop must use this assembler for the first provider call. Tool-call/result messages created
inside the current loop remain verbatim and are appended normally. Before every later provider
call, apply a bounded tail projection so a long tool result cannot bypass the input budget.
Sanitization remains the final step before provider I/O.

The manifest records layer names, ledger event ids/sequences or memory ids, estimates, and
truncation flags—never chain-of-thought. Append a `context.assembled` telemetry event containing
only totals and source identifiers. Telemetry failure remains best effort; research commits do not.

## 3. Compaction contract

`compaction.boundary` stays append-only and receives schema version 2 payloads:

```json
{
  "version": 2,
  "from_seq": 1,
  "to_seq": 42,
  "previous_boundary_seq": null,
  "summary": {
    "objective": "...",
    "constraints": ["..."],
    "decisions": ["..."],
    "open_items": ["..."],
    "research_state": "...",
    "evidence_refs": ["claim-...", "card-...", "run-..."]
  },
  "retained_from_seq": 35,
  "reason": "token_pressure"
}
```

Rules:

- A new boundary starts at the previous boundary's `to_seq + 1`; do not repeatedly summarize
  the full ledger. The new summary carries forward the previous structured summary.
- `to_seq` and `retained_from_seq` must be safe semantic boundaries. Never split a tool invocation
  from its result, a pending approval from its resolution, or a run chain from its terminal event.
- Compaction is triggered by token pressure (soft watermark), not only by turn count. Keep a raw
  recent tail even when a summary exists.
- Evidence is represented by ids only. A compactor cannot create, alter, or upgrade claims.
- Validate that referenced ids exist and that ranges are monotonic before append. On failure,
  preserve the previous boundary and fail safely.
- Backward compatibility: V1 string summaries remain readable; new writes use V2.
- Context overflow may compact and retry once. It must never enter an unbounded retry loop.

V2 initially uses deterministic structured extraction from projections/events. A summarizer model
can be added later behind an interface, but its output must pass the same validator.

## 4. Memory contract

`MemoryStore` keeps JSON compatibility while migrating records on read. Every V2 record has:

```json
{
  "id": "mem-...",
  "workspace_id": "sha256:<canonical-root>",
  "scope": "project",
  "kind": "preference|constraint|decision|rejection|procedure",
  "text": "...",
  "status": "active|superseded|retracted",
  "confidence": "explicit|verified|inferred",
  "source_ids": ["event-id-or-seq"],
  "fingerprint": "sha256:<normalized-content>",
  "created_at": 0,
  "updated_at": 0,
  "last_used_at": null,
  "use_count": 0,
  "supersedes": null,
  "expires_at": null,
  "sensitive": false,
  "schema_version": 2
}
```

Project scope is the default and is mandatory for automatic injection. Optional user-global
memory must be explicitly created. Legacy entries migrate to a caller-provided/default legacy
workspace and are not injected into unrelated projects.

Write path:

- `add` accepts scope, workspace, confidence, and provenance while preserving the legacy call.
- Normalize text and reject blank, secret-like, temporary/live metrics, and evidence assertions.
- Exact fingerprints deduplicate. Contradictory replacements use `supersedes`; retractions remain
  as tombstones instead of erasing provenance.
- No-op is a first-class success when a future run would not act differently.
- Automatic writes are limited to explicit user preferences and approved decision notes.
  Assistant suggestions remain candidates until accepted.

Read path:

- `search(query, workspace_id=..., kinds=..., limit=...)` filters active, unexpired records first.
- Rank deterministically by lexical relevance, exact project match, explicit/verified confidence,
  usage, and recency. A queryless call must not inject arbitrary global top-N memory.
- `select_for_context` returns records plus formatted provenance under a token/character cap and
  touches only records actually selected.
- Memory text is labeled as constraint/working knowledge, never evidence. RAG and evidence cards
  remain separate context layers.

Maintenance is explicit: `consolidate()` deduplicates/supersedes and `prune()` removes only expired
or long-unused low-confidence records. It must not delete active explicit preferences by default.

## 5. Integration and migration

- `ToolContext` gains `workspace_id` and optional `context_budget`; UI derives the workspace id
  from the canonical ledger/project root, not the current process-wide memory path.
- Approval notes call `remember_decision` with the current workspace and approval event provenance.
- The existing runner path may keep its text projection temporarily, but the primary UI loop must
  use V2. One projection implementation should ultimately serve both.
- Existing APIs remain callable during migration: `MemoryStore(path)`, `add(text, kind)`, `all()`,
  `touch(id)`, `search(query, limit)`, `to_context(max_items)`, `prune_unused(min_used)`.
- No migration rewrites the file merely by opening it; the next mutation performs an atomic V2 save.

## 6. Release acceptance criteria

1. Long histories retain objective, accepted constraints, open work, and recent exact messages.
2. Context stays under the configured budget and reports deterministic manifests.
3. Tool invocation/result and approval/run chains are never split by compaction.
4. Consecutive boundaries are monotonic and original ledger events remain unchanged.
5. Memory from project A is not injected into project B.
6. Query-relevant memory outranks merely recent memory; selected usage is recorded.
7. Legacy memory files load without data loss and migrate atomically on mutation.
8. Secret-like and evidence-like candidates are rejected; superseded/retracted records are not read.
9. Provider overflow performs at most one compact-and-retry.
10. Existing unit/product/eval/release gates remain green, with focused regression tests for all above.

## 7. Parallel work packages

- **Context/compaction core:** owns `harness/context_assembler.py`, `harness/compaction.py`, event
  constants, and focused context/compaction tests. Does not edit memory or UI.
- **Memory V2:** owns `memory/memstore.py`, `memory/__init__.py`, and focused memory tests. Does not
  edit agent loop, UI, or toolkit.
- **Integration:** after the two APIs land, owns `harness/agent_loop.py`, `toolkit.py`, `ui.py`,
  runner approval wiring, product/eval tests, and relevant architecture documentation.

