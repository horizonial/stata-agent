# IMPLEMENTATION REPORT — Trace Activity Timeline V1

## Completed

- 完成 `IMPLEMENTATION_PLAN.md` 的 Tasks 1–6，未改变 `ARCHITECTURE.md` 的核心架构约束。
- 新增 `provider.turn.started/completed/failed` 生命周期事件；事件只保存 provider/model 安全标识、回合/尝试序号、工具数量、耗时、usage、结果类别和安全错误码。
- 新增 application-owned、只读、可重建的 `TraceProjectionService`，按非空 `correlation_id` 精确分组请求，生成有界中文标题、步骤、状态、上下文峰值、Stata 子步骤和证据成熟度。
- 新增 `/api/trace/activity`，支持 workspace、请求组分页、cursor、category、status、search 和稳定 `trace_query_invalid` 错误；既有 `/api/trace` 保持技术审计兼容。
- 活动 API 使用 SQLite 的有界事件窗口和相关性聚合计数；跨游标加载不会把完整账本物化到应用内存。
- Trace 页面默认显示“活动时间线”，支持“技术审计”切换；请求卡片展示模型回合、工具、Stata、上下文、终态和证据状态，技术标识默认折叠且可复制。
- Trace 的 view/filter 偏好仅保存安全枚举值，刷新后恢复；raw/activity 两个 Trace 响应均使用 `Cache-Control: no-store`。
- 完成执行事实与证据事实分级：`仅执行`、`结构化结果`、`已验证`；没有对应签名事件时，模型自然语言不会把结果提升为已签证据。
- 证据签名事件从 agent request 透传 correlation metadata，使真实 `run_loop → Stata → EvidenceCard/Claim` 往返能归入同一请求卡；历史无 correlation 事件仍独立归入 legacy 区域。
- 设计文档 `design/TRACE_ACTIVITY_TIMELINE_V1.md` 已更新为当前实现状态。

## Files Changed

本轮 Trace 相关文件：

- `app/src/stata_agent/events/schema.py`
- `app/src/stata_agent/harness/agent_loop.py`
- `app/src/stata_agent/application/trace_projection.py`
- `app/src/stata_agent/application/__init__.py`
- `app/src/stata_agent/ui.py`
- `app/src/stata_agent/storage/sqlite_store.py`
- `app/src/stata_agent/webui/app.js`
- `app/src/stata_agent/webui/styles.css`
- `app/src/stata_agent/tools/evidence_signer.py`
- `app/src/stata_agent/toolkit.py`
- `app/tests/test_agent_loop.py`
- `app/tests/test_trace_projection.py`（新增）
- `app/tests/test_ui_trace_recovery.py`
- `app/tests/test_ui_v4.py`
- `design/TRACE_ACTIVITY_TIMELINE_V1.md`
- `IMPLEMENTATION_REPORT.md`

工作区中其他前轮未提交修改均保留，未执行 reset、删除测试或覆盖用户改动。

## Design Decisions

- SQLite append-only ledger 仍是唯一运行事实；Activity Timeline 是 application 层只读投影，不写账本、不参与编排、不建立第二状态机。
- correlation ID 是唯一正常请求分组键；缺失 correlation 的旧事件不按时间、UUID 或邻近用户消息猜测归属。
- provider retry 保留每个物理生命周期事件，但投影按 `turn_index` 合并为一个可读的逻辑模型回合；尝试次数只作为安全摘要/技术信息。
- 前端继续使用原生 DOM API 和 `textContent`，没有引入框架或 `innerHTML`；随机标识只在显式技术详情中呈现。
- 活动投影对缺失或乱序的工具调用不按位置猜配；零值上下文指标和嵌套 card/claim 标识按账本原值保留。
- 证据成熟度只由 ledger 中实际 `evidence.card_signed`/`claim.signed` 事件决定；自然语言回复和单独 `run.succeeded` 不具有签名权威。
- `sign_run_numeric_cards` 增加可选 `correlation_id` 仅用于审计关联，默认值保持兼容；不改变签名校验、幂等或证据内容。

