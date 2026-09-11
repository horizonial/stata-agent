# Document Authority

本文件是唯一有效的当前实施计划。`ARCHITECTURE.md` 是最高架构约束；
`design/TRACE_ACTIVITY_TIMELINE_V1.md` 是本轮详细设计；其他设计与历史报告只作参考。

# Objective

交付 **Trace Activity Timeline V1**：把当前面向内部事件账本的扁平 Trace，改造成按一次用户请求分组、
能直接回答“模型想了几轮、调用了什么、Stata 做了什么、结果是否形成结构化证据、为何停止”的产品级活动时间线；
随机 UUID、call ID、run ID 默认隐藏，仅在可展开的技术详情中保留。

# Context

最新真实运行“重新来”已经证明 agent loop、Stata 调用和工具后总结能够完成，但 Trace 页面仍主要展示
22 条内部事件和随机标识。用户无法从中判断模型回合数、工具层级、上下文变化与证据状态。

代码审计确认问题位于现有观测投影边界：SQLite 账本已经持久化 correlation ID 和底层事件，
但 `/api/trace` 的公开投影丢弃 correlation ID；agent loop 没有 provider lifecycle 事件；前端把每条原始事件
平铺为同等级行。此次只补充安全事件和 application-owned 只读投影，不改变既有编排、账本或恢复架构。

# Current Behavior

- `GET /api/trace` 返回 newest-first 的扁平公开事件，主要字段为 sequence、type、object、summary 和 payload。
- object 列优先展示 request/run/call UUID，普通用户看到大量字母数字而非业务含义。
- SQLite 事件保存 correlation ID，但公开 Trace 响应没有返回，无法可靠按一次用户请求分组。
- agent loop 没有 provider turn 的 started/completed/failed 事件，Trace 无法说明模型实际调用轮数、耗时或 token usage。
- 多种终态 payload 使用 `reply`、`status`、`code` 等字段，当前 summary fallback 不覆盖，许多行为空。
- `run_stata` 顶层工具、executor 内部命令、context snapshot 和 artifact 读取均平铺，内部细节压过主流程。
- 连续相同的 context snapshot 重复出现，没有聚合为“上下文保持不变”。
- “Stata 执行成功”“有结构化 machine result”“已生成 evidence card/claim”没有分级，界面可能把执行成功误述为证据已签入。
- 多类型筛选主要在前端当前已加载行上执行，total/pagination 容易造成完整筛选的错觉。

# Target Behavior

- 默认 Trace 为按 correlation ID 分组的请求卡片，标题使用“请求 4”和用户消息摘要，不展示随机 ID。
- 每个请求按因果顺序展示少量语义步骤，例如“模型回合 1 → Stata 运行 1 → 读取运行结果 → 模型回合 2 → 已完成”。
- provider 每次逻辑调用都有 started/completed/failed 账本事件，只记录安全元数据、耗时、usage 和结果类别。
- `run_stata` 及其内部 executor 事件折叠成一个 Stata 运行步骤；内部命令可展开查看，并使用受控命令族标签。
- 连续等价的 context snapshots 合并显示，同时保留首次/末次 sequence 和重复次数。
- 结果状态严格区分：仅执行成功、已获得结构化结果、已生成 EvidenceCard、已形成 Claim；不能越级宣称。
- 新增有界的 `GET /api/trace/activity` 服务端投影；现有 `GET /api/trace` 保持兼容并作为“技术审计”视图。
- 完整 UUID/call ID/run ID/correlation ID 仅在展开的“技术详情”中显示并可复制。
- 旧事件缺少 correlation ID 时单独进入“历史未分组事件”，不得依据时间或 UUID 猜测归属。

# Invariants / Hard Constraints

