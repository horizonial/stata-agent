# 实证研究 Agent —— 设计规格（v0.5，工业级）

> 用途：交给 codex 深化实现。本文是需求与架构的"钉子"，codex 在此基础上细化技术选型、产出代码。有分歧时以本文为准，存疑点标注 `[TODO]`。
> 关联项目：`C:\Users\user\stata-mcp`（Stata MCP 执行层，已完成）。本 agent 通过 MCP 协议驱动它跑 Stata。
> **版本基线：v0.5 = v0.4 + 合并成熟 coding agent（pi/codex/claude-code/claw-code）借鉴项（2026-09-07，来源 `../research/borrow_from_coding_agents.md` §3/§6）**。评审对象路径以本文件 `design/SPEC.md` 为准。

---

## 修订记录

### v0.4 采纳项（codex 架构深研，2026-09-03，来源 `../research/codex_report.md`）
- **P0-1 状态唯一真相**：SQLite **events 表（append-only）是唯一事实源**；当前状态由**物化视图**（可重建）给出；`ledger.jsonl` 降级为导出，不参与恢复。
- **P0-2 编排**：单 agent 循环放进**确定性阶段状态机**内的受约束循环；状态迁移只归编排器。多 agent M0–M3 不启用，启用需评测证明 + ADR。
- **P0-3 推理产物**：不存原始 thought（思维链），存**可审计 decision_summary** + 证据 ID + 候选/选中/拒绝理由。
- **P0-4 数值/引用接地升级**：写作 = **EvidenceBundle（先证据包后渲染）**；正文只许引用已验证 claim ID。
- **P0-5 事件与可靠性**：事件 schema/版本、幂等键（operation/attempt/语义哈希）、未决副作用与恢复协议；upcaster + 快照。
- **P0-6 隐私三档**：`local_strict / approved_remote / mixed_sanitized`；fallback 不得跨隐私边界。
- **P0-7 双 RAG 信任角色**：`style_only / citable_evidence / background_only` 类型化。
- **P0-8 评测升格**：五层 L0–L4，复现 ≥3 篇异质论文、遮蔽答案、专家容差。
- P1（M0–M1 完成项）与 P2（评测门控项）已并入 §5/§7。

### v0.5 采纳项（借鉴 coding agent，2026-09-07，来源 `../research/borrow_from_coding_agents.md`）
- **N1 events 补齐可重建字段 + reconcile**（B1，claw LaneEvent）：序号、来源、置信度、fingerprint、parent_id、attempt 状态；"终态后的不确定事件"要 reconcile，否则多来源会重复/矛盾，events 当不成唯一真相。
- **N2 压缩/恢复后健康探针**（B2，claw）：恢复 ≠ 读文件回来，先跑最小一致性检查再进 Loop。
- **N3 工具闭合不变量 + 半截不执行 + 取消为正常态**（B3，claude-code/pi）：每个 tool_use 必有带 ID 的结果；取消/权限拒绝 = cancelled 正常结果回模型（blocked 态）；`stopReason=length` 的半截参数不得执行。
- **N4 token 预算 = step 决策输入**（B4，codex/claude-code）：auto-compact 缓冲阈值，提前触发而非超限补救。
- **N5 研究完整性闸门 + 研究者自由度账本**（B5，claude-code/架构论文）：覆盖样本/换 FE/换聚类/删异常值单独审批记录，独立于隐私三档。
- **N6 结果三层契约**（B6，codex）：机器可验证层 / 模型可读层 / 人类附件层，固定进 EvidenceBundle。
- **N7 分级证据门槛 + 显式 Validation 状态**（B7，claw/codex）：可行性 vs 初稿是不同证据等级；"模型说可交付"不算完成。
- **N8 上下文 = 多级投影**（B8，claude-code/pi）：manifest→候选→按需读；投影不覆盖 canonical。
- **N9 ResearchState 内容层研究状态**（C1，claude-code/架构论文）：样本口径/变量角色/识别假设/证据引用结构化，独立于阶段机。
- **N10 Claim/EvidenceCard locator + ExperimentFamily**（C2，架构论文/codex）：防只记录喜欢的规格；追溯粒度到页码/表号/行列。
- **N11 写入权分离**（C3，codex/claude-code）：EvidenceBundle/EvidenceCard 只能由 Validator + Stata 执行事实生成，模型不得直接写 claim。
- **N12 探索性 vs 确认性分离 + confirmatory lock**（C4，架构论文）：看过结果后再改动必须 amendment 记录。
- **N13 分支语义 + rollback 分离上下文与文件副作用**（C5，pi/codex）：换 spec 即分支；"隐藏失败尝试"≠"删除写出的文件"。
- **N14 writer lease + revision/fence**（C6，pi）：防崩溃恢复后旧进程覆盖新状态。
- **N15 双投影事件溯源**（C7，codex，演进方向）：events 存核心事实，运行时投影为 模型 history / 用户 turn / 评测 event 三视图。
- **N16 阶段 0：无 Agent 可信实验内核**（C8，架构论文，M0 可选前置）。
- **N17 长期记忆借鉴 codex 记忆管线**（截断式，见 §4.9.1）。

---

## 0. 一句话定位

一个**研究伙伴 / 方法顾问**型 agent（不是全自动写论文机）：学者自己想 idea、找文献、找数据，放进约定文件夹；agent 与学者**对话式**判断 idea 可行性、指出还缺什么数据/文献、反复用 Stata 试不同 spec，最终产出**实证部分的论文初稿**（中文、Word、数字真实可溯源、引用真实可溯源、文风模仿顶刊）。

**v0.5 深化定位（面试叙事）**：把一次实证研究变成**可恢复、可复核、可追责的证据生产流程**——以研究问题为中心、以 Stata 结果为数值真相、以文献原文为引用真相、以事件台账为过程真相。架构 = **确定性骨架（阶段状态机/权限/证据/副作用做软件规则）+ 概率性推理（选题/解释/候选方案留给模型）**。v0.5 补一条硬规则：**模型的想法不是统计事实**——模型只能提议，进入正文的数字/引文只能由"验证器 + Stata 执行 + 真实文献块"生成。

## 1. 已钉死的决策（多轮访谈 + v0.4/v0.5 修订）

