# Outbox Lease Heartbeat and Recovery V1

> Status: implementation design for the next development round.
> Scope: lease-safe delivery of durable memory-extraction intents during queue wait,
> long provider calls, process failure, and restart.

## 1. Problem

The durable memory outbox already provides atomic intent persistence, idempotent
enqueue, leased claim, retry/dead-letter states, stale-token fencing, continuous
pumping, and startup reconciliation. The remaining ownership gap is time:

- a claim receives a fixed lease before it is submitted to the process-local queue;
- an accepted callback can wait behind another callback long enough for that lease to
  expire before execution starts;
- a provider call can run longer than the default lease;
- there is no renew operation in the storage or application repository contracts;
- after expiry, another dispatcher may reclaim the same intent while the original
  callback is still queued or running;
- completion is fenced, but the duplicate external call may already have happened;
- recovery does not short-circuit a reclaimed intent whose terminal ledger event was
  committed before the previous process failed to acknowledge the outbox row.

This is a delivery-ownership problem, not a need for a new task system. Replacing the
local queue or SQLite with Redis/RQ would not remove the need for explicit lease,
fencing, terminal-event, and recovery semantics.

## 2. Architectural decision

Extend the existing lease/token protocol with fenced renewal and keep a lightweight
heartbeat alive from queue admission until the callback reaches a terminal outbox
transition.

The contracts remain:

1. The event ledger is the canonical business history.
2. The outbox is a durable dispatch index, not a second research fact store.
3. A lease proves temporary delivery ownership; it does not prove progress or success.
4. Only a matching terminal memory-extraction event permits outbox completion.
5. A token that is missing, replaced, or expired cannot renew, complete, retry, or
   release a row.
6. Process death stops heartbeats naturally; the row becomes reclaimable after expiry.
7. The local queue remains process-local and replaceable behind the existing port.

No event type, SQLite column, migration, second orchestrator, or distributed runtime is
required.

## 3. Current flow

~~~text
requested event + outbox row
  -> claim(token, lease_until, attempt + 1)
  -> submit callback to LocalTaskQueue
  -> callback waits and/or calls provider
  -> terminal event exists?
       yes -> complete(token)
       no  -> retry(token)
~~~

The token fences late writes, but nothing extends lease_until. A second claimant can
therefore enter after expiry while the first callback is still active.

## 4. Target flow

~~~text
claim(token, lease_until)
  -> start heartbeat before queue submission
  -> submit leased callback
       admission rejected -> stop heartbeat -> release(token)
       accepted
         -> heartbeat renews while queued
         -> callback starts
              terminal already exists -> complete(token), no provider call
              ownership already lost  -> do not start provider call
              otherwise call worker while heartbeat continues
         -> stop heartbeat before final transition
         -> terminal exists and ownership retained -> complete(token)
         -> no terminal and ownership retained     -> retry(token)
         -> ownership lost                         -> no stale transition
~~~

Starting the heartbeat before submission is intentional. Starting it only inside the
callback would leave queued claims unprotected.

## 5. Storage renewal contract

SQLiteStore gains one narrow operation equivalent to:

~~~text
renew_outbox_lease(item, lease_token, lease_seconds, now=None) -> OutboxRecord
~~~

Required semantics:

- lease_seconds is positive and finite.
- The row must be processing, have the exact token, and have an unexpired lease.
- Renewal is one atomic fenced update inside the existing write transaction.
- lease_until moves monotonically forward to at least now + lease_seconds; an early
  heartbeat must never shorten the existing lease.
- Renewal does not change status, owner, token, attempt count, availability, error, or
  completion fields.
- A missing, expired, replaced, or completed lease raises OutboxLeaseError.
- Old tokens remain fenced after reclaim even if they attempt renewal.
- The existing columns are sufficient; no migration is allowed in this slice.

## 6. Application repository contract

MemoryOutboxRepository gains a transport-neutral renew operation using the existing
kind, idempotency key, and lease token. The SQLite adapter maps a definite
OutboxLeaseError or missing/type-mismatched row to False. Unexpected storage failures
remain distinguishable from definite lease loss so the heartbeat may retry; they must
not be reported as a successful renewal.

The fake repository implements the same ownership behavior and exposes deterministic
renewal observations for contract tests. It must not claim SQLite transaction
semantics.

## 7. Heartbeat lifecycle

