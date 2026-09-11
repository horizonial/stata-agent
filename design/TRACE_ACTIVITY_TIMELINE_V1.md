# Trace Activity Timeline V1

Status: implemented in the current round  
Priority: P1  
Primary objective: make one agent request understandable without reading raw event names or opaque IDs

Implementation note: V1 is delivered as an additive read model. Provider lifecycle events and
correlation propagation for signed evidence are recorded in the existing ledger envelope; the
`/api/trace/activity` endpoint and default Activity Timeline consume that projection. The raw
`/api/trace` view remains the Technical Audit fallback.

## 1. Problem Statement

The current Trace page is a technically correct ledger projection but a poor product explanation. It
renders one flat row per event with columns such as event type, actor, phase, object and summary. In a
real request this produces many nearly empty rows, while stable identifiers such as
`run-942a204a6f`, `op-run-942a204a6f:call:5` and provider call IDs occupy the most visible column.

The live request reviewed on 2026-09-11 illustrates the gap:

1. the user submitted “重新来”;
2. the model selected `run_stata`;
3. Stata executed three user-meaningful commands and two internal environment-attestation probes;
4. the model selected `read_artifact`;
5. the model returned a final answer;
6. the page displayed 22 separate technical events and mostly blank summaries.

The raw ledger already carried one common `correlation_id`, but `_public_event` did not expose it. The
ledger also had no explicit provider-turn lifecycle, so Trace could not show model-call count, latency,
response kind, usage, retry or safe failure metadata. This design fixes the read model without turning
Trace into a prompt/output dump or a second truth store.

## 2. Architectural Fit

- SQLite append-only events remain the only durable operational and research truth source.
- `ChatService → agent_loop → provider/tool/store` remains the only orchestration chain.
- A new application-layer trace projector folds existing events into a read-only activity view; it does
  not write state or become a second reducer for research truth.
- Additive provider lifecycle events use the existing event envelope and schema versioning. No database
  migration is required.
- Existing run, operation, call, evidence and request identifiers are not renamed or rewritten.
- The existing `/api/trace` raw projection remains backward compatible. A separate activity endpoint is
  the default UI data source.
- No chain-of-thought, prompt, raw provider response, tool arguments, Stata output, secret, credential,
  private path or document text is persisted for this feature.

No architecture change is required.

## 3. Product Model: Two Views, One Ledger

### 3.1 Default: Activity Timeline

The default view answers four questions:

1. What did I ask?
2. What did the agent decide to do?
3. What tools or research operations actually ran?
4. Did the request finish, stop, fail, or produce verified evidence?

It groups events by `correlation_id` into one request card. Cards are ordered newest-first; steps inside a
card are chronological. Internal events are collapsed beneath their owning step.

Example:

```text
请求 4 · 重新来                                      已完成 · 5.8 秒
  1  模型回合 1              选择运行 Stata             0.6 秒
  2  Stata 运行 1             执行成功                  3.0 秒
       ├─ 载入示例数据
       ├─ 查看数据结构
       ├─ 生成描述统计
       └─ 环境核验 2 项
       提示：执行成功，但未生成结构化可验证结果
  3  模型回合 2              读取本次运行结果             1.1 秒
  4  读取运行结果             已完成                    <0.1 秒
  5  模型回合 3              生成最终答复                1.0 秒

模型 3 次 · 顶层工具 2 次 · Stata 运行 1 次 · 上下文峰值 3,187 / 12,000
```

### 3.2 Secondary: Technical Audit

The technical view preserves the existing event-by-event table for support, recovery and contract
inspection. It may localize event labels and improve summaries, but it must not hide, merge or reorder raw
events. Full technical identifiers appear only here or in an activity step's expanded technical details.

## 4. Human-Readable Identity Contract

Opaque identifiers remain machine identity. Human labels are a deterministic presentation layer.

| Durable identity | Primary display | Expanded technical detail |
|---|---|---|
| request/correlation UUID | `请求 4 · <用户消息摘要>` | `request_id` with copy action |
| provider turn | `模型回合 1` / `结果总结` | provider, model, turn index, attempts |
| top-level tool call | `运行 Stata` / `读取运行结果` | tool name and full call ID |
| Stata run ID | `Stata 运行 1` | run ID and operation ID |
| executor sub-call | semantic command label | internal call ID and command fingerprint |
| evidence card/claim | metric/claim label when available | card/claim ID |
| context projection | `上下文 3,187 / 12,000` | source counts and compaction boundary |