| 维度 | 决策 | 说明 |
|---|---|---|
| Agent 形态 | **单 agent 循环起步**，预留多 agent | 先证明单 agent 能出东西；多 agent 是为解决"研究执行/写作上下文打架"，不是目标 |
| 编排 | **确定性阶段状态机 + 阶段内受约束单 agent 循环** | 状态迁移只归编排器；多 agent M0–M3 不启用，仅当独立并行检索/权限隔离/上下文隔离**经评测证明有收益**才启用（ADR） |
| 工作流终点 | idea → 数据 → 实证设计 → 跑主回归 → 稳健性/机制/异质性 → **实证部分初稿** | 引言/综述可浅，实证方法+结果+解读要完整 |
| 大脑 | **LLM 聚合层**，多 provider 可扩展，先 **deepseek** | 按**能力画像 + 契约测试**编程，不按模型名编程 |
| 交互 | **强交互 + 聊天 UI（后置）**；可打断、可自动 | 人工确认是**持久化的 approval 事件**，不只是聊天提示 |
| 可行性 | **对话式**：反问澄清 → 查项目文献库 → 查数据 → 给"可行/缺数据/缺文献"+下一步 | 学者持续迭代思考 |
| trace/审计 | **结构化全链路**（decision_summary/action/tool_result/结论），可回放；**不存原始 thought** | 可解释性靠"为何选它"，不靠回放 CoT |
| 状态 | **events 表（SQLite append-only）= 唯一事实源**；物化视图 = 当前状态 | v0.5：加可重建字段/序号/parent/reconcile（N1）；分支语义（N13） |
| 研究状态 | **ResearchState 内容层**：样本口径/变量角色/识别假设/证据引用 独立结构化（v0.5 N9） | 阶段机管"走到哪"，ResearchState 管"当前用什么样本/模型/结论前提" |
| 推理产物 | 不存思维链；存 decision_summary + 证据 ID + 候选/选中/拒绝 | 模型想法 ≠ 统计事实，写入权分离（v0.5 N11） |
| 记忆 | **分层记忆**：线程状态/项目记忆/证据库/遥测 | 证据库=可版本化长期，走 EvidenceBundle/溯源；偏好记忆借鉴 codex 两阶段（N17） |
| 方法论 | **skill 系统**：内置基础（面板/DID/机制异质性/稳健性/出表）+ 可扩展 | skill = **版本化政策包**（meta+触发+前置检查+步骤+规则+禁用+示例） |
| 文献 | **两独立库**：①顶刊库→学文风 ②项目库→变量/引用(带 bibtex) | **信任角色类型化**：style_only / citable_evidence / background_only |
| 初稿 | **中文 + Word 为主**（rtf 表嵌 Word），LaTeX 次要 | 文风模仿顶刊库 |
| 数据 | 不限定单目录；agent 需**通用文件读取**（受路径审计约束） | 学者指出数据位置 |
| Stata | **走 MCP 协议**连 stata-mcp | stdio 先，HTTP 后 |
| 隐私 | **三档运行模式**：local_strict / approved_remote / mixed_sanitized | fallback 不得跨隐私边界 |
| 研究完整性 | **研究闸门**：改样本/识别策略/FE/聚类/删异常值单独审批记录（v0.5 N5） | 与隐私三档（数据安全）分离 |
| 并发 | **一次一个 idea** 工作区 | 专注推进，done 存档再开 |

### 1.1 v0.5 借鉴采纳速查（来源与落点）

| 采纳 | 来源（借阅笔记 §3/§6） | 落实在本文 |
|---|---|---|
| events 可重建字段 + reconcile | B1 | §4.4 |
| 恢复/压缩后健康探针 | B2 | §4.9.3 |
| 工具闭合不变量 / 半截不执行 / 取消正常态 | B3 | §4.4、§4.9.3 |
| token 预算 = step 决策输入 | B4 | §4.9.1 |
| 研究完整性闸门 + 自由度账本 | B5 | §4.9.2 |
| 结果三层契约 | B6 | §4.8 |
| 分级证据门槛 + 显式 Validation | B7 | §4.3、§4.8 |
| 上下文多级投影 | B8 | §4.9.1 |
| ResearchState 内容层 | C1 | §4.3.1 |
| Claim/EvidenceCard + ExperimentFamily | C2 | §4.8 |
| 写入权分离 | C3 | §4.8 |
| 探索/确认分离 + lock | C4 | §4.9.5 |
| 分支语义 + rollback 边界 | C5 | §4.4、§2 |
| writer lease + fence | C6 | §4.4 |
| 双投影事件溯源（演进） | C7 | §4.4 |
| 阶段 0 无 Agent 内核 | C8 | §5 |

### 1.5 技术选型与面试叙事（工业级）

> 每个选型能讲清 trade-off；不堆功能，深做 2-3 个差异化点。成熟构件（混合 RAG、事件溯源、Pydantic、OTel、Chroma/Qdrant）做扎实即可不当卖点。

