# Outbox Retention and Safe Pruning Design

Status: proposed for the next implementation round  
Priority: P1  
Scope: completed `memory.extraction` outbox rows only

## Problem

The durable outbox now has lease heartbeat, dead-letter visibility, and guarded manual recovery, but
completed rows are retained forever. That is safe for correctness but leaves an unbounded operational
index in each workspace SQLite database. Direct SQL deletion is not an acceptable product operation:
it has no workspace or terminal-event validation, no stale-preview protection, and can accidentally
remove the only durable handle for a failed job.

This design adds a bounded, explicit operator workflow to preview and prune old completed rows. It does
not delete ledger events, automate retention, or compact the SQLite file.

## Architectural Fit

The existing contracts remain unchanged:

- The append-only event ledger is the business source of truth.
- `task_outbox` is a durable dispatch index, not evidence or research state.
- A requested memory extraction is complete only when the canonical ledger contains a matching terminal
  extraction event.
- Startup backfill recreates only requested intents that have no matching terminal event.
- FastAPI remains a composition/transport layer; retention policy belongs in a framework-neutral
  application service.
- SQLite remains the only local persistence and concurrency boundary. No Redis, RQ, scheduler, or second
  database is introduced.

No architecture change is required.

## Safety Classification

| Outbox state | Terminal event | Retention decision | Reason |
|---|---:|---|---|
| `pending` | either | never delete | Work may still be delivered. |
| `processing` | either | never delete | A live or reclaimable lease may exist. |
| `failed` | absent | never delete | The row is the durable dead-letter/redrive handle. Deleting it would allow startup backfill to recreate fresh pending work and silently reset operator intent and attempt history. |
| `failed` | present | never delete directly | It must first use the existing recovery operation to reconcile to `completed`. |
| `completed` | absent or unverified | block | Outbox status alone cannot prove canonical completion. |
| `completed` | matching terminal | eligible after cutoff | Deleting the dispatch index is safe because canonical terminal state prevents startup backfill from recreating it. |

Only the final case is in scope.

## Operator Workflow

Retention is a two-step preview/apply operation.

### 1. Preview

`GET /api/operations/memory-outbox/retention/preview`

Parameters:

- `ws`: resolved by the existing workspace resolver.
- `retention_days`: integer from 7 through 3650; default 90.
- `limit`: integer from 1 through 100; default 100.
- `cursor`: optional opaque keyset cursor returned by an earlier preview.

The service computes one absolute UTC epoch-second `cutoff`, scans old completed rows in stable
`(completed_at, outbox_id)` order, validates frozen identity and canonical terminal state, and returns at
most `limit` eligible safe items. The response also reports bounded diagnostic counts and a selection
token. It never returns the stored payload, source text, prompt, provider response, or `last_error`.

Illustrative response:

```json
{
  "cutoff": 1786200000,
  "retention_days": 90,
  "limit": 100,
  "eligible_count": 2,
  "blocked_count": 1,
  "scanned_count": 3,
  "scan_truncated": false,
  "next_cursor": null,
  "selection_token": "v1:...",
  "items": [
    {
      "outbox_id": "...",
      "idempotency_key": "memory-extraction:...",
      "fingerprint": "...",
      "completed_at": 1778400000,
      "state_version": 4
    }
  ]
}
```

The scan is keyset-paginated and hard-bounded to 1,000 inspected rows per request. A preview may return
fewer than `limit` eligible rows when invalid rows are blocked. When more rows remain,
`scan_truncated=true` and `next_cursor` let the operator inspect the next page even if the current page is
entirely blocked; blocked early rows therefore cannot permanently hide later eligible rows. Cursors are
validated opaque pagination state, not authorization.

### 2. Apply

`POST /api/operations/memory-outbox/retention/prune`

The body contains only:

```json
{
  "cutoff": 1786200000,
  "limit": 100,
  "cursor": null,
  "selection_token": "v1:...",
  "acknowledge_irreversible_delete": true
}
```

The application service recomputes the exact preview with the supplied absolute cutoff and limit. It
compares the token in constant time. Any changed, added, removed, re-ordered, or state-version-changed
candidate makes the request stale and returns a conflict. An empty, still-current preview may return a
safe `noop`; it performs no write.

