# Objective

建立一个**失败关闭、可机器判定、可由发布人员直接运行的 Windows 真 Stata 运行时验收门禁**：在不改变现有 Agent、事件账本和 Stata 执行架构的前提下，准确区分“仅配置”“MCP 可连接”“Stata 引擎可启动”“持久会话可工作”“`StataExecutor` 端到端结果已验证”，并对许可证失效、启动失败、协议结果不完整等情况给出非零退出码和脱敏诊断。

# Context

`ARCHITECTURE.md` 将真 Stata 长会话和 Windows 真实环境验收列为当前 P0 发布阻断项，同时明确 Stata 必须经 `stata-mcp`、FakeExecutor 只能显式用于演示/测试、事件账本仍是执行事实来源。`design/dd-04-tool-permission.md` 要求运行时能力先做前置检查且失败必须回传明确原因；`design/dd-06-eval.md` 要求真 Stata 测试在独立目录运行，L0 离线门禁保持确定性；`design/audit-stata-practice.md` 要求环境指纹、可复现 do-file 和持久会话纪律；`PRODUCTIZATION_PLAN.md` 已把“已配置、可连接、已验证”三者不得混淆写入发布标准。

当前仓库未找到上一轮 `IMPLEMENTATION_REPORT.md`；本计划以 `main@f79aa13`、最近四个 durable memory outbox 提交、当前 `ARCHITECTURE.md` 和现场验证结果作为交接基线。缺失的报告不得由下一轮猜测补写，也不影响本轮聚焦真 Stata 发布门禁。

2026-09-10 的现场状态：

- 离线全量测试：342 passed、4 skipped、1 warning；真 Stata和远端模型测试默认跳过。
- 本机 Stata 18 已更新为 perpetual（永久）授权，`stata-mcp` 能正常初始化；内置 auto 回归、机器层结果、环境指纹、事件链与 session 相关 live 检查均已通过。
- `STATA_LIVE=1` 下运行 `tests/test_stata_executor.py` 与 `tests/test_strategy_ado.py`：7 passed、1 failed。
- 剩余失败是产品 bug：`which_ados()` 对不存在的 ado 返回空映射，`missing_ados()` 因而返回空列表，把“响应不完整或无法验证”误当成“没有缺失项”。永久许可证已排除引擎启动失败这一外部干扰。
- 现有 `quick_check()` 只列工具并执行一条命令，没有稳定结果 schema、失败分类、退出码、长会话检查或事件链验收；现有 live pytest 也不是可交付给发布人员的独立验收入口。

# Current Behavior

1. `StataSession` 在专用线程中维护一个 `stata-mcp` stdio 会话；`StataExecutor(share_session=True)` 可跨执行复用该会话，关闭和取消语义已有覆盖。
2. `StataExecutor.execute()` 将脚本逐行提交，先写 `run.requested`，随后为每次 MCP 调用写 `tool.call/tool.result`，最终写 `run.succeeded`、`run.failed` 或 `run.uncertain`；成功结果包含 machine、`env_sig`、do-file 和 command hash。
3. 默认离线测试只证明 mock/fixture 契约。`tests/test_stata_executor.py` 和 `tests/test_strategy_ado.py` 只有显式设置 `STATA_LIVE=1` 才运行。
4. `quick_check()` 返回临时 dict，不能作为稳定发布报告；异常直接外抛，调用方无法可靠区分 MCP 路径错误、工具缺失、Stata 启动失败、许可证问题、超时和协议响应不完整。
5. `which_ados()` 使用 `zip(ados, rc_vals)`；当返回标记少于输入项时静默截断。启动失败时可能返回 `{}`，继而使 `missing_ados()` 错误地报告“无缺失”。
6. `/api/health` 展示的是工作区/运行状态和配置文本，不会证明真 Stata 当前可用。本轮不将昂贵的 live probe 塞入 HTTP 健康端点。

# Target Behavior

