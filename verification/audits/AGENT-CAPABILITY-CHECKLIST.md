# Agent 项目能力审计 Checklist

> 由两套题库的固定 revision 自动生成。题目是能力探针，不是产品需求。
> 主清单只展示可转化为产品能力的 170 项；纯理论和架构非目标仍保留在 JSON 母表中。

- 主清单 revision：`9a987322f2d82ddabe4c1aabc7b4795749fa90a2`（203 项）
- 压力题 revision：`f72d756fbd2c95c883e057806b2116ed44bcafc3`（92 项）
- 状态：✅ 已有证据；🟡 部分实现/证据不足；🔴 缺失

## ✅ `agent-boundary` — verified

证据：`src/stata_research_agent/runtime/agent_turn_driver.py`、`src/stata_research_agent/runtime/turn_worker.py`、`tests/vertical/test_agent_turn_driver_vertical.py`

- ✅ `guide-001` [01-基础概念/Q1] 一句话说明什么是 AI Agent？
- ✅ `guide-003` [01-基础概念/Q3] Agent 和 Prompt Chain 有什么本质区别？
- ✅ `guide-004` [01-基础概念/Q4] ChatBot 加上插件是不是就变成 Agent 了？
- ✅ `guide-008` [01-基础概念/Q8] 基于效用的 Agent 和基于目标的有什么区别？
- ✅ `guide-009` [01-基础概念/Q9] 反应式 Agent 有什么优缺点？
- ✅ `guide-010` [01-基础概念/Q10] 企业内部落地 Agent，你最先关心哪三个非功能需求？
- ✅ `guide-012` [01-基础概念/Q12] Agent 的最大风险是什么？
- ✅ `guide-013` [02-核心框架/Q1] ReAct 和「普通 CoT 提示」有什么本质区别？
- ✅ `guide-025` [02-核心框架/Q13] 请用一句话解释 ReAct。
- ✅ `guide-029` [02-核心框架/Q17] LATS 与 ReAct 在「探索能力」上如何对比？
- ✅ `guide-034` [02-核心框架/Q22] ReAct Prompt 为什么要给 few-shot 示例？
- ✅ `datawhale-041` [4. Agent/1] 你如何定义一个基于 LLM 的智能体（Agent）？它通常由哪些核心组件构成？
- ✅ `datawhale-042` [4. Agent/2] 请详细解释 ReAct 框架。它是如何将思维链和行动结合起来，以完成复杂任务的？
- ✅ `datawhale-047` [4. Agent/7] 在构建一个复杂的 Agent 时，你认为最主要的挑战是什么？
- ✅ `datawhale-050` [4. Agent/10] 如何确保一个 Agent 的行为是安全、可控且符合人类意图的？在 Agent 的设计中，有哪些保障对齐方法？

## 🟡 `planning-replanning` — partial

证据：`src/stata_research_agent/runtime/agent_turn_driver.py`、`src/stata_research_agent/runtime/research_workflow_executor.py`

- 🟡 `guide-006` [01-基础概念/Q6] 规划和执行要不要拆开两个模型？
- 🟡 `guide-015` [02-核心框架/Q3] Plan-and-Execute 相比 ReAct 什么时候更占优？
- 🟡 `guide-016` [02-核心框架/Q4] Re-planning 会不会导致「计划抖动」？怎么缓解？
- 🟡 `guide-027` [02-核心框架/Q15] Plan-and-Execute 的最大风险是什么？如何缓解？
- 🟡 `guide-035` [02-核心框架/Q23] Re-planning 与 Reflexion 都「改正错误」，区别是什么？
- 🟡 `guide-038` [02-核心框架/Q26] 你如何为一个企业场景选择 ReAct vs Plan-and-Execute？
- 🟡 `guide-196` [09-Prompt工程/Q21] Agent 里 ReAct 和 Plan-and-Execute 怎么选？
- 🟡 `datawhale-043` [4. Agent/3] 在 Agent 的设计中，“规划能力”至关重要。请谈谈目前有哪些主流方法可以赋予 LLM 规划能力？（例如 CoT, ToT, GoT等）

## ✅ `human-in-loop-control` — verified

证据：`src/stata_research_agent/application/turn_interaction_service.py`、`src/stata_research_agent/runtime/agent_turn_driver.py`、`tests/vertical/test_agent_turn_driver_vertical.py`

