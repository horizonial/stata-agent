# P2 核心能力深化设计稿

状态：`implemented and covered by release verification`
日期：2026-09-21

本目录包含五份轻量设计稿，用于在编码前对齐产品行为、技术边界、成功指标和明确不做的事项：

1. [RAG Ingestion v2](01-rag-ingestion-v2.md)
2. [Adaptive Research Plan](02-adaptive-research-plan.md)
3. [Product Evaluation Loop](03-product-evaluation-loop.md)
4. [Runtime Usage & Cost](04-runtime-usage-cost.md)
5. [Query Planning and Evidence Reranking](05-query-rewrite-rerank.md)

## 共同原则

- 用户讨论形成产品方向和默认策略，不自动升级为永久架构规则。
- 只冻结数据保真、可复现、可追溯、可暂停恢复等核心不变量。
- Agent 保持研究方法自由；系统约束执行真实性和流程完整性，不识别或白名单化研究方法。
- 自动模式应尽量少打扰用户；监督模式提高重大决策的可见性和确认频率。
- 新机制优先复用当前模块，不建设新的通用 Platform、Event Bus 或 Workflow Engine。

## 建议实施顺序

```text
RAG Ingestion vertical slice
        ↓
Adaptive Plan vertical slice
        ↓
把两者的真实指标汇入 Evaluation Loop
        ↓
Runtime Usage & Cost 聚合与展示
```

Evaluation 的最小比较合同可以先建，但不先建设脱离真实场景的评测平台。RAG 和 Plan 在开发时同步提供评测 Case，待指标稳定后再形成跨版本趋势。

## 权威边界

| 领域 | 权威事实 | 可重建/非权威内容 |
| --- | --- | --- |
| RAG | Source、Parse Revision、Knowledge Node、Index Revision、Retrieval Session | 排名缓存、评测报告、搜索 UI Projection |
| Plan | Plan Revision、Plan Adoption、Plan Node、执行与 Plan 的绑定 | 变化摘要、相似度、抖动诊断 |
| Evaluation | 普通使用产生的 Workspace/Turn/Journal/Run/Result/Feedback 事实 | 指标、趋势摘要、Benchmark 报告、Dashboard |
| Usage | Provider/Tool 实际使用事实、预算消耗 | 费用估算、效率摘要、优化建议 |

## 冻结前需要回答的问题

- 自动模式和监督模式的默认值以及切换入口。
- “实质计划变化”的产品展示方式，而不是硬编码研究方法分类。
- RAG 算法升级采用随软件版本发布，还是允许 Workspace 单独试验。
- 金额预算是否只提醒，还是未来允许用户配置硬停止。

这些问题不阻塞设计继续细化，但应在对应模块编码前确认。
