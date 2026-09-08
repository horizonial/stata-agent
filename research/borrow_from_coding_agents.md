# 借鉴引入笔记：从成熟 coding agent（pi/codex/claude-code/claw-code）学什么

> 日期：2026-09-07　目标文档：`design/SPEC.md`　依据语料：`D:\work\learn agent\`（repos/ 下各 agent 源码 + analysis/latex/ 下四份中文学习册 + 一份独立架构论文）。
> **处置（2026-09-07）**：本文 §6 的 B+C 候选**已全并**，SPEC 由 v0.4 升至 **v0.5**（见 SPEC 顶部"修订记录"与 §1.1 速查）。本文降为决策依据存档。
> 读法：本笔记只做"映射 + 建议"，不直接改 SPEC。改动动作集中在文末 §6 的候选清单，逐条评审后决定是否并回。

---

## 0. 一句话结论

SPEC v0.4 的方向**已被语料充分验证**（确定性阶段状态机、events 账本唯一真相、单 agent 先行、审批持久化、写作只消费已批准事实——四份学习册 + 架构论文全都朝这个方向收口）。因此借鉴**不是推倒重来，而是少数几个外科手术式的升级**，集中在一条主线：

> **"控制流状态机"不够，缺"内容层研究状态"与"规范/投影分离"；实证 agent 的本质是"模型想法 ≠ 统计事实"的硬隔离 + 分级证据门槛。**

## 1. 语料清单（谁说了什么）

| 语料 | 文件 | 最值得借鉴的机制 |
|---|---|---|
| 设计模式通用册 | `learn agent/analysis/latex/agent-design-patterns-guide.tex` | 模式五"历史是事实，上下文是投影"、模式十"追加不覆盖"、模式二"状态机"——与 SPEC v0.4 高度同构 |
| codex 学习册 | `.../codex-agent-study-guide.tex` | 受约束状态转移、Turn/StepContext 快照、双投影事件溯源、上下文工程、model 想法 vs 统计事实隔离 |
| claude-code 学习册 | `.../claude-code-study-guide.tex` | 给 StataAgent 的完整设计（ResearchState 内容对象）、规范/投影分离、权限政策流水线 + 研究闸门、Transcript 事件链 |
| claw-code 学习册 | `.../claw-code-study-guide.tex` | StataAgent 最小核心/理想状态转移、GreenContract 分级证据门槛、LaneEvent 序号/fingerprint/reconcile、压缩健康探针 |
| pi 学习册 + 设计分析 | `.../pi-agent-study-guide.tex`、`pi-agent-design-analysis.tex` | 会话树（分支只移 leaf）、canonical vs derived 分离、durable operation、writer lease/fence |
| 独立架构论文（Codex 2026-08-31） | `.../stata-agent-architecture.tex` | ResearchState / ExperimentFamily / Claim-EvidenceCard locator / 探索性-确认性护栏 / 研究者自由度账本（处置见 §5） |
| codex 记忆子系统源码 | `D:\work\learn agent\repos\codex\codex-rs\memories\` | 两阶段自动记忆管线（见 §4，**比学习册新**） |

## 2. 借鉴源确认"我们已做对"的共识（无需动作）

- 确定性阶段状态机 + 单 agent 起步，多 agent 需评测证明（全部四册 + 论文）✓ §4.3
- events 账本 append-only 唯一真相 + 物化视图/投影（pi canonical/derived、claude-code Transcript 可重建、codex rollout、claw LaneEvent）✓ §4.4
- 不存思维链，只沉淀 decision_summary + 证据 ID + 候选/选中/拒绝理由 ✓ §4.4
- 审批 = 持久化 approval 事件、fail-closed ✓ §4.3/§4.9.2
- 权限是策略对象不是布尔；工具结果结构化 + provenance ✓（stata-mcp 已给）
- 先业务投影/隐私过滤再转模型格式、mock 确定性替身做评测 ✓ §4.2/§4.9.4
- 双库隔离（论文的 FACTS/STYLE 命名空间）——我们 style_only/citable/background 类型化更强 ✓ §4.5

## 3. 借鉴项 → SPEC 映射

### B. 需补（小改，建议进 v0.4.x，不重构）

| # | 借鉴项 | 来源 | 动作 / SPEC 落点 |
|---|---|---|---|
| B1 | **events 补齐可重建字段**：序号、来源、置信度、fingerprint、parent_id（分支/因果）、attempt 完整状态；缺了它多来源（Stata 重试/人工批准/报告）会重复或矛盾，"events=唯一真相"就当不成 | claw LaneEvent #18-19；pi 会话树+durable op；claude-code Transcript parent 指针 | §4.4 事件列 + §7 增"reconcile 逻辑"设计项 |
| B2 | **压缩/恢复后健康探针**：恢复 ≠ 读文件回来，恢复态要过最小一致性检查再进 Loop | claw #11；claude-code compaction 定位 | §4.9.3 恢复协议前置 + M0 用例 |
| B3 | **工具闭合不变量**：每个 tool_use 必有带 ID 的结果；取消/拒绝 = 正常 cancelled 结果回模型（blocked 态）；`stopReason=length` 截断的半截参数**不得执行** | claude-code #22-24；pi #11 | §4.4 写入不变量 + §4.9.4 L0 测试用例 |
| B4 | **token 预算 = 每步决策输入**（含 auto-compact 缓冲阈值），不是超限才报错 | codex #10；claude-code #13-14 | §4.3/§4.9.1 上下文工程 |
| B5 | **分层权限补"研究完整性闸门"**：覆盖样本/换 FE/换聚类/删异常值 = 会让结果不可比，须独立于隐私三档单独审批 + 记事件 | claude-code #21 | §4.9.2 加一档 + §4.4 decision |
| B6 | **结果三层出口固定进 EvidenceBundle**：机器可验证层（exit/N/coef/SE/checksum，schema 只作用于这层）/ 模型可读层（摘要/警告）/ 人类附件层（do-file/日志） | codex #5/#12 | §4.8 契约化 |
| B7 | **分级证据门槛（GreenContract 的实证版）**：可行性判断 vs 初稿交付是不同证据等级；"模型说可交付"不成立，要查 do 文件/返回码/结果一致性 | claw #16；codex 显式 Validation 状态 | §4.8 + §4.3 门控退出条件 |
| B8 | **上下文 = 多级投影**：先读 manifest 头 → 候选列表 → 按需读全文；"没选中也合法"；投影不覆盖 canonical | claude-code #15-18；pi #6-7 | §4.9.1 加"投影层"小节 |

### C. 新增-结构性（v0.5，需设计决策）

| # | 借鉴项 | 来源 | 建议落点 / 说明 |
|---|---|---|---|
| C1 | **ResearchState 内容层对象**：sample_definition / variable_roles / identification_assumptions / evidence_refs 独立于阶段机；压缩保"研究状态+证据索引"而非对话摘要 | claude-code #1；codex #11/#24 | §4.3 或新增 §4.3.5。从"流程正确"到"结论可复核"的关键一跃 |
| C2 | **ExperimentFamily + Claim/EvidenceCard locator**：防只记录喜欢的规格；可追溯粒度抬到"页码/表号/行列" | 架构论文 §2；codex 四类对象 | 反事实分支一等对象 + p-hacking 防线；与 EvidenceBundle 分层 |
| C3 | **"模型想法 vs 统计事实"硬隔离**：EvidenceCard 只能由 Validator + Stata 执行事实生成，模型不得直接写 claim | codex #1；claude-code | 强化 §4.8 render_policy 为**写入权分离**（而非仅渲染约束） |
| C4 | **探索性 vs 确认性分离 + confirmatory lock**：看过结果后再改动必须 amendment 记录 | 架构论文；claude-code 研究闸门 | 方法学护栏；面试叙事（方法顾问而非 p-hacker），关联 L4 |
| C5 | **一次 idea 内多 spec 分支语义**：换 spec 即分支（共享不可变过去 + suffix）；rollback 分离"对话/实验上下文"与"文件系统副作用" | pi #4；codex #16-17 | §2/§4.4；这是"反复试 spec"的溯源本质 |
| C6 | **writer lease + revision/fence**：防崩溃恢复后旧进程覆盖新状态 | pi #16 | §4.4 写并发；长跑 Stata 场景的安全底线 |
| C7 | **双投影事件溯源**：events 表存核心事实；运行时投影为 模型 history / 用户 turn / 评测 event 三种视图 | codex #13-14；claude-code 三分 | §4.4 未来演进方向；若嫌重可只做 C5+投影层(B8) |
| C8 | **阶段 0 先做"无 Agent 可信实验内核"**（Spec→Run→manifest→Validator→不可变提交→重跑，无 LLM） | 架构论文 | 与 SPEC M0（带 mock LLM 循环）需取舍——可作 M0 前置的 0.5 步 |

## 4. codex 记忆系统复查（**已更新**，学习册未覆盖）

**现状（repos/codex，HEAD 2026-08-30）**：一套与 SPEC 所引"业界记忆"不同的**两阶段自动记忆管线**——
- 触发：根会话启动、非 ephemeral、非子 agent、Feature 开启时，异步后台 Phase1→Phase2（`memories/write/src/start.rs`）。
- **Phase1 逐 rollout 抽取（可并行，cap 8）**：认领 DB 任务 → 过滤只留记忆相关内容 → 脱敏 → 模型输出 `{raw_memory, rollout_summary, rollout_slug}` 存 DB。
- **Phase2 全局串行合并**：认领全局锁 → 选输入（按 usage_count / last_usage / max_unused_days 裁剪）→ 同步 `raw_memories.md` + `rollout_summaries/` → 在 `~/.codex/memories/` 做 **git 基线 diff** → 有变更则 spawn **consolidation 子 agent**（无审批、无网络、仅本地写记忆根、禁递归/记忆/MCP）→ 产出 `MEMORY.md`/`memory_summary.md`/`skills/<name>/SKILL.md`。
- 读路径：`memory_summary.md`（截断 2500 token）内联进 system prompt；`memories.{list,read,search,add_ad_hoc_note}` 工具；回复末尾带 `<oai-mem-citation>` 引用块供程序解析；按产物类型打 `codex.memories.usage` 遥测。
- 曾经历"drop 后重写"（`0035_drop_memory_tables.sql` → 独立 `state/memory_migrations/0001` 重建 schema）。

**对研究 agent 的启示（截断式借鉴，不能照搬）**：
- ✅ 借鉴：**两阶段解耦吞吐/一致性**、**git-baseline diff 作为"摄入+遗忘"信号**、**引用协议让"用了哪条"可程序化**、按产物类型的用量遥测、强沙箱的 consolidation 子 agent、无变更最低信号门。
- ❌ 不照搬：codex 记忆是**偏好/流程自改进记忆**（会裁剪原始证据、不回原文），而我们要的是**可追溯证据库（源+引用+版本）**；Phase1 抽取要把会话内容交给远端模型，在"文献/数据不出机"的 local_strict 约束下不可行，须本地推理或改走 mixed_sanitized 的脱敏路径（且脱敏后不可用于研究证据，只能用于运维记忆）。
- 落点：§4.9.1 记忆分层里，**证据库**走我们自己的 EvidenceBundle/引用溯源，绝不与 codex 式"偏好记忆"混；若做长期"研究者偏好/项目约定"记忆，可抄 codex 的两阶段+git diff+引用协议骨架。

## 5. 处置 `stata-agent-architecture.tex`

该论文（Codex 署名，2026-08-31）**不是本项目 SPEC 的前身**：它通篇不提 stata-mcp/MCP/deepseek/隐私三档/EvidenceBundle，Stata 执行用自实现进程 Runtime——与 v0.4 已钉死的 MCP 执行路径不同源。

**建议：不合并不作废，留作"领域模型富集源/实现蓝图"。** 执行层一律以 `stata-mcp` 为准（废弃其直接进程物化方案）。其中 7 个候选点已抽到 §3-C1/C2/C4/C6/C8 与 §4，逐条评审即可：
ExperimentFamily · Claim/EvidenceCard locator · canonical/derived 存储 · 研究者自由度账本 · 探索性/确认性护栏 · 16 类事件目录 · 阶段 0 无 Agent 内核。

## 6. SPEC v0.5 候选变更清单（建议，待评审）

**快速项（B 组，改 §4.4/§4.9.x，不动架构）**：B1 events 可重建字段+reconcile · B2 恢复后健康探针 · B3 工具闭合不变量 · B4 token 预算入决策 · B5 研究完整性闸门 · B6 结果三层契约 · B7 分级证据门槛 · B8 上下文投影层。
**结构项（C 组，需设计决策，可做 v0.5 主干）**：C1 ResearchState 内容层 · C2 ExperimentFamily+Claim locator · C3 写入权分离 · C4 探索/确认护栏 · C5 分支语义 · C6 writer fence · C7 双投影 · C8 阶段 0 内核。
**建议推进顺序**：先 B1+B2+B3+B6+B7（低成本、直接提升"可溯源/可恢复"），把 C1+C2+C4 合并成一次"研究状态与证据模型"的 v0.5 设计迭代；C5/C6 随 M0-M2 实现需求触发；C7/C8 延后。

## 7. 未决 / 风险

- B8/C7 的"投影层"是否值得独立建模，取决于 M0 长会话实测是否真爆窗——先做 token 预算(B4)再看。
- C4 confirmatory lock 可能过度限制学者自由；需按"研究闸门"设计成可配置策略而非硬规则。
- 记忆部分 codex 会把原始会话交给远端模型——我们 local_strict 下的长期记忆只能存脱敏摘要或纯本地推理，纳入 memory 设计约束。
- 借鉴源全部是 coding agent；"研究完整性/方法学护栏"（C2/C4）无成熟先例，是本项目的原创差异化空间，也意味着无参照，需自己定评测。