- ✅ `guide-130` [06-多智能体/Q19] 为什么需要「人机在环」？

## ✅ `evaluation-reflection` — verified

证据：`src/stata_research_agent/application/evaluation.py`、`src/stata_research_agent/runtime/model_evaluation_coordinator.py`、`tests/vertical/test_runtime_evaluation_vertical.py`

- ✅ `guide-017` [02-核心框架/Q5] Reflexion 和「让模型自己检查一遍」有什么不同？
- ✅ `guide-028` [02-核心框架/Q16] Reflexion 的关键产出是什么？它如何提升下一轮？

## ✅ `loop-safety` — verified

证据：`src/stata_research_agent/application/turn_driver.py`、`src/stata_research_agent/runtime/agent_turn_driver.py`、`src/stata_research_agent/persistence/turn_runtime_budget_store.py`、`tests/vertical/test_model_gateway_vertical.py`

- ✅ `guide-030` [02-核心框架/Q18] LangChain AgentExecutor 中为什么要限制 max_iterations？

## 🟡 `critical-path-degradation` — partial

证据：`src/stata_research_agent/runtime/application_runtime.py`、`src/stata_research_agent/application/diagnostic_service.py`

- 🟡 `guide-186` [08-工程化实践/Q27] 如何做「关键路径」与「非关键路径」分级？

## ✅ `context-engineering` — verified

证据：`src/stata_research_agent/application/context_compiler.py`、`src/stata_research_agent/persistence/context_authority.py`、`tests/integration/test_context_management.py`

- ✅ `guide-037` [02-核心框架/Q25] 如果工具返回噪声很大，ReAct 可能出什么问题？怎么改进？
- ✅ `guide-069` [04-工具调用/Q6] 工具返回 10MB 日志怎么办？
- ✅ `guide-087` [04-工具调用/Q13] 工具返回为什么要尽量结构化（JSON）？
- ✅ `guide-202` [09-Prompt工程/Q27] 长上下文模型出现后 Prompt 工程会消失吗？

## ✅ `rag-pipeline` — verified

证据：`src/stata_research_agent/application/knowledge_retrieval.py`、`src/stata_research_agent/persistence/knowledge_store.py`、`verification/runs/product-agentic-rag-20260921-v1/live-product-report.json`

- ✅ `guide-040` [03-RAG技术/Q1] 简述 RAG 两步流水线（离线与在线）。
- ✅ `guide-043` [03-RAG技术/Q4] RecursiveCharacterTextSplitter 的分隔符顺序为什么重要？
- ✅ `guide-050` [03-RAG技术/Q11] HyDE 的风险如何缓解？
- ✅ `guide-062` [03-RAG技术/Q23] 长上下文模型出现后 RAG 会消失吗？
- ✅ `guide-072` [04-工具调用/Q9] 向量路由选出来的工具不对怎么兜底？
- ✅ `guide-083` [04-工具调用/Q9] 向量路由的缺陷与改进？
- ✅ `guide-096` [05-记忆系统/Q5] 长期记忆为什么常用向量数据库？有什么局限？
- ✅ `guide-099` [05-记忆系统/Q8] 只用向量检索、不做摘要可以吗？
- ✅ `guide-103` [05-记忆系统/Q12] 只做强相关性检索会有什么问题？
- ✅ `guide-104` [05-记忆系统/Q13] MemGPT 和简单 RAG 的本质区别是什么？
- ✅ `guide-105` [05-记忆系统/Q14] 记忆图谱比向量库强在哪里，弱在哪里？
- ✅ `guide-173` [08-工程化实践/Q14] RAG 一定能降幻觉吗？
- ✅ `datawhale-054` [5. RAG/1] 请解释 RAG 的工作原理。与直接对 LLM 进行微调相比，RAG 主要解决了什么问题？有哪些优势？
- ✅ `datawhale-055` [5. RAG/2] 一个完整的 RAG 流水线包含哪些关键步骤？请从数据准备到最终生成，详细描述整个过程。
- ✅ `datawhale-056` [5. RAG/3] 在构建知识库时，文本切块策略至关重要。你会如何选择合适的切块大小和重叠长度？这背后有什么权衡？
- ✅ `datawhale-058` [5. RAG/5] 除了基础的向量检索，你还知道哪些可以提升 RAG 检索质量的技术？
- ✅ `datawhale-059` [5. RAG/6] 请解释“Lost in the Middle”问题。它描述了 RAG 中的什么现象？有什么方法可以缓解这个问题？
- ✅ `datawhale-061` [5. RAG/8] 在什么场景下，你会选择使用图数据库或知识图谱来增强或替代传统的向量数据库检索？
- ✅ `datawhale-063` [5. RAG/10] RAG 系统在实际部署中可能面临哪些挑战？
- ✅ `datawhale-064` [5. RAG/11] 了解搜索系统吗？和RAG有什么区别？
- ✅ `datawhale-065` [5. RAG/12] 知道或者使用过哪些开源RAG框架比如Ragflow？如何选择合适场景？