| 层 | 选型 | 理由 / 面试叙事 | 状态 |
|---|---|---|---|
| 协议层（工具循环/结构化/流式） | **PydanticAI** | 类型化模型/工具协议层，不垄断业务状态与恢复语义 | ✅ 已定 |
| 编排 | **自写确定性阶段状态机** + PydanticAI 跑阶段内 agent | 状态迁移协议独立成产品资产；未来跨天/分布式再换 LangGraph/DBOS/Temporal | ✅ 已定 |
| 状态/记忆 | **SQLite events 表**（append-only）+ 物化视图 + 快照 + 可重建字段/reconcile（N1） | events=唯一事实源；单 writer 假设显式记录 | ✅ 已定 |
| 研究状态 | **ResearchState 内容层对象**（N9，v0.5） | 压缩/续跑时保留"结论前提" | 🟡 待细化 schema |
| 契约 | agent→LLM 关键产物全 **Pydantic model**，校验不过自动重试 | schema 保"形状正确"，EvidenceBundle/Validator 保"事实正确" | ✅ 已定 |
| 证据链 | **Claim / EvidenceCard locator / ExperimentFamily**（N10，v0.5） | 追溯粒度到页码/表号/行列；防只记录喜欢规格 | 🟡 待细化 |
| 模型适配 | **ModelCapabilityProfile + provider 契约测试** | 按能力编程不按模型名；换模型跑契约回归 | 🟡 P1 |
| 可测性 | **mock LLM provider**（录 replay），harness 确定性单测 | 能回答"怎么保证 agent 逻辑对" | ✅ 已定 |
| RAG | **混合检索**（向量 + grep/BM25 + 可选 rerank）+ 信任角色 | 语义召回找意思、词法守住名字；rerank 由 gold set 定 | ✅ 已定 |
| Embedding | `Embedder` 接口抽象，可换（bge-m3/大陆 API） | 用本项目数据与更新候选比，不写死 | ✅ 接口抽象 |
| 向量库 | `VectorDB` 接口抽象；**Chroma 起步** | 换型看延迟/召回/并发/运维，不是篇数（Qdrant P2） | 🟡 候选 |
| 文献解析 | 中英 PDF → 分块，记页/章节/bbox | 分块级可溯源 | 🟡 待试 |
| 引用粒度 | **分块级**（段/页 + span），非文章级 | 不能把 A 文结论安到 B 文 | ✅ 已定 |
| 数值/引用接地 | **EvidenceBundle：先证据包后渲染 + 写入权分离（N11）** | 模型只能提议，claim 只能由 Validator+Stata 事实生成 | ✅ 已定 |
| 完成定义 | **分级证据门槛**（可行性/主回归/初稿分级，N7） | 模型说完成≠完成，查 do 文件/rc/结果一致性 | ✅ 已定 |
| 规模余量 | 按**几百篇**预留 | 索引/去重/多路召回按几百设计 | ✅ 已定 |
| Stata 执行 | stata-mcp（走 MCP） | 上层做 10 工具策略映射 + 错误分类（P1） | ✅ |
| 可观测 | SQLite 业务账本 + **OTel→现成后端** | 两本账稳定 ID 关联，默认不采内容 | 🟡 P1 |
| UI | 后置，但事件表数据模型现在定 | UI 只读物化视图 + 发消息 | ✅ 已定 |

**深做的 2-3 个差异化亮点**：① 数字来源闭环（数字 → result_id + command_hash + data_signature + do_file → EvidenceCard locator）② 研究决策可查询（ResearchState/自由度账本/反事实分支 = 一等对象）③ 实证社会科学单元格级复现协议（L3）。其余做扎实即可。

## 2. 工作流（一个 idea 的生命周期，跑在阶段状态机上）

阶段状态机：`IDEA → LITERATURE → DESIGN → DATA → ESTIMATION → ROBUSTNESS → WRITING → VALIDATION → DONE`。每阶段声明 entry/exit conditions、allowed_tools、exit_schema、human_gate、max_attempts、failure_policy；agent 只能在阶段内循环，**只有编排器能提交状态迁移**；中途失败进失败态（可回退/重做）。阶段内额外维护 **ResearchState**（当前样本/变量/识别假设/证据引用，见 §4.3.1）——阶段机管"走到哪"，ResearchState 管"现在用什么"，两者都随 events 重建。

```
[学者] 想 idea → 自己找文献放库 → 自己找数据 → 起一个 idea 工作区
   ↓ 告诉 agent：idea + 数据位置提示
[0. 建档(IDEA→DESIGN)] 初始化工作区：idea 声明 + events 账本/物化视图 + ResearchState + 载 skill
   ↓
[1. 对话式可行性判断(DESIGN 内循环)]   ← 证据等级：可行性结论
   agent 反问澄清（研究问题/因果链/可观测变量…）
   → 查项目文献库（只从 citable_evidence 取证）→ 查数据支撑
   → 结论："可行 / 缺数据(缺什么) / 缺文献(建议找哪类)" + 下一步（approval 事件持久化）
   ↓ (学者补料或确认)
[2. 实证设计与跑(ESTIMATION)]          ← 证据等级：单次运行可验证
   agent 按 skill 提 spec（候选 proposal）→ MCP 跑 Stata（stata_run/load/inspect…）
   → 结构化结果 + provenance 进证据库 → Validator 生成 EvidenceCard → ResearchState/ExperimentFamily 更新
   → 换 spec 即分支（共享不可变过去 + suffix），失败尝试不覆盖，只记录
   ↓
[3. 稳健性/机制/异质性(ROBUSTNESS)]（按加载的 skill 走一遍；首个可并行点，仍单 agent）
   ↓
[4. 实证初稿(WRITING→VALIDATION)]      ← 证据等级：可交付初稿
   agent 读顶刊库学文风(style_only) → 引项目库真实文献(citable_evidence)
   → 从 EvidenceBundle 渲染数字/表格（不现编；写入权分离）→ validator 过数字/引用/复现 → 中文 Word
   ↓ (学者改，agent 按反馈迭代；改稿再走 VALIDATION；看过结果后的改动走 amendment 记录)
[完成] 工作区存档（events 导出 + 快照 + do-files + 初稿 + 引用清单）
```

## 3. 系统架构

```
┌───────────────────────────────────────────────────────────┐
│  UI（后置 M1+）：聊天 / 进度 / trace 回放 / 结果预览          │
│  ——只读物化视图 + 发消息，与 harness 解耦                    │
├───────────────────────────────────────────────────────────┤
│  确定性阶段状态机（自写）：IDEA→…→VALIDATION；每阶段           │
│  entry/exit·allowed_tools·human_gate·failure_policy；      │
│  状态迁移/权限/审批只归它；含 blocked 态与 Validation 状态     │
├───────────────────────────────────────────────────────────┤
│  受约束的单 Agent 循环（PydanticAI，阶段内）：                │
│  plan → act → observe → reflect；模型只产出候选 proposal；    │
│  每步写事件；token 预算/健康探针为决策输入                    │
├───────────────────────────────────────────────────────────┤
│  能力层（注册式）                                            │
│  • research_state      → ResearchState 内容层读写(§4.3.1)   │
│  • stata_mcp_client    → MCP 驱动 stata-mcp（策略映射）     │
│  • file_reader         → 读本地数据（路径审计）              │
│  • ledger              → events + 物化视图 + 快照 + reconcile│
│  • rag（双库）          → 顶刊[style_only·文风]/项目[citable] │
│  • evidence_builder    → 结果→EvidenceCard→Claim（只此能写） │
│  • writer              → 只渲染已验证 claim → 初稿(Word)+rtf │
│  • validator           → claim-to-source + 分级证据门槛      │
├───────────────────────────────────────────────────────────┤
│  RAG 内部：Retriever(混合: 向量+grep/BM25+可选 rerank)      │
│           → VectorDB(Chroma 起步,接口可换) + Embedder(接口) │
├───────────────────────────────────────────────────────────┤
│  事件账本 SQLite：events(append-only,含 parent/fingerprint) │
│  + 物化视图 + 快照 + 单写者租约(fence)；双投影为演进方向       │
│  遥测 OTel → 现成后端（Langfuse/LangSmith/Logfire，可换）   │
├───────────────────────────────────────────────────────────┤
│  LLM 聚合层：ModelCapabilityProfile + provider 接口；       │
│  deepseek 起步，预留 qwen/kimi/claude；+ mock(测试)         │
│  └ 隐私三档门：local_strict / approved_remote / mixed      │
└───────────────────────────────────────────────────────────┘
```