- SQLite append-only ledger 仍是运行事实唯一真相源；Activity Timeline 只是可重建的只读投影。
- `ChatService → agent_loop → provider/tool/store` 仍是唯一编排链，不新增第二状态机或 Trace 专用状态源。
- provider lifecycle 事件只能观测既有调用，不得改变重试、路由、预算、工具、取消或 terminal-first-wins 语义。
- 现有 `/api/trace` 的字段和恢复/诊断消费者保持向后兼容；新能力使用独立 activity endpoint。
- 不保存或展示 system prompt、完整请求上下文、模型原始响应、隐藏推理、API key、附件正文或 Stata 原始敏感输出。
- correlation ID 是关联键，不是授权边界；workspace、路径和下载仍执行现有隔离与访问校验。
- EvidenceCard/Claim 的真实性以 ledger 中实际签名事件为准，UI 不得从自然语言回复或 run success 推断。
- 前端继续使用原生安全 DOM API，禁止 `innerHTML`；不引入前端框架。
- 投影输出必须设置数量、payload 深度、字符串长度和响应体上限，不能无界读取全账本。
- legacy null-correlation 事件不得启发式归组。
- 不删除、skip、xfail 或弱化现有测试。

# Scope

## In Scope

- provider turn 安全生命周期事件与 schema 注册。
- application-owned `TraceProjectionService` 和稳定的 activity DTO。
- correlation 分组、人类序号、语义 summary、层级折叠和 context 去重。
- Stata 执行/结构化结果/证据签名的准确状态投影。
- `/api/trace/activity` 的有界查询、event type 服务端筛选和稳定错误响应。
- Trace 页面默认 Activity Timeline、可切换 Technical Audit、展开技术详情和复制 ID。
- provider/model、工具、Stata、context、budget、cancel/failure/recovery 的成功与失败测试。
- browserless UI contract、产品 eval、兼容与隐私回归。
- 更新设计说明和最终 `IMPLEMENTATION_REPORT.md`。

## Out of Scope

- OpenTelemetry、Jaeger、云遥测、远程日志采集或多用户 observability backend。
- 修改 SQLite schema、重写 ledger、迁移或回填历史 correlation ID。
- 改变 agent 最大步数、provider 路由/重试、工具选择、context/memory 算法或 Stata 执行策略。
- 暴露 prompt、完整模型输入输出、chain-of-thought、secret、附件原文或原始数据集。
- RAG、OCR、rerank、econometrics、Writer/evidence 签名逻辑本身的质量改造。
- installer、Windows 签名、升级/回滚和发布型 UX。
- 通用 tracing DSL、插件式 renderer 或新的前端框架。

# Architecture Change Required

None。

新增 provider lifecycle 事件属于既有 append-only event vocabulary 的兼容扩展；新增 Trace projector 位于 application
读模型边界，不拥有写权限、不参与编排，也不成为第二真相源。

若实现必须修改 SQLite schema、把投影状态持久化为另一套权威数据、记录完整 prompt/response，或让 Trace 服务参与
agent 控制流，应立即停止并单独提出架构变更。

# Files

## Primary files

- `app/src/stata_agent/application/trace_projection.py`（新增）
- `app/src/stata_agent/application/__init__.py`
- `app/src/stata_agent/events/schema.py`
- `app/src/stata_agent/harness/agent_loop.py`
- `app/src/stata_agent/ui.py`
- `app/src/stata_agent/webui/index.html`
- `app/src/stata_agent/webui/app.js`
- `app/src/stata_agent/webui/styles.css`

## Likely test files

- `app/tests/test_trace_projection.py`（新增）
- `app/tests/test_agent_loop.py`
- `app/tests/test_ui_trace_recovery.py`
- `app/tests/test_application_layer.py`
- `app/tests/test_e2e_full_pipeline.py`
- `app/tests/test_ui_interaction_contract.py`
- `app/tests/test_product_ux.py`
- `app/tests/test_product_ux_coverage.py`
- `app/tests/test_product_eval.py`

## Reference-only files