## 🟡 `rag-ingestion-indexing` — partial

证据：`src/stata_research_agent/interfaces/literature_catalog.py`、`src/stata_research_agent/persistence/dense_knowledge_store.py`、`verification/EMBEDDING-UPGRADE.md`、`verification/runs/rag-adaptive-pdf-20260921-v4/adaptive-pdf-report.json`

- 🟡 `guide-042` [03-RAG技术/Q3] 为什么需要 chunk_overlap？
- 🟡 `guide-044` [03-RAG技术/Q5] 语义分块比递归分块更好吗？
- 🟡 `guide-045` [03-RAG技术/Q6] 父子文档如何存储？
- 🟡 `guide-046` [03-RAG技术/Q7] Embedding 是否需要归一化？
- 🟡 `guide-047` [03-RAG技术/Q8] FAISS IndexFlatIP 与 IndexHNSW 区别？
- 🟡 `guide-052` [03-RAG技术/Q13] Cross-Encoder 为何不能替代向量索引？
- 🟡 `guide-060` [03-RAG技术/Q21] 索引频繁更新如何保持一致性？

## ✅ `embedding-index-upgrade` — verified

证据：`src/stata_research_agent/application/dense_retrieval.py`、`verification/EMBEDDING-UPGRADE.md`、`tests/unit/test_dense_index_upgrade.py`

- ✅ `guide-110` [05-记忆系统/Q19] Embedding 模型升级后旧向量怎么办？

## ✅ `rag-retrieval-quality` — verified

证据：`src/stata_research_agent/application/dense_retrieval.py`、`src/stata_research_agent/application/rag_evaluation.py`、`verification/runs/rag-real-scenarios-20260921-v6/rag-scenario-report.json`

- ✅ `guide-048` [03-RAG技术/Q9] 混合检索权重 alpha 怎么定？
- ✅ `guide-049` [03-RAG技术/Q10] RRF 为什么鲁棒？
- ✅ `guide-051` [03-RAG技术/Q12] BM25 在中文要不要分词？
- ✅ `guide-053` [03-RAG技术/Q14] MMR 的 lambda 参数含义？
- ✅ `guide-058` [03-RAG技术/Q19] RAGAS 的局限？
- ✅ `guide-059` [03-RAG技术/Q20] 如何做低成本在线评估？
- ✅ `guide-102` [05-记忆系统/Q11] 混合检索怎么去重与限长？
- ✅ `datawhale-057` [5. RAG/4] 如何选择一个合适的嵌入模型？评估一个 Embedding 模型的好坏有哪些指标？
- ✅ `datawhale-060` [5. RAG/7] 如何全面地评估一个 RAG 系统的性能？请分别从检索和生成两个阶段提出评估指标。

## ✅ `rag-agentic-multihop` — verified

证据：`src/stata_research_agent/application/knowledge_retrieval.py`、`tests/integration/test_canonical_knowledge_runtime.py`、`verification/runs/product-agentic-rag-20260921-v1/live-product-report.json`

- ✅ `guide-055` [03-RAG技术/Q16] Agentic RAG 与一次性 RAG 差异？
- ✅ `guide-056` [03-RAG技术/Q17] Self-RAG 核心思想？
- ✅ `guide-057` [03-RAG技术/Q18] Corrective RAG 触发条件？
- ✅ `datawhale-062` [5. RAG/9] 传统的 RAG 流程是“先检索后生成”，你是否了解一些更复杂的 RAG 范式，比如在生成过程中进行多次检索或自适应检索？