## 4. 关键模块与数据契约

### 4.1 idea 工作区（目录契约）

每个 idea 一个目录；事件/状态在共享账本（按 idea_id 分账），目录不放 jsonl：
```
ideas/<idea_slug>/
├── idea.md               # idea 声明：问题、预期贡献、初始假设（含 phase/ResearchState 版本）
├── data_refs.md          # 学者告诉 agent 的数据位置/文件清单
├── dofiles/              # 复现用 do 文件（含 MCP 提供的 provenance do_file）
├── outputs/              # 初稿、表格(rtf)、图；events 导出(ledger.jsonl 仅此用途)
└── notes/                # agent 与学者的讨论要点/待办
```
一次只开一个活跃工作区。

### 4.2 LLM 聚合层 + 能力画像（llm.py / capabilities.py）

- 统一接口：`chat(messages)`, `chat_tools(messages, tools)`, `chat_structured(messages, schema)`
- provider 抽象：`class LLMProvider(Protocol)`；DeepSeekProvider（OpenAI 兼容）
- **ModelCapabilityProfile**：`provider/model/version; supports_tools / strict_schema / thinking_with_tools / streaming; max_context / max_output / json_schema_subset; known_quirks / tested_at / contract_test_version`
- **Provider 契约测试**：空参/非法枚举/并行工具/超长 schema/流式中断/工具结果回填/重试副作用；锁定实际模型 ID 与 request ID；模型升级必触发回归
- **隐私门**：provider fallback 受 §4.9.2 三档约束——本地→远端是新的授权事件

### 4.3 Harness 与阶段状态机

- **阶段状态机**（确定性部分）：`IDEA → LITERATURE → DESIGN → DATA → ESTIMATION → ROBUSTNESS → WRITING → VALIDATION → DONE`；每阶段注册 `entry_conditions / allowed_tools / exit_schema / human_gate / max_attempts / failure_policy`。agent 提出候选迁移，**编排器提交**。
- **状态集含正常失败分支**（v0.5 N3/N7）：`blocked`（等外部输入/审批）、`validating`（显式 Validation 状态，验证通过才发 EvidenceCard）是正常态，不是异常；阶段迁移**拒绝非法序列**（如未 started 就 completed、completed 后又 append attempt）。
- **skill 加载**：按当前阶段+任务激活，注入 system prompt。
- **人工门控**：可行性结论 / 主回归结果 / 稳健性方案 / 初稿前 停等学者；确认持久化为 `approval` 事件。
- **token 预算**：作为每步决策输入（N4），含 auto-compact 缓冲阈值，提前触发压缩而非超限补救。
- checkpoint/续跑：从 events 重放最近**稳定阶段**（含未决副作用核对 + 健康探针，§4.9.3），不简单续写对话。

#### 4.3.1 ResearchState（内容层研究状态，v0.5 N9）

独立于"阶段机"（控制流）的**内容层对象**，回答"当前研究到底在用什么/信什么"：
```
ResearchState:
  sample_definition        # 样本口径：面板?样本区间?筛选规则(及各自版本/批准记录)
  variable_roles           # 因变量/处理/协变量/聚类层级的变量映射(带构造 do-file 引用)
  identification_assumptions  # 识别假设(平行趋势?外生性?)与其依据文献/证据
  spec_versions            # 当前主 spec + 已探索变体(ExperimentFamily 指针)
  evidence_refs            # 当前结论依赖的证据 ID 集(EvidenceCard)
  phase                    # 与控制流同步但独立存储
```
- 由 events 重建（物化视图的一种），**不依赖对话文本**。
- 压缩/续跑/分支时保留的是它 + 证据索引，**不是对话摘要**（codex/claw 的 checkpoint 教训）。
- 每次 reflect 前把 ResearchState 摘要进 prompt（多级投影的"研究状态层"）。

### 4.4 事件账本 Ledger + 物化视图（核心）

SQLite **events 表 = 唯一事实源**（append-only）；**当前状态 = 物化视图**（可重建）；`ledger.jsonl` 只做导出。

**events 表**关键列（v0.5 补齐 N1/N13/N14）：
```
event_id(uuid) | idea_id | phase | event_type | schema_version | actor(user|agent|system)
| correlation_id | causation_id | operation_id | attempt_id | seq(单调序号)
| source(来源: agent/stata-mcp/user/system/validator) | confidence(置信度)
| fingerprint(去重指纹) | parent_id(分支/因果) | leaf_id(当前分支尾)
| payload(json) | created_at
```
事件种类（`kind`=event_type）：
```json
{"ts":..., "kind":"agent_step","decision_summary":"用 fe+cluster(id) 做主回归是因为…",
 "evidence_ids":["res_.."],"candidates":["fe","fe+cluster"],"chosen":"fe+cluster","rejected":{...}}
{"ts":..., "kind":"tool_call","tool":"stata_run","operation_id":"op_..","attempt_id":1,
 "semantic_input_hash":"sha256(...)","side_effect_state":"committed","args_hash":...,"rc":0,
 "structured_summary":"...","cost":...}                       // 闭合：必有对应 tool_result（N3）
{"ts":..., "kind":"result","result_id":"res_..","hypothesis":...,"conclusion":...}
{"ts":..., "kind":"user","text":"..."}
{"ts":..., "kind":"approval","what":"采用 fe+cluster 做主回归","status":"approved","by":"user"}
{"ts":..., "kind":"provenance","result_id":...,"do_file":...,"command_hash":...,
 "data_signature":...,"num_ref":"表3-列2","spec_version":...}
```
- **不存原始 thought**；推理只沉淀为 decision_summary + 证据 ID + 候选/选中/拒绝理由。
- **可重建字段 + reconcile（N1）**：`seq/source/confidence/fingerprint/parent_id` 使多来源（Stata 重试、人工批准、报告生成）可去重、可按序重建；"终态后的不确定事件"由 reconcile 逻辑判断（查现状/补偿/人工），不能盲信写入顺序。
- **分支语义（N13，N5 相关）**：一次 idea 内换 spec = 在 parent_id 树上新开分支（共享不可变过去 + 自己的 suffix/leaf）；失败/被否的尝试**保留**，只标记不覆盖。**rollback 只从"实验历史"隐藏，不撤销文件系统**——写出的 do-file/结果文件是否删除由显式策略决定，二者不绑定。
- **物化视图**（随事件在**单写者事务**内原子更新投影）：`idea_state / current_phase / research_state(§4.3.1) / experiment_family / spec_version / result_index / citation_index / approval_log`。
- **写并发（N14）**：单写者事务 + **writer lease + 单调 revision（fence）**，防崩溃恢复后旧进程覆盖新状态（长跑 Stata 场景）。
- **快照 + upcaster**：恢复从最近快照重放；schema_version 变化走 upcaster + 迁移测试。
- **双投影（N15，演进方向）**：events 存核心事实；运行时投影为 ①模型 history（多级投影 §4.9.1）②用户可见 turn ③评测所需 event 三视图——先以物化视图落地，标注为演进项不立即实现。
- 每个进初稿数字都要有 `num_ref` → `result`（数字接地，见 §4.8）。
- 遥测走 OTel（§6），两本账分离。