1. 仓库提供一个安装后可调用的真 Stata 验收入口（模块入口与 console script），支持人类可读输出和稳定 JSON 输出。
2. 验收结果使用版本化、确定性的 schema，至少报告以下阶段：MCP 配置/进程可启动、所需工具存在、Stata 引擎可执行、同一 `StataSession` 的多次调用保持状态、内置 auto 回归结果正确、执行事件链闭合且 provenance 完整。
3. 每个阶段明确为 `passed`、`failed` 或 `not_run`；依赖阶段失败后，后续阶段必须是带原因的 `not_run`，不得误报通过。
4. 预期的环境不可用是正常、可诊断的失败：命令返回 1，并输出稳定失败码和脱敏提示；参数/报告构造等命令自身错误返回 2；全部检查通过才返回 0。
5. JSON 不包含 API key、Stata 序列号、完整原始 stderr、用户目录、临时绝对路径或研究数据内容。人类输出可以给出修复建议，但同样必须脱敏。
6. ado 检查严格失败关闭：只要 transport 报错、结果 `is_error`、响应缺失、标记数量不等于请求数量或标记不可解析，就抛出明确的运行时/协议错误；只有完整验证后才返回安装状态。
7. 当前永久授权的 Windows 真 Stata 环境中，新命令必须在所有检查通过后返回 0；通过 mock 注入的许可证、启动或 transport 故障仍必须稳定返回 1 并给出脱敏阻断原因。
8. 默认 pytest、产品 eval 和 CI 继续完全离线，不因本轮引入 Stata、许可证或网络依赖。

# Invariants / Hard Constraints

- 不改变 `ARCHITECTURE.md` 确定的 `Agent Loop → Tool → Runtime` 分层；验收入口是外层 adapter，不成为第二套编排器。
- 真 Stata 仍只经 `stata-mcp`；不得新增 PyStata 直连、shell 执行 Stata 或绕开 `StataSession/StataExecutor` 的生产路径。
- `StataExecutor` 现有执行事件顺序、terminal closure、幂等复用、取消/超时后 uncertain 语义和 provenance 字段保持兼容。
- SQLite append-only 事件账本仍是 run 成功与否的唯一事实来源；验收不得仅凭控制台文本声称成功。
- FakeExecutor 不得参与 live 验收，也不得在真 Stata失败时自动 fallback。
- 默认配置不启动 Stata、不联网；导入模块、`--help`、离线单测和产品 eval 均不得触发 MCP/Stata 子进程。
- live 验收必须使用临时且隔离的 ledger/run 目录，不读取或修改用户现有工作区、研究数据或默认产品数据库。
- 不删除测试、不无理由新增 skip、不放宽已有数值/事件断言、不扩大 Ruff/Mypy/coverage 豁免。
- 不把 Stata 许可证续期作为代码任务；代码只负责准确检测、报告和拒绝误判。
- 不在日志、JSON、异常摘要或测试 golden 中持久化凭据、许可证序列号、完整本机路径或不受控原始 stderr。

# Scope

## In Scope

- 定义稳定的真 Stata readiness/acceptance 结果契约、失败分类和退出码。
- 复用 `StataClient/StataSession/StataExecutor` 实现分阶段 preflight、持久会话检查、内置 auto 回归和 ledger/provenance 验证。
- 修复 ado 预检在 transport/协议不完整时的失败开放问题。
- 新增可安装 console script 和 `python -m` 入口，以及相应操作文档。
- 为所有逻辑增加完全离线的单元/入口测试；保留并收紧显式 live 验收。
- 在当前已永久授权的 Windows 环境实际运行门禁，并保存真实通过结果；若出现新的外部环境故障，必须如实记录而不伪造通过。

## Out of Scope

- 远端 DeepSeek/Qwen、隐私出境验证或真 provider 长会话。
- Stata 许可证购买、续期、网络许可证服务器配置或 `stata-mcp` 仓库修改。
- econometrics skill、`verify_result`、prepared dataset cache、作者 do-file sandbox 或 L3 论文复现 benchmark。
- UI `/api/health` 实时探测、前端健康页重做、Windows 安装器/签名/发布渠道。
- outbox heartbeat/redrive、附件、记忆、RAG、可观测性平台和 UX 改造。
- 修改事件 schema、SQLite migration、Agent Loop、ChatService、隐私策略或 provider registry。
- 为了本轮顺便重构 `StataExecutor`、`ui.py` 或现有错误体系。

# Architecture Change Required