## ✅ `rag-trust-security` — verified

证据：`verification/RAG-TRUST-BOUNDARY.md`、`verification/rag-prompt-injection-redteam.v1.json`、`verification/runs/rag-prompt-injection-live-p0.json`

- ✅ `guide-061` [03-RAG技术/Q22] 如何防止 Prompt 注入污染 RAG？
- ✅ `guide-195` [09-Prompt工程/Q20] 间接注入如何与 RAG 结合防御？

## ✅ `tool-contract-admission` — verified

证据：`src/stata_research_agent/application/tool_broker.py`、`src/stata_research_agent/application/tool_broker_service.py`、`tests/vertical/test_tool_broker_vertical.py`

- ✅ `guide-002` [01-基础概念/Q2] 为什么说 Agent = LLM + Planning + Memory + Tools？缺一块会怎样？
- ✅ `guide-007` [01-基础概念/Q7] 如何避免 Agent 在工具调用间「迷失」？
- ✅ `guide-020` [02-核心框架/Q8] 工具描述为什么重要？
- ✅ `guide-039` [02-核心框架/Q27] 为什么说「工具描述」是 Agent 的接口设计？
- ✅ `guide-064` [04-工具调用/Q1] OpenAI 的 Function Calling 大致流程是什么？
- ✅ `guide-065` [04-工具调用/Q2] 为什么用 JSON Schema 描述参数？
- ✅ `guide-067` [04-工具调用/Q4] 如何做参数校验？
- ✅ `guide-068` [04-工具调用/Q5] Tool 与业务里的普通 Python 函数有何不同？
- ✅ `guide-075` [04-工具调用/Q1] Function Calling 和「让模型输出 JSON」有什么本质区别？
- ✅ `guide-076` [04-工具调用/Q2] 描述 OpenAI 兼容接口里 `tool_calls` 与 `tool` 消息的对应关系。
- ✅ `guide-078` [04-工具调用/Q4] 如何设计 JSON Schema 降低模型填错概率？
- ✅ `guide-090` [04-工具调用/Q16] 审计日志至少记哪些字段？
- ✅ `guide-091` [04-工具调用/Q17] Calculator 为什么禁止 `eval`？
- ✅ `guide-107` [05-记忆系统/Q16] 记忆和 Tool Use 的边界是什么？
- ✅ `guide-166` [08-工程化实践/Q7] 为什么说「永远不信任模型输出的工具调用」？
- ✅ `guide-178` [08-工程化实践/Q19] 工具调用的「两步授权」怎么做？
- ✅ `guide-203` [09-Prompt工程/Q28] Function Calling 与「输出 JSON」二选一？
- ✅ `datawhale-044` [4. Agent/4] Memory是 Agent 的一个关键模块。请问如何为 Agent 设计短期记忆和长期记忆系统？可以借助哪些外部工具或技术？
- ✅ `datawhale-045` [4. Agent/5] Tool Use是扩展 Agent 能力的有效途径。请解释 LLM 是如何学会调用外部 API 或工具的？（可以从 Function Calling 的角度解释）
- ✅ `datawhale-049` [4. Agent/9] 当一个 Agent 需要在真实或模拟环境中（如机器人、游戏）执行任务时，它与纯粹基于软件工具的 Agent 有什么本质区别？

## ✅ `tool-selection-routing` — verified

证据：`src/stata_research_agent/application/default_tool_contracts.py`、`src/stata_research_agent/interfaces/production_turn_runner.py`、`verification/tool-selection-gold.v2.json`、`tests/unit/test_production_tool_catalog.py`

- ✅ `guide-066` [04-工具调用/Q3] 模型选错工具怎么办？
- ✅ `guide-077` [04-工具调用/Q3] 为什么工具 `description` 比函数名更重要？
- ✅ `guide-079` [04-工具调用/Q5] LangChain Tool 的 docstring 为什么要写「何时不要用」？
- ✅ `guide-082` [04-工具调用/Q8] 工具路由什么时候必须上？

## ✅ `tool-scheduling` — verified

证据：`src/stata_research_agent/runtime/agent_turn_driver.py`、`tests/unit/test_agent_tool_batch_execution.py`、`tests/integration/test_cross_workspace_real_stata.py`

