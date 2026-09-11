# Outbox Operator Recovery Plane

Status: implementation-ready design  
Priority: P1  
Scope: durable `memory.extraction` outbox only

## 1. Problem

The durable outbox now protects accepted memory-extraction work with atomic intent,
leased claims, bounded retries, dead-letter state, continuous pumping, heartbeat
renewal, terminal-event prechecks, and stale-token fencing. What it still lacks is a
safe operator path after a row reaches `failed`.

Today an operator can see only aggregate counts in `/api/health`. Recovering one
failed row requires editing SQLite manually. That bypasses status validation,
workspace isolation, terminal-ledger authority, attempt budgeting, and concurrency
fences. It can also replay an external provider call after the canonical ledger has
already recorded a terminal extraction outcome.

This slice adds a bounded recovery plane: list safe failed-item summaries, reconcile a
row that already has a terminal event, or explicitly return a genuinely unterminated
row to normal dispatch. It does not add deletion, bulk replay, or a second queue.

## 2. Architectural fit

- The append-only event ledger remains the business source of truth.
- `task_outbox` remains a durable dispatch index, not research evidence.
- The application layer owns recovery policy and safe response models.
- `SQLiteStore` owns atomic state transitions and compare-and-swap fencing.
- FastAPI is only the local transport/composition adapter.
- The existing dispatcher remains the only component that claims work and calls the
  provider. A recovery request never calls the provider synchronously.
- Redis/RQ is not required for a local, single-user, SQLite-backed deployment.

This is an additive implementation inside the existing architecture. No new event
type or business state machine is introduced.

## 3. Reference lessons applied

The local research corpus contains two useful patterns:

- Hermes recovery operations accept only an exact recoverable state, carry an
  expected generation, and perform an atomic conditional update. Stale operator
  views fail rather than acting on a newer execution.
- The Codex memory pipeline uses bounded leased claims, explicit retry outcomes, and
  heartbeat renewal. Recovery should re-enter that normal path rather than bypass it.

Accordingly, this design requires exact-state transitions, a persistent state
generation, explicit at-least-once acknowledgement, and normal dispatcher pickup.

## 4. Recovery lifecycle

~~~text
GET failed items
  -> safe, workspace-scoped summaries + state_version

POST recover one item (key + expected state_version + explicit acknowledgement)
  -> reload and validate failed memory.extraction row
  -> check canonical ledger for matching terminal event
     -> terminal exists: failed -> completed, no provider call
     -> no terminal:     failed -> pending, reset attempt budget
  -> continuous dispatcher later claims pending work
  -> dispatcher terminal precheck remains the final duplicate-call guard
~~~

The action is deliberately single-item. Bulk replay makes it too easy to generate
provider load or duplicate side effects and is not needed to close the present P1
operability gap.

## 5. Persistent state fence

Add `state_version INTEGER NOT NULL DEFAULT 0` to `task_outbox` through the next
ordered SQLite migration and expose it on `OutboxRecord`.

Every operation that mutates an outbox row increments `state_version`, including:

- claim or automatic transition to `failed`;
- lease renewal;
- complete, retry, or release;
- operator reconciliation or redrive.

The version is not a lease, timestamp, authorization token, or business revision. It
is an optimistic concurrency generation for the complete durable outbox row. The
operator transition uses `WHERE status='failed' AND state_version=?`; a row count
other than one is a conflict. This prevents double submission and ABA replay after a
row has gone through another delivery cycle.

Existing databases receive version `0` without rewriting payloads or events. Existing
rows and idempotency keys remain valid.

## 6. Storage transitions

SQLite exposes two narrow failed-row transitions. Both run under the existing write
transaction and writer fence, require the exact idempotency key, task type, and
expected `state_version`, and reject a missing or non-failed row.

### Reconcile terminal

`failed -> completed`

- clear lease owner/token/until;
- set `completed_at` and `updated_at` to now;
- increment `state_version`;
- preserve payload, idempotency key, event sequence, attempt counts, maximum attempts,
  creation time, and last failure detail.

This is permitted only when the application service has observed a matching canonical
terminal memory-extraction event.

### Redrive unterminated

`failed -> pending`

- reset `attempt_count` to zero, creating one fresh bounded delivery budget;
- set `available_at` and `updated_at` to now;
- clear lease owner/token/until and `completed_at`;
- increment `state_version`;
- preserve payload, idempotency key, event sequence, maximum attempts, creation time,
  and last failure detail.