Rules:

- Request ordinal is the chronological ordinal of correlated `user.message` events in the workspace.
- Step ordinals are scoped to one request. They are deterministic after reload and pagination.
- Random-looking IDs never appear in the collapsed activity card, table header or primary object label.
- Full IDs remain selectable and copyable under “技术详情”; ellipsis is visual only and never changes the
  copied value.
- Historical events without a trustworthy correlation are placed in `历史记录（未关联）`. The projector
  must not guess ambiguous tool pairing or attach an event to the nearest user message.
- Reused historical runs keep their original request ownership. A current tool step may display
  `复用既有 Stata 运行` and link to that run without rewriting history.

## 5. Deterministic Trace Projection

Create a framework-neutral `TraceProjectionService` in the application layer. Its inputs are a bounded
chronological event sequence and filter/page options. Its output contains request groups and presentation
metadata only.

### 5.1 Request Group

```text
TraceActivityGroup
  request_id                 opaque technical identity
  display_ordinal            workspace request ordinal
  title                      bounded user-message summary
  started_at / completed_at
  duration_ms
  status                     running/completed/paused/failed/uncertain/cancelled
  status_label               fixed localized label
  phase_label                optional; omitted when phase is null
  counts                     provider_turns/tools/runs/evidence/warnings
  budget                     used/limit for provider turns and top-level tools
  context                    latest and peak bounded token snapshots
  evidence_status            none/executed_only/structured/verified
  steps[]
  technical_ids              returned but rendered only on expansion
```

### 5.2 Activity Step

```text
TraceActivityStep
  ordinal
  kind                       model/tool/run/evidence/approval/context/system/failure
  label                      human action label
  summary                    deterministic safe sentence
  status                     stable enum
  started_at / completed_at / duration_ms
  children[]                 collapsed executor/internal events
  warning                    optional stable safe warning
  links                      run/card/claim detail actions
  technical                  allow-listed IDs and fingerprints only
```

### 5.3 Grouping Rules

- `correlation_id` is the only normal request-group key.
- `tool.invoked → tool.done` pairs by stable `call_id`.
- `run.requested → tool.call/result* → run terminal` pairs by operation/run identity.
- Provider lifecycle pairs by `(correlation_id, turn_index, attempt_index)`.
- Evidence/approval events link by their existing identity fields.
- Adjacent identical `context.assembled` snapshots are one visible context update; raw events remain in
  Technical Audit.
- An incomplete pair remains visible as `未闭合` or `状态未知`; it is never silently discarded.

## 6. Provider Turn Observability

Add three append-only event types:

- `provider.turn.started`
- `provider.turn.completed`
- `provider.turn.failed`

Every event uses the current request `correlation_id`. Payloads are strictly allow-listed.

Started metadata:

- provider/model catalog IDs;
- logical `turn_index` and physical `attempt_index`;
- whether this is a finalization-only turn;
- number of exposed tools;
- bounded context estimated/budget tokens.

Completed metadata:

- duration in milliseconds;
- response kind: text/tool_calls/ask/empty;
- tool-call count;
- provider attempt count;
- input/output/total usage integers only when supplied by the provider.

Failed metadata:

- duration, attempt count, stable safe error code and retryable flag.

Forbidden metadata includes prompts, message bodies, response bodies, reasoning, tool arguments, headers,
URLs, keys, raw exception text and local paths. Instrumentation must not change provider retry, fallback,
cancellation or terminal-first-wins behavior.

## 7. Safe Semantic Summaries

Backend summaries are deterministic and localized; the browser does not recreate business meaning from
raw payload keys.

Examples:

| Event/step | Human summary |
|---|---|
| user message | bounded existing text summary |
| provider completed with tool calls | `模型选择了 1 个工具` |
| `run_stata` invoked | `开始运行 Stata` |
| `sysuse` executor call | `载入示例数据` |
| `describe` | `查看数据结构` |
| `summarize` | `生成描述统计` |
| environment probe | `环境核验` |
| successful tool result | `已完成` |
| agent terminal | bounded `reply` summary, not an empty decision field |
| context assembly | `上下文 3,187 / 12,000 tokens` |
| budget limit | fixed safe reason and `used / limit` |