无。现有 Runtime、`StataSession`、`StataExecutor`、事件账本和 entrypoint 机制足以实现本轮目标。新增的验收模块只负责组合现有能力并形成发布报告，不改变核心依赖方向。若实现中发现必须修改事件 schema、绕过 MCP 或改变 executor terminal 语义，应停止本轮并在 `IMPLEMENTATION_REPORT.md` 中提出独立的 architecture change，而不是混入实现。

# Files

## Primary files

- `app/src/stata_agent/stata_doctor.py`（新增）：live Stata 验收编排、版本化结果 schema、脱敏失败分类、人类/JSON 输出和 CLI 退出码。
- `app/src/stata_agent/tools/stata_client.py`：复用或补强最小连接/probe 原语与错误规范化；保持 `StataClient`、`StataSession` 公共行为兼容。
- `app/src/stata_agent/tools/ado.py`：修复响应不完整和 transport 失败时的失败开放行为。
- `app/pyproject.toml`：注册 `stata-agent-stata-check` console script，并确保 wheel 安装后入口可用。
- `app/README.md`：记录命令、退出码、JSON/脱敏契约、live 前置条件和运行时不可用时的预期行为。

## Likely test files

- `app/tests/test_stata_doctor.py`（新增）：验收编排、失败分类、短路、脱敏、输出和退出码的离线测试。
- `app/tests/test_release_entrypoints.py`：模块/console 边界、`--help`、JSON 格式和不启动 live runtime 的回归测试。
- `app/tests/test_strategy_ado.py`：完整/缺失/错误/少标记/坏标记等 ado fail-closed 测试。
- `app/tests/test_stata_executor.py`：显式 live 成功路径与 ledger/provenance 断言；不得并入默认离线运行。
- `app/tests/test_executor_reuse.py`：确认验收接入未破坏 executor 幂等复用。
- `app/tests/test_runtime_control.py`、`app/tests/test_runtime_control_coverage.py`：仅当 probe 复用 session 生命周期代码而触及取消/关闭边界时补回归。

## Reference-only files

- `ARCHITECTURE.md`
- `PRODUCTIZATION_PLAN.md`
- `design/dd-01-domain-events.md`
- `design/dd-04-tool-permission.md`
- `design/dd-06-eval.md`
- `design/audit-stata-practice.md`
- `design/PHASE3_APPLICATION_STORAGE.md`
- `app/src/stata_agent/tools/executor.py`
- `app/src/stata_agent/events/schema.py`
- `app/src/stata_agent/storage/sqlite_store.py`
- `app/src/stata_agent/eval/runner.py`

## Files that should not be modified unless necessary

- `ARCHITECTURE.md`、`PRODUCTIZATION_PLAN.md` 与 `design/**`：本轮按既有设计实现，不重写架构文档；只在发现文档与已验收事实直接冲突且修订对交付必需时改动。
- `app/src/stata_agent/tools/executor.py`：现有事件与执行契约应直接复用；只有验收暴露明确 correctness bug 且可在现有契约内修复时才修改。
- `app/src/stata_agent/ui.py`、`app/src/stata_agent/application/**`、`app/src/stata_agent/harness/agent_loop.py`：不把发布探针接入请求热路径或 ChatService。
- `app/src/stata_agent/events/**`、`app/src/stata_agent/storage/**`、`app/src/stata_agent/domain/**`：不得为验收命令新增事件类型或 schema migration。
- `app/src/stata_agent/providers/**`、`app/src/stata_agent/privacy/**`：本轮不处理远端模型。
- `app/eval_golden/**`：不得把机器、时间、路径或 live 结果加入离线 golden。
- `app/src/stata_agent/tools/fake_executor.py`：live 门禁不得依赖或修改 FakeExecutor。

# Implementation Tasks

## Task 1 — 定义版本化验收契约与失败分类

- **Goal**：先建立调用方可依赖的 readiness 模型，再连接真实执行，避免临时 dict 和异常文本成为隐式 API。
- **Files**：`app/src/stata_agent/stata_doctor.py`；必要时仅在 `app/src/stata_agent/tools/stata_client.py` 暴露已有数据。
- **Required behavior**：
  - 定义 `schema_version=1` 的顶层报告，包含整体 `ok`、稳定 summary、按顺序排列的 checks、每项 status、稳定 failure code 和脱敏 detail/hint。
  - 固定检查阶段及依赖关系：MCP/tool discovery → Stata engine command → persistent-session sequence → executor regression → ledger/provenance closure。
  - 统一预期失败到有限 code 集；异常类名或第三方自由文本不得成为唯一机器判定字段。
  - 明确退出码：0=全部通过，1=环境/检查失败，2=命令参数、内部配置或报告生成错误。
  - 序列化内容只包含稳定字段；持续时间可展示但不得参与测试 golden 或决定成功。