The previous error remains stored for diagnosis until normal delivery writes a newer
outcome. Redrive must not increase `max_attempts` and must not create a new row.

## 7. Application service contract

Create a framework-neutral outbox recovery service with small repository and terminal
lookup ports. It accepts only `memory.extraction` rows belonging to the resolved idea
and workspace. Malformed or mismatched payload metadata fails closed.

The list result exposes only operationally safe fields:

- outbox/idempotency identity;
- idea/workspace identity;
- attempts and maximum attempts;
- created/updated timestamps;
- persistent `state_version`;
- a stable allow-listed error code or generic `delivery_failed`;
- whether a matching terminal event currently exists.

It must not expose raw source text, prompts, provider responses, exception text,
credentials, or the full stored payload.

The recovery result is one of:

- `reconciled`: terminal ledger outcome existed; row is completed without dispatch;
- `redriven`: no terminal outcome existed; row is pending for the normal pump.

Missing rows are not found. Wrong workspace/task kind, malformed payload, non-failed
state, or stale `state_version` is rejected. Unexpected storage failures are not
reported as a successful recovery.

## 8. Local operator API

Add two local API operations under `/api/operations/memory-outbox`:

- `GET /failed?ws=<workspace>&limit=<1..100>` returns safe failed-item summaries.
- `POST /recover?ws=<workspace>` accepts an idempotency key, expected
  `state_version`, and `acknowledge_at_least_once: true`.

The acknowledgement is mandatory because an unterminated provider attempt may have
caused a remote side effect without producing a terminal local event. The system can
reduce duplicates but cannot promise provider exactly-once execution.

HTTP behavior:

- invalid/missing acknowledgement or malformed input: 422;
- unknown item or workspace-hidden item: 404;
- stale generation, unsupported state/task, or malformed stored metadata: 409;
- successful reconcile/redrive: 200 with the explicit outcome and current safe item.

The API does not expose a bulk action, force flag, arbitrary status mutation, raw SQL,
or synchronous dispatch. No browser UI is required in this slice.

## 9. Concurrency and failure behavior

- Two recovery requests for the same failed generation cannot both succeed.
- An old request cannot act after any intervening outbox mutation, even if the row has
  returned to `failed` with identical attempts and error text.
- If a terminal event appears after the service check but before or after redrive, the
  dispatcher's existing terminal precheck completes the pending claim without a
  duplicate provider call.
- If the API process stops after committing redrive, the pending row remains durable
  and is picked up on the next pump/startup reconcile.
- If provider mode is currently unavailable, the action still succeeds as `redriven`;
  the row remains pending rather than being falsely reported as delivered.
- Recovery never edits or deletes ledger events and never manufactures a terminal
  event.

## 10. Security and privacy boundary

This is a localhost, single-user operator surface consistent with the current product
deployment. `state_version` and acknowledgement protect correctness and accidents;
they are not authentication. Remote/multi-user authentication is a separate product
boundary and must not be improvised in this slice.

Outbox payload privacy remains unchanged. API serializers use an explicit allow-list,
and tests seed secret-like raw errors/payload fields to prove they are not returned.

## 11. Compatibility and migration

- Schema migration is additive and transactional.
- Opening a v2 database upgrades it to v3 without changing ledger rows or existing
  outbox identity/status.
- New stores start directly at the latest schema.
- Existing claim/heartbeat/retry/release/complete behavior remains the same except for
  monotonic `state_version` increments.
- Existing health response stays backward compatible.
- No event schema, provider, queue, ChatService, or memory extraction contract changes.

## 12. Deferred work

- completed/failed history retention, archive, vacuum, or destructive cleanup;
- bulk recovery, scheduled auto-redrive, or arbitrary attempt-budget changes;
- remote operator authentication/authorization;
- unified operation logs, diagnostic bundles, alerts, metrics, and SLOs;
- provider cancellation or exactly-once remote execution;
- Redis/RQ, distributed workers, and additional outbox task kinds.

## 13. Acceptance boundary

This design is complete when an operator can safely discover a failed memory outbox
item and recover exactly the observed generation; canonical terminal outcomes are
reconciled without provider execution; genuinely unterminated work re-enters the
existing bounded dispatcher; stale or cross-workspace actions fail closed; and no
sensitive payload or raw failure detail crosses the API boundary.