- ✅ `guide-073` [04-工具调用/Q10] 并行与串行如何取舍？
- ✅ `guide-084` [04-工具调用/Q10] 并行工具调用要注意什么？
- ✅ `guide-085` [04-工具调用/Q11] 什么是工具编排中的「依赖 DAG」？

## ✅ `tool-security-permissions` — verified

证据：`src/stata_research_agent/runtime/sandbox_tool_executor.py`、`src/stata_research_agent/interfaces/windows_sandbox_executor.py`、`tests/integration/test_windows_sandbox_executor.py`

- ✅ `guide-074` [04-工具调用/Q11] 如何防止模型通过工具泄露敏感数据？
- ✅ `guide-086` [04-工具调用/Q12] 敏感操作为什么推荐「两阶段提交」式工具设计？
- ✅ `guide-088` [04-工具调用/Q14] 如何做工具调用的权限控制？
- ✅ `guide-089` [04-工具调用/Q15] 代码执行工具如何做到基本安全？

## ✅ `mcp-integration` — verified

证据：`src/stata_research_agent/stata/stdio_runtime.py`、`tests/integration/test_real_stata_mcp_runtime.py`

- ✅ `guide-070` [04-工具调用/Q7] MCP 里 Client 和你在 OpenAI 里写的「执行工具的 Python 代码」是什么关系？
- ✅ `guide-071` [04-工具调用/Q8] 企业为什么愿意接 MCP 而不是每个业务线自己写 Function？
- ✅ `guide-080` [04-工具调用/Q6] MCP 解决的主要痛点是什么？
- ✅ `guide-081` [04-工具调用/Q7] MCP 与 Function Calling 是替代关系吗？

## ✅ `memory-architecture` — verified

证据：`src/stata_research_agent/application/memory.py`、`src/stata_research_agent/persistence/memory_store.py`、`tests/integration/test_project_memory.py`

- ✅ `guide-005` [01-基础概念/Q5] Agent 的记忆一般怎么设计？
- ✅ `guide-092` [05-记忆系统/Q1] 为什么 Agent 需要记忆？没有行不行？
- ✅ `guide-093` [05-记忆系统/Q2] 用人类记忆模型设计 Agent 记忆有什么好处？
- ✅ `guide-094` [05-记忆系统/Q3] Conversation Buffer 和 Window Buffer 的区别与取舍？
- ✅ `guide-095` [05-记忆系统/Q4] 如何做 Token 预算分配才不容易翻车？
- ✅ `guide-101` [05-记忆系统/Q10] 情景记忆和语义记忆为什么要区分存储？
- ✅ `guide-109` [05-记忆系统/Q18] 未来记忆系统趋势你怎么看待？

## 🟡 `memory-quality-lifecycle` — partial

证据：`src/stata_research_agent/application/memory_curator.py`、`src/stata_research_agent/persistence/memory_curator_store.py`、`tests/integration/test_memory_curator.py`、`tests/integration/test_memory_quality_lifecycle.py`

- 🟡 `guide-097` [05-记忆系统/Q6] 记忆的更新怎么做才不容易脏数据？
- 🟡 `guide-098` [05-记忆系统/Q7] 衰减会不会把重要但很久不用的信息删掉？
- 🟡 `guide-100` [05-记忆系统/Q9] 增量摘要误差累积怎么缓解？
- 🟡 `guide-106` [05-记忆系统/Q15] 生产环境记忆系统最容易出的事故是什么？怎么防？
- 🟡 `guide-108` [05-记忆系统/Q17] 如何评测记忆系统好坏？

## ✅ `model-configuration-budget` — verified

证据：`src/stata_research_agent/application/model_configuration.py`、`src/stata_research_agent/application/output_budget.py`、`tests/unit/test_output_budget.py`

