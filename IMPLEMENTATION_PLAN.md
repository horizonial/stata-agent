# Objective

为系数型 Stata 结果建立版本化、确定性、失败关闭的证据验收门：只有声明了
`ResultContract`、由可信执行层提取实际模型元数据、并通过 `verify_result` 全部检查的
run，才能自动签发 numeric EvidenceCard。

# Context

真 Stata transport、持久会话、provenance 和发布探针已经通过验收。当前最高优先级风险
转为计量结果语义：系统能运行 Stata，但尚不能证明被报告的系数对应声明的 estimator、
因变量、目标 term、VCE、聚类和固定效应。

本轮落实 `design/ECONOMETRICS_RESULT_VERIFICATION.md`，实现 DD-01 已定义但尚未落地的
`output_contract` 思想。它强化既有 Tool/Executor/EvidenceSigner 链路，不建立新编排器、
工作流或事实源。

# Current Behavior

- `run_stata` 接受自由文本 `code`，提示模型自行打印通用 `MACHINE_B/MACHINE_SE`。
- `parse_machine()` 接受首个通用 marker，未绑定到具体 run 或目标 term。
- machine 缺少 estimator、depvar、vce、cluster、absvars 等模型元数据。
- `_run_stata()` 对任何非空有限 machine 值立即签卡；签卡异常被静默吞掉。
- `verify_result` 仅回显 ledger 中的 machine/provenance。
- signer 只检查 do-file 存在，不复算文件 hash。
- `RunRecord` 没有结果合同；旧 run 与可报告 run 无法结构化区分。
- packaged causal-inference Skill 未强制结构化结果合同。

Targeted baseline：toolkit/executor/evidence/agent/skill 相关测试 48 passed。

# Target Behavior

- `run_stata`/`run_do_file` 可携带 `result_contract`；无合同探索命令仍可执行，但不得自动
  产生 numeric evidence。
- V1 合同明确 target term、estimator、dependent variable、VCE、clusters、fixed effects 和
  required stats，仅支持 `regress`/`reghdfe` 系数结果进入证据链。
- extraction suffix 由 executor 生成并使用 run-scoped marker namespace；模型输出的旧通用
  marker 不能伪造机器层。
- semantic input hash 覆盖用户代码与 canonical contract；command hash 覆盖实际 do-file。
- `verify_result` 返回稳定、版本化、逐项可判定的 `VerificationReport`。
- signer 自己调用 verifier；只有 `evidence_ready=true` 才签卡，locator 绑定 term、contract
  hash 和 verification schema version。
- run 成功但验证失败时仍为 succeeded；工具明确返回验证失败和零签卡。
- 旧 ledger 无需迁移即可回放；无合同旧 run 明确为不可签证据。

# Invariants / Hard Constraints

- SQLite append-only event ledger 仍是唯一研究事实源，不改写历史事件。
- 模型不能签 EvidenceCard/Claim；写权分离与 actor 校验不得弱化。
- `ChatService -> agent_loop -> ToolEnforcer -> toolkit` 仍是主编排链。
- Tool permission/enabled/Skill.allowed_tools 约束保持生效。
- FakeExecutor 只能显式用于 test/demo，不能充当 real 或 live fallback。
- 取消、timeout、uncertain、幂等复用、lease/fence 和隐私契约不得改变。
- verification 失败不等于 Stata 执行失败。
- 合同字段不得作为任意 Stata 代码插值；target term 必须保守校验。
- 不保存思维链，不把 Skill/Memory 内容提升为研究证据。
- 本轮不修改前端；现有 `innerHTML` 禁令保持。

# Scope

## In Scope

- ResultContract、VerificationReport 和纯验证逻辑。
- RunRecord 对可选合同的兼容投影。
- executor 内可信 marker、模型元数据提取、双 hash 语义和复用兼容。
- `run_stata`、`run_do_file`、`verify_result` 合同升级。
- numeric signer verification gate 和 locator 补充。
- FakeExecutor/fixtures 对显式 test contract 的支持。
- packaged causal-inference Skill 的最小方法约束。
- `regress`、`reghdfe` offline 测试及一个 opt-in real Stata smoke。
- README 的合同示例和失败语义。

## Out of Scope

