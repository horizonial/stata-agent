# Project Architecture Audit

> Baseline: `57c737c` (`feat: add live Stata release acceptance gate`)
>
> Date: 2026-09-10
> Purpose: describe the current system, identify verified gaps, and order future
> module research. This document does not replace the architectural constraints in
> `../ARCHITECTURE.md`.

## 1. Executive conclusion

The repository already has a coherent local-first agent architecture. Its strongest
properties are the append-only research ledger, evidence write separation, a general
tool-calling loop, bounded context construction, workspace-scoped memory, and a real
Stata execution path. The next product risk is no longer basic connectivity. It is
whether the agent chooses and verifies econometrically correct specifications.

No core architecture change is currently required. Future work should strengthen one
module at a time and preserve the existing boundaries.

## 2. Document authority

Documents have accumulated across several design generations. Use this order when
they disagree:

1. `ARCHITECTURE.md`: current architectural constraints and component map.
2. This audit: current implementation maturity, risks, and research order.
3. Latest focused designs: `agent-tool-routing.md`, `rethink-autonomy.md`,
   `CONTEXT_MEMORY_V2*.md`, and `PHASE3_APPLICATION_STORAGE.md`.
4. `SPEC.md` and DD-01 through DD-07: foundational contracts that remain valid unless
   a later focused design explicitly supersedes them.
5. `research/codex_report.md`: research rationale and alternatives, not current state.
6. `PRODUCTIZATION_PLAN.md` and `audit-code-gaps.md`: historical planning snapshots;
   their completion markers must not be treated as current facts.
7. Root `IMPLEMENTATION_PLAN.md` and `IMPLEMENTATION_REPORT.md`: the latest completed
   development round only.

Important supersession: the current product is a normal LLM-first agent with tool
calling. Research phases constrain tools and evidence; they are not a second rigid
chat workflow. References in the old SPEC to PydanticAI or an outer phase machine do
not authorize replacing the current `ChatService -> agent_loop` path.

## 3. Current system shape

```text
FastAPI/UI (composition, HTTP, SSE)
  -> ChatService (framework-neutral use case and lifecycle)
    -> Agent Loop (decide, call tools, observe, stop)
      -> ContextAssembler / compaction / memory projection
      -> provider registry + privacy gate
      -> Skill matching + ToolEnforcer + toolkit
        -> Stata / RAG / evidence / writer / workspace tools
      -> append domain events
    -> SQLite ledger / projections / snapshots / durable outbox
  -> SSE response and materialized workspace state
```

The architecture has five distinct kinds of state:

| State | Canonical status | Rule |
|---|---|---|
| User input and research events | Canonical | Append-only SQLite ledger |
| Stata outputs and signed Card/Claim records | Canonical | Deterministic components sign; the model cannot sign |
| Research/project projection | Derived | Must be rebuildable from canonical records |
| Prompt context and compaction summaries | Derived | Bounded projection; never overwrites history |
| Project memory | Durable constraint/preference store | Has provenance; never becomes research evidence |

Large immutable artifacts such as do-files, source PDFs, exports, and generated files
remain outside the prompt and are referenced through stable identifiers or provenance.

## 4. End-to-end runtime flow

1. UI validates the workspace/request and delegates to `ChatService`.
2. `ChatService` records the user turn and invokes the single canonical agent loop.
3. `ContextAssembler` folds ledger state, evidence, relevant memory, and a recent
   lossless tail into a token-bounded prompt projection.
4. The selected provider receives only data allowed by the configured privacy mode.
5. Function calling selects from tools allowed by the active Skill, permission policy,
   runtime capability, and current context.
6. `ToolEnforcer` validates the call before an external side effect is started.
7. Tool execution writes auditable events. Real Stata execution records provenance;
   evidence builders produce Card/Claim records separately from model text.
8. SQLite projections and snapshots are derived from the event history. Durable
   memory work is coupled through the outbox rather than a cross-store best effort.
9. Structured progress and the final response are streamed to the UI.
10. Cancellation, budget exhaustion, timeout, or uncertain side effects must end in an
    explicit auditable state.

## 5. Module maturity and research agenda

| Module | Current evidence | Maturity | Next question |
|---|---|---:|---|
| `application/` | `ChatService`, request control, queue ports and lifecycle tests | Strong | Are any orchestration or resource ownership decisions still duplicated in `ui.py`? |
| `harness/` | General loop, cancellation, compaction, recovery, telemetry | Strong but high-risk hub | Can loop complexity be reduced without introducing a second orchestrator? |
| `events/`, `domain/` | Versioned events, reducers, claims, families, replay tests | Strong | Are every invalid transition and schema-evolution case covered by replay tests? |
| `storage/` | SQLite ledger, migration, lease/fence, snapshots, durable outbox | Strong | How should heartbeat, redrive, retention, backup, and recovery be operated? |
| `memory/` | Workspace isolation, provenance, candidates, review, SQLite repository | Functional | How well do retrieval and consolidation behave in long real projects? |
| `skills/` | Progressive loading, matching, allowed-tool filtering, staging | Functional | Do econometric Skills cause correct model and diagnostic choices? |
| `tools/` / Stata | Real MCP session, ado preflight, executor, evidence signer, live doctor | Transport verified | Can results and specification semantics be independently verified? |
| `rag/` | Stable chunks, lexical/vector hybrid, role separation, cache | Functional baseline | What do OCR, reranking, and a Chinese-domain retrieval gold set improve? |
| `writer/` | Numeric grounding, semantic tables, citations, figures, DOCX | Functional | Can every rendered number, table cell, figure, and citation round-trip to canonical evidence? |
| `providers/`, `privacy/` | Capability abstraction and privacy-mode gate | Contract covered | Do live remote requests preserve the boundary during fallback and failure? |
| `eval/` | L1-L4 offline scenarios and stable release gate | Engineering gate strong | How should econometric correctness and expert research quality be measured? |
| `ui.py`, `ui/` | Chat, SSE, workspaces, stop, approval, trace | Usable baseline | Attachments, diagnostic export, tool-node fidelity, and full UX regression remain |

