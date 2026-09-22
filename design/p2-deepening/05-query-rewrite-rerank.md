---
artifact: solution-brief
version: "0.1"
created: 2026-09-21
status: implemented-first-slice
---

# Solution Brief: Query Planning and Evidence Reranking

## Decision

RAG 使用独立的三阶段检索管线，而不是把 Query Rewrite、候选融合和最终排序混成一个分数：

```text
Original Query（不可覆盖）
    ↓
Query Planner → 0..N 个保守 Query Variant
    ↓
Lexical / Dense 多路候选召回
    ↓
RRF 粗融合
    ↓
Evidence Reranker
    ↓
去重与来源多样性选择
    ↓
Exact Read / Expansion / Context Compiler
```

Query Planner 只提高召回，不决定研究方法。Reranker 只判断节点对原问题和当前检索目标的
支持相关性，不判断研究结论是否正确。原始 Query、所有变体、完整候选池、融合分、精排分、
原因码和最终选择都进入 Retrieval Hop 的不可变记录。

## First Slice

首个本地实现不增加额外模型调用：

- `DeterministicQueryPlanner` 保留原 Query，并按需加入 public subquestion、objective 和去除
  问句脚手架后的 keyword variant；不自动发明翻译、理论或同义词。
- 每个 Variant 独立执行 FTS5 与可选 Dense Retrieval，再跨 Variant 融合。
- `DeterministicEvidenceReranker` 使用原问题、目标、章节和正文重新计算相关性，和 RRF 的
  候选来源分开。
- Evidence Selector 先去除文本重复并限制单一来源挤占，候选不足时再按精排顺序回填。

这些实现是可替换 Policy，不是永久算法。以后可以在真实评测通过后替换为模型 Query
Planner、领域词典、Cross-Encoder 或其他 Reranker，而不改 Retrieval Session 和账本结构。

## Authority and Trace

新增权威记录：

```text
knowledge_retrieval_query_variants
knowledge_retrieval_candidates
knowledge_retrieval_selections.rerank_score / relevance_label
```

因此系统可以分别回答：

1. Agent 最初搜索了什么？
2. 系统派生了哪些 Query，为什么？
3. 各召回器找到了哪些候选？
4. RRF 如何融合？
5. Reranker 为什么把某个节点排在前面？
6. 哪些节点因为重复或来源集中没有进入最终上下文？

## Upgrade Rule

模型型 Query Planner 或 Reranker 不得静默替换当前版本。候选 Policy 必须至少比较：

- Recall@K 与 Evidence Group Coverage；
- Rerank 后的 MRR / nDCG；
- 无答案误支持率；
- Corpus Role 泄漏；
- 来源多样性；
- 延迟、模型调用和 Token 成本。

算法升级可以改变未来检索结果，但不得改写历史 Retrieval Hop。