## Tests Run + Results

- `python -m pytest -q tests/test_agent_loop.py tests/test_trace_projection.py tests/test_ui_v4.py tests/test_ui_trace_recovery.py tests/test_application_layer.py tests/test_ui_interaction_contract.py tests/test_product_ux.py tests/test_product_ux_coverage.py`：通过。
- `python -m pytest -q tests/test_ui_trace_recovery.py tests/test_application_layer.py tests/test_e2e_full_pipeline.py`：通过。
- `python -m pytest -q tests/test_product_eval.py tests/test_release_entrypoints.py`：通过（仅既有 CLI `RuntimeWarning`）。
- `node --check src/stata_agent/webui/app.js`：通过。
- `python -m pytest`：689 passed，6 skipped，1 个既有 warning；无失败。
- `python -m ruff check src tests`：通过。
- `python -m mypy src`：通过，109 source files；仅既有 untyped-body note。
- `python -m stata_agent.eval --json`：L1–L7，7/7 passed。
- `python -m coverage run --branch -m pytest -q` + `python -m coverage report --fail-under=75`：通过，TOTAL 77%（20,079 statements）。
- `python -m build --wheel`：通过，wheel 包含 `trace_projection`、webui 静态资源和新增事件常量。
- `python -m stata_agent.release_doctor --offline --json --wheel .\\dist\\stata_agent-0.1.0-py3-none-any.whl`：`automated_ok=true`；发布型人工 gates 按既定范围保持 pending。
- `git diff --check`：通过，仅有 Git 的 LF→CRLF 提示。
- codespaces belief map 已以 `--full` 重建；`boundaries all` 与 `invariants all` 均通过。

## Deviations from Plan

- 无架构变更；没有修改 SQLite schema、agent budget、provider routing/retry、context/memory 算法或 Stata 执行策略。
- 为满足 activity 读模型的有界读取，给现有 `SQLiteStore.scan_recent_events` 增加了可选 `before_seq`，并增加只读 `count_event_correlations` SQL 聚合；没有新增表、迁移或第二真相源。
- 为使真实签名证据能够回到所属请求，新增了可选 correlation metadata 透传到 EvidenceCard/Claim 事件。这是现有事件 envelope 的兼容性扩展，不改变证据写入条件或内容。
- `webui/index.html` 无需修改：Trace 页面由现有 `view-root` 原生 DOM 渲染，静态资源 contract 已覆盖。
- 自动化环境未执行真实浏览器中的人工短验收；没有把这项结果伪造为通过。
- 由于 activity 的 status/search 语义依赖 projector，带这两类筛选时 `total_groups` 仍以当前有界窗口为准；无筛选或事件词汇筛选使用 SQLite 聚合总数，并通过 `truncated` 明示历史窗口边界。

## Remaining Issues

- 仍需在真实 Windows UI 发起一次包含 Stata 工具往返的请求，确认 Activity Timeline 的模型回合、Stata 运行、结果读取、证据状态和 Technical Audit 切换；并核对窄屏与剪贴板降级体验。
- 历史数据库中的 null-correlation 事件会继续显示为“历史未分组事件”，这是设计约束，不回填或猜测关联。
- 离线门禁未覆盖每家真实 provider 的线上配额/协议差异，也未在本轮执行真实 Stata/网络 canary。
- Windows 签名、干净安装、升级/回滚等发布型人工 gate 仍由 release doctor 标记 pending，不属于本轮 Trace 目标。

## Recommended Next Step

重启当前应用进程，完成一次真实 Stata 往返的人工 Trace 验收；确认通过后再进入全局计划中的下一个模块（优先 Agent 编排与应用边界），不要在本轮继续扩展 RAG、econometrics、installer 或发布工作。