- **Edge cases**：
  - `STATA_MCP_DIR` 不存在、Python executable 不存在、MCP server 启动失败。
  - `list_tools` 成功但缺 `stata_run`。
  - Stata 初始化失败、许可证不可用、超时、EOF/断连、返回非 JSON 或 `is_error=True`。
  - detail 含 Windows 用户路径、临时目录、许可证序列号或疑似 secret。
  - 一个阶段失败时后续阶段不得继续启动新的外部调用。
- **Dependencies**：现有 `server_params()`、`StataClient`、`StataSession`、`StataExecutor` 和 `SQLiteStore`；不依赖 UI、provider 或 Agent Loop。
- **Acceptance criteria**：
  - 报告可被 `json.dumps` 稳定序列化，字段与状态集合有离线测试锁定。
  - 任一依赖阶段失败会产生一个 `failed` 和后续明确 `not_run`，整体 `ok=false`。
  - 脱敏测试证明绝对路径、序列号和 secret-like 字符串不出现在 JSON/人类输出。
  - 导入模块和构造 parser 不启动任何 MCP/Stata 进程。

## Task 2 — 让 Stata 与 ado preflight 严格失败关闭

- **Goal**：消除“无法验证被当成可用”的 correctness bug，并为验收入口提供可靠的底层结果。
- **Files**：`app/src/stata_agent/tools/ado.py`、`app/src/stata_agent/tools/stata_client.py`、`app/tests/test_strategy_ado.py`；必要时 `app/tests/test_release_entrypoints.py`。
- **Required behavior**：
  - `which_ados()` 必须检查每个 `CallResult` 的 `is_error`、transport/timeout 异常和协议标记完整性。
  - 返回映射必须与输入 ado 一一对应且保持输入语义；不得使用会静默截断的 partial `zip` 结果。
  - 结果缺失、重复、顺序不可信或不可解析时抛出明确异常，不得返回部分成功映射。
  - `missing_ados()` 仅在 `which_ados()` 完整成功后计算缺失项；底层 unavailable 必须原样成为 unavailable，而不是 `[]`。
  - 若补强 `quick_check()`，保留既有调用兼容；稳定发布契约只放在 doctor 层。
- **Edge cases**：
  - 空 ado 列表仍应纯函数返回空映射且不创建 session。
  - 一项和多项 ado；全部存在、部分缺失、全部缺失。
  - MCP 返回少一个/多一个 `ADOOK_rc`、非整数标记、`is_error=True` 但文本碰巧含 `ADOOK_rc=0`。
  - session 构造、`run_batch` 和 close 分别抛错；主错误不得被 close 错误覆盖，session 必须 best-effort 关闭。
- **Dependencies**：Task 1 的失败分类可复用，但工具模块不得反向依赖 CLI/entrypoint；如需共享异常，放在最低必要层并保持依赖向内。
- **Acceptance criteria**：
  - 当前永久授权环境中，不存在的 ado 必须被完整返回为 missing；任何不完整响应仍必须明确报 protocol/runtime unavailable，不能得到 `missing_ados([])` 式假成功。
  - 完整响应仍保持现有 `{ado: bool}` 和 `list[str]` 公共返回形状。
  - 新增离线测试覆盖 partial/invalid/error 响应，现有成功、失败和 cleanup 测试继续通过。

## Task 3 — 实现隔离的真 Stata 发布验收流程