- ✅ `guide-134` [07-大模型基础/Q3] Encoder 和 Decoder 的自注意力有何不同？
- ✅ `guide-141` [07-大模型基础/Q10] Tokenizer 不一致会导致什么问题？
- ✅ `guide-142` [07-大模型基础/Q11] Prefill 和 Decode 阶段特点？
- ✅ `guide-144` [07-大模型基础/Q13] Temperature、Top-k、Top-p 各影响什么？
- ✅ `guide-158` [07-大模型基础/Q27] 开源模型相对闭源 API 的核心优势场景？
- ✅ `guide-162` [08-工程化实践/Q3] 为什么说本地 tiktoken 计数只能「估算」？
- ✅ `datawhale-005` [1. LLM 八股/5] 请比较一下几种常见的 LLM 架构，例如 Encoder-Only, Decoder-Only, 和 Encoder-Decoder，并说明它们各自最擅长的任务类型。
- ✅ `datawhale-007` [1. LLM 八股/7] 在LLM的推理阶段，有哪些常见的解码策略？请解释 Greedy Search, Beam Search, Top-K Sampling 和 Nucleus Sampling (Top-P) 的原理和优缺点。

## ✅ `provider-resilience-routing` — verified

证据：`src/stata_research_agent/application/model_gateway_service.py`、`tests/vertical/test_model_gateway_vertical.py`

- ✅ `guide-160` [08-工程化实践/Q1] 为什么要做模型路由，而不是全量用一个最强模型？
- ✅ `guide-161` [08-工程化实践/Q2] 熔断器和重试分别解决什么问题？一起用时要注意什么？
- ✅ `guide-174` [08-工程化实践/Q15] 你会如何设计一个多模型网关的架构？
- ✅ `guide-175` [08-工程化实践/Q16] 指数退避为什么要加 jitter？

## ✅ `observability-audit` — verified

证据：`src/stata_research_agent/application/diagnostics.py`、`src/stata_research_agent/persistence/model_gateway_store.py`、`src/stata_research_agent/persistence/tool_broker_store.py`

- ✅ `guide-164` [08-工程化实践/Q5] Agent 系统为什么比传统服务更需要 Trace？
- ✅ `guide-165` [08-工程化实践/Q6] 结构化日志和 Trace 有什么区别？
- ✅ `guide-169` [08-工程化实践/Q10] Streaming 会影响计费或日志吗？
- ✅ `guide-185` [08-工程化实践/Q26] 小型团队没有 LangSmith，最小可观测方案是什么？

## 🟡 `observability-telemetry` — partial

证据：`src/stata_research_agent/application/diagnostics.py`、`src/stata_research_agent/interfaces/filesystem_diagnostics.py`

- 🟡 `guide-177` [08-工程化实践/Q18] OpenTelemetry 在 Agent 里一般打哪些 Span？

## 🟡 `streaming-performance` — partial

证据：`src/stata_research_agent/application/streaming.py`、`src/stata_research_agent/interfaces/api/app.py`、`tests/contract/test_workspace_stream.py`

- 🟡 `guide-181` [08-工程化实践/Q22] 异步一定能提高 Agent 吞吐吗？
- 🟡 `guide-187` [08-工程化实践/Q28] 并发控制 Semaphore 设多大？

## 🟡 `cost-usage-monitoring` — partial

证据：`src/stata_research_agent/persistence/model_gateway_store.py`、`src/stata_research_agent/application/output_budget.py`

- 🟡 `guide-018` [02-核心框架/Q6] LATS 相比单次 ReAct 多在哪里成本？换来什么收益？
- 🟡 `guide-182` [08-工程化实践/Q23] 如何监控一次 Agent 任务的「真实成本」？
- 🟡 `datawhale-072` [6. 模型评估与 Agent 评估/7] 在评估一个 Agent 的任务完成情况时，除了最终结果的正确性，还有哪些过程指标是值得关注的？（例如：效率、成本、鲁棒性）

## ✅ `prompt-versioning-output-validation` — verified

证据：`src/stata_research_agent/application/model_gateway.py`、`src/stata_research_agent/persistence/model_gateway_store.py`、`src/stata_research_agent/interfaces/openai_compatible_transport.py`

- ✅ `guide-188` [08-工程化实践/Q29] 为什么 Agent 更需要「版本化」的 Prompt 与模型？
- ✅ `guide-193` [09-Prompt工程/Q18] 结构化输出为什么要后端校验？
- ✅ `guide-194` [09-Prompt工程/Q19] System Prompt 能否被用户覆盖？
- ✅ `guide-199` [09-Prompt工程/Q24] 如何版本管理 Prompt？

## ✅ `prompt-injection-redteam` — verified

