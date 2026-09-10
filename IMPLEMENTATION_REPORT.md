# Completed

- 完成 Task 1：新增版本化、机器可判定的 Stata acceptance 报告契约（`schema_version=1`）、稳定检查顺序、失败码、脱敏和退出码。
- 完成 Task 2：`which_ados()` 改为严格失败关闭；不完整、错误、乱序、不可解析或 transport 失败均不会返回部分成功，且始终尽力关闭 session。
- 完成 Task 3：新增隔离的 MCP/Stata engine、持久会话、内置 auto 回归、SQLite ledger/provenance 验收链；依赖失败会短路为 `not_run`，不自动 fallback 到 FakeExecutor。
- 完成 Task 4：提供 `python -m stata_agent.stata_doctor` 与 `stata-agent-stata-check` 两个等价入口，并保留默认 pytest/eval 的离线行为。
- 完成 Task 5：README 已补充 live 前置条件、命令、JSON/脱敏契约和退出码。
- 当前 Windows Stata 18 perpetual 授权已由 `stata-mcp` 实际识别；20 次持久会话验收全部通过。

# Files Changed

- `app/src/stata_agent/stata_doctor.py`（新增）
- `app/src/stata_agent/tools/ado.py`
- `app/src/stata_agent/tools/stata_client.py`
- `app/pyproject.toml`
- `app/README.md`
- `app/tests/test_stata_doctor.py`（新增）
- `app/tests/test_strategy_ado.py`
- `app/tests/test_release_entrypoints.py`
- `IMPLEMENTATION_REPORT.md`（本文件）

未修改事件 schema、SQLite migration、Agent Loop、ChatService、UI、provider 或 FakeExecutor。

# Design Decisions

- doctor 是 Runtime 外层 adapter，只组合既有 `StataClient`、`StataSession`、`StataExecutor` 和 `SQLiteStore`，不建立第二套编排器。
- 验收顺序固定为 MCP tools → engine → persistent session → executor → ledger；前置失败后不再启动后续外部调用。
- live ledger/run 写入 `TemporaryDirectory`，通过真实 `run.requested → tool.call/tool.result → run.succeeded` 事件链、唯一 operation、machine 结果和 attested real provenance 共同判定通过。
- `stata-mcp` 启动 banner（可能包含许可证信息）被隔离到 `os.devnull`；报告只输出有限指标和脱敏 detail，不保留原始 stderr、序列号、凭据或本机路径。
- 客户端增加可选 `errlog` 注入且默认调用方式保持兼容；doctor 不修改现有执行事件语义。
- ado 预检使用直接 `which` 加独立 marker：实际 `stata-mcp` 每次调用会单独捕获 `_rc`，`cap which` 后再 `di _rc` 会把缺失 ado 的错误码重置为 0。结果数量、每项响应身份、marker 和 transport 均严格校验。

# Tests Run + Results

- Targeted offline：指定 doctor、entrypoint、ado、executor、reuse、runtime-control 测试，`61 passed, 3 skipped`（仅 live-gated skip）。
- Explicit live：`STATA_LIVE=1` 下 `tests/test_stata_executor.py tests/test_strategy_ado.py`，`14 passed`。
- Doctor live：`python -m stata_agent.stata_doctor --json --iterations 20`，5/5 checks passed，`ok=true`，退出码 `0`；JSON 未混入启动 banner。
- Installed console entrypoint：`stata-agent-stata-check --help` 退出码 `0`；console script 与 module target 一致。
- Full pytest：`364 passed, 4 skipped`，保留 1 个既有 legacy `runpy` warning。
- Ruff：`python -m ruff check src tests` 通过。
- Mypy：`python -m mypy src` 通过（92 个 source files；仅既有 annotation notes）。
- Product eval：4/4 golden scenarios passed。
- Branch coverage：总覆盖率 `77%`，高于 75% 门槛。
- Wheel：`python -m build --wheel` 通过；wheel 含 `stata_agent/stata_doctor.py` 和 `stata-agent-stata-check` console metadata。
- Architecture belief map：使用 bundled `build_belief_map.py --full` 重建，`boundaries all` 与 `invariants all` 均通过。

# Deviations from Plan

- 计划描述的旧 `cap which` 方案在现场 `stata-mcp` 语义下无法可靠保留缺失 ado 的 `_rc`；采用直接 `which` + 配对 marker 是保持公共返回形状不变所需的最小兼容修正。
- 为避免 MCP 启动 banner 污染稳定 CLI 输出，未修改 executor 事件契约，而是在 doctor 运行边界隔离 stderr，并在 client 保留可选日志流注入。
- 计划示例中的 `belief_search.py build` 不是当前 bundled 工具支持的子命令；按 skill 说明改用 `build_belief_map.py --full`，随后边界和 invariant gate 均通过。

# Remaining Issues

- 远端 provider、UI `/api/health` 实时探测、许可证管理、outbox/记忆/RAG、论文复现和 Windows 发布签名仍按计划保持未处理。
- 现有 legacy CLI module test 仍报告一个 `runpy` warning；与本轮 acceptance 逻辑无关，未扩大范围处理。

# Recommended Next Step

将本轮 doctor 命令接入发布人员的 Windows release checklist（保留显式 live 前置条件），并在下一轮单独选择架构 backlog 中的一个 P0 继续规划；不要把当前验收探针接入 UI 热路径或改用 FakeExecutor 代替真实环境验证。
