---
artifact: instrumentation-spec
version: "1.0"
created: 2026-09-23
status: implemented-first-slice
---

# Instrumentation Spec: Fault Investigation and Observability

## Overview

**Feature:** Agent 运行错误定位、工具决策追踪与本地诊断

**Analytics Goals:**

1. 一个 Turn 出问题时，先判断故障位于控制、上下文、Provider、工具选择、准入、执行、Stata、Evaluator 还是恢复层。
2. Agent 调错工具时，在不读取模型隐藏思维的前提下，还原模型可见上下文、公开回复、原始/规范化参数、工具合同、调度与准入结果。
3. 区分“研究事实确实发生了什么”和“某段代码为什么慢或崩溃”，避免用可清理的 Span 证明正式 Result。
4. 用稳定 ID 从 Turn 下钻至 Step、Invocation、Tool Call、Operation、Attempt、Artifact、Result 与 Evidence。

**Analytics Platform:** Workspace 内 SQLite 权威账本 + 应用私有、有界本地 JSONL Diagnostics。V0.1 不向外部遥测平台发送数据。

**Naming Convention:** 权威 Journal 延续项目的 `domain.action` 命名；诊断事件使用已注册的 `domain.action` schema；原因码与状态码使用稳定 `lower_snake_case` token。

## Investigation Contract

| 问题 | 首查入口 | 继续下钻 | 权威性 |
| --- | --- | --- | --- |
| 这个 Turn 为什么失败、暂停或卡住？ | `GET /api/v1/workspaces/{workspace_id}/turns/{turn_id}/investigation` | 返回的 `next_queries` 与 Tool Call ID | 权威事实的只读汇总 |
| Agent 为什么调用了这个工具？ | `GET /api/v1/workspaces/{workspace_id}/turns/{turn_id}/tool-decisions/{tool_call_id}` | Context Manifest、Assistant Output、参数、Contract、Dispatch Plan | 可核验输入/输出与结构化决策链；不含隐藏思维 |
| 工具为什么没有执行？ | Tool Decision Trace 的 `status_history`、`admission`、`structural_diagnosis` | `tool.admission_blocked` Journal | 权威 |
| 工具到底有没有产生副作用？ | Tool Decision Trace 的 Operation/Attempt | Completion Manifest、Recovery Report、Journal | 权威；不得由 Span 推断 |
| 某个数字从哪里来？ | `GET /api/v1/workspaces/{workspace_id}/lineage` | Result → Stata Run → Command Instance → do/log/dta Artifact | 权威研究来源 |
| Provider 是否重试、失败或回传 usage？ | Turn Investigation 与 Turn Usage | Model Invocation → Provider Attempt | 权威运行事实 |
| 为什么很慢、哪个函数报异常？ | Diagnostic Bundle / `agent.span` | `trace_id`、`parent_span_id` 与领域 ID | 非权威诊断 |
| Evaluator 为什么拦截或警告？ | `GET /api/v1/workspaces/{workspace_id}/turns/{turn_id}/evaluation` | evaluation finding 与关联对象 | 权威评价记录/可重建汇总按各自合同判断 |

调查顺序固定为：

```text
Turn Investigation
        ↓
选中异常 Tool Call / Provider Attempt / Evaluation Finding
        ↓
Tool Decision Trace 或对应领域 API
        ↓
按 turn_id / operation_id / attempt_id 查询原始 Journal
        ↓
需要性能或异常栈定位时，再关联本地 Span / Diagnostic Bundle
```

## Event Inventory

### `tool.proposed`

| Field | Value |
| --- | --- |
| **Trigger** | Tool Proposal 完成规范化并形成 Tool Call 身份 |
| **Description** | 记录模型提出了哪个工具；原始与规范化参数保存在不可变参数表中，不塞入 Journal 摘要 |

**Required correlation:** `turn_id`, `step_id`, `assistant_output_id`, `tool_call_id`, `tool_contract_id`, `workspace_revision`.

### `tool.rejected`

| Field | Value |
| --- | --- |
| **Trigger** | Preflight 因工具不存在、schema 无效或规范化失败而拒绝 Proposal |
| **Description** | 表示 Proposal 从未获得调度或执行权限 |

**Required properties:** stable `reason_code`, requested tool name, proposal ordinal, Tool Call ID when identity was created.

### `tool.dispatch_plan_committed`

| Field | Value |
| --- | --- |
| **Trigger** | 一组 Scheduled Tool Calls 的顺序、依赖与批次被提交 |
| **Description** | 证明“计划调用”，不证明“已经准入”或“已经执行” |

### `tool.admission_blocked`

| Field | Value |
| --- | --- |
| **Trigger** | Scheduled Call 在 Just-in-Time Admission 被权限、状态、预算、资源、确认或 freshness 规则挡住 |
| **Description** | 保留稳定原因，同时保持 Call 为 Scheduled/Pending Revalidation；不伪造 Operation |