- **Goal**：用一个命令验证真实 transport、持久会话、数值结果和事件账本，而不是依赖人工拼接 pytest 与 SQL。
- **Files**：`app/src/stata_agent/stata_doctor.py`、`app/src/stata_agent/tools/stata_client.py`；`app/src/stata_agent/tools/executor.py` 仅在发现现有契约 bug 时。
- **Required behavior**：
  - tool-discovery 检查 MCP server 能初始化且至少包含 `stata_run`。
  - engine check 执行无研究数据依赖的只读/内置命令，并确认响应不是 transport error。
  - persistent-session check 在同一个 `StataSession` 中完成可验证的状态写入和多次读取；迭代次数有安全上限，默认值足以暴露会话复用问题但能在一个开发 session 内完成。
  - executor check 在临时 ledger/run 目录调用现有 `auto_regress_script()`，验证 N、系数符号、R²范围、Stata version/flavor、do-file 存在、command hash 一致。
  - ledger check 从 `SQLiteStore.project/scan` 读取事实，验证同一 operation 的 requested、每个 call/result closure 和唯一 succeeded terminal；不得仅检查 executor 返回 dict。
  - 所有 session/store/executor 在成功、失败、超时和 KeyboardInterrupt 路径都关闭；验收不写默认产品 DB。
  - CLI 支持 `--json` 和有限的 `--iterations`；未知参数或越界迭代数返回 2。
- **Edge cases**：
  - 第一次调用成功、后续持久会话调用失败。
  - 数值返回成功但缺 env marker、do-file 或 terminal event。
  - transport 在提交前失败与提交后断连；报告要区分 failed/not_run，不得触发自动重跑写操作。
  - 临时目录包含非 ASCII 或空格。
  - 用户 Ctrl+C；必须清理资源并返回非零，不生成“通过”报告。
- **Dependencies**：Task 1 的报告契约；Task 2 的 fail-closed 底层语义；现有 executor 和事件 reducer。
- **Acceptance criteria**：
  - 全部 fake/mock 的离线测试能确定性验证每个阶段和短路顺序。
  - 有效 Windows Stata + stata-mcp 环境运行全部 checks 为 `passed` 且退出 0。
  - 当前永久授权环境中全部真实检查通过并退出 0；故障注入环境退出 1，将失败阶段标为 `failed`、后续阶段标为 `not_run`，且不泄漏许可证序列号。
  - 任一报告为通过时，ledger 中确有闭合且 provenance 完整的真实 succeeded run。

## Task 4 — 提供安装后入口并建立分层测试

- **Goal**：让开发源码、wheel 安装环境和发布人员使用同一个入口，同时保持默认 CI 离线。
- **Files**：`app/pyproject.toml`、`app/tests/test_stata_doctor.py`、`app/tests/test_release_entrypoints.py`、`app/tests/test_stata_executor.py`、`app/tests/test_strategy_ado.py`。
- **Required behavior**：
  - 注册 `stata-agent-stata-check = stata_agent.stata_doctor:main`；`python -m stata_agent.stata_doctor` 行为一致。
  - entrypoint 测试覆盖 help、JSON、human output、退出码和异常边界；测试通过依赖注入/monkeypatch，不启动真实 Stata。
  - 保留显式 live 标记；默认 `python -m pytest` 仍不需要 `mcp` 运行服务、Stata 或许可证。
  - live 测试通过统一 preflight 给出清楚失败，不因 marker 缺失产生第二个误导性断言。
  - 不删除现有 `test_real_regression_machine_env_and_events` 和 `test_missing_ados_live` 的核心语义；可重组公共 helper，但断言强度不得下降。
- **Edge cases**：
  - 安装时没有 `[stata]` extra：`--help` 可用，实际检查明确报告依赖缺失并退出 1。
  - 从非仓库当前目录运行 console script。
  - console script 与 module entrypoint 输出/退出码漂移。
- **Dependencies**：Tasks 1–3。
- **Acceptance criteria**：
  - targeted 离线测试通过，且测试能证明命令在 import/help 时没有外部副作用。
  - wheel build 包含 doctor 模块并生成 console script metadata。
  - 默认全量测试仍为离线绿色；live 失败不会被改成 skip 或 xfail 来掩盖。

## Task 5 — 固化发布操作说明与本轮报告

- **Goal**：让下一位发布人员无需阅读源码即可运行门禁、解释失败并保存证据。
- **Files**：`app/README.md`、根目录 `IMPLEMENTATION_REPORT.md`（交付时新增或替换为本轮报告）。
- **Required behavior**：
  - README 写明 prerequisites、源码/module/console 三种等价调用、`--json`、退出码、脱敏保证和有效/无效环境示例。
  - 明确 Stata 许可证、stata-mcp 环境是外部前置条件；门禁失败不等于应启用 FakeExecutor。
  - `IMPLEMENTATION_REPORT.md` 记录当前 perpetual 授权已被 `stata-mcp` 识别、live 命令真实退出码及各阶段结果，不得声称未执行或失败的 live gate 已通过。