## 6. Hard constraints

All module work must preserve these invariants:

- SQLite events remain the append-only research truth; projections are rebuildable.
- Raw chain-of-thought is not persisted. Store decision summaries and evidence links.
- Models may propose; only trusted deterministic components may sign evidence.
- Privacy mode cannot be weakened by provider fallback or error handling.
- Tools remain dynamically exposed through Skill, permission, capability, and policy.
- FakeExecutor is test/demo-only and must be explicitly enabled.
- `ChatService` and queue ports remain framework-neutral; FastAPI is composition.
- Context summaries and memory cannot create research facts.
- Cancellation and uncertain side effects produce explicit terminal/reconcile states.
- The v1 product remains local-first, single-user, and SQLite-backed. Redis/RQ or a
  distributed runtime needs a measured multi-process/multi-machine requirement.
- A proposed change to these rules must be documented separately as
  `Architecture Change Required`, with migration and compatibility risks.

## 7. Risk register

| Priority | Risk | Evidence / status | Closure evidence |
|---|---|---|---|
| P0 | Econometrically wrong tool/specification choice | Real Stata transport passes, but model correctness is not attested | Adversarial Skill fixtures plus independent `verify_result` checks |
| P0 | Documentation drift causes architecture regression | SPEC, roadmap, audits, and test counts describe different generations | Authority rules here plus update-on-merge discipline |
| P1 | Long provider calls outlive outbox leases | Durable outbox exists; heartbeat/redrive is unfinished | Lease heartbeat, operator redrive, retention and crash tests |
| P1 | Weak cross-layer observability | Several IDs exist but no unified support view/SLO | Correlated structured logs and privacy-safe diagnostic bundle |
| P1 | Live provider privacy/fallback behavior | Offline contracts exist; live boundary still needs acceptance | Explicit live privacy acceptance and redacted capture review |
| P1 | Unsafe or unmanaged attachments | Upload/OCR lifecycle is absent | Sandbox, sniffing, limits, malware cases, workspace cleanup |
| P2 | RAG quality is unmeasured on the target corpus | Hybrid retrieval exists without a domain gold set | Retrieval dataset, ablation, OCR/rerank decision |
| P2 | UI state diverges under failures | Main paths are tested; full manual regression remains | Release UX matrix covering stop, disconnect, approval and recovery |

## 8. Ordered module research plan

Each research round should produce a focused design, an implementation plan for one
primary objective, and explicit acceptance evidence. Do not combine adjacent rounds.

1. **Econometrics Skill and result verification — next round.** Define what must be
   checked for sample, estimator, fixed effects, clustering, weights, return codes,
   required diagnostics, and reported values. Reuse current Stata and evidence APIs.
2. **Outbox and recovery operations.** Design lease heartbeat, manual redrive,
   retention, shutdown, and operator-visible failure semantics.
3. **Observability and support.** Establish request/operation/run/outbox correlation,
   structured logging, redaction, diagnostic export, and local-product SLOs.
4. **Evidence production audit.** Verify RAG span and Stata result through Card/Claim,
   semantic table, figure, citation, and DOCX round-trip.
5. **Context and memory quality.** Evaluate long conversations, retrieval relevance,
   compaction fidelity, contradictions, consolidation, and review workload.
6. **Attachment and workspace lifecycle.** Add a secure ingestion design only after
   sandbox and cleanup contracts are settled.
7. **UI and release operations.** Complete failure-state UX, packaging, signing,
   upgrade/rollback, backup/import/export, and manual acceptance.

## 9. Required template for each module study

A module study is complete only when it records:

- current behavior and code boundary;
- applicable architectural invariants;
- evidence from current tests and real runtime where relevant;
- external design references and alternatives;
- concrete gaps, failure modes, and threat cases;
- proposed contract, inputs/outputs, compatibility, and migration;
- acceptance tests and stop conditions;
- one recommended implementation objective;
- deferred items that must not leak into that implementation round.

## 10. Current baseline and immediate stop condition

The latest completed round reports `364 passed / 4 skipped`, Ruff, Mypy, product eval,
77% branch coverage, wheel build, and a 20-iteration live Stata acceptance run passing.
The targeted Stata doctor/ado/entrypoint suite was rerun before freezing `57c737c`.

This architecture-audit round stops after documenting the current system and updating
the handoff baseline. It must not implement econometric verification, outbox changes,
attachments, observability, or UI refinements.