### 4.5 双 RAG（混合检索，分块级溯源，信任角色类型化）

- 三信任角色：`style_only`（顶刊，学文风，不得产生引用）/ `citable_evidence`（项目/权威，可 claim-to-span 引用）/ `background_only`（理解用，终稿不可直接引用）。
- 入库版本链：`file_hash → parser_version → page/section/bbox → chunk_id(稳定) → source_role → embedding_version → index_version`；PDF 更新后旧 chunk 失效保留历史。
- 检索 = 向量召回 + grep/BM25 精确并查 + （可选）rerank；每块带 `doc_id + source_role + 页/章节/span`。
- M1 优先级：① 抽取质量/错页 QA ② 元数据+稳定 chunk ID ③ 50–100 条 gold set ④ dense/BM25/hybrid/rerank 消融 ⑤ 换库/换 embedding 最后。
- 接口：`Retriever.search(query, top_k, roles) -> list[Chunk]`；`VectorDB`；`Embedder`。

### 4.6 Skills（文件化方法论 = 版本化政策包）

`panel_did` / `mechanism_heterogeneity` / `robustness` / `regression_table_word` / `_template`。每个 = `meta + 触发条件 + 前置检查 + 步骤 + 规则 + 禁用条件 + 示例 + 版本`；skill 声明 **`requires: {ados:[reghdfe, esttab, coefplot, …], stata_min:"…"}`**，激活时 preflight 查 `which <ado>`，缺则提示安装/换机、**不静默跑错**（否则污染"代码 vs 环境"归因，审计 D2）；skill 变更走版本，供 L1/L2 评测与回归。

### 4.7 Writer（初稿）——只渲染已验证证据

- 输入：被"选定用于初稿"的 **Claim/EvidenceCard 集** + 项目库引用 + 顶刊文风样本。
- 约束（N11）：模型只能**提议**叙述；正文数字只能引用已过 validator 的 `NumericClaim`（带 result_id），引文只能引用 `CitationClaim`（带 chunk span）；**模型不得现注入新数字/新引文**。
- 输出：中文实证初稿（方法/结果/解读），表格由数字渲染（esttab→rtf / csv→Word），每格带 `num_ref`。
- Word 生成：`[TODO]` 选型（pandoc md→docx / python-docx / rtf 直出）——目标"学者能在 Word 里继续编辑"。

### 4.8 EvidenceBundle + Claim/EvidenceCard + Validator（写稿前必跑）

**证据链分层（v0.5 N6/N10/N11）**——结果不是直接变文字，中间经过两层：
```
Stata 结构化结果(带 provenance)              ← 机器层事实
   → Validator 生成 EvidenceCard               ← 精确 locator：页码/表号/行列/result_id/data_signature
   → Claim（可核查命题）                       ← 一句话研究级命题，Run→Claim→初稿段落
   → 初稿                                        ← writer 只消费 Claim/EvidenceCard，写入权分离
```

**结果三层契约（N6）**：每个执行结果同时产出
- 机器可验证层：exit code / N / 系数 / SE / checksum（schema 只作用于这层，只读）
- 模型可读层：摘要 / 警告 / 下一步
- 人类可追溯层：完整 do-file / 日志 / 环境
模型能解释机器层，**不能改机器层**。

**EvidenceBundle 中间对象**：
```python
class NumericClaim:     # value, unit, display_precision, result_id, data_signature, command_hash, table_ref
class CitationClaim:    # claim, chunk_id, doc_id, page/section, source_span, source_role, version
class EvidenceCard:     # locator 到 页码/表号/行列/result_id；由 Validator 签发（N10/N11）
class ResearchDecision: # hypothesis, spec_version, approval_record
# render_policy: 正文只能引用已验证 claim_id；写入权 = 仅 evidence_builder/validator，模型只提议
```

**ExperimentFamily（N10）**：一次研究的所有运行按"尝试家族"分组——预计划 vs 探索、停止规则、多重检验记数、家族内全成员 Run、主结果选择理由。**防只记录喜欢的规格**；主结果必须能回答"为何是这个 spec"。

**分级证据门槛（GreenContract 实证版，N7）**：

| 证据等级 | 何时用 | 门槛 |
|---|---|---|
| L-C 可行性 | DESIGN 出结论 | 项目库有据可查 + 数据变量可构造 + 学者确认 |
| L-R 单次运行 | ESTIMATION 出结果 | rc=0 + 结构化结果 + 校验通过，可复现 |
| L-D 可交付 | WRITING→VALIDATION 出初稿 | 每表对应 dofile 可跑 + 数字/引用全溯源 + validator 过 + 对抗用例过 |

"模型说可交付"不成立——按 L-D 检查 do 文件/返回码/结果一致性才算完成（N7）。

Validator 检：数字（命中 NumericClaim，原始值与展示值分离、渲染后反查）、引用（溯源到 citable_evidence 真实块）、复现（每表一个 dofile，签名一致）。schema 只保形状正确，validator 才保事实正确。

