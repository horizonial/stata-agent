# DD-04 详细设计：工具契约与权限（Policy）

> 文档层级：`design/SPEC.md` v0.5 的下位细化。本文把 SPEC §4.9.2（安全/隐私三档/研究闸门）、§4.10（联网受控）、DD-02 的 `policy.check()`、DD-01 的工具类事件落成可实现设计。数据契约以 **DD-01**、控制流以 **DD-02** 为准。
> 借鉴：claude-code 学习册"Tool Contract：validate/check_permission/call/summarize_result 四段 + 权限政策流水线 + 拒绝回模型"；claw-code"Policy/Prompt/Enforcer 三拆 + 固定裁决顺序"；codex"Tool Spec/Registry/Router/Runtime 分层 + Approval 设计"。
> **stata-mcp 工具清单核对**（`C:\Users\user\stata-mcp\src\stata_mcp\tools\`，2026-09-07）：10 工具见 §3，名称/职责已实锤。

---

## 0. 设计输入与要回答的问题

**回答**：工具契约怎么建模？10 个 stata 工具各自的权限策略？policy 怎么判、顺序固定？权限维度怎么组合？模型被拒/被打断怎么回？author do-file / 联网这类高风险入口怎么控？

## 1. 工具 = 受控能力对象（不是函数）

每个工具四段分离（claude-code）：**schema → permission → call → summarize**，副产物标注；授权与执行不耦合。

```python
class ToolContract(BaseModel):
    name: str
    input_schema: dict                 # 模型可见（DD-01 event tool.call 的 args 校验用）
    side_effect: Literal["read","write"]   # write=会改数据/写文件/删文件
    read_only: bool
    destructive: bool                  # 删/覆盖原始数据 等
    timeout_ok_retry: bool             # read 类可安全重试
    # 四段：
    validate(args) -> args|err         # schema+语义校验（变量名/路径/命令安全）
    permission(ctx) -> PolicyVerdict   # 交给 §4 policy（DENY/ASK/ALLOW+理由）
    call(args) -> ToolOutcome          # 真实执行（stata 走 MCP / 本地）
    summarize(outcome) -> ContextItem  # 三层出口之模型层摘要（DD-03 §5），非整段塞
