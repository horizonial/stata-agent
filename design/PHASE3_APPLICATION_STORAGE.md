# Phase 3 — Application Boundary and Durable Local State

Status: implementation contract

## 1. Goal

Move orchestration out of the FastAPI module and replace split process-local state with a
versioned SQLite foundation, while preserving the local-first, single-user product boundary.
This phase does not add a second intent classifier, does not make remote calls by default, and
does not turn memory into research evidence.

## 2. Non-negotiable boundaries

- `ui.py` is an HTTP/SSE adapter. It must not own agent-loop orchestration, resource lifetime,
  background-job semantics, or storage migrations.
- Application services must not import FastAPI or `stata_agent.ui`.
- The event ledger remains the canonical research history and evidence source.
- Memory remains logically separate from evidence, even when both use the same SQLite file.
- Schema changes are explicit, ordered, transactional, and recorded. Importing a module must
  never mutate a database or migrate a legacy file.
- Existing `memory.json` data is imported explicitly and idempotently. The importer never deletes
  or overwrites the source file.
- Background work is submitted through a small queue port. The local adapter is bounded and
  process-local; durable intent lives in SQLite events/outbox state.
- Redis/RQ is an optional future deployment adapter, not a v1 runtime dependency.
- Existing privacy, write-authority, provenance, cancellation, and deterministic fallback
  contracts remain in force.

## 3. Target dependency direction

```text
FastAPI routes / CLI
        |
        v
application services  --->  application ports
        |                         |
        v                         v
agent loop / tools       local adapters (SQLite, local queue, provider, Stata)
        |
        v
domain + event contracts
```

Transport and adapters may depend inward. Application and domain modules must not depend on the
FastAPI module or transport-global state.

## 4. First implementation wave

### 4.1 Application core

Introduce framework-neutral request-control and chat-turn services. Dependencies are injected as
factories or narrow protocols. A chat turn owns and closes the store/executor resources it opens
on success, failure, and cancellation.

The first wave is intentionally not wired into `ui.py`; focused tests establish the contract
before the transport is changed.

### 4.2 SQLite migrations and memory repository

Introduce a standard-library migration runner and version table. The initial product-state schema
covers:

- active/superseded/retracted memory records;
- pending/accepted/rejected memory candidates;
- workspace registry metadata;
- workspace, status, fingerprint, and recency indexes.

Candidate decisions are atomic at repository scope. Workspace isolation and fingerprint
idempotency are database constraints, not only Python checks.

### 4.3 Queue port

Introduce a `TaskQueue` port with explicit accepted, duplicate, full, and closed submission
outcomes plus bounded shutdown and observable statistics. The first adapter is local and wraps or
replaces the existing single-worker scheduler without claiming durability.

## 5. Integration wave

After the first-wave contracts pass focused tests:

1. Apply migrations from the explicit application startup path.
2. Add a compatibility memory facade backed by the SQLite repository.
3. Import legacy `memory.json` once and record the import fingerprint/version.
4. Route candidate extraction and review through the SQLite-backed facade.
5. Move chat-turn and request-control orchestration from `ui.py` into application services.
6. Replace direct scheduler use with the queue port.
7. Move workspace registry persistence to SQLite.
8. Keep legacy files as recoverable backups until a later, explicit cleanup release.

Cross-resource operations use one of two declared models:

- one SQLite transaction when repository and ledger mutations share a connection/unit of work; or
- a durable requested/outbox record followed by an idempotent terminal transition.

Returning HTTP success after an unrecorded or half-applied mutation is forbidden.

## 6. Acceptance criteria

1. Application modules have no FastAPI or `ui.py` dependency.
2. Store and executor resources close on all chat-turn exit paths.
3. Migration reruns are no-ops; failed migrations roll back completely.
4. Legacy memory import is repeatable without duplicate records or candidate decisions.
5. Candidate acceptance/rejection is atomic and workspace-isolated.
6. Runtime memory writes no longer require process-local JSON locking after integration.
7. Queue submission distinguishes duplicate, full, and closed states and never leaks worker
   exceptions into a completed chat turn.
8. Existing tests, Ruff, Mypy, product evaluation, branch coverage threshold, and wheel build pass.
9. The belief map reports no boundary or naming invariant violations after structural changes.
10. Real Stata/model/Windows wheel validation remains an explicit release gate after this phase.

## 7. Deferred work

- Redis/RQ adapter and multi-process worker deployment.
- Multi-user or multi-tenant authorization.
- Removing legacy JSON backups.
- Cross-machine synchronization.
- New memory extraction models or broader intent-classification paths.
