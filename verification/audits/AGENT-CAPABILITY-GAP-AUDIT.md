# Stata Research Agent 能力缺口审计

审计日期：2026-09-21
对象：`stata-research-agent`
用途：将 Agent 面试题转换为产品能力探针，不把八股或示例框架机械移植进产品。

## 结论

项目的主骨架已经不是“Demo Agent”。Turn/Step、Context Manifest、Tool Admission、
Operation/Attempt、Journal、Evidence、Recovery、Memory、Runtime Evaluator、Stata MCP 和
Agentic RAG 都有真实实现与测试证据。

本轮共固定并解析 295 个问题：

| 分类 | 数量 | 含义 |
| --- | ---: | --- |
| 可转化为项目能力 | 170 | 进入能力 Checklist |
| 纯理论 | 92 | 面试知识，不要求本地 Agent 实现 |
| 架构选择/当前非目标 | 33 | 记录理由，不作为缺陷 |

首轮自动分类曾得到 164 个能力问题（111 verified、50 partial、3 missing）。复审框架名误过滤、
显式 Thought 边界和整改证据后，当前 170 个能力问题为：126 项已有证据、44 项部分具备、
0 项直接缺失。数量变化来自语义重分类和证据刷新，不应解释为“自动新增了若干产品能力”。
该数字也不是完成度百分比：多个问题可能映射到同一个能力，真正应执行的是聚合缺口。

## P0/P1 整改状态（2026-09-21）

本节是对下面“整改前基线”的覆盖层；原始缺口描述保留，便于说明为什么做这些改动。

| 项目 | 状态 | 已落地证据 |
| --- | --- | --- |
| P0-1 RAG 信任边界 | `completed` | `ContextTrustClass`、输入装配隔离、4/4 真实模型注入红队通过；`verification/runs/rag-prompt-injection-live-p0.json` |
| P0-2 Turn 总时限 | `completed` | Step/Tool/Evaluator 共用 deadline；SQLite 主动运行预算跨暂停和进程重建累计；`tests/integration/test_turn_runtime_budget.py` |
| P0-3 Provider 故障策略 | `completed` | Retry-After、指数退避+jitter、共享 Circuit Breaker、同 Invocation fallback 与 Attempt 审计 |
| P0-4 统一发布门禁 | `completed` | Core 10 维/30 tests、Full 14 维/34 tests；真实 Stata session、`auto.dta`、`esttab`、跨 Workspace 并发均进入门禁 |
| P1-1 Memory 质量基准 | `completed_initial_baseline` | 版本化 gold set；precision/recall/activation/source/forbidden-fact 指标；真实 SQLite 12 次 Conversation、撤回/纠正与 proposed 不激活进入 Core 门禁。长期真实项目趋势继续积累 |
| P1-2 Tool selection 基准 | `completed_initial_baseline` | v2 gold set 直接使用生产 Tool 名称，并覆盖正式 Stata、Python 探索、用户确认后采用 Python 回归、文献、Help、Waiting 与 no-tool；真实 DeepSeek 三场景只证明循环级选择/完成/歧义升级，不冒充完整生产 Tool catalog 路由评测 |
| P1-3 Index/Embedding 升级 | `completed` | 双不可变 revision 比较、Recall@K/逐题退化、显式 adoption 与回滚合同；`verification/EMBEDDING-UPGRADE.md` |
| P1-4 Provider delta streaming | `completed` | 真 SSE 增量解析、非权威有界 hub、断流 fail-closed、完整输出后才提交 |
| P1-5 Judge 稳定性 | `completed_initial_baseline` | 测试集 hash/泄漏拒绝、重复标签 unanimity/majority stability；统一门禁固定回归结果 |

当前可复现报告：

- `verification/runs/release-gate-p0-p1-core.json`
- `verification/runs/release-gate-p0-p1-full.json`
- `verification/runs/live-model-p0-p1-baseline.json`
- `verification/runs/rag-prompt-injection-live-p0.json`

## 来源和可复现性