**Required properties:** `turn_id`, `tool_call_id`, stable `reason_code`, current proposal status, `workspace_revision`.

### `tool.admitted`

| Field | Value |
| --- | --- |
| **Trigger** | Tool Call 通过准入并创建唯一 Operation |
| **Description** | 表示已获得执行权限，仍不证明外部执行已经发生 |

### `tool.handoff_committed`

| Field | Value |
| --- | --- |
| **Trigger** | 外部执行 Handoff UoW 已提交 |
| **Description** | 这是“不再能够安全断言 definitely-not-started”的边界，不等同于执行器确认启动 |

### `tool.completed` / `tool.failed` / `tool.interrupted`

| Field | Value |
| --- | --- |
| **Trigger** | Attempt 获得稳定完成、失败或丢失可靠完成信号的观察 |
| **Description** | 记录当时执行事实；Recovery classification 是另一层事实 |

### Provider Attempt lifecycle

| Field | Value |
| --- | --- |
| **Trigger** | 一次 Provider transport attempt 被创建并进入成功、失败或 delivery-unknown 状态 |
| **Description** | 保存重试、模型/Provider、usage quality 与失败分类；纯 transport retry 不创建新 Step |

### `agent.span`

| Field | Value |
| --- | --- |
| **Trigger** | 已接入的 Turn Driver、Model Gateway、Worker 或 Tool Runtime 操作结束 |
| **Description** | 非权威、可清理的耗时与故障关联信号 |

**Properties:** `trace_id`, `span_id`, `parent_span_id`, `span_name`, `span_kind`, `status_code`, `component`, `process_role`, logical Workspace/Turn/Operation/Attempt refs, `duration_ms`, exception type when safe. 禁止 prompt、completion、工具参数、工具结果、研究文本和密钥。

## User Properties

V0.1 是本地单用户产品，不建设用户画像或持久 user properties。Workspace、Conversation、Turn 和 Research Path 是领域身份，不作为跨项目用户追踪标签，也不发送至第三方 Analytics。

## PII & Privacy Considerations

### Data Classes

| Data class | Location | Handling |
| --- | --- | --- |
| 用户消息、公开 Assistant Output、系统/Skill 快照 | Workspace SQLite | 属于本地权威研究历史，受 Workspace 生命周期管理 |
| Tool 原始/规范化参数与结果 | Workspace SQLite / Artifact Store | 仅经本地认证 API 按对象身份读取，不进入 Diagnostics |
| 文献、数据、do/log/Word | Artifact Store | Diagnostics 只允许逻辑 ID，不读取 payload |
| Provider credential | 受控凭据边界 | 不进入 Worker、Journal、Span 或 Diagnostic Bundle |
| Span 元数据 | 应用私有 Diagnostics | schema 白名单 + SensitiveOutputGate；不含研究内容 |

### Consent Requirements

- 创建 Workspace 并运行 Agent 即会生成完成产品核心功能所需的本地权威 Trace；V0.1 不进行外部遥测。
- Diagnostic Bundle 只有用户显式请求保存后才形成可分享文件；预览与保存沿用现有敏感输出边界。

### Data Retention

- 权威 Journal、模型输出、工具参数/结果跟随 Workspace 保留与删除策略，不由 Diagnostics retention 清理。
- Diagnostics 默认单文件 8 MiB 轮转、14 天或总量 256 MiB 上限；Crash Capsules 默认 30 天且最多 20 个。

### Model Trace Capture

| Question | Decision |
| --- | --- |
| **Data classes captured** | Workspace 权威账本捕获用户消息、系统/Skill 快照、检索上下文引用、公开模型输出、工具参数与结果；Diagnostics 只捕获元数据和逻辑 ID |
| **What is captured** | 权威 Trace 保存产品复现所需的已提交输入/输出；不读取或保存 Provider 隐藏思维。Diagnostics 为 metadata only |
| **Minimization before egress** | V0.1 没有外部 collector。Diagnostic Bundle 必须经 SensitiveOutputGate，只允许已注册字段和逻辑 locator |
| **If egress minimization fails** | Fail closed：不产生/不保存 Bundle，不发送原始内容 |
| **Minimization before storage** | Diagnostics 在 append 前由 registry + SensitiveOutputGate 校验；研究 payload 只存入 Workspace 权威存储，不复制到诊断日志 |
| **If storage minimization fails** | Fail closed：丢弃该诊断事件；不得影响 Turn 主流程，也不得降级写入原始值 |
| **Terminal disposition of a failed trace** | 被拒 Diagnostics 直接丢弃，不进入 retry/dead-letter；权威 Journal 走独立事务，不受影响 |
| **Who can read a trace** | 当前本机通过有效 loopback session 的用户；没有远程团队角色 |
| **Whether a read is logged** | V0.1 不把只读 investigation 查询写回权威 Journal，也没有多用户 reader identity；这是本地单用户边界下的显式限制，而非已实现的访问审计 |
| **Retention** | Workspace Trace 随 Workspace；Diagnostics 按 14 天/256 MiB，Crash Capsule 按 30 天/20 个 |
| **Sampling** | 权威 Trace 100% 记录已提交事实；已接入的 `agent.span` 不采样，但允许因本地诊断不可用而缺失 |
| **User opt-out** | 核心权威 Trace 不可关闭，否则无法满足可复现；Diagnostics 不外发并可清理，当前无逐事件 UI 开关 |