- **Edge cases**：
  - 若永久授权环境仍因其他外部条件不可用，报告同时区分“代码/离线门禁完成”和“发布环境验收未通过”。
  - 文档示例不得包含真实 key、用户名、许可证序列号或机器绝对路径。
- **Dependencies**：Tasks 1–4 及最终验证结果。
- **Acceptance criteria**：
  - 文档命令可直接复制运行，且与实际 parser/entrypoint 一致。
  - 报告完整包含 Deliverables 中要求的七个小节。
  - 不修改架构 backlog 状态为“完成”，除非真 Stata live command 在当前永久授权环境实际退出 0。

# Tests

## Unit tests to add

- 版本化报告的 serialization、check 顺序、status 枚举、整体 summary 与退出码。
- 失败分类覆盖：missing executable/module、tool missing、startup/license failure、timeout/EOF、protocol invalid、numeric mismatch、ledger closure failure。
- 脱敏覆盖：Windows/Unix 绝对路径、临时路径、Stata serial/license 行、API-key-like 内容和多行第三方错误。
- 依赖短路：前置 check 失败后，后续 runner 未被调用且状态为 `not_run`。
- persistent-session 成功、第二次调用失败、close-on-all-paths 和迭代边界。
- ado 完整成功、部分缺失、空输入、`is_error`、少/多/坏 marker、transport exception 和 cleanup。
- CLI `--help`、human/JSON 输出、0/1/2 exit code、module entrypoint 与 console target 一致。

## Integration tests to add or retain

- 使用 fake session + 临时 SQLiteStore 跑完整 doctor pipeline，断言 executor 返回值与 ledger 事件链同时被验证。
- 使用故意缺 terminal、重复 terminal、缺 result 或缺 provenance 的 ledger fixture，证明“返回值看似成功”仍不能通过。
- 显式 live：真 `StataSession` 多次调用保持状态。
- 显式 live：`auto_regress_script()` 的 N=74、系数为负、R²在既有范围，且真实 run 的 do-file/hash/env/terminal closure 完整。
- 显式 live：不存在的 ado 被判为 missing；若引擎不可用则整个 preflight 明确失败，不能返回空缺失列表。

## Success paths that must be covered

- 完整可用的 MCP + Stata：所有阶段通过、JSON `ok=true`、exit 0。
- ado 列表完整响应：每个输入恰有一个布尔结果。
- wheel/module entrypoint 在非仓库工作目录可加载并显示 help。
- 临时验收 workspace 成功关闭，原产品 DB 和工作区没有新增事件或文件。

## Failure paths that must be covered

- 通过离线故障注入覆盖 Stata engine/license 初始化失败；当前永久授权环境不应再出现该失败。
- MCP Python/server 路径不存在，或 server 未暴露 `stata_run`。
- Stata 调用 `is_error=True`、非零 rc、超时、EOF/断连、响应非 JSON/标记不完整。
- 持久会话中途丢失状态或断开。
- executor 数值正确但 provenance/事件链不完整。
- ado probe 返回零个、部分或非法 marker。
- Ctrl+C/取消和资源 close 失败；不得输出成功或掩盖主错误。

## Regression tests

- `StataExecutor` 幂等复用不新增事件。
- run requested/call/result/terminal closure、failed/uncertain 分类和 cancellation 语义保持不变。
- FakeExecutor 仍只能显式启用，doctor 永不 fallback 到 fake。
- 默认 `python -m pytest` 不启动外部 Stata；现有 342 个通过项不得减少。
- L1–L4 离线 product eval golden 不变。
- Ruff、Mypy、75% branch coverage 和 wheel build 不得退化；不得通过新增 ignore/omit/skip 修复门禁。

# Verification Commands

以下命令均从 `D:\work\stata agent\app` 执行。

1. Targeted offline tests：