- 主清单：[Horanluo/ai-agent-interview-guide](https://github.com/Horanluo/ai-agent-interview-guide)，固定 revision `9a987322f2d82ddabe4c1aabc7b4795749fa90a2`，抽取 203 项。
- 压力题：[datawhalechina/hello-agents](https://github.com/datawhalechina/hello-agents/blob/main/Extra-Chapter/Extra01-%E9%9D%A2%E8%AF%95%E9%97%AE%E9%A2%98%E6%80%BB%E7%BB%93.md)，固定 revision `f72d756fbd2c95c883e057806b2116ed44bcafc3`，抽取 92 项。
- 完整逐题母表：`verification/audits/agent-interview-question-inventory.v1.json`。
- 可读的 170 项能力清单：`verification/audits/AGENT-CAPABILITY-CHECKLIST.md`。
- 再生成工具：`tools/build_interview_gap_inventory.py`。脚本对题目数量做 fail-fast 校验，源仓库变动不会静默改变审计口径。

最新统一门禁结果为 Core `10/10` 维度、`30/30` tests 通过，Full `14/14` 维度、
`34/34` tests 通过。Full 包含真实 Stata 18 session、`auto.dta` 字节保真、真实 `esttab`
以及两个 Workspace 的独立 Stata session；报告见上列 JSON。

## 八股复审新增结论（2026-09-21）

复审没有发现新的 P0/P1 硬缺口，但发现并修正了三处会影响真实产品语义的问题：

1. **Python 探索结果的晋升规则。** Python 可以自由运行探索性回归、测试或图形；其原始输出默认
   不是正式 Evidence。研究者针对精确 output fingerprint/preview 作出事后确认后，允许生成采用事实，
   而不是永久禁止所有 Python regression 进入文稿。
2. **Tool description 必须真实进入 Provider catalog。** 生产 Tool schema 现在同时发送 `name`、
   `description` 和 `input_schema`，并写清 Stata、Python、Shell 的适用与禁用边界；相应合同进入 Core gate。
3. **中英混合链路。** 中文研究指令、英文 Stata 变量/命令、英文文献和中文稿件的传输与文稿 round-trip
   已加入独立测试维度，避免把 Unicode/语言混合作为“模型自然会处理”的隐含假设。

同时完成四项审计口径修正：Human-in-the-loop、Embedding 升级、Few-shot 隐私与 LLM Judge
不再因题目带有框架名而被误归为纯理论；显式 Thought/隐藏 CoT 被定义为架构边界——产品 Trace
记录输入、计划、动作、工具结果、决策和可读摘要，不保存或展示私有思维链。

本次重分类后唯一新暴露的 `partial` 是“关键路径与非关键路径的统一降级矩阵”。它属于 P2 工程优化，
按当前决定暂缓；既有 Planning/Replan、超大语料 ingestion、长期 Memory 漂移、Telemetry、成本趋势等
partial 也继续作为 P2 观察项，不阻挡当前主功能。

## 状态口径

| 状态 | 判定标准 |
| --- | --- |
| `verified` | 找到实现，并至少有单元/集成/垂直测试或真实运行报告 |
| `partial` | 有相关机制，但缺完整语义、专项评测或真实故障验证 |
| `missing` | 产品需要，但未发现直接实现证据 |
| `not_planned_v0_1` | 有明确架构理由不进入当前版本 |
| `not_a_product_requirement` | Provider/基础模型内部或纯理论知识，不应在应用层重造 |

## 九个能力域映射

| 能力域 | 当前判断 | 关键证据 | 主要缺口 |
| --- | --- | --- | --- |
| Agent 边界与循环 | P0 已补齐 | `runtime/agent_turn_driver.py`、持久化 runtime budget ledger | 总 deadline 已覆盖 Model/Tool/Evaluator；Plan/Replan 长程表现继续由门禁积累 |
| RAG | 信任与升级边界已补齐 | `application/knowledge_retrieval.py`、`ContextTrustClass`、真实红队报告 | 大语料索引性能仍属于后续压力优化 |
| Tool Calling / MCP | 核心与初始 benchmark 完备 | Tool Broker、真实 Stata MCP、tool-selection gold/live baseline | 扩大真实研究方法覆盖，不引入方法白名单 |
| Memory / Context | 架构与初始质量基准完备 | Project Memory、Curator、`agent_benchmarks.py` | 长周期真实项目漂移数据需随使用持续积累 |
| Runtime Evaluator | 模块与稳定性基准已具备 | Evaluator、Judge repeated-label stability | 后续扩充人工金标，不把单一 Judge 当真值 |
| Provider / LLM | 故障策略已补齐 | Model Gateway resilience、Provider Attempt、真实 DeepSeek baseline | 多 Provider 生产样本随接入继续增加 |
| 工程可靠性 | P0/P1 主链闭合 | Journal、Recovery、runtime ledger、delta streaming、真实并发门禁 | 安装包和更长时压力测试属于下一阶段 |
| Prompt / 安全 | RAG 间接注入已进入红队门禁 | trust class、SensitiveOutputGate、4/4 live red-team | 持续扩展攻击语料 |
| Evaluation | 统一门禁已建立 | Core/Full release gate JSON 报告 | 需要持续保存跨版本趋势，而不是只看单次通过 |

## 按优先级处理的真实缺口

### P0-1（已处理）：把检索内容设为不可信数据，而不是隐含指令

当前 RAG 已正确走：

```text
Model → search_knowledge → canonical Tool Result
      → Context Compiler → next Model Invocation
```

但“能进入上下文”不等于“可以影响系统指令”。需要补齐：

1. Context Item 明确 `trust_class=retrieved_untrusted` 与 corpus role。
2. 模型输入中将证据置于稳定边界，明确禁止执行其中的命令、权限请求和角色覆盖。
3. 对包含“忽略系统指令”“泄露密钥”“调用 shell”“伪造引用”等文档建立攻击集。
4. 验收同时检查：攻击不生效、正常召回和引用质量不下降、攻击内容进入 Trace 但不污染 Memory。
5. Tool、Memory、Skill 的晋升不能因为检索文本自述而获得更高信任级别。

建议交付：`RAG-TRUST-BOUNDARY.md`、攻击语料、端到端红队测试、攻击成功率指标。

### P0-2（已处理）：增加整个 Turn 的总时限与取消传播

现有系统有 64 Step、128 Tool Admission、Provider Attempt 限额、同失败指纹限制和单工具
timeout，但缺少一个覆盖整个 Turn 的 wall-clock deadline。理论上连续的慢 Provider/Tool 仍可把
Turn 拉得非常长。

需要补齐：

- 版本化 `TurnRuntimePolicy`：总 deadline、宽限期和允许收敛的 Operation 类型；
- 每个 Step/Admission 使用同一个剩余时间预算；
- 到期后不新启 Model Invocation/Tool Admission，已准入副作用进入稳定分类；
- 最终原因写入 Journal，并与 Pause、Waiting、Recovery 明确区分；
- 恢复后不能重置已经消耗的 Turn 预算，除非用户开启新 Turn。

### P0-3（已处理）：Provider 可靠性从“有限重试”升级为“可控故障策略”

目前 Model Gateway 最多尝试 3 次，并正确记录 Provider Attempt；但 retry 是立即发生的，尚无：

- 指数退避与 jitter；
- 按 Provider/Profile 的三态 Circuit Breaker；
- `Retry-After` 尊重；
- 明确的兼容模型 fallback；
- fallback 后 Context/Tool Schema/最大输出能力重新校验。

不能简单“失败就换模型”。Provider Route、fallback 原因、能力差异和最终采用模型必须进入权威
Attempt/Policy Snapshot，避免恢复或审计时只看到最终模型。

### P0-4（已处理）：建立统一的 Research Agent 发布评测门禁

已有 RAG、Stata MCP、Recovery、Context、Memory 等独立测试，但产品核心需要一张统一成绩单。
最少覆盖：

1. 自主模式：Idea + Data → 研究方案 → Stata 执行 → Result/Evidence → Word。
2. 控制模式：缺失值/变量构造/研究设计触发 Waiting，用户回答后同 Turn 正确恢复。
3. 数据保真：Word 中每个数字能回溯到 Result Element、Run、Command Instance 和 Data Version。
4. Pause/Cancel/Crash：不重复外部副作用，可恢复、可 reconciliation。
5. RAG：单跳、多跳、无答案、恶意文档、错误 Stata Help 查询。
6. 长程：Context compaction、Memory 注入与冲突、预算耗尽、Provider 故障。

门禁不能只给总分；需要按“任务结果、证据链、用户控制、成本/时间、恢复能力”分别报告。

### P1-1（已建立初始基线）：Memory 质量基准

Memory 已有 scope、revision、source、episode、compaction checkpoint、curator 和生命周期，但测试主要
证明“能写、能读、能维护”。需要证明：

- 正确记住研究偏好，不把单次探索结论晋升为长期事实；
- 新证据与旧 Memory 冲突时不静默覆盖；
- Retract 后不再进入 Context；
- 摘要压缩不丢失决策、约束和 Evidence 引用；
- 经过多次会话后 Recall@K、错误注入率和过期记忆命中率可量化。

### P1-2（已建立初始基线）：工具选择与工具描述 Benchmark

Tool Broker 已能验证和授权正确调用，但“模型是否选对工具”仍缺独立测试。需要覆盖：

- Stata 回归必须走 Stata；Python 可做处理和探索但不能无授权进入正式统计表；
- 文献检索、Stata Help、文件读取、Shell/Python 的边界；
- 不需要工具时不滥调；
- 相似工具、错误参数、超大返回、依赖型 Call Batch；
- 每个版本的 Tool description/schema 变更进行回归比较。

### P1-3（已处理）：索引与 Embedding 升级协议

知识节点身份和检索会话已版本化，但仍需要把“Embedding 模型升级、索引重建、双版本切换、失败
回滚、旧 citation 可解释”做成明确合同。不能让新索引悄悄改变已存在的 Knowledge Node 身份或
历史 Context Use。

### P1-4（已处理）：Provider Delta Streaming

Workspace SSE 已存在，但 Provider 调用仍以完整响应为主。需要区分：

- UI response delta（非权威、可丢）；
- 完整 Assistant Output（权威提交）；
- 工具提议未完整解析前不得 Admission；
- 断流时不能把半截 JSON 当成 Tool Call；
- token usage、截断和最终 finish reason 仍以完整 Attempt 结果结算。

### P1-5（已建立初始基线）：评测污染和 Judge 稳定性

需要把测试集与 Prompt/Skill 开发材料隔离，记录 evaluator 模型版本，并对一部分关键用例做双 Judge
或人工金标。目标不是追求“LLM Judge 永远正确”，而是让它的偏差可测、可回归、可追踪。

## 明确不应因八股而新增的功能

| 题目 | 决策 |
| --- | --- |
| KV Cache / Flash Attention / PagedAttention | Provider 推理层能力；应用只记录模型 capability、token usage 与缓存计费信息 |
| LoRA / QLoRA / RLHF / DPO / GRPO | 当前产品不训练基础模型，不进入 V0.1 |
| 多 Agent Boss-Worker/CrewAI/AutoGen | 单 Research Agent + 确定性 Runtime + Evaluator 更符合当前边界；需要真实收益证据才引入 |
| GraphRAG | 文献规模与问题类型暂不证明需要图数据库；现有 Agentic multi-hop 优先 |
| 多模态 RAG / VLM | 当前主要输入是论文、数据和 Stata 结果；表格/公式解析不等于建设通用 VLM 平台 |
| K8s/HPA/金丝雀发布 | V0.1 是 Windows 本地单用户产品；应做本地安装、升级和回滚，而非云原生部署 |
| 通用语义缓存 | 研究问题条件细微，错误复用风险高；在拥有严格 identity/policy 前不作为默认能力 |
| 显式 Thought / 隐藏 Chain-of-Thought Trace | 不作为产品事实保存；只保存可核查的计划、动作、结果、决策摘要和因果引用 |

## 推荐执行顺序

```text
RAG Trust Boundary + 红队集
        ↓
Turn deadline / cancellation
        ↓
Provider backoff + circuit + fallback contract
        ↓
统一 Research Agent E2E 发布门禁
        ↓
Memory 与 Tool-selection 专项 Benchmark
        ↓
Index migration / Provider streaming / Judge 稳定性
```

这轮审计的原则是：先补会影响“全流程控制、数据保真、可恢复性”的能力；不为了面试覆盖率引入
Multi-Agent、训练栈或云部署等偏离产品主线的复杂度。