- `ARCHITECTURE.md`
- `design/TRACE_ACTIVITY_TIMELINE_V1.md`
- `design/CORRELATION_DIAGNOSTIC_BUNDLE_V1.md`
- `design/dd-01-domain-events.md`
- `design/ui-design-codex.md`
- `design/ui-requirements-codex.md`
- `IMPLEMENTATION_REPORT.md`
- `app/src/stata_agent/application/diagnostics.py`
- `app/src/stata_agent/stata_executor.py`
- `app/src/stata_agent/evidence/`
- `app/src/stata_agent/storage/sqlite_store.py`

## Files that should not be modified unless necessary

- `app/src/stata_agent/storage/migrations.py` 和 SQLite schema。
- `app/src/stata_agent/domain/` reducer、ResearchState 与审批状态机。
- provider transports、routing/fallback、tool permissions 和 Stata execution behavior。
- context、compaction、memory、RAG、Writer 与 evidence signer 实现。
- settings、release、installer、signing 和 upgrade/rollback 代码。
- 既有 golden/eval fixtures；只有新增独立 trace scenario 时可追加，不得覆盖历史预期。

# Implementation Tasks

## Task 1 — Provider turn lifecycle observability

- **Goal**：让账本能够无歧义记录每次 provider 逻辑回合，而不暴露模型内容或改变执行语义。
- **Files**：`events/schema.py`、`harness/agent_loop.py`、`tests/test_agent_loop.py`。
- **Required behavior**：
  - 注册 `provider.turn.started`、`provider.turn.completed`、`provider.turn.failed`。
  - 每次实际 provider 调用前 append started；正常返回 append completed；异常路径 append failed 后保持原异常处理。
  - payload 只允许 turn ordinal、provider/model 安全标识、tools-enabled 布尔值、duration、标准化 usage、result category、safe error code。
  - 所有事件继承当前 request correlation ID；不得记录 messages、prompt、response content、tool arguments、secret 或 traceback。
  - cancellation、budget exhaustion、provider fallback 和 terminal outcome 保持既有行为。
- **Edge cases**：provider 在 yield 前失败、stream 中途失败、usage 缺失/部分字段、fallback、最终汇总回合 tools disabled、账本 append 冲突。
- **Dependencies**：无。
- **Acceptance criteria**：
  - 两回合 tool→final 流程产生两个完整 turn lifecycle，ordinal 稳定且 correlation 相同。
  - provider failure 产生 failed 而非 completed；safe payload 不含输入、输出或 secret sentinel。
  - 新观测写入不改变 provider call count、预算计数、terminal event 和现有测试结果。

## Task 2 — Application-owned trace projection

- **Goal**：从 ledger 事件确定性构造请求级、人类可读、可重建的 Activity Timeline。
- **Files**：新增 `application/trace_projection.py`、`application/__init__.py`、新增 `tests/test_trace_projection.py`。
- **Required behavior**：
  - 定义 framework-neutral query、request group、step、technical details 和 page contracts。
  - 只通过现有 store/read interfaces 读取事件，不直接依赖 HTTP 或 DOM，不写 ledger。
  - 以非空 correlation ID 精确分组；请求 ordinal 按首次 sequence 稳定计算；组内按 sequence 升序。
  - 从首条用户消息生成有界标题摘要；缺少用户消息时使用安全事件类别标题。
  - 把 provider turn、顶层 tool、Stata executor 子事件、artifact read、context、budget/cancel/failure/terminal 转成闭合集合的语义 step kind。
  - 相邻等价 context snapshots 合并并记录 repeat count；Stata 内部调用作为父运行的可展开 children。
  - 随机 ID 不进入 collapsed label，只放 technical details；legacy null correlation 单独返回。
  - 所有字符串、children 数、payload 深度和每页 group/event 数有硬上限，并给出 truncation metadata。