Stata command labels are derived from a closed command-family map. Unknown commands display
`执行 Stata 命令`; raw code is not copied into the activity API. The existing run/artifact detail remains
the place to inspect authorized local output.

## 8. Execution Truth vs Evidence Truth

Trace must not equate a successful process with verified research evidence.

| Condition | Display |
|---|---|
| run succeeded, empty machine result, no cards | `执行成功 · 未生成结构化可验证结果` |
| machine result present, no signed card | `已提取结构化结果 · 尚未签入证据` |
| signed cards present | `已验证并签入证据 · N 项` |
| run failed | safe failure label |
| run uncertain | `执行状态待确认` with recovery action |

The current generic conversation sentence “机器层结果已签入证据链” must only be emitted when signed
evidence actually exists. Otherwise it says “运行完成” and reports the appropriate evidence status.

## 9. API Contract

Keep `GET /api/trace` unchanged for compatibility and Technical Audit.

Add:

```text
GET /api/trace/activity
  ws
  limit=20                  request groups, 1..50
  before_seq               cursor for older request groups
  category                 all/model/tool/run/evidence/approval/failure
  status                   optional stable status
  search                   bounded text/semantic label search
```

Response schema: `stata-agent.trace-activity.v1` with `items`, `next_before_seq`, `total_groups`,
`workspace` and `legacy_uncorrelated_count`. Pagination operates on request groups, not individual events.
Filtering is server-side and remains correct across pages.

The implementation reads at most 500 events per activity page (and exposes `truncated` when older
history exists); unfiltered and event-vocabulary category totals use a read-only SQLite correlation
aggregate so loading an older cursor does not materialize the full ledger.

The existing raw endpoint gains only backward-compatible safe fields where useful: `correlation_id`,
localized label and corrected summary. Existing fields and cursor semantics remain.

## 10. UI Contract

- Trace opens on `活动时间线`; `技术审计` is a secondary tab.
- The activity page is a vertical list of request cards, not a seven-column event table.
- Card header shows user summary, status and duration. Card body shows 3–8 semantic steps; internal calls
  are collapsed under the owning run/tool.
- Summary chips show model calls, top-level tools, Stata runs, context budget and evidence readiness.
- “技术详情” reveals full request/operation/run/call IDs with copy buttons.
- Search placeholder is `搜索请求、工具、运行或错误`; random IDs are searchable only in Technical Audit.
- Filters are `全部 / 模型 / 工具与 Stata / 证据 / 审批 / 异常` and are applied by the server.
- Null phase is omitted rather than rendered as a blank column. Actor becomes a secondary detail, not a
  primary concept.
- Keyboard expansion, focus restoration, narrow-screen cards, reduced motion and no-`innerHTML` remain
  mandatory.

## 11. Compatibility, Privacy and Failure Behavior

- Existing 51-event workspaces render immediately; provider-turn rows appear only for new requests.
- Legacy null correlations are counted and accessible in Technical Audit without inferred grouping.
- Trace projection is read-only and must trigger zero provider, executor, dispatcher, outbox or file work.
- All lists and strings are bounded before serialization. Search input and pagination are bounded.
- IDs are opaque metadata, not authorization tokens. Workspace resolution remains the existing boundary.
- Projection failure returns a stable safe error and leaves the last successfully rendered page visible.
- No schema migration, OTel backend, remote telemetry upload or retention-policy change is included.

## 12. Acceptance Scenarios

1. The reviewed “重新来” fixture renders as one completed request with three model turns, two top-level
   tools, one Stata run, three meaningful Stata commands, two collapsed environment probes and no raw ID
   in the collapsed card.
2. The same card reports `执行成功 · 未生成结构化可验证结果` when machine/cards are empty.
3. Expanding Technical Details exposes copyable exact request/run/operation/call IDs.
4. Provider timeout shows the failed model turn, attempts, duration and safe code without raw exception.
5. Budget exhaustion shows turns used/limit and the last completed step.
6. Interleaved same-name tools pair only by `call_id`; ambiguous legacy rows are not guessed.
7. Raw Technical Audit preserves exact event order and existing pagination.
8. No secret, prompt, response body, tool argument, Stata output, private path or reasoning appears in the
   activity response, DOM or diagnostics.