MemoryOutboxDispatcher owns heartbeat policy because it owns both the durable claim
and queue admission. The queue port continues to know only idempotency keys and
callbacks.

- The default interval is derived from the lease duration and remains strictly shorter
  than the lease. Tests may configure a shorter interval.
- Heartbeat scheduling uses a stoppable wait, not an uninterruptible sleep.
- One accepted claim has at most one heartbeat.
- The heartbeat starts before queue.submit.
- FULL, CLOSED, DUPLICATE, submission exception, retry, release, and completion all stop
  the heartbeat.
- Callback exit stops and joins the heartbeat boundedly; no heartbeat continues after
  its claim reaches a terminal outbox transition.
- A transient renewal exception does not create a false success. The heartbeat keeps
  retrying while ownership has not been definitively rejected.
- A definite renewal rejection marks the claim as lease-lost.
- If loss is known before worker start, the worker is not called.
- If loss happens during a blocking external call, the call cannot be forcibly
  cancelled by this slice. When it returns, the stale callback performs no outbox
  completion/retry/release. Any terminal event it already wrote remains canonical and
  idempotent.

This design reduces duplicate external calls but does not promise exactly-once remote
provider execution.

## 8. Recovery semantics

Recovery uses existing expired-lease reclaim and terminal ledger events:

- If a process dies before a terminal event, its heartbeat stops; after lease expiry a
  later dispatcher reclaims and runs the intent.
- If a process writes the terminal event but dies before outbox completion, the next
  callback checks for the terminal event before invoking the worker and completes the
  reclaimed row without another provider call.
- If an old process resumes after another owner reclaimed the row, every old-token
  transition remains fenced.
- Heartbeats do not consume attempts. Reclaim after actual expiry continues to consume
  an attempt under the existing policy.
- Startup reconciliation/backfill remains idempotent and does not manufacture terminal
  events.

## 9. Shutdown semantics

Application shutdown first stops the periodic pump and then invokes the existing
bounded queue shutdown. It does not claim to cancel a running provider call.

- A callback that continues after the bounded join retains its heartbeat until it
  returns or the process exits.
- If the process exits, the heartbeat disappears and normal lease expiry enables
  recovery.
- Queued or running intent is never deleted during shutdown.
- This slice does not add a new UI endpoint or shutdown coordinator.

## 10. Failure and concurrency cases

Acceptance tests must cover:

- repeated renewal without attempt-count change;
- early renewal never shortening a lease;
- expired, completed, replaced, or wrong tokens being rejected;
- a queued callback retaining ownership past its original lease;
- a long worker retaining ownership while another claimant sees no available row;
- heartbeat stop after success, retry, release, and admission failure;
- lease loss before worker start preventing the worker call;
- lease loss during work preventing a stale acknowledgement;
- restart reclaim after heartbeats cease;
- terminal-event precheck completing without a duplicate provider call;
- transient renewal error followed by successful renewal;
- no raw source text or provider output entering outbox/heartbeat diagnostics.

Tests should use controllable clocks/events where possible and must not rely on long
wall-clock sleeps.

## 11. Compatibility and boundaries

- Existing outbox rows and databases remain valid.
- Existing claim/retry/release/complete behavior remains unchanged.
- TaskQueue and LocalTaskQueue public contracts remain unchanged.
- ChatService, agent loop, Stata recovery, memory extraction semantics, privacy
  freezing, and terminal event types remain unchanged.
- The UI may continue to compose the dispatcher with defaults; UI behavior and API
  shape are out of scope.
- Redis/RQ remains a future deployment adapter only after a measured multi-process or
  multi-machine requirement.

## 12. Deferred slices

- Manual redrive of failed/dead-letter rows.
- Retention and deletion policy for completed/failed outbox history.
- Backup/restore and database maintenance procedures.
- Unified request/operation/outbox logging, diagnostic bundles, alerts, and SLOs.
- Operator UI/API for outbox inspection or control.
- Cooperative cancellation of an in-flight provider request.
- Other outbox task kinds or distributed workers.

## 13. Acceptance boundary

This slice is complete when an outbox claim remains exclusively owned while queued or
running beyond its original lease, loses ownership safely when renewal is fenced,
recovers after process death without stale acknowledgement, and skips a duplicate
provider call when the canonical terminal event already exists. All existing outbox,
memory, UI lifecycle, privacy, migration, release, and architecture gates must remain
green.