- **Edge cases**：空账本、乱序输入、重复 sequence 防御、unknown event、null correlation、缺失 tool parent、失败后恢复、多 terminal 竞争、超长文本/深层 payload。
- **Dependencies**：Task 1 的 provider event vocabulary；对旧数据仍须可用。
- **Acceptance criteria**：
  - 同一固定事件集重复投影结果完全相同，不依赖 wall clock 或随机数。
  - collapsed 输出不含 UUID-looking identifiers；展开详情保留完整原始 ID 和 sequence。
  - unknown/legacy event 安全降级，不猜测关联、不使整个请求投影失败。
  - 单元测试覆盖所有 step kinds、截断、分页、filter 和 payload 脱敏边界。

## Task 3 — Bounded activity API and trace compatibility

- **Goal**：以稳定 HTTP contract 暴露请求级时间线，同时保持原始审计 API 兼容。
- **Files**：`ui.py`、`application/trace_projection.py`、`tests/test_ui_trace_recovery.py`、`tests/test_application_layer.py`。
- **Required behavior**：
  - 新增 `GET /api/trace/activity`，支持有界 limit/cursor、workspace scope、status 和 event type 服务端筛选。
  - 响应返回 request groups、legacy group、total/cursor/truncation metadata；不返回超出 allow-list 的 payload。
  - `GET /api/trace` 保留现有字段和默认行为，只允许兼容性增加 correlation ID/技术字段，不删除或改义。
  - invalid cursor/filter/limit 使用稳定 4xx code；storage busy/corrupt 使用现有安全错误 envelope。
  - Trace 响应设置 `Cache-Control: no-store`，遵循 localhost/workspace 安全边界。
- **Edge cases**：limit 为零/负数/超限、未知 type、翻页中新增事件、当前 workspace 切换、空 correlation、损坏 payload、storage lock。
- **Dependencies**：Task 2。
- **Acceptance criteria**：
  - activity endpoint 的 grouping、pagination、filter 和 error contract 集成测试通过。
  - 旧 `/api/trace` consumer fixtures 保持通过；existing recovery/diagnostic routes 不退化。
  - 服务端返回的 total/filter 语义与实际查询一致，不依赖前端当前 50 行再筛选。

## Task 4 — Human-readable Activity Timeline UI

- **Goal**：默认向用户展示业务流程，原始事件和随机 ID 退居可展开的技术审计层。
- **Files**：`webui/index.html`、`webui/app.js`、`webui/styles.css`、相关 UI/product tests。
- **Required behavior**：
  - Trace 默认页渲染请求卡片：人类请求序号、摘要、开始/结束时间、耗时、状态和步骤计数。
  - 步骤 label 使用“模型回合 1”“Stata 运行 1”“读取运行结果”“上下文准备”“已完成/失败/暂停”等闭合中文词汇。
  - 父步骤可展开内部 Stata 命令、context metrics 和 failure detail；完整 ID 只在“技术详情”内展示，并提供 copy action。
  - 提供明确的“活动时间线 / 技术审计”切换；技术审计继续使用旧 raw endpoint。
  - 筛选、分页和 total 使用服务端 activity contract；刷新后保留安全的 view/filter state，但不把 payload/ID 写入 browser storage。
  - unknown/legacy 事件以中性文案展示；空态、loading、storage busy、API failure 均有可恢复提示。
  - 保持键盘操作、focus、ARIA、窄屏布局和原生 DOM 安全实现。
- **Edge cases**：只有一个事件、正在运行无 terminal、失败后续跑、同秒多请求、长中文摘要、长模型名、legacy group、copy API 不可用、窄屏。
- **Dependencies**：Task 3。
- **Acceptance criteria**：
  - collapsed timeline DOM 中没有 UUID/call ID/run ID；展开后能复制完整技术标识。
  - 用户能在一个视图判断 provider 回合数、顶层工具数、Stata 运行数、终态和失败点。
  - Activity/Technical 切换、filter、pagination、refresh、keyboard 和 responsive contract tests 通过。
  - `app.js` 不使用 `innerHTML`，不新增前端框架或不安全 HTML sink。