~~~powershell
python -m pytest tests/test_stata_doctor.py tests/test_release_entrypoints.py tests/test_strategy_ado.py tests/test_stata_executor.py tests/test_executor_reuse.py tests/test_runtime_control.py tests/test_runtime_control_coverage.py -q
~~~

2. Doctor entrypoint contract：

~~~powershell
python -m stata_agent.stata_doctor --help
stata-agent-stata-check --help
~~~

3. Current-machine live gate：

~~~powershell
python -m stata_agent.stata_doctor --json --iterations 20
$LASTEXITCODE
~~~

当前机器已是 perpetual 授权，预期 `$LASTEXITCODE -eq 0` 且所有 checks 为 `passed`。若返回 1，必须根据脱敏 failure code 修复实际环境或代码问题；不得把失败改成 skip、mock 或 FakeExecutor 通过。

4. Existing explicit live regressions（当前永久授权环境必须通过）：

~~~powershell
$env:STATA_LIVE = "1"
python -m pytest tests/test_stata_executor.py tests/test_strategy_ado.py -q
Remove-Item Env:STATA_LIVE
~~~

5. Full offline pytest：

~~~powershell
python -m pytest
~~~

6. Ruff：

~~~powershell
python -m ruff check src tests
~~~

7. Mypy：

~~~powershell
python -m mypy src
~~~

8. Product eval：

~~~powershell
python -m stata_agent.eval --json
~~~

9. Branch coverage：

~~~powershell
python -m coverage erase
python -m coverage run --branch -m pytest
python -m coverage report
~~~

10. Wheel build and entrypoint metadata：

~~~powershell
python -m build --wheel
python -c "import glob,zipfile; p=sorted(glob.glob('dist/stata_agent-*.whl'))[-1]; z=zipfile.ZipFile(p); assert any(n.endswith('stata_doctor.py') for n in z.namelist()); print(p)"
~~~

11. Architecture belief-map gates（若有源代码结构改动）：

~~~powershell
python "C:\Users\user\.codex\skills\codespaces\scripts\belief_search.py" build
python "C:\Users\user\.codex\skills\codespaces\scripts\belief_search.py" boundaries
python "C:\Users\user\.codex\skills\codespaces\scripts\belief_search.py" invariants
~~~

# Stop Conditions

- Tasks 1–5、targeted tests、全量离线门禁、Ruff、Mypy、product eval、coverage 和 wheel build 完成后立即停止；不得继续处理远端 provider、计量 skill、outbox、附件、UI 或其他 backlog。
- 当前永久授权环境下，doctor 与显式 live tests 必须全部通过后才能宣告本轮完成；若失败，应修复本轮范围内的 correctness 问题或如实记录新的外部阻断，绝不切换 FakeExecutor、skip、xfail 或放宽断言。
- doctor/live tests 全绿后记录真实结果并停止，不继续做 L3 论文复现或 provider 长会话。
- 若必须改变事件 schema、核心依赖方向、Stata transport 方案或 executor terminal 语义才能推进：停止实现，输出 `Architecture Change Required` 提案，不能把变更混入本轮。
- 若发现与目标无关的 bug：只记录在 `Remaining Issues`；除非它直接阻断本轮验收且可在既有架构内最小修复，否则不处理。
- 不进行命名清理、目录搬迁、公共错误体系重构、typing debt 清理或“顺便优化”。

# Deliverables

本轮 Codex 必须交付：

1. 实现真 Stata readiness/acceptance 门禁及 fail-closed ado 修复的代码修改。
2. 覆盖成功、失败、短路、脱敏、资源关闭、CLI 与 ledger closure 的离线测试，并保留显式 live 测试。
3. 根目录 `IMPLEMENTATION_REPORT.md`，严格包含以下小节：
   - `# Completed`
   - `# Files Changed`
   - `# Design Decisions`
   - `# Tests Run + Results`
   - `# Deviations from Plan`
   - `# Remaining Issues`
   - `# Recommended Next Step`

`IMPLEMENTATION_REPORT.md` 必须分别报告离线代码门禁与真实环境门禁，并明确记录 Stata 18 perpetual 授权已被当前 `stata-mcp` 识别。若 doctor/live gate 仍失败，`Remaining Issues` 必须保留真实原因，`Recommended Next Step` 只建议修复后重跑同一门禁。不得把未执行、失败、skip 或 mock 的结果写成通过。
