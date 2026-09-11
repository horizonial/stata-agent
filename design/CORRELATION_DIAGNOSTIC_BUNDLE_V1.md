# Correlation Spine and Privacy-Safe Diagnostic Bundle V1

Status: proposed for the next implementation round  
Priority: P1  
Scope: one local workspace, recent operational metadata only

## Problem

The product already creates several useful identifiers, but they do not form one trace:

- the HTTP/chat boundary creates a `request_id`;
- the event schema already has `correlation_id`, `operation_id`, and `attempt_id`;
- Stata execution creates an operation and run id;
- the memory outbox has an outbox id and points back to a requested event by `event_seq`;
- queue and health surfaces expose point-in-time counters.

Today `ChatService` stores the request id only inside the user-event payload. Agent-loop, Stata-run, and
memory-extraction events do not inherit it. Support therefore cannot reliably answer “what happened to
this request?” The existing Trace serializer is also unsuitable for export: it intentionally includes
user text, model decisions, error strings, tool payload previews, and machine results.

This design establishes a request-rooted correlation spine and a bounded, allow-list-only diagnostic
JSON bundle. It does not create a second telemetry truth source.

## Architectural Fit

- The append-only SQLite event ledger remains the durable business and operational authority.
- `ChatService` remains the single owner of one chat turn and assigns the effective request id once.
- Existing `Event.correlation_id` is used; no event schema or database migration is required.
- `ToolContext` carries environment metadata to the loop/tools without exposing it as model-authored input.
- Stata `operation_id`, run id, and attempt semantics remain unchanged; correlation is additional metadata.
- The outbox remains a dispatch index. Its `event_seq` links to the correlated requested ledger event.
- Queue counters remain explicitly process-local and non-durable.
- FastAPI only resolves scope, gathers adapter snapshots, invokes the application service, and serializes.

No architecture change is required.

## Correlation Contract

For every `ChatService.run` call:

1. Validate a supplied 1..128-character opaque `request_id`, or generate one before opening the turn.
2. Pass an effective immutable `ChatTurnRequest` containing that id to context and post-turn hooks.
3. Store that id in both the existing user payload field and `Event.correlation_id`.
4. Set `ToolContext.request_id` to the same value.
5. Every orchestration event emitted by `agent_loop` uses `ctx.request_id` as `correlation_id`.
6. `run_stata` forwards the id only when the executor supports the optional keyword. Real and fake
   executors attach it to every newly appended run/call/result/terminal event.
7. Memory extraction's requested event inherits the effective request id before the atomic
   event-plus-outbox append. The outbox row is correlated through its immutable `event_seq`; no duplicate
   request id is added to its payload.

The request id is a correlation key, not authorization, idempotency, tenancy, or evidence identity.
Reused historical Stata runs keep their original correlation; the current correlated tool completion may
reference the reused run id without rewriting history.

Compatibility rules:

- Existing events with null `correlation_id` remain valid and are reported as uncorrelated.
- Direct executor callers may omit `correlation_id`.
- Custom executors that do not accept the keyword continue to work through capability inspection.
- Event ordering, fingerprints, operation ids, attempts, reducers, and side-effect state do not change.

## Diagnostic Bundle Contract

Endpoint: `GET /api/operations/diagnostics/bundle`

Inputs:

- `ws`: existing local workspace resolver.
- `event_limit`: 1..500, default 200.
- `outbox_limit`: 1..200, default 100.
- `request_id`: optional exact opaque correlation filter.

The application service builds `diagnostic.bundle.v1` in memory and returns JSON with an attachment
filename. It performs no writes, provider calls, queue submissions, network upload, or filesystem export.
Rows are queried with SQL limits; the service must not materialize the complete ledger or outbox.

Top-level sections:

- `manifest`: schema name/version, product version, generated epoch, resolved opaque workspace id, applied
  limits/filter, and truncation indicators;
- `health`: safe booleans/enums and counts only; never health detail/error text;
- `queue`: the existing fixed-shape `QueueStats`, labelled process-local/non-durable;
- `outbox`: existing aggregate stats plus bounded safe row metadata;
- `events`: bounded chronological safe projections;
- `correlations`: deterministic groups from request id to event sequences, operation ids, run ids, and
  linked outbox ids;
- `coverage`: included correlated/uncorrelated event counts so missing legacy links are visible.

Storage receives two narrow read operations: bounded recent/correlation-filtered events and bounded recent
outbox rows for the existing memory-extraction task type. Ordering must be deterministic. A `limit + 1`
query may be used to report truncation without an unbounded count.

## Allow-List and Redaction Boundary

Diagnostic serialization is independent of `_public_event` and `_event_payload_preview`; those are Trace
UI contracts and are deliberately content-bearing. The bundle starts from an empty projection and copies
only explicitly approved scalar metadata.

Allowed event metadata:

- sequence, event type, created time, actor/source, phase;
- correlation, operation and attempt ids;
- side-effect state;
- event-specific safe scalars such as tool name, boolean success, budget kind/limit/tool-call count,
  terminal reason from a fixed allow-list, run/call/result/spec ids, return code, and test/reuse booleans.

Allowed outbox metadata:

- outbox id, task type, state, attempts/max attempts, lease-present/expired booleans, event sequence,
  timestamps, state version, and `has_error`;
- never lease owner/token, idempotency key, payload, fingerprint, or raw `last_error`.

Forbidden everywhere:

- user/system/model text, reply/ask/decision summaries;
- tool arguments or result bodies, Stata code/output, machine estimates, provenance paths;
- prompts, memory sources/content, provider response, research material;
- raw exception/health detail/`last_error`, secrets, tokens, environment values, filesystem paths.

Unknown event types receive metadata only. Unknown payload keys are ignored. Serialization failure or an
unexpected source shape fails closed with a sanitized application error; it must never fall back to raw
`model_dump`, `payload`, `repr`, or exception text.

## Consistency and Failure Semantics

The bundle is an explicitly labelled point-in-time support snapshot, not an atomic database snapshot
across ledger, outbox, queue, and health. Each durable query is individually bounded and deterministic;
queue counters may change concurrently. Missing linked rows are represented as missing, never invented.

- Invalid bounds/filter: validation error (`422`).
- Unknown workspace: existing workspace mapping.
- Storage/read/projection failure: sanitized server error (`500`).
- Empty workspace/filter: successful empty bundle with complete fixed shape.
- Provider unavailable or local-strict privacy: bundle still succeeds without contacting a provider.

## Explicitly Deferred

- OpenTelemetry, log shipping, remote collectors, tracing backend, metrics database, alerting, dashboards.
- Product SLO thresholds or claims before real workload baselines exist.
- Changes to legacy `harness/telemetry.py` JSONL or Trace UI payloads.
- Full historical export, ZIP/support archive, artifacts, logs, database copies, automatic upload.
- Remote authentication/RBAC, support consent workflow, encryption/key management.
- Correlating every pre-existing direct domain/evidence write or rewriting historical events.
- Queue durability, Redis/RQ, provider exactly-once, SQLite compaction/retention.

## Acceptance Summary

1. A new ChatService turn has one request id across user, loop, new Stata, and memory-requested events.
2. Existing callers and historical null correlations remain compatible.
3. Diagnostic reads are SQL-bounded and deterministic.
4. A hostile payload containing canary secrets never appears in serialized bundle JSON.
5. Outbox rows link through requested-event sequence without copying payload/content.
6. Empty, filtered, local-strict, provider-disabled, and partial-legacy states return a stable shape.
7. Export is read-only and causes zero provider, executor, dispatcher, or queue work.
8. No schema migration, OTel dependency, SLO claim, or second truth store is introduced.