## Task 5 — Truthful summaries and evidence readiness

- **Goal**：保证 Trace 文案来自机器事实，并准确表达从执行到证据签名的不同成熟度。
- **Files**：`application/trace_projection.py`、必要时 `ui.py` 的旧 public summary helper、projection/UI/e2e tests。
- **Required behavior**：
  - 建立安全字段优先级，覆盖 `reply`、`status`、`decision_summary`、`summary`、`text`、safe error code 和闭合事件模板。
  - Stata 命令只投影受控 command family，例如“加载示例数据”“查看变量结构”“描述统计”“回归估计”；未知命令显示“执行 Stata 命令”，默认不回显完整 code。
  - 根据实际 ledger 事件分别投影 `execution_succeeded`、`structured_result_available`、`evidence_card_signed`、`claim_signed`。
  - machine result 为空时不得显示结构化结果完成；仅 assistant 自然语言声称“已签入”不得提升 evidence state。
  - failure、cancel、budget、pause、resume 采用稳定原因码和可操作的安全中文摘要。
- **Edge cases**：`machine={}`、只有 raw artifact、card 签名失败、claim 被拒绝、回复与账本矛盾、未知 Stata code、敏感错误文本、超长 reply。
- **Dependencies**：Task 2；可与 Task 4 后半并行，但验收以最终 UI 为准。
- **Acceptance criteria**：
  - 空 machine result 的成功 run 只显示“执行成功”，不显示结构化结果或证据已签入。
  - card/claim 只有对应成功 ledger 事件才显示完成；失败/拒绝状态准确。
  - summary fixtures 无空白主步骤、无 raw code/路径/secret 泄漏，且与 end-to-end ledger 一致。

## Task 6 — Product regression, eval and documentation closure

- **Goal**：证明新 Trace 可读、可审计、兼容且不影响 agent 执行。
- **Files**：上述测试文件、`design/TRACE_ACTIVITY_TIMELINE_V1.md`、必要的产品 eval fixtures、`IMPLEMENTATION_REPORT.md`。
- **Required behavior**：
  - 增加端到端 scenario：纯聊天、tool→final、真实形态 Stata 多子调用、provider failure、cancel、budget limit、pause/resume、legacy data。
  - 每个 scenario 同时断言 ledger raw truth、activity projection 和 UI contract，避免只测文案。
  - 增加 privacy regression，使用 sentinel 证明 prompt/response/tool args/secret 不进入 trace public output。
  - 验证旧 raw trace、diagnostic bundle、recovery、request control 和 release gates 不退化。
  - 最终更新 `IMPLEMENTATION_REPORT.md`，只记录真实执行结果和未完成项。
- **Edge cases**：非 Windows CI、无真实 provider/Stata、dirty worktree、wheel 安装后的静态资源、现有历史 DB。
- **Dependencies**：Tasks 1–5。
- **Acceptance criteria**：
  - targeted、full、lint、type、eval、coverage、wheel/release smoke 全部通过或对既有环境性失败给出明确证据。
  - 无新增无理由 skip/xfail，无删除测试或断言弱化。
  - 实现与设计偏差全部写入 report；完成本轮后不继续其他模块。

# Tests

## Unit tests

- provider lifecycle 的 started/completed/failed、ordinal、duration、usage normalization 和安全 payload。
- correlation 精确分组、人类序号、组内排序、unknown/legacy 降级。
- provider/tool/Stata/context/artifact/terminal/budget/cancel/recovery 的 step mapping。
- context snapshot 去重、Stata parent-child 折叠、截断和有界 payload。
- safe summary precedence、Stata command family、敏感 sentinel 脱敏。
- execution/structured/card/claim 四级状态真值表。

## Integration tests