### 4.9 工程纵深四维（面试必答：上下文/安全/异常/评测）

#### 4.9.1 上下文与记忆管理（不靠"记住"，靠"随时能查"）

- **分层记忆**：

| 层次 | 内容 | 生命周期 | 是否可为证据 |
|---|---|---|---|
| 线程状态 | 当前阶段/待办/最近工具结果摘要 | 一次运行 | 否 |
| 项目记忆 | 已确认定义/偏好/变量映射/否决记录 | idea 生命周期 | 仅作约束 |
| 证据库 | 文献原文块、Stata 结构化结果、数据签名、EvidenceCard | 可版本化长期保存 | **是** |
| 遥测 | token/延迟/错误/trace | 运维保留期 | 否 |

- **多级上下文投影（v0.5 N8）**：完整历史/ResearchState 是**规范**（events 可重建）；给模型的是**投影**——按层折叠（稳定规则 → ResearchState → 证据摘要 → 原始材料按需读）。读文献/记忆用渐进披露：先 manifest 头 → 候选列表 → 按需读全文，"没选中也合法"；**投影不覆盖 canonical**。
- **token 预算（N4）**：每步算 full active / auto-compact scope / model limit / buffer / remaining；提前触发压缩而非超限崩溃。
- **压缩 = 语义 checkpoint（N2/N8）**：压成"研究问题/数据版本/已跑 spec 成败/已确认事实(Claim)/识别风险/下一允许动作"，保留证据索引；写 events（boundary 事件），**不是删旧消息**；压缩点在完整 turn/试算之后，绝不截断半条工具调用。压缩/恢复后跑**健康探针**再进 Loop（§4.9.3）。
- **长期记忆借鉴 codex 记忆管线（N17，截断式）**：可借鉴——两阶段解耦（逐 idea/phase 抽取 + 全局串行合并）、git-baseline diff 当"摄入+遗忘"信号、`<citation>` 引用协议让"用了哪条"可程序化、按产物类型用量遥测、consolidation 强沙箱、无变更最低信号门。**不照搬**——codex 记忆是偏好/流程记忆且会裁剪原始证据、抽取要联网；我们 local_strict 下长期记忆只能存脱敏摘要/纯本地，**证据库必须走 EvidenceBundle/溯源，绝不与偏好记忆混**。
- **明令**：不持久化原始思维链；短期上下文 ≠ 长期记忆。
- **金句**：*agent 不是记住一切，而是需要时从自己的长期记忆（事件表/证据库）检索——RAG-over-own-history，可复现、不爆窗口。*

#### 4.9.2 安全与风险（威胁模型 + 分层防线 + 隐私三档 + 研究闸门）

- **威胁模型**：一切外部输入不可信（文献/数据/粘贴都可能 prompt 注入；OWASP LLM01/LLM06）。
- **信任标签**：`trusted_system / user_instruction / retrieved_untrusted / tool_result_verified`；检索内容即使写着"运行 shell"也只是文本 span。
- **工具副作用分级（v0.5，与信任标签正交）**：

| 工具/动作 | 默认策略 |
|---|---|
| 读授权文献、inspect data | 自动允许 |
| 受限模式跑 Stata、读结果 | 阶段内允许，参数校验 |
| 覆盖原始数据、删除文件 | **禁止或单次强审批** |
| 联网下载 | Deferred + 来源/版本校验 |
| 生成衍生变量/覆盖 do-file | workspace 审批 |
| 非受限 Stata、写授权目录外 | 人工确认或 allowlist |
| shell、凭据访问 | 禁止或单次强审批 |

- **研究完整性闸门（N5）——独立于隐私的"研究闸门"**：改样本口径 / 换识别策略 / 换 FE / 换聚类 / 删异常值 / 主结果选择——这些默认"可执行但**必须记录假设与变更**（decision_summary + approval）"，且会让前后结果不可比的变更要显式提示。对应 **研究者自由度账本**（何时选、是否看过结果、依据）。
- **执行边界**：Stata 全走 MCP restricted/路径审计；分隔符只是标签，真正边界=工具能力+文件权限+确定性策略（fail-closed）。
- **隐私三档**（P0-6）：local_strict / approved_remote / mixed_sanitized；**fallback 不得跨隐私边界**；本地→远端是新授权事件（记 events）。
- **金句**：*最危险的是数据/文献里的注入被当成你的指令；最隐蔽的是 fallback 偷偷变成数据出境；研究 agent 最贵的是样本/识别策略被悄悄改掉——那要单独一道闸门。*

#### 4.9.3 异常处理与兜底（每层失败有明确降级 + 幂等 + 恢复协议）

| 层 | 失败 | 兜底 |
|---|---|---|
| LLM | 超时/限流/坏格式/拒答 | 分类：transient→重试退避+jitter；validation/model_semantic→修 prompt 重出；policy/data→回退/人工 |
| 工具(Stata) | 崩溃/超时 | MCP reset+重放；**超时≠失败**——先查未决副作用 |
| 流程 | 中断/进程死 | **恢复协议**：读最后稳定 phase → 核未决副作用 → 验数据签名/结果台账 → **健康探针** → 决定复用/重跑/人工 |
| 质量 | validator 失败 | 回生成阶段重做，**不硬出** |
| 预算 | token 超 | 停并总结已得，不硬跑 |

- **工具闭合不变量（N3）**：每个 tool_use 必须有结果、结果带 ID（`tool_use_id`）；中断有 cancelled 消息；取消/权限拒绝 = 正常分支（blocked），结果回模型让模型知道"没得到结果"。`stopReason=length` 的半截参数**即使能 parse 也不执行**（pi 教训）。
- **幂等与副作用**（P0-5 + N14）：`operation_id / attempt_id / semantic_input_hash / side_effect_state(prepared|executing|committed|failed|uncertain) / retry_class / reconcile_action`。读取类可重试；写文件/改数据命令必须幂等键 + 先查结果台账再执行。样本定义冲突不能靠"再问一次模型"。
- **rollback 边界（N13）**：回滚只动"实验历史/上下文"，不自动撤销文件系统；do-file/结果删除由显式策略决定。
- **健康探针（N2）**：压缩/恢复后，对重建的 Session/ResearchState 跑最小一致性检查（工具调用闭合、ResearchState 与结果台账一致），不一致先失败再进 Loop。
- **金句**：*超时是"不知道成功没"，不是"肯定没成功"；兜底是每类失败有明确降级 + 幂等恢复 + 恢复后先自检。*
- M0–M3 不引入 Temporal/DBOS；迁移触发：跨天等待/多人审批/分布式 worker/外部不可幂等副作用/明确 SLA（P2）。