```
- `side_effect` 决定 reconcile（DD-01 §3.5）：read 超时可重试；write 超时按"可能已提交+可能污染内存 .dta → 从 prepared 重建"处理。
- 工具调用 = 状态机的正式转移，不是旁路副作用（结果带 `tool_use_id/operation_id` 回模型；closure 不变量，DD-01 §3.3）。

## 2. 工具分层（Registry / Router / Runtime）

codex 分层（模型可见 vs 真实执行分开）：
- **ToolRegistry**：全部 ToolContract 注册（含 schema 与元数据）。
- **ActionRouter（orchestrator 侧）**：把模型 `Act`（DD-02 §5）路由到"允许执行的 op"，决定走哪个工具/哪个 harness 内置动作；**模型只看到 schema，看不到实现**。
- **Runtime**：真实执行器——stata 走 MCP client（stata-mcp）、文件/检索走本地实现。加新后端只实现 ToolContract，主循环不改。

## 3. 工具清单与策略映射（含 stata-mcp 10 工具，已实锤）

| 工具 | 职责 | 来源 | 副作用 | 阶段默认 | 默认策略 | 参数/前置校验 | 备注 |
|---|---|---|---|---|---|---|---|
| `stata_run` | 执行一段 Stata 代码 | stata-mcp | **write**（可改内存数据/写文件） | ESTIMATION/DATA | 阶段内 allow，restricted 模式 | command 过 restrict()（危险命令/路径审计/宏危险）；涉及覆盖原始数据→升级 ASK | 核心风险；代码级受限见 §6 |
| `stata_load_data` | 载入数据文件到内存 | stata-mcp | **write**（改会话内存） | DATA/ESTIMATION | allow（路径在 allowlist）；URL→Deferred/ASK | 本地路径审计（DataPathAuditor）；URL 来源未知→ASK + hash | 载入后 ResearchState.sample 更新 |
| `stata_inspect_data` | describe/summarize/codebook | stata-mcp | read | 全 | allow | variables 名校验 | 只读查看当前数据 |
| `stata_data_rows` | 当前数据前 N 行 | stata-mcp | read | 全 | allow | 行数上限 | agent"看数据"；结果走三层摘要 |
| `stata_get_results` | 读 e()/r() 结构化 | stata-mcp | read | ESTIMATION+ | allow | – | 机器层结果入口，validator 依赖 |
| `stata_get_help` | 查官方帮助 | stata-mcp | read | 全 | allow | – | agent 不瞎猜语法 |
| `stata_session_history` | 会话命令日志 | stata-mcp | read | 恢复/审计 | allow(own session) | – | 重放/追溯用 |
| `stata_break` | 打断正在执行的命令 | stata-mcp | write(控制) | 全 | allow（对自有会话） | session 归属校验 | 打断后 rc=1，引擎存活 |
| `stata_task_status` | 后台任务状态 | stata-mcp | read | 全 | allow | job 归属 | 异步 run 的配套 |
| `stata_export_graph` | 导出图为文件 | stata-mcp | write(写图) | ESTIMATION+ | allow（写 `_mcp_graphs/`） | 路径在白名单内 | 图 → figure artifact（DD-01） |
| `file_reader` | 读 dta/csv/xlsx 结构/样本 | 本地 | read | DATA/DESIGN | allow | 路径审计 | 学者指的数据位置 |
| `rag_search` | 混合检索双库 | 本地 | read | LITERATURE+ | allow | roles 限制（style_only 不可当证据） | 返回 chunk 带 source_role |
| `artifact_read` | 读工件/do/日志/图 | 本地 | read | 全 | allow | 路径白名单 | L4 按需读（DD-03 §5） |
| `web_search / web_fetch / download` | 联网查复现包/文献/文档（**新增，§4.10**） | 受控 | read（fetch）/download=write | 视 mode | 仅 approved_remote/mixed_sanitized；local_strict=DENY | 下载内容 retrieved_untrusted + 来源/版本 artifact；禁"下载即执行" | 用途先锁：复现包/公开数据/在线文献/命令核对 |
| `approval`（非工具） | 人工门 | harness | – | 全 | – | – | approval.requested/granted/rejected（DD-01 #4/#5） |

> 模型可见工具 = 上表（file/rag/artifact/stata_*/web）；**writer/validator/evidence_builder 不是模型工具**，是 orchestrator 侧的确定性组件（模型只能 proposal/act，写不进 card/claim——DD-01 §2.7）。

## 4. Policy 裁决：三拆 + 固定顺序

借鉴 claw 三拆（Policy 裁决 / Enforcer 无交互快速 / Prompter 询问），对应我们：
- **Policy**：完整裁决，输出 `DENY | ASK | ALLOW + reason(可审计)`。
- **Enforcer**：热路径，命中已缓存规则（如"read 白名单"）直接 allow/deny，不打断。
- **Approval/Prompter**：需要人工时，生成带"帮用户做决定"上下文的 approval 请求（DD-02 §4）。

**固定裁决顺序**（每步都可短路并给可理解原因，claude-code/claw 一致）：

```
1  无条件拒绝          —— destructive 于白名单外 / 凭据 / 跨 idea 写；DENY(不可协商)
2  策略规则           —— (phase × tool × args) 查表：read 白名单→ALLOW_FAST；越权→DENY
3  隐私模式门         —— local_strict 下 web/远程=DENY；approved/mixed 按 §4.10
4  研究闸门           —— spec/样本/识别策略/FE/聚类/主结果选择 变更：默认 ASK（带 diff+理由）或(explore 期可 ALLOW 但必记 amendment)
5  approval ask       —— 人工门/高影响写：ASK → approval.requested
6  ALLOW
```
- 每个 DENY/ASK 都**回给模型**（结果带 reason），不是静默失败（DD-02 §6：deny→tell；ask→等待）。
- 结果一律审计可解释：谁判的、依据哪条规则/模式/闸门。

## 5. 权限维度矩阵（组合而非开关）

一次 `check(act)` 实际是 4 维组合：**信任标签(内容来源) × 阶段 × 工具副作用 × 隐私模式**，再叠加**研究闸门**与**授权来源**：

| 维度 | 取值 | 说明 |
|---|---|---|
| 信任标签 | trusted_system / user_instruction / retrieved_untrusted / tool_result_verified | 检索到的论文即使写"运行 shell"也只是文本 span（§4.9.2） |
| 阶段 | 当前 phase + gate_mode(explore/formal) | 越权工具（如 WRITING 期 stata_run）DENY |
| 副作用 | read / write / destructive / control | 决定 reconcile 与审批级别 |
| 隐私模式 | local_strict / approved_remote / mixed_sanitized | 决定能否碰网络/远端模型内容 |
| 研究闸门 | 变更检测（ResearchState diff） | 静默改样本=研究事故，须拦截/留痕 |
| 授权来源 | user approval 记录 | 审批是持久化事件，可查"谁批了为什么" |

**权限不是开关，是带来源与时效的能力**：一个 allow 只对 (阶段,工具,参数模式,隐私模式,当前 spec) 有效；换 spec/换 phase/跨 privacy 边界都重新裁决。

## 6. 高风险入口（作者 do-file / 联网复现 / 长命令）

- **author do-file（复现 eval 用）** = 外部不可信代码，属 **destructive 级 + 独立沙箱**：只在 restricted + 显式审批 + 独立工作目录跑，**绝不碰学者真实数据/工作目录**；下载的复现包标 `retrieved_untrusted`（§4.10）。
- **stata_run 长命令**：默认 restricted 解析（危险命令/路径/宏危险拦截，guard/restrict.py 已给）。确需 unrestricted（作者原样 do）→ 走第 1 步无条件拒绝的分支改为单次强审批 + 沙箱 + 全量审计，默认关闭。
- **联网**：web_search 的 query 会被发给第三方 → 只查元信息；download 落 artifact（URL+hash+版本）；**绝不把研究数据文本放进 web query**。

## 7. 接线与事件

- 每次裁决写审计字段（谁判/依据），执行走 DD-01 事件链：`tool.call → tool.result(rc/cancelled/denied_reason) → run.*`；closure 不变量保证模型下一轮知道"被拒/被打断/没结果"。
- approvals/amendment/研究闸门相关事件（DD-01 #4/#5/#18）由本模块触发。
- DD-02 research_turn 的 `policy.check(...)`（DD-02 §6）在本模块实现；DD-03 的 L1 规则层注入"信任标签/模式"给模型，让它知道哪些被禁、为何。

## 8. M0 最小实现指南

1. `ToolContract` + Registry（先 file_reader / rag_search / stata_run / stata_inspect_data 4 个 + approval 假）。
2. Policy 五步顺序的纯函数实现（read 白名单 / 阶段 allowlist / research 闸门 stub / privacy 常量）。
3. stata_run 接 MCP client：restricted 模式 + validate()（guard 复用）；closure + summarize()。
4. 验收（L0）：模型 act 越权工具被 DENY 且 reason 回模型；`mark_done`/`sign_claim` 类 act 不存在于 schema；跨 privacy 边界 fallback 触发新 approval 事件。

## 9. 开放决策 / TODO

1. stata-mcp 各工具的 schema 字段与 args 校验细则（接 MCP 实例后填）。
2. "研究闸门"触发面的精确 diff 检测（哪些字段变才算：sample.filters / variables.role / identification.strategy / spec.model）与 explore 期自动放行规则。
3. web 工具的 provider 与去重/缓存（同 query 复用上次 artifact，省网络+审计）。
4. author do-file 沙箱目录策略与"允许的重新授权"一次性强审批 UX。
5. 是否把 `stata_run` 的 restricted 判定结果透出给上层（限制点 vs 已放行点），供 trace/评测断言。

## 10. 面试叙事（一句话）
> 工具不是函数，是带 schema、副作用、权限、摘要四段的受控能力；每次调用都是 信任×阶段×隐私×研究闸门 的组合裁决，被拒不是异常而是回给模型的正常结果——权限是带来源与时效的能力，不是布尔开关。