## Implementation Notes

### SDK/Integration

- **Platform:** Windows 本地 Python/FastAPI 服务 + Workspace SQLite + 浏览器客户端。
- **Integration:** `SqliteInvestigationQuery` 做跨表只读关联；`DiagnosticTracer` 在非权威 Sink 上生成父子 Span。
- **Initialization:** 每个 Workspace 使用自己的只读查询连接；Diagnostics 初始化失败时服务继续运行并明确降级。

### Event Timing

- 权威状态变化与对应 Journal 必须在同一 SQLite UoW 原子提交。
- Admission block 必须在 Call 仍为 Scheduled 时持久化；不能只把错误文本放入下一轮临时上下文。
- Span 关闭失败不得改变业务执行结果；Span 永远不能补写或替代 Journal。
- Investigation API 只读，不 bump `workspace_revision`。

### Structural Diagnosis Boundary

系统可以确定性识别：preflight rejection、admission block、执行不确定、执行失败、Tool Result error 与不完整 Call。一个工具若 schema 合法且执行成功，但在研究语义上选错，系统不能假装从日志中自动知道“为什么错”。此时 Trace 提供 Context Manifest、公开回复、工具参数和可用工具合同，由 Evaluator 或用户做语义判断并留下新的评价事实。

## Testing Checklist

### Event and Correlation Validation

- [x] `tests/contract/test_investigation_api.py`：同一 Turn 内同时构造 Admission Block 与成功 Operation，验证总览、下钻、参数、原因、Operation、Result 与 Journal 关联。
- [x] `tests/vertical/test_tool_broker_vertical.py`：验证 Tool Proposal、Dispatch、Admission、Attempt 与结果的持久化边界。
- [x] `tests/vertical/test_agent_turn_driver_vertical.py`：验证 Turn Driver 在模型—工具—观察循环中的推进和停止。
- [x] `tests/unit/test_diagnostic_tracing.py`：验证父子 Span、领域 ID、耗时、失败状态和诊断故障不反噬主流程。
- [x] `tests/contract/test_api_contract.py`：验证 OpenAPI 与 TypeScript 合同没有漂移。

### Trace Capture Validation

| Claim under test | Normal-path test | Failure or degraded-path test | QA owner |
| --- | --- | --- | --- |
| Diagnostics 只接受注册字段 | `tests/unit/test_diagnostics.py` schema acceptance | 同文件 unknown/unsafe field rejection | Runtime maintainer |
| Span 含父子关联但不含研究 payload | `tests/unit/test_diagnostic_tracing.py` field assertions | 同文件 sink failure is swallowed | Runtime maintainer |
| 本地文件 retention 有界 | `tests/unit/test_diagnostics.py` rotation/retention | 同文件 sink failure/health marker | Runtime maintainer |
| Bundle 经敏感输出边界 | `tests/integration/test_diagnostic_bundle.py` safe projection | 同文件 secret canary fail-closed cases | API maintainer |
| Investigation 读不修改 Workspace | `tests/contract/test_investigation_api.py` 返回 revision 与事实 | 缺失 Workspace/Turn 返回 404 的 API contract coverage | Persistence maintainer |
| 读操作不记录 reader identity | 当前产品明确不承诺访问审计 | 多用户功能进入范围前必须新增 principal + read-audit 测试 | Product owner |
| Diagnostics 不外发 | service wiring review + no external sink implementation | 网络不可用不影响本地执行；无外部路径可调用 | Runtime maintainer |

### Debug Tools

- Turn 首查：`/turns/{turn_id}/investigation`。
- 单个工具决策：`/tool-decisions/{tool_call_id}`。
- 原始顺序史：`/journal-entries`，按 `turn_id`、`operation_id`、`attempt_id` 或 `event_type` 过滤。
- 数字保真：`/lineage`，定位 Stata Run、Command Instance 和 Artifact。
- 性能/异常：在设置页显式生成 Diagnostic Bundle；按 `trace_id` 和领域 ID 关联，但不把它当研究证据。