#### 4.9.4 评测（实证 agent 独有的优势：有 ground truth）

**五层评测**：

| 层级 | 评什么 | 判分方法 |
|---|---|---|
| L0 契约 | schema、ID、权限、事件一致性、幂等、**工具闭合不变量**、**取消验收**、**非法事件序列被拒** | 确定性测试 |
| L1 组件 | 检索、变量映射、工具参数、skill 触发 | gold set + exact/graded |
| L2 轨迹 | 走对阶段、不越权、无无效循环、**健康探针通过** | trace grader + 规则 |
| L3 结果 | 表格/样本/系数/SE/N/显著性 | 单元格级数值评分（每格独立容差） |
| L4 研究质量 | 解释是否被证据支持、局限是否充分、**探索/确认是否干净（自由度账本）** | 盲评专家 + 校准过的 LLM judge |

**复现基准**：≥3 篇异质设计（面板 FE / IV-事件研究 / 离散选择），每篇定目标单元格/样本/变量/估计器/FE/聚类/权重/缺失处理；每指标单独容差（N/模型结构/数据签名 exact）；系数符号/点估计/SE/p 档位/R² 分计；≥3 次独立运行报 pass@1/均值/最差/token/费用。防污染：遮蔽数字、盲测不给 do-file、无计算 baseline、held-out、正文数字必须回链本次工具结果。
**对抗集**：小数点/符号篡改、真实引文支持错误主张、虚假引文、PDF injection、恶意变量标签、陈旧记忆、工具结果篡改、重复回放、超时后不确定状态、**半截工具调用**、**取消后模型假装已执行**、**探索性结果当作确认性（confirmatory lock 需拦下）**。
- **金句**：*评"怎么得到"，不只评"像不像答案"；能确定性判分的地方用代码判，LLM judge 只评开放维度且先与专家校准。*

#### 4.9.5 方法学护栏（探索性 vs 确认性，v0.5 N12）

- **显式 intent**：每次试算标注 `analysis_intent ∈ {exploratory, confirmatory}`。
- **confirmatory lock**：确认性检验一旦锁定（样本/识别策略/spec），**看过结果后改动必须走 amendment 记录**（谁改/何时/依据/影响哪些已出结果），防止把探索当确认（p-hacking 防线）。
- **研究者自由度账本**：记录"什么时候选过哪些建模自由度、当时有没有看过结果"——诚实披露，L4 与写作"局限性"章节使用。
- 实现为**可配置策略**而非硬规则（学者可关），但关闭要留痕。

### 4.10 能力边界：外部世界（联网）与输入模态（图片/OCR）（2026-09-07 审计决策）

> 审计结论：`design/audit-stata-practice.md` §1/§3。两能力都做成**受控**，方向已定、落点待 DD-04(权限)/DD-03(上下文)。

**联网（受控，非默认开）**：
- 新增工具 `web_search / web_fetch / download`；用途先锁：**复现包/公开数据、在线补文献、命令/文档核对**，不做自由 browsing。
- 门控随隐私三档：`local_strict` 全禁；`approved_remote / mixed_sanitized` 允许，但下载内容一律标 `retrieved_untrusted`（HTML/PDF/zip 都可能注入），落盘走 artifact 链（来源 URL + 内容 hash + 版本）。
- **"下载即自动执行"禁止**；复现用作者 do-file 属最高风险执行（见 §4.9.2 工具副作用分级 + DD-04）。
- 远程搜索会把查询词发给第三方 → 只在非 local_strict 下可用；研究数据/文本**不回传**（与 privacy.mode 绑定，跨边界是授权事件）。

**图片 / 视觉输入（分两层）**：
- 输入层：学者贴图（论文表截图/数据字典/想复现的目标表/扫描件）= `retrieved_untrusted`（图内文字也可能夹注入）；`ModelCapabilityProfile` 增 `vision: bool`（§4.2）；上下文投影层按模型能力裁剪（无 vision 的 provider 走本地 OCR/视觉或明确不支持）。
- 证据层：Stata 自产图（event-study/margins/coefplot/placebo/balance）= artifact → `figure` EvidenceCard（DD-01 §2.6）→ writer 嵌 Word + caption 引 claim；图可复现（有对应 dofile）。**读自产图确认"图讲的是 claim 讲的事"依赖 vision**。
- 中文扫描 PDF OCR 与贴图解析共用同一视觉能力；`local_strict` 下视觉必须在本地（本地 OCR/本地视觉模型），不许为了看图而把数据图发远端。
- eval/mock 支持 image fixture（防评测退化文本化）。

**落地归属**：联网 → DD-04 工具/权限；图片读入与上下文投影 → DD-03；figure 证据已在 DD-01。

## 5. 分阶段路线（v0.4 合并 P1/P2；v0.5 并入 C 项）

- **阶段 0（可选前置，N16）**：无 Agent 可信实验内核——Spec→Run→manifest→Validator→不可变提交→重跑，全无 LLM。若求稳先做它再挂模型；与 M0 取舍见 §7。
- **M0 骨架**：阶段状态机（最小）+ PydanticAI 协议层 + LLM 聚合(deepseek+mock) + **SQLite events 表(schema/版本/幂等/可重建字段/reconcile 骨架)** + ResearchState 最小版 + 物化视图 + 工作区脚手架 + CLI 对话（都进事件表；不存 thought）。P1：ModelCapabilityProfile + 契约测试骨架。**验收含：工具闭合不变量/取消用例（L0）**
- **M1 可行性**：file_reader + 双 RAG 落库(版本链/稳定 chunk_id/source_role) + 混合检索 + 50–100 条 gold set 消融 + 对话式可行性（证据等级 L-C）+ 隐私三档默认 local_strict + OTel 埋点；P1 收尾：stata-mcp 10 工具策略映射/错误分类、抽取 QA。**验收：真实 idea 跑通可行性**
- **M2 实证**：连 stata-mcp + skills(先 panel_did) + approval 门控事件 + 恢复协议(幂等+未决副作用核对+**健康探针**) + **ExperimentFamily 全成员记录 + 分支语义** + 研究闸门事件。**验收：复现 1–2 篇基准主表（L3 单元格计分开跑）**
- **M3 初稿**：Claim/EvidenceCard + evidence_builder + writer + validator(数字/引用/复现/分级证据门槛 L-D) + Word/rtf + 顶刊文风(style_only 隔离)。**验收：对抗注入 validator 全抓 + 复现集扩 ≥3 篇异质 + L0–L3 全套**
- **M4 观测/UI**：聊天+进度+trace 回放（只读物化视图）+ OTel 接现成后端。
- **P2（评测证明需要后再做）**：多 Agent 文献广搜 / Qdrant / Temporal-DBOS / 自研 Dashboard / 双投影完整实现(N15)。

