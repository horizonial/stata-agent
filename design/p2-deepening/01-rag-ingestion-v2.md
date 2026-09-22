---
artifact: solution-brief
version: "0.1"
created: 2026-09-21
status: implemented-first-slice
---

# Solution Brief: RAG Ingestion v2

## Problem Recap

当前系统已经具备论文文件同步、pypdf 快速解析、疑难页 MinerU、Canonical Knowledge Node、Dense Index Revision 和 Agentic multi-hop，但“不同结构应该如何解析和分块”仍主要由统一规则承担。继续增加论文类型后，统一 Chunk 策略容易在正文、表格、公式、跨页段落和文风样例之间顾此失彼。

## Proposed Solution

建设结构感知、可演进的 Ingestion Pipeline。用户只需把论文放入指定目录；系统自动识别变化、解析文档结构、生成可引用节点并更新当前索引。普通内容增删自动生效，算法或 Embedding 升级则通过真实问题集比较后随软件版本采用。算法可以变化，但历史引用始终能够定位到原始论文、页码和文本区域。

```text
Source Discovery
    ↓
Fast Parse（普通正文）
    ↓ suspicious pages only
Rich Parse（表格/公式/跨页疑难内容）
    ↓
Structure Normalization
    ↓
Document-specific Chunking
    ↓
Canonical Knowledge Nodes
    ↓
Lexical + Dense Index Revision
```

## Key Features

1. **结构感知解析：** 标题、段落、列表、表格、公式、脚注和跨页连续内容保留不同结构语义，不把 PDF 只当作一段长文本。

2. **按语料角色分块：** 文献正文、Stata Help 和文风样例使用不同策略；文风节点只能提供风格示例，不能成为事实证据。

3. **稳定引用定位：** 节点保存 Source Revision、Parse Revision、页码、章节和局部 Span；检索算法升级不改写历史 Citation。

4. **自动内容同步：** 新增、修改或删除论文后自动建立完整的新 Index Revision，验证成功后切换；失败继续使用旧索引并展示错误。

5. **算法升级评测：** Chunk、Embedding、Parent Expansion 或 Reranker 改变时，与当前发布策略进行真实问题集比较，不因“技术更新”自动认定更好。

## Success Metrics

| Metric | Current | Target | Timeline |
| --- | --- | --- | --- |
| 可解析节点的引用定位完整率 | 已有 page/source，未形成统一结构指标 | 100% 具有 source revision；适用节点具有 page/section/span | 首个 vertical slice |
| 关键问题 Recall@K | 已有真实 DID 评测基线 | 新策略不得使任何关键 Case 丢失全部相关节点 | 每次算法升级 |
| 无答案拒答质量 | 已有无答案测试 | 不因 Parent Expansion 或 Rich Parse 增加伪支持 | 每次算法升级 |
| 普通论文 MinerU 使用范围 | 已实现疑难页选择 | 保持选择性处理，并记录选择原因和耗时 | 首个 vertical slice |
| 索引更新完整性 | 版本化索引已存在 | 更新失败时 current index 永不指向半成品 | 首个 vertical slice |

## Trade-offs Considered

| What We're Not Doing | Why |
| --- | --- |
| 为全部 PDF 执行全文 OCR/MinerU | 慢且会降低普通文本的准确性；保留疑难页升级路径 |
| 现在切换到 HNSW/FAISS 服务 | 当前本地论文规模尚未证明 Flat Cosine 是瓶颈 |
| 建设 GraphRAG | 多跳问题先由 Agentic Retrieval 解决，避免维护第二套知识图谱 |
| 全局固定一种 Chunk 大小 | 不同文档结构和用途不适合共享唯一参数 |
| 每次内容变化都要求用户采用索引 | 普通内容同步应自动完成，用户不需要理解索引实现 |

## Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
| --- | --- | --- | --- |
| 结构解析增加节点数量和上下文噪声 | Medium | High | Parent/child 两阶段召回；限制扩展窗口；记录 token 成本 |
| Chunk 策略升级使旧引用不可解释 | Medium | High | Source/Parse/Node identity 不可变；历史 Retrieval Session 固定命中节点 |
| 自动内容同步误删可用索引 | Low | High | 新 Revision 完整构建和验证后再切换；旧 Revision 可恢复 |
| MinerU 失败造成整篇文献不可用 | Medium | Medium | 保留 pypdf 结果和 page finding；仅疑难页降级并显式标注 |
| 评测集过度集中于 DID | High | Medium | DID 作为首个真实库，后续增加至少一个不同结构领域作为 holdout |

## Next Steps

1. 盘点当前 Canonical Node 对 section/span/table/formula 的表达缺口。
2. 定义 Literature、Stata Help、Style Exemplar 三类 Ingestion Policy 的最小字段。
3. 用现有十篇 DID 论文建立结构解析与 Parent Expansion Case。
4. 先实现一种候选策略与当前策略的可重复比较，不同时加入多个新算法。
5. 冻结内容同步与算法升级两条不同的采用路径。

## Implementation Record

首个 production slice 已完成：Canonical IR v2 为结构节点保存页码、页内 span、语料角色、
Evidence eligibility 和表格 payload；同一文件在 IR/ingestion policy 升级后会重解析，而不是
被内容 hash 错误跳过。选择性 MinerU 失败时保留完整 pypdf 解析并登记 degraded finding；后续
候选解析失败也不会覆盖 last-known-good current parse。服务启动可显式装配 pinned 本地
Sentence Transformer 与 MinerU，浏览器资料页展示 parser profile、IR、节点数和疑难页诊断。

本轮按 Founder 指示暂缓跨版本评测采用机制；Dense/lexical index 仍是可重建投影，历史 Source、
Parse、Node 与 Retrieval Session 身份保持不变。