The token is a deterministic SHA-256 digest over a versioned canonical serialization containing the
resolved idea/workspace identity, task type, cutoff, limit, page cursor, and ordered candidate tuples
`(outbox_id, idempotency_key, completed_at, state_version)`. It is a freshness/integrity fence, not
authentication or authorization.

After the token matches, storage deletes the entire candidate batch in one SQLite transaction. Each row
must still match all of:

- exact `outbox_id` and idempotency key;
- `task_type = memory.extraction`;
- `status = completed`;
- exact expected `state_version`;
- non-null `completed_at` at or before the supplied cutoff.

If any row no longer matches, the transaction rolls back and the API returns conflict. There is no
partial-success response. The storage primitive accepts a typed set of expected completed-row identities;
it does not accept arbitrary SQL predicates or statuses.

## Validation and Privacy

Before a row enters the selection, the application service must fail it closed unless all of these hold:

- row task type and frozen payload kind are `memory.extraction`;
- payload idea/workspace/fingerprint fields are present and match the resolved scope and row identity;
- `completed_at` is present and no newer than cutoff;
- terminal lookup succeeds and returns one of the existing terminal extraction event types for the same
  fingerprint.

Malformed, cross-scope, or unterminated rows increment `blocked_count` but are not included in item data
or the delete batch. Terminal lookup/storage errors abort the request; they are not treated as “blocked”
or “no terminal.” Unexpected HTTP errors remain sanitized by the existing error handling.

Expected transport mapping:

- `200`: preview, `pruned`, or current empty `noop`.
- `409`: stale token, changed selection, CAS conflict, or malformed/inconsistent stored row selected for
  mutation.
- `422`: invalid days/limit/cutoff/token or missing literal acknowledgement.
- Existing workspace-not-found behavior remains unchanged.

## Storage Contract

Two narrow capabilities are added to the existing SQLite store:

1. Stable keyset listing of completed rows at/before a cutoff for one task type.
2. Atomic batch deletion with per-row status/time/version identity predicates.

No schema migration or index is required in this round. Scans and writes are bounded, and correctness is
more important than speculative indexing. Query-plan/index work should be driven by measured database
sizes after this control plane exists.

## SQLite File Size Semantics

`DELETE` releases SQLite pages for reuse but normally does not reduce the database file size. This API
must not claim reclaimed filesystem bytes. `VACUUM`, WAL checkpoint/truncation, free-space preflight, and
maintenance locking have materially different failure and availability risks and are intentionally a
separate future design.

The Hermes reference implementation reinforced three choices used here: a conservative 90-day default,
dry-run/preview before deletion, and bounded deletion. Its explicit treatment of `VACUUM` and WAL growth
also supports keeping physical compaction outside this operation.

## Failure and Concurrency Semantics

- Preview is read-only and repeatable for a fixed database state and absolute cutoff.
- Apply never trusts client-provided row IDs; it derives them again from the exact preview contract.
- A concurrent outbox mutation or retention call causes token/CAS conflict, never partial deletion.
- A committed delete followed by response loss may make an identical retry stale. This is a safe
  at-most-once deletion outcome; the operator can preview again to observe current state.
- No provider, queue, dispatcher, or memory pipeline call occurs in preview or prune.
- After deletion and restart, canonical terminal events prevent backfill from resurrecting pruned rows.

## Explicit Non-Goals

- Automatic startup, scheduled, or threshold-triggered pruning.
- Deleting or archiving failed, pending, or processing rows.
- Ledger event, memory, snapshot, run, or artifact retention.
- SQLite `VACUUM`, `auto_vacuum`, WAL checkpoint, backup, restore, or file-size reclamation.
- Browser UI, remote authentication/RBAC, bulk redrive, generic admin SQL, metrics/SLO, or audit-log
  infrastructure.

## Acceptance Summary

The design is complete when implementation proves:

1. Only old completed rows with matching canonical terminal events are selectable.
2. Preview data is safe, workspace-scoped, stable, and bounded.
3. Explicit acknowledgement plus an exact fresh token is required.
4. Batch deletion is all-or-nothing under concurrent changes.
5. Failed/pending/processing and blocked completed rows survive unchanged.
6. Pruned rows are not recreated by startup backfill.
7. No events are deleted and no provider/dispatcher work is triggered.
8. Documentation states that database file size does not necessarily shrink.