## 6. 给 codex 的默认建议（可按需推翻）

- 语言：Python 3.12；核心依赖：pydantic-ai、sqlite3/aiosqlite、官方 `mcp` sdk；RAG：chromadb + pymupdf + embedding 接口实现一个；OTel SDK（可选后端）
- **events 表(SQLite)是唯一真相源**；物化视图出当前状态（含 ResearchState/ExperimentFamily）；jsonl 仅导出
- 深文件夹深度的东西（Skill 触发、RAG 检索、writer 分段、阶段机参数）都做成数据/配置驱动
- 与 stata-mcp 分工：agent 管"研究逻辑/ResearchState/台账/文献/写作"，stata-mcp 管"Stata 执行/结构化结果/溯源"；Stata 细节不渗进 agent
- 外部输入一律不可信（§4.9.2）；不存思维链，存可审计决策（§4.4）；模型想法≠统计事实，写入权分离（§4.8）
- 每步落事件、幂等、可续跑、恢复后健康探针（§4.9.3）；分级证据门槛不硬出（§4.8）；评测以复现为北标（§4.9.4）

## 7. 待深化 / 待试的开放点（v0.5 更新）

> 详细设计进展（DD-01…07 已全部产出，M0 实现依此）：**DD-01 领域对象与事件账本**（`dd-01-domain-events.md`，数据契约）· **DD-02 阶段状态机 + harness**（`dd-02-phase-machine-harness.md`，控制流）· **DD-03 上下文/投影/记忆**（`dd-03-context-memory.md`）· **DD-04 工具契约与权限**（`dd-04-tool-permission.md`，含 stata-mcp 10 工具真实策略映射）· **DD-05 Writer+Validator**（`dd-05-writer-validator.md`，esttab 表语义/图/Table1/Word）· **DD-06 评测 L0–L4**（`dd-06-eval.md`，复现 benchmark）· **DD-07 文献管线+skill 规范**（`dd-07-rag-skills.md`）。§7 多数待深化点已落到这些文档。
> **2026-09-07 Stata 实证审计**：`design/audit-stata-practice.md`。S1（prep/pipeline、env_sig、表语义 locator/figure 卡）已修进 DD-01；S2 门控探索环已修进 DD-02；skill requires 已修进 §4.6；联网/图片 = **新能力方向（§4.10，已记为决策）**，落 DD-03/DD-04。待做：figure 证据接 writer、描述/平衡表模板、esttab 表语义解析器。
> **落实规划**：`design/impl-plan.md`（切片 0–5 + 代码布局 + §5 默认值表 + 切片 0 详单验收）。当前阶段：规划已出、待用户审后开写切片 0。

**v0.4 已关闭**：ledger.jsonl vs SQLite 冲突、thought 持久化、本地隐私 vs DeepSeek 矛盾、双库只靠 collection 隔离、validator 事后校验。
**v0.5 新关闭**：events 可重建字段/reconcile（N1）→ §4.4；恢复后健康探针（N2）→ §4.9.3；工具闭合不变量/半截不执行（N3）→ §4.4/§4.9.3；研究完整性闸门+自由度账本（N5）→ §4.9.2；结果三层契约（N6）→ §4.8；分级证据门槛（N7）→ §4.8；上下文投影层（N8）→ §4.9.1；ResearchState 内容层（N9）→ §4.3.1；Claim/EvidenceCard+ExperimentFamily（N10）→ §4.8；写入权分离（N11）→ §4.8；探索/确认护栏（N12）→ §4.9.5；分支语义+rollback 边界（N13）→ §4.4；writer fence（N14）→ §4.4；阶段 0 内核（N16）→ §5。

**待深化（M0–M1）**：
1. ResearchState / ExperimentFamily / EvidenceCard 的 Pydantic schema 细化（字段、版本、批准链）
2. 阶段状态机细节：迁移协议、blocked/validating 态、失败回退语义、approval 事件 payload
3. events reconcile 算法：fingerprint 去重、终态后不确定事件判定、非法序列拒绝
4. 分支/leaf 语义落地：一次 idea 内多 spec 分支怎么建/存/展示/合并（parent_id 树查询）
5. 事件 schema 演进：upcaster + 快照；单写者事务"事件+投影"原子化；writer lease/fence 实现
6. ModelCapabilityProfile 字段与 provider 契约测试用例集；deepseek tool-call/结构化输出稳定性
7. 中文 PDF 解析（扫描版 OCR 与否）、抽取 QA/错页/双栏/bbox
8. Word 产出链路（pandoc md→docx？python-docx？rtf 表直出？）
9. mock LLM provider replay 协议，保证 harness 确定性单测
10. OTel 埋点规范 + 与 events 稳定 ID 关联；隐私默认不采内容

**评测待做（M2–M3）**：
11. 评测集：≥3 篇异质论文 golden；目标级容差（N/结构/签名 exact）
12. 五层 L0–L4 落地顺序与工具；遮蔽答案/held-out/无计算 baseline 自动化；L4 LLM judge 与专家对齐
13. 检索 gold set：50–100 条 + dense/BM25/hybrid/rerank 消融（Recall@k/MRR/nDCG/证据充分率/错误引用率/无答案拒答率/P95/每问成本）
14. 方法学护栏评测：自由度账本一致性、confirmatory lock 是否拦下"探索当确认"、amendment 链完整
15. 上下文压缩策略具体化：多级投影怎么折叠、何时压缩、从物化视图拉多少（§4.9.1）

**P2 门控（评测证明需要再做）**：多 Agent 文献广搜（判据：独立上下文+清晰输出契约+可隔离副作用）、Qdrant、Temporal/DBOS、自研 Dashboard、双投影完整实现。