- 自动选择识别策略或证明因果有效性。
- IV、RDD、event-study、非线性、survival、survey、MI、bootstrap、多方程 profile。
- 平行趋势、弱工具变量、聚类阈值、安慰剂、多重检验等领域诊断。
- prepared dataset、真实外部数据签名或跨机器独立复现。
- 新事件类型、SQLite migration、新 API/UI 页面。
- ChatService、agent loop、provider、memory、RAG、writer、outbox 重构。
- 附件、可观测性、发布签名和后续 backlog。

# Architecture Change Required

None。结果合同是 DD-01 `ResearchSpec.output_contract` 与既有 validator 写权分离的实现切片；
可选 RunRecord 字段通过现有 JSON event payload 回放，不改变事实源或数据库架构。

# Files

## Primary files

- `app/src/stata_agent/tools/result_verifier.py`（新增）
- `app/src/stata_agent/tools/executor.py`
- `app/src/stata_agent/tools/fake_executor.py`
- `app/src/stata_agent/tools/evidence_signer.py`
- `app/src/stata_agent/toolkit.py`
- `app/src/stata_agent/domain/models.py`
- `app/src/stata_agent/domain/reducers.py`
- `app/src/stata_agent/skills/SKILL.md`
- `app/README.md`

## Likely test files

- `app/tests/test_result_verifier.py`（新增）
- `app/tests/test_executor_parse.py`
- `app/tests/test_stata_executor.py`
- `app/tests/test_evidence_skill.py`
- `app/tests/test_ledger_evidence_hardening.py`
- `app/tests/test_toolkit_direct.py`
- `app/tests/test_agent_loop.py`
- `app/tests/test_skill_decision.py`
- `app/tests/test_runner.py`
- `app/tests/test_e2e_full_pipeline.py`
- `app/tests/test_product_eval.py`

## Reference-only files

- `ARCHITECTURE.md`
- `design/PROJECT_ARCHITECTURE_AUDIT.md`
- `design/ECONOMETRICS_RESULT_VERIFICATION.md`
- `design/dd-01-domain-events.md`
- `design/dd-04-tool-permission.md`
- `design/dd-05-writer-validator.md`
- `design/dd-06-eval.md`
- `app/src/stata_agent/events/schema.py`
- `app/src/stata_agent/harness/agent_loop.py`
- `app/src/stata_agent/harness/tool_enforcer.py`
- `app/src/stata_agent/tools/strategy.py`
- `app/src/stata_agent/stata_doctor.py`

## Files that should not be modified unless necessary

- `app/src/stata_agent/events/schema.py`
- `app/src/stata_agent/storage/`
- `app/src/stata_agent/application/`
- `app/src/stata_agent/providers/`
- `app/src/stata_agent/memory/`
- `app/src/stata_agent/rag/`
- `app/src/stata_agent/writer/`
- `app/src/stata_agent/ui.py`
- `app/src/stata_agent/ui/`
- `app/pyproject.toml`

# Implementation Tasks

## Task 1 — 定义版本化结果合同和纯验证器

- **Goal**：建立与 UI/LLM/transport 解耦、可单测、稳定排序的验证契约。
- **Files**：`tools/result_verifier.py`、`domain/models.py`、`domain/reducers.py`、
  `tests/test_result_verifier.py` 和相关 reducer tests。
- **Required behavior**：
  - 定义 schema v1 的 ResultContract、CheckResult、VerificationReport。
  - V1 只接受 `regress`/`reghdfe`，校验 target term、列表长度、字符和重复项。
  - RunRecord 新增可选 contract；reducer 从 `run.requested` fold，旧事件正常回放。
  - verifier 按设计文档固定 12 项顺序检查，不依赖网络、provider 或 Stata session。
  - report 只含稳定安全字段；contract/machine 使用 canonical JSON hash。
- **Edge cases**：旧 run 无合同；run 不存在/未成功；NaN/Inf/bool；缺字段；unsupported
  estimator；空/重复 cluster/FE；大小写/空白；畸形 provenance。
- **Dependencies**：无。
- **Acceptance criteria**：每个失败码、稳定顺序、hash 确定性和旧 ledger 回放均有单测；
  不新增 migration/event type。

## Task 2 — 将机器层提取移入可信 executor 边界

- **Goal**：模型控制的 Stata 输出不能伪造待签系数或模型元数据。
- **Files**：`tools/executor.py`、`tools/fake_executor.py`、`tests/test_executor_parse.py`、
  `tests/test_stata_executor.py`。
