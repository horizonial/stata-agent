**从'能跑 Stata'到'可审计的实证研究代理'**

工业级架构调研、SPEC 修订建议与面试答辩手册

Codex Research

2026-09-03

目录

> [0. 一页结论：这套方案应该怎么定 [3](#一页结论这套方案应该怎么定)](#一页结论这套方案应该怎么定)
>
> [推荐的目标架构 [4](#推荐的目标架构)](#推荐的目标架构)
>
> [1. 子系统一：Agent 编排与工作流 [5](#子系统一agent-编排与工作流)](#子系统一agent-编排与工作流)
>
> [A. 成熟方案与证据 [5](#a.-成熟方案与证据)](#a.-成熟方案与证据)
>
> [B. 对本项目的差异化判断与 SPEC 修改 [5](#b.-对本项目的差异化判断与-spec-修改)](#b.-对本项目的差异化判断与-spec-修改)
>
> [C. 面试弹药 [6](#c.-面试弹药)](#c.-面试弹药)
>
> [2. 子系统二：记忆、状态与事件溯源 [6](#子系统二记忆状态与事件溯源)](#子系统二记忆状态与事件溯源)
>
> [A. 成熟方案与证据 [6](#a.-成熟方案与证据-1)](#a.-成熟方案与证据-1)
>
> [B. 对本项目的差异化判断与 SPEC 修改 [6](#b.-对本项目的差异化判断与-spec-修改-1)](#b.-对本项目的差异化判断与-spec-修改-1)
>
> [C. 面试弹药 [7](#c.-面试弹药-1)](#c.-面试弹药-1)
>
> [3. 子系统三：类型化协议、模型适配与工具层 [8](#子系统三类型化协议模型适配与工具层)](#子系统三类型化协议模型适配与工具层)
>
> [A. 成熟方案与证据 [8](#a.-成熟方案与证据-2)](#a.-成熟方案与证据-2)
>
> [B. 对本项目的差异化判断与 SPEC 修改 [8](#b.-对本项目的差异化判断与-spec-修改-2)](#b.-对本项目的差异化判断与-spec-修改-2)
>
> [C. 面试弹药 [8](#c.-面试弹药-2)](#c.-面试弹药-2)
>
> [4. 子系统四：文献 RAG、双库隔离与引用溯源 [9](#子系统四文献-rag双库隔离与引用溯源)](#子系统四文献-rag双库隔离与引用溯源)
>
> [A. 成熟方案与证据 [9](#a.-成熟方案与证据-3)](#a.-成熟方案与证据-3)
>
> [B. 对本项目的差异化判断与 SPEC 修改 [9](#b.-对本项目的差异化判断与-spec-修改-3)](#b.-对本项目的差异化判断与-spec-修改-3)
>
> [C. 面试弹药 [10](#c.-面试弹药-3)](#c.-面试弹药-3)
>
> [5. 子系统五：安全、权限与提示词注入防护 [10](#子系统五安全权限与提示词注入防护)](#子系统五安全权限与提示词注入防护)
>
> [A. 成熟方案与证据 [10](#a.-成熟方案与证据-4)](#a.-成熟方案与证据-4)
>
> [B. 对本项目的差异化判断与 SPEC 修改 [10](#b.-对本项目的差异化判断与-spec-修改-4)](#b.-对本项目的差异化判断与-spec-修改-4)
>
> [C. 面试弹药 [11](#c.-面试弹药-4)](#c.-面试弹药-4)
>
> [6. 子系统六：可观测性与可审计性 [12](#子系统六可观测性与可审计性)](#子系统六可观测性与可审计性)
>
> [A. 成熟方案与证据 [12](#a.-成熟方案与证据-5)](#a.-成熟方案与证据-5)
>
> [B. 对本项目的差异化判断与 SPEC 修改 [12](#b.-对本项目的差异化判断与-spec-修改-5)](#b.-对本项目的差异化判断与-spec-修改-5)
>
> [C. 面试弹药 [12](#c.-面试弹药-5)](#c.-面试弹药-5)
>
> [7. 子系统七：可靠性、重试、幂等与恢复 [13](#子系统七可靠性重试幂等与恢复)](#子系统七可靠性重试幂等与恢复)
>
> [A. 成熟方案与证据 [13](#a.-成熟方案与证据-6)](#a.-成熟方案与证据-6)
>
> [B. 对本项目的差异化判断与 SPEC 修改 [13](#b.-对本项目的差异化判断与-spec-修改-6)](#b.-对本项目的差异化判断与-spec-修改-6)
>
> [C. 面试弹药 [13](#c.-面试弹药-6)](#c.-面试弹药-6)
>
> [8. 子系统八：评测、复现基准与 Ground Truth [14](#子系统八评测复现基准与-ground-truth)](#子系统八评测复现基准与-ground-truth)
>
> [A. 成熟方案与证据 [14](#a.-成熟方案与证据-7)](#a.-成熟方案与证据-7)
>
> [B. 对本项目的差异化判断与 SPEC 修改 [14](#b.-对本项目的差异化判断与-spec-修改-7)](#b.-对本项目的差异化判断与-spec-修改-7)
>
> [C. 面试弹药 [15](#c.-面试弹药-7)](#c.-面试弹药-7)
>
> [9. 真正可讲的差异化：哪些成立，哪些不要夸大 [15](#真正可讲的差异化哪些成立哪些不要夸大)](#真正可讲的差异化哪些成立哪些不要夸大)
>
> [9.1 强差异化 [15](#强差异化)](#强差异化)
>
> [9.2 有价值但不独特 [16](#有价值但不独特)](#有价值但不独特)
>
> [9.3 推荐的一句话定位 [16](#推荐的一句话定位)](#推荐的一句话定位)
>
> [10. SPEC v0.4 建议变更清单 [16](#spec-v0.4-建议变更清单)](#spec-v0.4-建议变更清单)
>
> [P0：写进架构基线后再开发 [16](#p0写进架构基线后再开发)](#p0写进架构基线后再开发)
>
> [P1：M0--M1 必须完成 [16](#p1m0m1-必须完成)](#p1m0m1-必须完成)
>
> [P2：评测证明需要后再做 [17](#p2评测证明需要后再做)](#p2评测证明需要后再做)
>
> [11. 五分钟架构叙事 [17](#五分钟架构叙事)](#五分钟架构叙事)
>
> [12. 高频追问清单 [17](#高频追问清单)](#高频追问清单)
>
> [13. 尚未关闭的风险 [18](#尚未关闭的风险)](#尚未关闭的风险)
>
> [14. 主要来源 [19](#主要来源)](#主要来源)
>
> [15. 研究边界 [20](#研究边界)](#研究边界)

**评审对象：** C:\\Users\\user\\stata-agent\\SPEC.md v0.3\
**既有执行底座：** C:\\Users\\user\\stata-mcp（10 个 MCP 工具）\
**研究日期：** 2026-09-03\
**结论性质：** 架构研究与规格评审，不包含代码实现

## 0. 一页结论：这套方案应该怎么定

这套系统最值得做的，不是"让大模型会写 Stata"，而是把一次实证研究变成**可恢复、可复核、可追责的证据生产流程**。建议把产品定义成：

一个以研究问题为中心、以 Stata 结果为数值真相、以文献原文为引用真相、以事件台账为过程真相的研究代理。

核心架构建议如下。

  -----------------------------------------------------------------------------------------------------------------------------
  **议题**                **v0.3 方向**                    **建议定案**
  ----------------------- -------------------------------- --------------------------------------------------------------------
  编排                    单 Agent 循环                    保留单 Agent；外层增加确定性阶段状态机，阶段内才允许模型循环

  多 Agent                预留                             M0--M3 不启用；仅在独立并行检索或上下文隔离经评测证明有收益时启用

  协议层                  PydanticAI                       保留，但定位为类型化模型/工具协议层，不让它垄断业务状态和恢复语义

  模型                    DeepSeek 起步                    改为能力注册表 + 契约测试；模型名只是配置，不能进入业务逻辑

  隐私                    数据/embedding 本地 + DeepSeek   当前存在冲突；新增三档隐私模式，禁止 fallback 跨越数据出境边界

  状态真相                SQLite append-only               保留；事件表是历史真相，物化视图是当前状态；ledger.jsonl 只做导出

  记忆                    短期 + 长期混合                  明确分为线程状态、项目记忆、证据库；禁止保存原始思维链

  RAG                     向量 + grep/BM25                 保留；先做抽取、元数据、引用定位和检索评测，数据库换型不是首要问题

  两套文献库              风格库 + 项目库                  保留，但建立硬隔离：风格库只能影响表达，不得成为可引用证据

  数值接地                Validator 校验                   提升为"先生成证据包、后渲染文本"；正文数字必须引用 result_id

  可观测性                日志/trace                       业务事件与遥测分离；SQLite 是事实账本，OpenTelemetry 是观测出口

  可靠性                  retry/resume                     按错误类别重试；副作用使用幂等键；恢复到稳定阶段而非简单续写对话

  评测                    1--2 篇论文复现                  至少 3 篇异质设计；分层评测、盲测、专家容差、污染基线和成本指标
  -----------------------------------------------------------------------------------------------------------------------------

### 推荐的目标架构

    研究者
      │  提问 / 确认 / 否决
      ▼
    确定性阶段状态机
      ├─ 立题 ─ 文献 ─ 设计 ─ 数据 ─ 估计 ─ 稳健性 ─ 写作 ─ 验证
      │                   每个阶段有进入条件、退出条件、人工门和失败状态
      ▼
    受约束的单 Agent 循环（PydanticAI）
      ├─ 模型能力适配器（DeepSeek / 其他提供商）
      ├─ 工具策略与审批器
      ├─ 证据包构建器
      └─ 技能包路由器
      │
      ├──────── 文献证据层：抽取 → 混合检索 → 重排 → claim-to-span 引用
      │
      └──────── 数值证据层：现有 stata-mcp → 结构化结果 + provenance
                             │
                             ▼
    SQLite 事件账本 + 物化视图 + 结果台账
      │
      ├─ Word 渲染器（只渲染已验证证据）
      ├─ Validator（数字、引用、样本、模型、可复现性）
      └─ OpenTelemetry → 本地观测后端（可选 Langfuse / Logfire / LangSmith）

这是一种"**确定性骨架 + 概率性推理**"的混合架构：把状态迁移、权限、证据引用和副作用做成软件规则；把选题、解释、候选方案生成等开放问题留给模型。

## 1. 子系统一：Agent 编排与工作流

### A. 成熟方案与证据

业界已形成三类编排方式：顺序/分支工作流、单 Agent 工具循环、多 Agent 协作。Anthropic 把 workflow 定义为预先确定的代码路径，把 agent 定义为由模型动态决定过程；其工程经验是从最简单方案起步，因为复杂度会直接增加延迟、成本和错误面。OpenAI Agents SDK 同样区分"handoff"与"agent-as-tool"，并建议只有在指令、工具、策略或上下文隔离确有差异时才拆分代理。；[Anthropic：Building Effective Agents](https://www.anthropic.com/engineering/building-effective-agents)[OpenAI：Multi-agent orchestration](https://developers.openai.com/api/docs/guides/agents/orchestration)

LangGraph 的成熟价值主要是检查点、持久化、人机中断和故障恢复，而不是"图看起来更高级"。它适合长流程和显式状态迁移；如果当前只服务一位研究者和一个活跃 idea，直接引入完整图运行时不一定划算。；[LangGraph overview](https://docs.langchain.com/oss/python/langgraph/overview)[LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence)

多 Agent 在广度优先、子任务相互独立的研究搜索中可能明显增益；Anthropic 报告其多 Agent 研究系统在内部评测上优于单 Agent 90.2%，但 token 消耗约为普通对话的 15 倍，并指出共享上下文和强依赖任务并不适合多 Agent。[Anthropic：Multi-agent research system](https://www.anthropic.com/engineering/multi-agent-research-system)

### B. 对本项目的差异化判断与 SPEC 修改

"单 Agent 起步"是正确选择，但 SPEC 需要把单 Agent 从"无限 while-loop"改成**阶段状态机中的受约束循环**。建议加入：

- 固定研究阶段：IDEA → LITERATURE → DESIGN → DATA → ESTIMATION → ROBUSTNESS → WRITING → VALIDATION → DONE。

- 每个阶段声明 entry_conditions、allowed_tools、exit_schema、human_gate、max_attempts、failure_policy。

- Agent 可以在阶段内提出多个候选动作，但只有编排器可以提交状态迁移。

- 人工确认不是聊天提示，而是持久化的 approval_requested / approved / rejected 事件。

- 多 Agent 的启用条件写入 ADR：独立并行检索、不同权限边界、或需要隔离上下文；其他情况继续单 Agent。

项目的独特性不在"用了哪一种 Agent 框架"，而在于**研究决策被显式建模**：为什么选择双向固定效应、为什么拒绝另一种标准误、哪一次稳健性检验改变了结论。这些决策应该成为可查询的对象，而不是散落在聊天记录里。

### C. 面试弹药

**问题 1：为什么不直接上多 Agent？**\
口语回答：我先看任务的耦合度。实证研究的选题、数据、估计和写作共享同一套假设与样本定义，拆成多个 Agent 会增加上下文同步和责任归属问题。我的 v1 用确定性阶段机加单 Agent；只有文献广搜这种可独立并行任务，或写作与验证需要权限隔离时，才引入多 Agent。这样能够用评测证明复杂度的回报。\
一句话：**先用状态机管住过程，再让模型在局部自由。**\
追问挑战：如果两个步骤可以并行，你如何防止它们基于不同样本口径？答：所有 worker 只读同一个版本化 ResearchSpec，输出带 spec_version，合并时版本不一致直接拒绝。

**问题 2：为什么不用纯 LangGraph？**\
口语回答：LangGraph 的检查点和人机中断很成熟，但我当前是单机、单研究者、SQLite 已经承担业务事件和恢复。我的业务状态不能绑死在框架 checkpoint 里，所以先把状态迁移协议独立出来；未来流程需要长时间等待或分布式 worker 时，再把执行器替换为 LangGraph、DBOS 或 Temporal。\
一句话：**框架是执行器，研究状态才是产品资产。**

## 2. 子系统二：记忆、状态与事件溯源

### A. 成熟方案与证据

成熟 Agent 记忆通常区分短期线程状态与长期命名空间记忆。LangGraph 指出，长上下文本身会造成性能下降和陈旧信息干扰，因此记忆不是"把所有对话都塞进去"，而是选择性保存、压缩和检索。[LangGraph memory](https://docs.langchain.com/oss/python/concepts/memory)

事件溯源把追加事件作为事实记录，并通过投影得到当前状态，天然支持审计、回放和时间旅行；代价是事件模式演进、并发、查询与重建复杂度。微软的架构说明也强调，实际系统通常需要物化视图，而不是每次从头重放事件。[Microsoft：Event Sourcing pattern](https://learn.microsoft.com/en-us/azure/architecture/patterns/event-sourcing)

SQLite WAL 允许读写并发，但同一时刻仍只有一个 writer，且不适合网络文件系统。对于本项目"单机、一个活跃 idea"的阶段非常合适；一旦出现多进程写入，应增加单写者队列或迁移存储，而不是误认为 WAL 等于无限并发。[SQLite WAL](https://www.sqlite.org/wal.html)

### B. 对本项目的差异化判断与 SPEC 修改

SPEC 中"SQLite append-only 是唯一真相"与目录中的 ledger.jsonl 冲突。建议明确：

- events 表：唯一的历史事实源，append-only。

- idea_state / current_phase / result_index / citation_index：可重建的物化视图。

- ledger.jsonl：只作为导出、调试或兼容格式，不参与恢复决策。

- 事件至少包含 event_id、idea_id、phase、event_type、schema_version、causation_id、correlation_id、actor、payload、created_at。

- 增加事件 upcaster 和 snapshot；恢复时从最近快照回放，而不是扫描全部历史。

- 通过单写者事务把"事件追加 + 投影更新"原子化。

必须删除或重命名 SPEC 中的 thought 字段。系统不应持久化模型原始思维链；应保存可审计的 decision_summary、输入证据 ID、候选方案、选中方案和拒绝理由。短期上下文也不等于长期记忆：

  ----------------------------------------------------------------------------------------------------
  **层次**          **内容**                                   **生命周期**       **是否可作为证据**
  ----------------- ------------------------------------------ ------------------ --------------------
  线程状态          当前阶段、待办、最近工具结果摘要           一次运行           否

  项目记忆          已确认定义、偏好、变量映射、否决记录       idea 生命周期      仅作约束

  证据库            文献原文片段、Stata 结构化结果、数据签名   可版本化长期保存   是

  遥测              token、延迟、错误、trace                   运维保留期         否
  ----------------------------------------------------------------------------------------------------

### C. 面试弹药

**问题 1：为什么 event log 不能直接等于 memory？**\
口语回答：event log 记录发生过什么，memory 负责下一步应该看到什么。事件是不可变事实，记忆是带策略的投影；我会从事件重建当前状态，但不会把整个日志塞进 prompt。\
一句话：**日志负责真实，记忆负责相关。**

**问题 2：SQLite 会不会不工业？**\
口语回答：工业不等于先上分布式数据库。当前是一位研究者、单机和有限写并发，SQLite 的事务、WAL、备份和可移植性更匹配；我会显式记录单 writer 假设和迁移触发条件，例如多用户、远程访问、持续写竞争或审计保留要求上升。\
一句话：**用约束清楚的简单存储，比用错场景的重型存储更工业。**

## 3. 子系统三：类型化协议、模型适配与工具层

### A. 成熟方案与证据

PydanticAI 支持工具输出、原生结构化输出和提示词结构化输出，并允许输出校验器触发重试。官方文档也指出，prompted JSON 是最不可靠的模式；工具调用、输出校验、模型传输和 fallback 应按不同层分别处理。；[PydanticAI：Output](https://ai.pydantic.dev/output/)[PydanticAI：Retries](https://ai.pydantic.dev/retries/)

DeepSeek 的工具调用和 JSON Schema 能力可用，但官方 Responses API 仍提醒工具参数可能无效或幻觉，因此必须在应用侧校验。严格模式还存在接口版本和 schema 子集约束，不能把"提供商支持 strict"理解为业务数据一定正确。；[DeepSeek：Tool Calls](https://api-docs.deepseek.com/guides/tool_calls/)[DeepSeek：Responses API](https://api-docs.deepseek.com/api/create-response/)

现有 stata-mcp 已经提供 10 个工具、通用 e-class 结果提取、结构化错误和 command_hash / data_signature / exec_seq / do_file。上层最有价值的工作是定义输入/输出契约、权限与证据流，而不是重写 Stata 执行。

### B. 对本项目的差异化判断与 SPEC 修改

保留 PydanticAI，但把它限定在四件事：模型调用、工具 schema、输出模型、局部重试。新增独立的 ModelCapabilityProfile：

    provider/model/version
    supports_tools / strict_schema / thinking_with_tools / streaming
    max_context / max_output / json_schema_subset
    known_quirks / tested_at / contract_test_version

模型切换不能只测"能回答"，而要跑契约测试：空参数、非法枚举、并行工具调用、超长 schema、流式中断、工具结果回填、重试后重复副作用。锁定模型版本或至少记录提供商返回的实际模型 ID和 request ID；模型升级必须触发回归评测。[OpenAI：API overview](https://developers.openai.com/api/reference/overview)

建议增加一个最关键的中间对象 EvidenceBundle：

- numeric_claims\[\]：值、单位、展示精度、result_id、data_signature、command_hash、表格坐标。

- citation_claims\[\]：主张、chunk_id、页码/段落、原文 span、文献版本。

- research_decisions\[\]：研究假设、规格版本、人工批准记录。

- render_policy：正文只能引用已验证 claim ID；模型不得直接注入新数字或新引文。

### C. 面试弹药

**问题 1：结构化输出已经有 schema，为什么还要 validator？**\
口语回答：schema 只能保证"形状正确"，不能保证数字来自真实回归、引用支持当前主张。我的 validator 还会检查 result_id、数据签名、命令哈希、引用 span 和展示精度。\
一句话：**类型安全解决格式，证据安全解决事实。**

**问题 2：怎么支持 DeepSeek 之外的模型？**\
口语回答：我不把差异散落在 prompt 里，而是做能力画像和契约测试。编排器只依赖能力，例如"支持工具调用且通过严格输出测试"，模型只是满足能力的一个实现。\
一句话：**按能力编程，不按模型名字编程。**

## 4. 子系统四：文献 RAG、双库隔离与引用溯源

### A. 成熟方案与证据

混合检索是成熟选择：embedding 擅长语义相似，BM25 擅长精确术语、变量名和引文。Anthropic 的 Contextual Retrieval 把 chunk 上下文、BM25、向量检索和 rerank 组合，在其测试中显著降低检索失败率；但其收益依赖语料，必须用自己的问题集复测。[Anthropic：Contextual Retrieval](https://www.anthropic.com/engineering/contextual-retrieval)

BGE-M3 支持 100 多种语言、dense/sparse/multi-vector 和最长 8192 token，是中英文混合学术语料的合理基线，但它是 2024 年模型，必须通过本项目数据集与更新候选比较，而不是写死为长期最优。[BGE-M3 paper](https://arxiv.org/abs/2402.03216)

Chroma 的单机能力足以覆盖数百到数千篇论文，真正限制通常是 HNSW 内存和部署方式。其 PersistentClient 更偏本地开发/测试；若需要服务隔离、多用户、运维指标或复杂过滤，再考虑 server 模式或 Qdrant。换库触发条件应由延迟、召回率、并发和运维要求决定，而不是笼统的"文档数量"。；[Chroma performance](https://docs.trychroma.com/guides/deploy/performance)[Qdrant quickstart](https://qdrant.tech/documentation/quick-start/)

### B. 对本项目的差异化判断与 SPEC 修改

双库设计有价值，但"风格库"和"项目证据库"不能只靠不同 collection 名称。建议建立强类型来源角色：

- style_only：顶刊表达、段落结构、论证节奏；不得产生引用。

- citable_evidence：项目论文与权威资料；可以进入 claim-to-span 引用。

- background_only：用于理解但不允许在最终稿中直接引用。

摄取链要版本化：file_hash → parser_version → page/section/bbox → chunk_id → embedding_version → index_version。每个引用保存原文 span、页码和文献版本；PDF 更新后旧 chunk 不覆盖，而是失效并保留历史。

M1 的正确优先级不是"先调向量库"，而是：

1.  PDF/HTML 抽取质量与表格、脚注、双栏顺序。

2.  元数据和稳定 chunk ID。

3.  50--100 个真实研究问题的检索 gold set。

4.  dense、BM25、hybrid、rerank 的消融对比。

5.  最后才是数据库和 embedding 换型。

建议指标同时看 Recall@k、MRR/nDCG、答案证据充分率、错误引用率、无答案拒答率、P95 延迟和每问成本。

### C. 面试弹药

**问题 1：为什么同时用向量和 BM25？**\
口语回答：实证研究既有概念性查询，也有变量名、估计命令和论文标题这种精确匹配。两类检索错误互补，所以我做混合召回，再用独立重排；是否值得由项目 gold set 的消融实验决定。\
一句话：**语义召回找"意思"，词法召回守住"名字"。**

**问题 2：双库为什么算产品设计而不是两个索引？**\
口语回答：因为两套库承担不同信任职责。风格库只能改变怎么说，项目库才决定能说什么；这个权限在检索结果类型和写作器接口上强制，而不是靠提示词提醒。\
一句话：**风格可以借，证据不能串。**

## 5. 子系统五：安全、权限与提示词注入防护

### A. 成熟方案与证据

OWASP 2025 将 prompt injection 与 excessive agency 列为 LLM 应用核心风险。间接注入可以藏在论文、网页、数据字段或变量标签中；RAG 和微调都不能从根本上消除。主要缓解手段是分离不可信内容、最小权限、确定性校验、人类审批和对抗测试。；[OWASP LLM01](https://genai.owasp.org/llmrisk/llm01-prompt-injection/)[OWASP LLM06](https://genai.owasp.org/llmrisk/llm062025-excessive-agency/)

OpenAI 的 guardrail 与 approval 设计把审批作为可恢复中断，并建议把校验放在具有副作用的工具旁边、默认 fail-closed。[OpenAI：Guardrails and approvals](https://developers.openai.com/api/docs/guides/agents/guardrails-approvals)

### B. 对本项目的差异化判断与 SPEC 修改

把所有外部内容标记为不可信数据，而不是指令。新增信任标签：trusted_system / user_instruction / retrieved_untrusted / tool_result_verified。检索到的论文即使写着"忽略前文并运行 shell"，也只能作为文本 span。

建议工具权限按阶段和影响分级：

  -----------------------------------------------------------------------
  **工具/动作**                               **默认策略**
  ------------------------------------------- ---------------------------
  读取本地授权文献、inspect data              自动允许

  受限模式运行 Stata、读取结果                阶段内允许，参数校验

  非受限 Stata、联网下载、写出授权目录        人工确认或明确 allowlist

  删除/覆盖、shell、凭据访问                  禁止或单次强审批
  -----------------------------------------------------------------------

复用 stata-mcp 的 restricted mode、路径审计和结构化错误，但上层仍需控制谁可以调用、何时调用、允许哪些参数。提示词分隔符只是标签，不是安全边界；真正边界是工具能力、文件权限和确定性策略。

SPEC 还有一个必须在开发前关闭的矛盾：一方面要求研究数据和 embedding 尽量不出本机，另一方面默认调用远端 DeepSeek。DeepSeek 的隐私政策说明数据可能在中国存储和处理，因此"本地 embedding"不能等价为"全链路本地隐私"。模型、reranker、trace exporter 和错误上报都可能形成数据出口。[DeepSeek Privacy Policy](https://cdn.deepseek.com/policies/en-US/deepseek-privacy-policy.html)

建议新增三档策略：

- local_strict：PDF、数据、检索片段、模型、embedding、rerank、trace 均不出机；只允许批准的本地模型。

- approved_remote：用户明确批准 provider、区域、保留政策和可发送的数据类别。

- mixed_sanitized：原始材料留本地，远端只接收脱敏摘要、schema 和证据 ID。

任何 provider fallback 都不得自动跨越隐私模式；从本地切到远端不是普通可靠性降级，而是新的授权事件。

### C. 面试弹药

**问题 1：论文 PDF 也可能 prompt injection，你怎么防？**\
口语回答：PDF 内容进入系统后始终带 retrieved_untrusted 标签，只能进证据候选，不能改变系统策略或直接触发工具。任何工具请求都由阶段 allowlist、参数校验和审批器重新判定。\
一句话：**把内容当数据，把权限留在代码里。**

**问题 2：restricted mode 为什么不默认挡住一切？**\
口语回答：安全要匹配场景。可信研究者的自有 do-file 与陌生论文附带代码风险不同；我按来源和阶段选择模式，高风险内容默认受限，解除限制需要显式审批并记录事件。\
一句话：**权限不是开关，是带来源和时效的能力。**

**问题 3：既要本地隐私又用 DeepSeek，是否自相矛盾？**\
口语回答：如果是远端 API，确实冲突，所以我不会用一句"本地 embedding"掩盖它。我把隐私做成运行模式；严格本地模式禁止远端 fallback，允许远端时必须先脱敏并记录用户批准、provider 和发送字段。\
一句话：**模型 fallback 不能偷偷变成数据出境策略。**

## 6. 子系统六：可观测性与可审计性

### A. 成熟方案与证据

OpenTelemetry 已开始定义生成式 AI 和 Agent 的语义约定，包括 invoke_agent、execute_tool、planning、retrieval 和 memory 操作；这些约定仍在演进，因此内部事件模型不应直接等同于某个观测厂商的数据模型。；[OpenTelemetry semantic conventions](https://opentelemetry.io/docs/specs/semconv/)[GenAI agent spans](https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/gen-ai-agent-spans.md)

OpenAI Agents SDK 默认记录模型、工具、handoff 和 guardrail 等 span。PydanticAI 也能发出 OpenTelemetry spans，并可接入任意兼容后端。Langfuse、LangSmith、Braintrust 均提供 trace、dataset 和 evaluator 能力，适合复用，而不是先开发自己的观测 UI。[OpenAI：Observability](https://developers.openai.com/api/docs/guides/agents/integrations-observability)；；[PydanticAI：Logfire/OTel](https://ai.pydantic.dev/logfire/)[Langfuse offline evaluation](https://langfuse.com/docs/evaluation/get-started/offline)

### B. 对本项目的差异化判断与 SPEC 修改

明确"两本账"：

- SQLite 业务账本回答：做了什么研究决定、用哪份数据、哪个结果进入正文。

- OpenTelemetry 遥测回答：哪一步慢、贵、失败、重试或发生权限拒绝。

二者用稳定 ID 关联，但不互为唯一真相。每个 span 至少带 trace_id / idea_id / phase / run_id / attempt_id / event_id / tool_name / model_id / latency / token / cost / status / error_class / privacy_class。结果 span 再带 result_id / command_hash / data_signature。

默认不记录完整 prompt、论文全文、数据行或原始模型思维；采用摘要、哈希、采样和可配置保留期。调试时临时开启内容捕获必须有审计记录。

### C. 面试弹药

**问题 1：SQLite trace 和 Langfuse 有什么区别？**\
口语回答：SQLite 保存产品事实和恢复状态，Langfuse 或其他 OTel 后端用于性能与质量观测。trace 可以按保留期删除，研究结论的 provenance 不能因此消失。\
一句话：**业务账本保证可追责，遥测系统帮助可运营。**

**问题 2：为什么不先做自定义 Dashboard？**\
口语回答：早期最重要的是埋点语义和可查询 ID，不是界面。先用 OTel 接现成后端验证指标，等用户真正需要跨项目对比和审计工作台，再把稳定查询做成 UI。\
一句话：**先标准化事件，再产品化视图。**

## 7. 子系统七：可靠性、重试、幂等与恢复

### A. 成熟方案与证据

指数退避加 jitter 是处理瞬时故障的成熟模式，但重试必须有上限并选择单一责任层，否则多层叠加会形成 retry storm。AWS 的架构建议明确要求区分可重试和不可重试故障。；[AWS：Backoff and jitter](https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/)[AWS：Limit retries](https://docs.aws.amazon.com/wellarchitected/2023-04-10/framework/rel_mitigate_interaction_failure_limit_retries.html)

PydanticAI 可与 DBOS、Temporal、Prefect、Restate 等 durable execution 后端结合；其 DBOS 文档也提醒框架重试与模型重试叠加会过度执行。LangGraph 在恢复时可能从节点开头重跑，因此带副作用的动作仍必须幂等。；[PydanticAI durable execution](https://ai.pydantic.dev/durable_execution/overview/)[PydanticAI DBOS](https://ai.pydantic.dev/durable_execution/dbos/)

### B. 对本项目的差异化判断与 SPEC 修改

不要把"有 event log"写成"就能 exactly-once"。建议在每个可重放动作上增加：

- operation_id：逻辑动作 ID。

- attempt_id：每次尝试 ID。

- semantic_input_hash：规格版本、数据版本和参数的稳定哈希。

- side_effect_state：prepared / executing / committed / failed / uncertain。

- retry_class：transient / validation / policy / data / model_semantic / unknown。

- reconcile_action：查询现状、补偿或人工确认。

Stata MCP 已有 session 自愈和命令历史；上层恢复策略应是：读取最后稳定阶段 → 检查未决副作用 → 验证数据签名和结果台账 → 决定复用、重跑或人工处理。模型 schema 错误可以局部重试；引用不支持主张应返回检索/写作阶段；样本定义冲突不能靠"再问一次模型"解决。

M0--M3 不需要立即引入 Temporal/DBOS。迁移触发条件是：流程跨天等待、多人审批、分布式 worker、外部不可幂等副作用或对运行中部署恢复有明确 SLA。

### C. 面试弹药

**问题 1：如何保证 Stata 不会重复跑并污染结果？**\
口语回答：读取类命令可以重试；会修改数据或写文件的命令必须带 operation_id 和语义输入哈希。恢复时先查结果台账是否已有同一输入的 committed 结果，再决定复用或执行，不能把超时等同于失败。\
一句话：**超时是"不知道成功没"，不是"肯定没成功"。**

**问题 2：为什么不用统一重试三次？**\
口语回答：网络 429、schema 校验失败、样本缺失和权限拒绝的处理完全不同。统一重试会放大成本和副作用；我按错误分类决定 backoff、修复提示、回退阶段或人工接管。\
一句话：**先分类故障，再决定重试。**

## 8. 子系统八：评测、复现基准与 Ground Truth

### A. 成熟方案与证据

OpenAI 的 Agent evals 建议先做 trace grading，再建立数据集和可重复评测；grader 可以分别检查工具名和参数，数值/代码规则优先，只有代码不足以判断的语义质量才使用 LLM judge，并用人工样例校准。；[OpenAI：Agent evals](https://developers.openai.com/api/docs/guides/agent-evals)[OpenAI：Graders](https://developers.openai.com/api/docs/guides/graders)

PaperBench 用 20 篇 ICML 论文和 8,316 个可评分任务测试论文复现，并让论文作者参与 rubric 设计；ReplicationBench 用 19 篇可复现论文、107 个任务、作者提交容差和代码测试评估科学复现。它们证明"论文复现 benchmark"并非空白，也表明任务拆解、专家容差、代码判分和裁判校准缺一不可。；[OpenAI：PaperBench](https://openai.com/index/paperbench/)[ReplicationBench paper](https://arxiv.org/abs/2510.24591)

### B. 对本项目的差异化判断与 SPEC 修改

本项目不要宣称"首个论文复现 Agent"；更可信的差异化表述是：**面向实证社会科学/Stata 工作流，把表格单元格、估计规格和来源追溯做成可执行评分协议**。

建议建立五层评测：

  -------------------------------------------------------------------------------------------------
  **层级**                **评什么**                                **判分方法**
  ----------------------- ----------------------------------------- -------------------------------
  L0 契约                 schema、ID、权限、事件一致性              确定性测试

  L1 组件                 检索、变量映射、工具参数、技能触发        gold set + exact/graded match

  L2 轨迹                 是否走对阶段、是否越权、是否有无效循环    trace grader + 规则

  L3 结果                 论文表格与图、样本、系数、SE、N、显著性   单元格级数值评分

  L4 研究质量             解释是否被证据支持、局限是否充分          盲评专家 + 校准过的 LLM judge
  -------------------------------------------------------------------------------------------------

M3 不应只复现 1--2 篇论文，建议至少 3 篇且设计异质：面板固定效应、工具变量/事件研究、离散选择或其他模型各一。每篇建立：

- 目标表格单元格、样本筛选、变量变换、估计器、固定效应、聚类、权重、缺失处理。

- 对每个指标单独定义绝对/相对容差；N、模型结构和数据签名通常应 exact match。

- 主系数符号、点估计、SE/CI、p 值档位、R² 定义分别计分，避免"一个平均误差掩盖结构性错误"。

- 至少三次独立运行，报告 pass@1、均值/方差、最差值、时间、token 和费用。

防止基准泄漏与投机：遮蔽论文最终数字；盲测层不提供原始 do-file；保留无计算 baseline；使用 held-out 论文；所有输出必须回链到工具结果。人工容差应由领域专家基于可重复性和数值非确定性制定，而不是一个全局 epsilon。

对抗集至少包含：小数点/符号篡改、真实引文支持错误主张、虚假引文、PDF prompt injection、恶意变量标签、陈旧记忆、工具结果篡改、重复回放和超时后不确定状态。

### C. 面试弹药

**问题 1：怎么证明 Agent 真的会做研究，不是在抄答案？**\
口语回答：我把最终数字遮蔽，盲测层不提供原始 do-file，再设置不调用计算工具的 baseline。评分不仅看系数，还看样本、变量变换、估计器、固定效应和聚类是否一致；每个正文数字必须回链到本次工具结果。\
一句话：**评"怎么得到"，不只评"像不像答案"。**

**问题 2：为什么 LLM judge 不能做主裁判？**\
口语回答：数值、表格坐标、引用存在性和工具参数都有确定答案，应该用代码判。LLM judge 只评价解释质量等开放维度，而且要先与盲评专家样本对齐，测误报和漏报。\
一句话：**能确定性判分的地方，不把裁判权交给另一个模型。**

**问题 3：容差怎么定？**\
口语回答：容差是目标级的。样本量和规格结构通常必须精确；迭代算法或随机过程的系数由专家根据重复运行分布定绝对或相对容差。报告同时保留原始值和展示四舍五入值。\
一句话：**容差来自方法和重复性，不来自拍脑袋。**

## 9. 真正可讲的差异化：哪些成立，哪些不要夸大

### 9.1 强差异化

1.  **数字来源闭环。** 每个正文数字绑定 Stata result_id + command_hash + data_signature + do_file，写作器只能渲染已验证 claim。单一模块容易复制，跨执行、台账、验证和写作的合同难复制。

2.  **研究决策可查询。** 规格选择、人工确认、拒绝理由、反事实分支成为一等对象，能够回答"为什么是这个模型"。

3.  **实证社会科学的单元格级复现协议。** 与 PaperBench/ReplicationBench 有方法继承，但把 FE、cluster、weights、sample、table cell 和 Stata provenance 具体化。

4.  **版本化计量技能包。** 技能不是 prompt 片段，而是带适用条件、前置检查、证据要求、禁用条件和回归测试的政策包。

### 9.2 有价值但不独特

- 混合 RAG、事件溯源、Pydantic 输出、多 Agent 预留、Chroma/Qdrant、OTel tracing 都是成熟构件。

- "能写论文""会做回归""有记忆"不是可靠卖点，除非能用评测和 provenance 证明。

- 安全是门槛能力，不宜作为唯一核心卖点；但不可信论文内容与高权限统计工具结合，是本项目必须严肃处理的独特威胁面。

### 9.3 推荐的一句话定位

我不是让模型自由写一篇看似正确的论文，而是把文献证据、研究决策和 Stata 数值串成一条可回放、可验证的生产线。

## 10. SPEC v0.4 建议变更清单

### P0：写进架构基线后再开发

- 解决 SQLite source of truth 与 ledger.jsonl 的冲突，后者降级为导出。

- 定义确定性阶段状态机和每阶段的工具、schema、人工门、失败策略。

- 删除原始 thought 持久化，改为可审计决策摘要。

- 定义 EvidenceBundle，强制数字和引文 claim-to-source。

- 定义事件 schema、版本、幂等键、attempt、未决副作用和恢复协议。

- 新增 local_strict / approved_remote / mixed_sanitized，禁止 fallback 跨隐私边界。

- 把两套 RAG 库的信任角色写进类型与权限。

- 定义评测分层、最少 3 篇论文、盲测与专家容差。

### P1：M0--M1 必须完成

- 模型能力注册表与 provider contract tests。

- RAG 摄取版本链与稳定 chunk ID。

- OpenTelemetry 埋点规范；隐私默认关闭内容捕获。

- 50--100 条真实检索问题 gold set 和 hybrid 消融。

- 现有 stata-mcp 10 工具的上层策略映射与错误分类。

### P2：评测证明需要后再做

- 多 Agent 文献广搜。

- Qdrant 或其他服务化向量库迁移。

- Temporal/DBOS 等 durable runtime。

- 自研 Dashboard 和多用户权限体系。

## 11. 五分钟架构叙事

**第 0--1 分钟：问题。** 现在的大模型可以生成 Stata 代码和论文语言，但研究者真正不敢交付的是"看起来对、来源不明"的结果。我的目标是把研究过程从聊天变成证据生产线。

**第 1--2 分钟：骨架。** 系统外层是确定性阶段状态机，管理立题、文献、设计、数据、估计、稳健性、写作和验证；每个阶段有进入条件、允许工具和人工门。阶段内使用 PydanticAI 驱动单 Agent，开放推理被限制在局部。

**第 2--3 分钟：两条证据链。** 文献侧用混合检索和 rerank，保存页码与原文 span；数值侧复用已有 Stata MCP，结果自带命令哈希、数据签名和可复现 do-file。两条链汇总成 EvidenceBundle，写作器只能引用其中已经验证的 claim。

**第 3--4 分钟：状态与可靠性。** SQLite append-only 事件表保存业务事实，物化视图给出当前状态，OTel 单独负责性能观测。所有副作用有 operation_id、attempt 和语义输入哈希；恢复时先核对未决状态，不能盲目重试。

**第 4--5 分钟：证明。** 评测从契约、组件、轨迹、数值复现到专家研究质量分五层。至少选择三篇异质论文，遮蔽答案，逐单元格检查样本、模型、系数、标准误和来源。我的差异化不是某个框架，而是端到端证据闭环和可执行复现协议。

## 12. 高频追问清单

1.  为什么单 Agent 足够，何时升级多 Agent？

2.  阶段状态机与模型自主性如何平衡？

3.  SQLite 的单 writer 限制如何处理？

4.  event log、memory、trace、result ledger 分别是什么？

5.  为什么不保存 chain-of-thought？如何保留可解释性？

6.  Pydantic schema 能保证什么、不能保证什么？

7.  DeepSeek 换模型时如何做契约回归？

8.  如何防止正文出现模型编造的新数字？

9.  两套 RAG 库如何做到权限隔离？

10. PDF 中的 prompt injection 如何防？

11. Chroma 什么时候需要换成 Qdrant？

12. 如何判断检索差还是生成差？

13. timeout 后为什么不能直接重跑？

14. 如何处理 Stata session 崩溃和数据状态丢失？

15. LLM judge 如何校准？

16. 为什么至少三篇论文？

17. 复现容差由谁定、如何避免放水？

18. 如何防止 benchmark 污染和抄 do-file？

19. 最重要的 P0 是什么？为什么不是 UI？

20. 这套系统最难复制的部分是什么？

## 13. 尚未关闭的风险

  --------------------------------------------------------------------------------------------------------------
  **风险**                        **当前判断**            **关闭方式**
  ------------------------------- ----------------------- ------------------------------------------------------
  论文 PDF 抽取错页/错序          高                      建立抽取 QA、页码/bbox、人工抽样和 parser 版本

  模型工具调用在升级后退化        高                      能力画像、固定/记录版本、契约回归

  数值经过多层转换后失真          高                      原始值与展示值分离，claim 绑定 result_id，渲染后反查

  事件 schema 演进破坏回放        中高                    schema_version、upcaster、快照和迁移测试

  重试造成重复写文件/重复执行     中高                    operation_id、语义哈希、未决状态与 reconcile

  小规模复现集被过拟合            高                      held-out 论文、答案遮蔽、重复运行和污染 baseline

  领域判断缺少专家 ground truth   高                      作者/研究者参与 rubric，保存分歧与裁决记录

  OTel 记录敏感内容               中                      默认不采集内容、脱敏、哈希、短保留期

  多 Agent 带来成本与上下文漂移   中                      默认关闭，以增量评测和预算阈值启用

  向量库换型过早                  中                      用检索指标和运维触发条件决策
  --------------------------------------------------------------------------------------------------------------

## 14. 主要来源

1.  Anthropic, .[Building Effective Agents](https://www.anthropic.com/engineering/building-effective-agents)

2.  OpenAI, .[Multi-agent orchestration](https://developers.openai.com/api/docs/guides/agents/orchestration)

3.  Anthropic, .[How we built our multi-agent research system](https://www.anthropic.com/engineering/multi-agent-research-system)

4.  LangChain, [LangGraph overview](https://docs.langchain.com/oss/python/langgraph/overview)与 .[Persistence](https://docs.langchain.com/oss/python/langgraph/persistence)

5.  Microsoft, .[Event Sourcing pattern](https://learn.microsoft.com/en-us/azure/architecture/patterns/event-sourcing)

6.  SQLite, .[Write-Ahead Logging](https://www.sqlite.org/wal.html)

7.  PydanticAI, 、、.[Output](https://ai.pydantic.dev/output/)[Retries](https://ai.pydantic.dev/retries/)[Durable execution](https://ai.pydantic.dev/durable_execution/overview/)

8.  DeepSeek, [Tool Calls](https://api-docs.deepseek.com/guides/tool_calls/)与 .[Responses API](https://api-docs.deepseek.com/api/create-response/)

9.  Anthropic, .[Contextual Retrieval](https://www.anthropic.com/engineering/contextual-retrieval)

10. BAAI, .[BGE-M3](https://arxiv.org/abs/2402.03216)

11. Chroma, .[Performance](https://docs.trychroma.com/guides/deploy/performance)

12. Qdrant, .[Quickstart](https://qdrant.tech/documentation/quick-start/)

13. OWASP, [LLM01 Prompt Injection](https://genai.owasp.org/llmrisk/llm01-prompt-injection/)与 .[LLM06 Excessive Agency](https://genai.owasp.org/llmrisk/llm062025-excessive-agency/)

14. OpenAI, .[Guardrails and approvals](https://developers.openai.com/api/docs/guides/agents/guardrails-approvals)

15. OpenTelemetry, .[Semantic conventions](https://opentelemetry.io/docs/specs/semconv/)

16. OpenAI, [Integrations and observability](https://developers.openai.com/api/docs/guides/agents/integrations-observability).

17. AWS, .[Exponential Backoff and Jitter](https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/)

18. OpenAI, [Agent evals](https://developers.openai.com/api/docs/guides/agent-evals)与 .[Graders](https://developers.openai.com/api/docs/guides/graders)

19. OpenAI, .[PaperBench](https://openai.com/index/paperbench/)

20. Yamada et al., .[ReplicationBench](https://arxiv.org/abs/2510.24591)

## 15. 研究边界

本报告优先采用官方文档、标准组织和原始论文。供应商公布的性能数字只作为其自身场景的证据，不外推为本项目必然收益。stata-mcp 的能力判断来自本地 README 与架构文档核对；本报告未重新运行真实 Stata 回归，因为任务范围是上层架构研究而非执行底座验收。