证据：`verification/rag-prompt-injection-redteam.v1.json`、`tools/run_live_prompt_injection_redteam.py`、`verification/runs/rag-prompt-injection-live-p0.json`

- ✅ `guide-167` [08-工程化实践/Q8] Prompt 注入和越狱有什么区别？

## ✅ `prompt-data-privacy` — verified

证据：`src/stata_research_agent/application/sensitive_output.py`、`src/stata_research_agent/application/rag_evaluation.py`、`tests/unit/test_rag_evaluation.py`

- ✅ `guide-190` [09-Prompt工程/Q15] 如何避免 Few-shot 示例泄露隐私？
- ✅ `datawhale-082` [7. LLM 前景与发展/7] 个性化是 LLM 应用的重要方向。在实现高度个性化的 Agent 或助手的过程中，我们应如何平衡效果、隐私和安全？

## ✅ `multilingual-contract-integrity` — verified

证据：`tests/unit/test_openai_compatible_transport.py`、`tests/unit/test_table_document_rules.py`

- ✅ `guide-200` [09-Prompt工程/Q25] 多语言混合 Prompt 注意什么？

## 🟡 `evaluation-regression` — partial

证据：`src/stata_research_agent/application/rag_evaluation.py`、`tools/run_real_product_stress.py`、`verification/rag-real-questions.v1.json`、`src/stata_research_agent/interfaces/release_evaluation_gate.py`、`verification/runs/release-gate-p0-p1-full.json`

- 🟡 `guide-011` [01-基础概念/Q11] 怎么评估一个 Agent 的好坏？
- 🟡 `guide-170` [08-工程化实践/Q11] 用 LLM 给 LLM 打分有什么坑？
- 🟡 `guide-171` [08-工程化实践/Q12] 如何设计 Agent 的回归测试集？
- 🟡 `guide-172` [08-工程化实践/Q13] 只靠「请诚实回答」能否解决幻觉？
- 🟡 `guide-183` [08-工程化实践/Q24] 评估集泄露怎么防？
- 🟡 `guide-184` [08-工程化实践/Q25] 线上发现幻觉率升高，你如何排查？
- 🟡 `guide-201` [09-Prompt工程/Q26] 如何评估 Prompt 好坏？
- 🟡 `datawhale-052` [4. Agent/12] 你用过哪些Agent框架？选型是如何选的？你最终场景的评价指标是什么？
- 🟡 `datawhale-066` [6. 模型评估与 Agent 评估/1] 为什么传统的 NLP 评估指标（如 BLEU, ROUGE）对于评估现代 LLM 的生成质量来说，存在很大的局限性？
- 🟡 `datawhale-067` [6. 模型评估与 Agent 评估/2] 请介绍几个目前行业内广泛使用的 LLM 综合性基准测试，并说明它们各自的侧重点。（例如：MMLU, Big-Bench, HumanEval）
- 🟡 `datawhale-068` [6. 模型评估与 Agent 评估/3] 什么是“LLM-as-a-Judge”？使用 LLM 来评估另一个 LLM 的输出，有哪些优点和潜在的偏见？
- 🟡 `datawhale-069` [6. 模型评估与 Agent 评估/4] 如何设计一个评估方案来衡量 LLM 的特定能力，比如“事实性/幻觉水平”、“推理能力”或“安全性”？
- 🟡 `datawhale-070` [6. 模型评估与 Agent 评估/5] 评估一个 Agent 为什么比评估一个基础 LLM 更加困难和复杂？评估的维度有哪些不同？
- 🟡 `datawhale-071` [6. 模型评估与 Agent 评估/6] 你了解哪些专门用于评估 Agent 能力的基准测试？这些基准通常如何构建测试环境和任务？
- 🟡 `datawhale-073` [6. 模型评估与 Agent 评估/8] 什么是红队测试？它在发现 LLM 和 Agent 的安全漏洞与偏见方面扮演着什么角色？
- 🟡 `datawhale-074` [6. 模型评估与 Agent 评估/9] 在进行人工评估时，如何设计合理的评估准则和流程，以保证评估结果的客观性和一致性？
- 🟡 `datawhale-075` [6. 模型评估与 Agent 评估/10] 如何持续监控和评估一个已经部署上线的 LLM 应用或 Agent 服务的表现，以应对可能出现的性能衰退或行为漂移？