- **Required behavior**：
  - executor 接受可选 ResultContract，生成不可预知的 run-scoped marker namespace。
  - 有合同才追加 `_b[]/_se[]` 与 e(cmd/depvar/vce/clustvar/absvars/N/r2) extraction；环境
    extraction 保持。
  - parser 只接受本次 namespace；legacy marker 不参与 contracted result。
  - semantic hash = 原代码 + canonical contract；command hash = 实际 do-file 全文。
  - request 保存 contract/semantic hash，terminal 保存 command hash；reuse 区分不同合同。
  - FakeExecutor 仅在 explicit test provenance 下产生等形合同结果。
- **Edge cases**：主动打印伪 marker；term 不存在；字符串含空格；无 r2；不同 VCE；多向
  cluster/FE；无合同非估计命令；取消与 uncertain。
- **Dependencies**：Task 1。
- **Acceptance criteria**：伪 marker 测试通过；do-file hash 可复算；不同合同不复用；相同
  合同保持幂等；opt-in live `regress` 验证 term/cmd/depvar/vce。

## Task 3 — 让 verify_result 成为真正的 fail-closed 工具

- **Goal**：工具返回验证结论，不再只回显 ledger。
- **Files**：`toolkit.py`、`tests/test_toolkit_direct.py`、`tests/test_agent_loop.py`。
- **Required behavior**：
  - `run_stata`/`run_do_file` schema 接受设计规定的 `result_contract`。
  - 删除让模型自行构造 MACHINE marker 的提示；handler 把合同交给 executor。
  - `verify_result` 返回完整 report 及兼容所需的 run_id/machine 摘要。
  - 无合同运行成功但 `evidence_ready=false`；失败提供稳定 code/suggestion。
  - 不吞 verification/signing 异常；结构化返回且不改写成功 run 状态。
- **Edge cases**：缺 run_id；旧 run；store 异常；合同畸形；reused run；run succeeded 但
  verification failed；执行前后取消。
- **Dependencies**：Tasks 1–2。
- **Acceptance criteria**：schema 拒绝额外/畸形字段；所有签卡尝试都有可见 report；
  ToolEnforcer permission、timeout、取消行为不回退。

## Task 4 — 将 verification gate 接到 EvidenceSigner

- **Goal**：numeric card 不能绕过确定性验证进入主证据链。
- **Files**：`tools/evidence_signer.py`、`tools/result_verifier.py`、
  `tests/test_evidence_skill.py`、`tests/test_ledger_evidence_hardening.py`、
  `tests/test_runner.py`、`tests/test_e2e_full_pipeline.py`。
- **Required behavior**：
  - signer 从 canonical RunRecord 重新验证，不信任调用者布尔值/report。
  - 未通过时不 append card/claim，并抛带稳定 code 的验证异常。
  - coef/se locator 加 target_term、contract_hash、verification schema；N/r2 绑定同一合同。
  - 签名前复算 do-file hash并比对 command hash。
  - 保持 card ID、actor、append-only、幂等和 test-only provenance 隔离。
  - legacy runner/eval fixtures 仅补显式 test contract，不恢复无合同自动签卡。
- **Edge cases**：do-file 修改/删除；machine/contract hash 改变；重复验证；部分旧卡；fake
  冒充 real；card/claim append 中途失败。
- **Dependencies**：Tasks 1–3。
- **Acceptance criteria**：失败路径零新增 card/claim；通过路径 locator 完整；重复调用不重复
  写；writer grounding tests 不弱化。

## Task 5 — 强化 Skill、评测和文档

- **Goal**：让模型知道何时必须声明合同，并建立稳定产品回归门。
- **Files**：`skills/SKILL.md`、`eval/runner.py`（仅必要时）、`app/README.md`、skill/agent/
  product eval tests、`IMPLEMENTATION_REPORT.md`。
- **Required behavior**：
  - Skill 明确“方法选择不等于软件验证”，报告型系数必须有完整合同并检查 evidence_ready。
  - adversarial scenario：伪 marker 或错误 cluster/FE 必须拒签。
  - success scenario：explicit test contract → verified → card/claim → writer 可消费。
  - README 给出最小 `regress` contract、失败语义和 live 命令。
  - 报告不得把 V1 描述为可证明因果有效性。
- **Edge cases**：Skill 未匹配；无 executor；Fake 未显式启用；旧 golden 稳定字段。
- **Dependencies**：Tasks 1–4。
- **Acceptance criteria**：Skill routing/allowed tools 不变；对抗 eval 稳定通过；README 与 schema
  一致；报告完整。