- 两个 provider 回合的 tool→final 请求显示正确回合数和因果顺序。
- Stata 顶层调用与内部多命令被归入同一运行步骤，展开后 sequence 完整。
- activity endpoint 的 cursor、limit、status/type filter、total 和 storage error contract。
- provider failure、用户停止、max steps、暂停/续跑的 raw ledger 与 activity 终态一致。
- 老数据库 null correlation 可加载但不被错误归组。

## UI/browserless tests

- 默认进入 Activity Timeline，可切换 Technical Audit。
- collapsed DOM 不显示随机 ID；technical details 展开与 copy 正常。
- request card 状态、步骤计数、filter、pagination、refresh、empty/error/recovery。
- keyboard、focus、ARIA、窄屏和 long-content contract。
- 禁止 `innerHTML`，禁止把 event payload/ID 写入 browser storage。

## Regression tests

- agent loop call count、budget、tool permissions、provider fallback、cancel 和 terminal-first-wins 不变。
- raw `/api/trace`、diagnostic bundle、request recovery 和 settings 页面保持兼容。
- context/memory、Stata executor/verifier、evidence/Writer 与 release gates 不退化。
- 禁止通过删除测试、无理由 skip/xfail 或放宽已有断言解决失败。

# Verification Commands

在 `app/` 目录按任务顺序运行：

```powershell
python -m pytest -q tests/test_agent_loop.py tests/test_trace_projection.py
python -m pytest -q tests/test_ui_trace_recovery.py tests/test_application_layer.py tests/test_e2e_full_pipeline.py
python -m pytest -q tests/test_ui_interaction_contract.py tests/test_product_ux.py tests/test_product_ux_coverage.py
node --check src/stata_agent/webui/app.js
python -m pytest -q tests/test_product_eval.py tests/test_release_entrypoints.py
python -m pytest -q
python -m ruff check src tests
python -m mypy src
python -m stata_agent.eval --json
python -m coverage run --branch -m pytest -q
python -m coverage report --fail-under=75
python -m build --wheel
python -m stata_agent.release_doctor --offline --json --wheel .\dist\stata_agent-0.1.0-py3-none-any.whl
```

在仓库根目录运行：

```powershell
git diff --check
```

完成代码后重建 codespaces full belief map，并运行 `boundaries all` 与 `invariants all`，确认 Trace projector
没有成为写模型或第二编排器。

自动门禁通过后，在真实 Windows UI 做一次短验收：发出一个包含 Stata 工具往返的请求，确认 Activity Timeline
显示请求、模型回合、Stata 运行、结果读取与终态；展开技术详情核对完整 ID，再切换 Technical Audit 核对原始事件。

# Stop Conditions

- Tasks 1–6 和本轮验证完成后立即停止，不继续 RAG、econometrics、settings、installer 或其他模块。
- 不修改 agent budget、provider routing/retry、Stata command selection、context/memory 算法或 evidence 写入逻辑。
- 不引入远程遥测、OpenTelemetry、第二数据库、事件迁移或历史 correlation 猜测。
- 不记录或暴露 prompt、原始模型响应、隐藏推理、secret、附件正文或敏感 Stata 输出。
- 不重命名或删除 durable technical IDs；只改变它们在产品 UI 的默认可见级别。
- 若无法在现有 application read-model 边界内实现，或必须改变 SQLite/schema/编排契约，应停止并报告
  `Architecture Change Required`，不得把架构重构混入本轮。

# Deliverables

1. Tasks 1–6 的代码修改。
2. 新增及更新的 unit、integration、UI、privacy 和 regression tests。
3. 更新后的 Trace 设计/产品文档。
4. `IMPLEMENTATION_REPORT.md`，必须包含：
   - Completed
   - Files Changed
   - Design Decisions
   - Tests Run + Results
   - Deviations from Plan
   - Remaining Issues
   - Recommended Next Step