# Tests

## Unit tests to add

- ResultContract 合法/非法 schema、canonical hash、term allowlist。
- VerificationReport 固定检查顺序和每个失败 code。
- trusted marker namespace，确认旧/伪 marker 被忽略。
- cmd/depvar/vce/cluster/absvars 正常化和不匹配。
- required stats 缺失、非有限值、bool 拒绝。
- semantic/command hash 分离及 do-file tamper。
- RunRecord 可选 contract 的新旧事件回放。

## Integration tests to add or update

- contracted Fake/Stub → run_stata → verifier → signer → card/claim。
- 无 contract 和 mismatch 都执行成功但零证据。
- 相同代码+相同合同复用；相同代码+不同合同不复用。
- `verify_result` 经 ToolEnforcer 返回机器可判定报告。
- opt-in real Stata `regress`；本机有 reghdfe 时再覆盖 reghdfe。
- product eval 的伪 marker/错误 VCE 或 FE 对抗场景。

## Success paths that must be covered

- `regress price mpg` 与 mpg term、price depvar、ols VCE 合同通过并签卡。
- reghdfe metadata 形状 offline 通过，live 取决于 ado 可用。
- verified card/claim 仍可供 table/writer 使用。
- 重复 verification/signing 幂等。

## Failure paths that must be covered

- marker spoofing、缺 term、错误 estimator/depvar/vce/cluster/FE。
- run missing/failed/uncertain、合同缺失/不支持/畸形。
- do-file 缺失/修改；command、machine、contract hash 不一致。
- machine 缺 coef/se/N，或含 NaN/Inf/bool。
- fake 冒充 real，或 real provenance 未 attested。
- 签卡异常不得被 `_run_stata` 静默吞掉。

## Regression tests

- 禁止删除、skip 或弱化 ledger、write-authority、cancellation、reuse、writer tests。
- 无合同 describe/list/探索代码仍能运行。
- 旧 ledger/snapshot/upcast 继续通过。
- 默认 pytest 不要求 Stata、ado 或网络；live tests 继续显式 gate。
- FakeExecutor 仍只能显式配置。

# Verification Commands

在 `app/` 下依次运行：

```powershell
python -m pytest -q tests/test_result_verifier.py tests/test_executor_parse.py tests/test_evidence_skill.py tests/test_ledger_evidence_hardening.py tests/test_toolkit_direct.py
python -m pytest -q tests/test_agent_loop.py tests/test_runner.py tests/test_e2e_full_pipeline.py tests/test_skill_decision.py tests/test_product_eval.py
$env:STATA_LIVE='1'; python -m pytest -q tests/test_stata_executor.py; Remove-Item Env:STATA_LIVE
python -m pytest -q
python -m ruff check src tests
python -m mypy src
python -m stata_agent.eval --json
python -m coverage run --branch -m pytest -q
python -m coverage report --fail-under=75
python -m build --wheel
```

在仓库根目录运行：

```powershell
python C:\Users\user\.codex\skills\codespaces\scripts\build_belief_map.py --root "D:\work\stata agent" --full
python C:\Users\user\.codex\skills\codespaces\scripts\belief_search.py boundaries all
python C:\Users\user\.codex\skills\codespaces\scripts\belief_search.py invariants all
```

# Stop Conditions

- Tasks 1–5 和上述门禁全部通过后立即停止。
- 不继续实现其他 estimator profile、领域诊断、outbox、附件、观测或 UI。
- 若 Stata `e()` 元数据不足，仅将对应 profile 标为 unsupported；不得用命令文本猜测后放行。
- 若需要新事件类型、SQLite migration 或改变 EvidenceCard 写权，停止并记录
  `Architecture Change Required`，不得混入本轮。
- live reghdfe 因本机缺 ado 可明确记录 skip；offline contract 与 real `regress` smoke 仍必须过。

# Deliverables

1. 代码修改。
2. 新增/更新测试。
3. 与实际 schema 一致的 README。
4. `IMPLEMENTATION_REPORT.md`，必须包含：
   - Completed
   - Files Changed
   - Design Decisions
   - Tests Run + Results
   - Deviations from Plan
   - Remaining Issues
   - Recommended Next Step

报告必须明确区分“Stata 执行成功”“规格符合合同”和“因果设计有效”，并记录任何不支持的
estimator/profile，不得夸大验证范围。
