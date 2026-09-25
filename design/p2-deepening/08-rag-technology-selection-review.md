---
artifact: adr
version: "0.1"
created: 2026-09-24
status: accepted_with_observed_default
---

# ADR-RAG-001：本地研究 RAG 的组件化技术选型

## Status

Accepted with an observed V0.1 default profile. The reranker and vector-storage
backend remain open sub-decisions.

**Date:** 2026-09-24  
**Deciders:** Founder / Product Owner, Stata Research Agent

## Context

当前系统已经具备 Canonical Knowledge Node、Source / Parse Revision、Corpus Role、
Retrieval Session / Hop、Query Variant、Candidate、Selection、Evidence Packet、Context Use
和 SQLite 权威账本。现有算法链为：

```text
pypdf 全页快速解析
→ 疑难页选择性 MinerU
→ 结构化 Canonical Node
→ SQLite FTS5 + 可选 Dense Retrieval
→ RRF
→ Deterministic Query Planner / Reranker
→ Agentic Multi-hop
→ Evidence Packet
→ Context Compiler
```

这些身份、版本和证据合同已经满足产品的追溯要求，不应由第三方 RAG 框架重新定义。
本次选型要解决的不是“采用哪套 RAG 全家桶”，而是：

1. Windows 11 本地产品默认交付哪些解析和模型能力；
2. 如何提高中英论文检索的 Precision、Grounding 和无答案可靠性；
3. 如何支持论文库扩大和多个 Workspace 并行，而不引入第二权威账本；
4. 如何控制模型体积、冷启动、CPU/RAM、许可证和安装复杂度；
5. 如何让未来模型和索引升级不改写历史 Retrieval Hop。

当前实现还有四个已知边界：

- `sentence-transformers` 与 embedding 模型不在核心安装依赖中，未配置时退化为词法检索；
-默认 Embedding Adapter 固定使用 E5 的 `query:` / `passage:` 输入格式，不能安全地直接替换成所有模型；
- Dense Index 目前把向量存为 SQLite BLOB，并在 Python 中逐向量计算 cosine；
- Reranker 是词项覆盖和先验分加权规则，不是语义 Cross-Encoder。

## Evaluation Criteria

候选方案按以下标准评估：

1. 中英及跨语言论文检索质量；
2. Stata 命令、变量名和错误信息的精确检索能力；
3. 公式、表格、跨页内容和页码定位；
4. Windows 11 CPU 默认可运行，GPU 只作为加速；
5. 本地离线、无 Docker 的安装体验；
6. 许可证允许公开发布和未来商业使用；
7. 可锁定模型、代码和解析器 revision；
8. 不绕过 SQLite 权威身份、Corpus Role 和 Trace；
9. 可在相同问题集上比较质量、延迟、内存和成本；
10. 失败后可降级，索引可以删除和重建。

## Decision

### Observed bake-off decision (2026-09-25)

A 30-paper, 1,511-page corporate-finance corpus and a frozen 41-case question set
were run on the target Windows host. The retained evidence is in
`verification/reports/rag-corporate-finance-bakeoff-v1.md`.

The observed V0.1 profiles are:

```text
pypdf fast path
-> critical-only MinerU recovery
-> SQLite FTS5 + multilingual-E5-small default
-> optional Qwen3-Embedding-0.6B GPU quality profile
-> deterministic CPU fallback
-> optional GPU adaptive fusion with bge-reranker-base
-> evidence-sufficiency and coverage guards
```

This supersedes the earlier proposal to make Docling the first rich-parser candidate.
Docling remains available for experiments, but did not recover one ciphered PDF and
hit an ONNX `bad_alloc` on another on the 16 GB target machine. BGE-M3 remains an
optional quality profile: it preserved full multi-hop coverage but did not beat E5 on
MRR, while taking 5.22x longer to build its index and 1.70x longer per query.

A second observed round compared additional embeddings and neural rerankers. Qwen3
Embedding 0.6B reached the best MRR (0.986 versus E5's 0.962) and full multi-hop
coverage, but used about 7.4 GB GPU memory during indexing and was 2.24x slower per
query. It is accepted as an optional quality profile, not the lightweight default.

Neither initially tested replacement-style neural reranker improved first-hit ranking: BGE reranker v2-m3 reached
MRR 0.949 at 2.14 seconds/query and Qwen3 Reranker 0.6B reached MRR 0.918 at 2.04
seconds/query. A later fusion round showed that replacing first-stage relevance was the
wrong composition. Adaptive fusion with `bge-reranker-base` reached MRR 0.977, complete
marker/multi-hop coverage, 100% no-answer accuracy and zero false abstentions on the
frozen set. In a paired 141-query-per-variant latency run it added 0.335 seconds on
average while invoking neural reranking on about 51.4% of queries. It is accepted for
the GPU balanced profile; CPU and missing-model paths remain deterministic. GTE
embedding/reranking failed reproducibly in the supported runtime;
Snowflake Arctic m-v2 requires an additional `xformers` runtime and remains on hold.

The experiment also showed that parser recovery was the largest quality lever:
positive-case source recall improved from 0.889 to 1.000 before changing the default
embedding model.

### 1. 总体架构

采用：

```text
自研权威层
+ 可替换的 Parser / Embedding / Dense Index / Reranker Policy
+ 外挂评测与非权威 Observability
```

不采用 LlamaIndex、Haystack、LangGraph、GraphRAG 或 LightRAG 作为主 Runtime。
这些框架可以用于离线实验或 shadow benchmark，但不得接管 Retrieval Session、Citation Identity、
Journal、Recovery 或 Evidence Authority。

### 2. 文档解析

V0.1 保留：

```text
pypdf 全页快速解析
→ Page Diagnostics
→ 仅疑难页调用 MinerU
→ pypdf 结果始终作为 fallback
```

新增 `DoclingParserAdapter` 作为首要候选，而不是立即替换 MinerU。两者使用同一批真实论文比较
section、table、formula、page/bbox/span、延迟、内存和失败降级质量。

其他方案：

| 方案 | 结论 | 理由 |
| --- | --- | --- |
| Docling | P1 候选 | MIT、本地、Windows、统一结构、表格和公式能力完整 |
| MinerU | 保留可选 rich parser | 已有真实实验，复杂公式和表格能力强；需锁定版本并履行附加许可条款 |
| GROBID | 后续 metadata adapter | 适合 DOI、作者、参考文献和 citation context，不适合主公式/表格解析 |
| Marker | shadow benchmark | 结构能力较强，但代码和模型权重许可需要分别审查 |
| Unstructured | 不进入主链 | 格式覆盖广，但公式能力不是本产品重点优势，依赖和 telemetry 需要额外治理 |
| PyMuPDF4LLM | 不默认采用 | 性能和布局能力有吸引力，但 AGPL 或商业许可不适合直接打入当前产品 |

### 3. 分块与知识身份

继续使用结构感知 Canonical Node 和父子/相邻关系，不改成固定字符 Chunk。长节点只在检索投影层
做 token-aware 子段，子段必须映射回原 Node、Source Revision、Page Range 和 Span。

解析器输出只是候选结构；正式 Node 身份仍由当前 ingest / commit 流程生成。

### 4. 词法与向量索引

保留 SQLite FTS5。它继续承担 Stata 命令、错误文本、变量名、估计量名称和论文原词的精确召回。

Dense Index 按以下顺序演进：

1. 当前 flat cosine 作为正确性基线；
2. `sqlite-vec` 作为首要 P1 候选，在同一 SQLite 边界内提供 C 级向量检索；
3. 当节点数量和压力测试证明 SQLite 路径不足时，再评估 LanceDB；
4. Qdrant 只在未来形成共享检索服务或更大规模多 Workspace 时评估；
5. FAISS 只作为算法后端或 benchmark，不承担 metadata、权限和权威身份；
6. Chroma 暂不采用。

任何外部索引都只是可重建 Projection，必须保存并返回：

```text
canonical_node_id
source_revision_id
parse_revision_id
index_revision_id
embedding_profile_revision
```

索引后端切换的触发条件不是 PDF 文件大小，而是 Canonical Node 数量、向量维度、P95 查询延迟、
索引构建/增量更新时间和峰值 RAM。

### 5. Embedding 模型

不在没有本项目评测证据时直接冻结新默认模型。候选矩阵为：

| ID | 模型 | 定位 | 主要风险 |
| --- | --- | --- | --- |
| E0 | `intfloat/multilingual-e5-small` | 当前轻量基线 | 上下文短、质量上限有限 |
| E1 | `BAAI/bge-m3` | 成熟的多语言/长文本候选 | 569M、约 2.27 GB；只用 dense 会浪费其 sparse / multi-vector 能力 |
| E2 | `intfloat/multilingual-e5-large-instruct` | 成熟 instruct 候选 | 必须使用其专用 query instruction，不能沿用 E5-small 模板 |
| E3 | `Alibaba-NLP/gte-multilingual-base` | 较轻的长文本候选 | `trust_remote_code` 增加供应链治理成本 |
| E4 | `Qwen/Qwen3-Embedding-0.6B` | 已接受的 GPU 质量 profile | MRR 最优；约 7.4 GB 建索引显存，查询与构建成本高于 E5 |
| E5 | `Snowflake/snowflake-arctic-embed-m-v2.0` | 轻量长文本候选 | 当前 profile 强制依赖 xformers，Windows 打包待解决 |
| E6 | `nomic-ai/nomic-embed-text-v2-moe` | 轻量 MoE / Matryoshka 候选 | 自定义 MoE 运行时和 512-token 上限 |
| E7 | `google/embeddinggemma-300m` | 端侧轻量候选 | Gemma 条款与 gated asset 安装流程 |

Qwen3 4B / 8B、GTE-Qwen 1.5B 及更大模型不作为 Windows CPU 默认候选。
Jina 非商业许可模型只允许实验，不进入公开产品默认依赖。

Observed adoption:

- E0 `multilingual-e5-small`：V0.1 默认；
- E4 `Qwen3-Embedding-0.6B`：可选 GPU 高质量 profile；
- E1 BGE-M3：保留实验 profile；
- E3 GTE multilingual：当前运行时不兼容；
- E5-E7：未满足运行时或发布门槛，不进入默认安装。

Embedding Adapter 必须从“E5 专用实现”改为 profile 驱动：

```text
model revision
runtime backend
pooling
normalization
query template / instruction revision
document template
maximum input tokens
output dimension
quantization
```

模型变更必须创建新的 Dense Index Revision，不覆盖旧索引。

### 6. Reranker

保留 `DeterministicEvidenceReranker` 作为无模型降级方案；新增统一的
`CrossEncoderEvidenceReranker` port。首批候选为：

| ID | 模型 | 定位 | 主要风险 |
| --- | --- | --- | --- |
| R0 | 当前 deterministic | fallback / 可解释基线 | 语义能力弱 |
| R1 | `BAAI/bge-reranker-v2-m3` | 成熟多语言候选 | 0.6B，实测可用但被 base 模型支配 |
| R2 | `Alibaba-NLP/gte-multilingual-reranker-base` | CPU 友好候选 | 约 0.3B，但需要 `trust_remote_code` |
| R3 | `Qwen/Qwen3-Reranker-0.6B` | 质量优先候选 | decoder-style scoring，模板、内存和延迟更复杂 |
| R4 | `BAAI/bge-reranker-base` | GPU balanced adopted profile | 约 0.28B、MIT；CPU 推理仍过慢 |
| R5 | `cross-encoder/mmarco-mMiniLMv2-L12-H384-v1` | 低延迟实验候选 | 速度较好，但本题集 MRR 低于 baseline |

只对融合后的 Top 24～50 候选重排，不让 Cross-Encoder 扫描整个语料库。是否进入默认策略由
Precision@K、MRR、nDCG、无答案误支持率、P95 延迟和内存共同决定。

Observed adoption: V0.1 不启用 replacement-style 或 always-on neural reranker。GPU
balanced profile 使用 `bge-reranker-base` 的 adaptive rank fusion；调度器只在复杂、低分差且
词法/向量排序冲突的查询上重排 Top 32。融合权重为 neural 0.55、deterministic 0.25、
first-stage rank 0.20。Reranker 失败时同一查询回退 deterministic。CPU 默认不加载 neural
reranker：同一方案 CPU 平均约 6.04 秒/query，不符合默认交互延迟。

### 7. 本地推理运行时与安装

模型不直接膨胀核心安装包。模型文件存放在受控的 per-user model cache 中，按 revision、hash、
license manifest 和 runtime profile 登记，并由首次设置或显式能力包安装。

开发和 bake-off 阶段使用 Sentence Transformers / PyTorch；正式 Windows CPU 交付优先验证
ONNX Runtime 或 OpenVINO INT8。只有在目标模型导出、pooling、分数和速度均通过等价性测试后，
才采用优化后端。TEI、Docker / WSL sidecar 不作为 V0.1 默认安装要求。

如果本地模型不可用，系统必须显式显示 lexical-only / no-neural-rerank degraded profile，不能静默
声称正在运行 Hybrid RAG。

### 8. Query Planning 与多跳

保留 `DeterministicQueryPlanner` 作为普通查询路径；增加可选 `ModelQueryPlanner`，只在查询模糊、
方法比较、证据冲突或上一跳存在明确证据缺口时运行。原始 Query 永远保留，所有变体和原因进入
Retrieval Hop。

不默认采用 HyDE。若未来实验 HyDE，其假设答案只能生成检索 Query，不得进入 Evidence Packet。

当前使用 Canonical Node 邻接扩展和 Agentic Multi-hop。GraphRAG / LightRAG 只在真实使用证明
“跨论文实体关系”和“全库整体主题问题”长期失败时进入 shadow benchmark。

### 9. Citation Grounding

正式引用身份继续由本项目控制。模型输出必须引用已进入 Context 的 Node ID，系统将其解析为：

```text
claim
supporting_node_id
source_revision_id
page / span locator
retrieval_session_id / hop_id
support disposition
```

第三方 Citation Engine、Ragas、DeepEval 或 LLM Judge 可以评价质量，但不能授予 Evidence 权威性。

### 10. Evaluation 与 Observability

保留当前确定性评测作为硬门禁，增加模型辅助评测作为质量信号：

- Retrieval：Recall@20/50、Precision@K、MRR、nDCG、Evidence Group Coverage；
- Grounding：Claim Citation Coverage、Citation Precision、Faithfulness、Noise Sensitivity；
- Agentic：hop 数、novel evidence、证据缺口关闭率、停止原因；
- Negative：无答案准确率、误支持率、Corpus Role Leakage；
- Runtime：P50/P95、冷启动、峰值 RAM/VRAM、索引时间、安装/下载体积；
- Product：Context 利用率、Citation 利用率、用户纠正率。

Ragas / DeepEval 可以作为离线 grader adapter，不作为生产 Runtime 依赖或唯一门禁。

Observability 采用 OpenTelemetry / OpenInference 命名对齐；Phoenix 可作为开发期本地诊断 UI。
SQLite Journal 与 Retrieval 账本仍是权威 Trace，Span 永远是非权威 Diagnostics。

## Adoption Gates

所有模型和后端在采用前必须在同一组冻结输入上比较：

```text
相同 Source / Parse / Node Revision
相同问题集和 split
相同候选深度与 Top-K
相同 Context Budget
相同生成模型与提示版本
```

最少覆盖：

- 中文问题检索英文论文；
- 英文问题检索英文论文；
- 方法比较和多跳；
- 表格、公式、章节与页码定位；
- Stata Help 精确命令与报错；
- 近似但错误的 hard negative；
- 知识库无答案；
- 10k / 50k / 100k / 250k Node 压力档；
- 多 Workspace 并发查询。

候选通过后仍以新的 versioned policy / model / index revision 显式采用，不改写历史记录。

## Consequences

### Positive

- 保住研究证据、版本和恢复模型，不引入第二权威系统；
- 可以独立替换解析器、Embedding、索引和 Reranker；
- 模型选择以本项目真实论文评测为依据，而不是通用榜单；
- 本地轻量 fallback 与质量 profile 可以共存；
- 许可证、模型资产和 degraded mode 进入正式产品合同。

### Negative

- 需要维护 Profile、Adapter、模型资产清单和组合评测；
- Cross-Encoder 和高级解析器会增加下载体积、冷启动和本地资源占用；
- 在完成 bake-off 前不能宣称已经选定最终默认 Embedding / Reranker；
- Docling、MinerU、ONNX/OpenVINO 都需要单独的 Windows 安装与性能验证。

### Neutral

- GraphRAG、Qdrant 和大型模型没有被永久排除，只是必须由真实规模和质量证据触发；
- SQLite 继续保存权威事实，向量索引仍属于可删除、可重建 Projection。

### Model Choice

| Consequence | This decision |
| --- | --- |
| **Build, buy, or prompt** | 使用可本地部署的开源 Embedding / Reranker，保留远端 API 为非默认可选能力；不自行训练首版模型 |
| **What is now coupled to it** | Query / document template、pooling、dimension、index revision、阈值、上下文预算和评测集 |
| **Operating cost accepted** | 首次模型下载、索引构建、每次 Top-N rerank 的 CPU/GPU 延迟；具体上限由 bake-off 决定 |
| **Reversal cost** | 实现新 Adapter、重建 Dense Index、重新校准阈值和跑完整评测；历史 Retrieval Hop 不迁移 |
| **What would reopen this** | 真实 Recall/Precision 回归、P95/内存不可接受、许可证改变、Windows 打包失败或新模型在冻结评测上显著优于当前采用版本 |

## Alternatives Considered

### 全量采用 LlamaIndex / Haystack

组件齐全，适合快速搭建和离线实验；但会与现有 Node、Session、Journal、Recovery、Citation 和
SQLite 事务形成重复状态，因此不采用为主架构。

### 采用 LangGraph 运行 Agentic RAG

具备 checkpoint、interrupt 和 durable execution；但这些能力项目已经在 Turn / Step / Operation /
Waiting / Recovery 中实现。引入后会出现两套状态机，因此只允许无副作用实验。

### 默认采用 Microsoft GraphRAG / LightRAG

适合大语料全局主题和实体关系；当前论文库规模与主要问题没有证明其索引成本和派生事实复杂度
值得。保留为 future shadow benchmark。

### 直接采用 Qdrant / LanceDB

都具备更成熟的大规模向量能力，但会引入第二存储边界。当前先比较 sqlite-vec；规模证据出现后
再采用外部 Projection。

### 只使用远程 Embedding / Rerank API

降低本地运行负担，但引入隐私、网络、成本和供应商依赖，不符合默认本地能力要求。

## References

- [Qwen3 Embedding official repository](https://github.com/QwenLM/Qwen3-Embedding)
- [Qwen3 Embedding 0.6B model card](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B)
- [Qwen3 Reranker 0.6B model card](https://huggingface.co/Qwen/Qwen3-Reranker-0.6B)
- [BGE-M3 documentation](https://bge-model.com/bge/bge_m3.html)
- [GTE multilingual base model card](https://huggingface.co/Alibaba-NLP/gte-multilingual-base)
- [Snowflake Arctic Embed m-v2.0 model card](https://huggingface.co/Snowflake/snowflake-arctic-embed-m-v2.0)
- [Nomic Embed Text v2 MoE model card](https://huggingface.co/nomic-ai/nomic-embed-text-v2-moe)
- [EmbeddingGemma 300M model card](https://huggingface.co/google/embeddinggemma-300m)
- [multilingual-e5-large-instruct model card](https://huggingface.co/intfloat/multilingual-e5-large-instruct)
- [GTE multilingual reranker model card](https://huggingface.co/Alibaba-NLP/gte-multilingual-reranker-base)
- [Sentence Transformers inference optimization](https://www.sbert.net/docs/sentence_transformer/usage/efficiency.html)
- [Docling official repository](https://github.com/docling-project/docling)
- [MinerU license](https://github.com/opendatalab/MinerU/blob/master/LICENSE.md)
- [PyMuPDF licensing](https://pymupdf.io/licensing)
- [sqlite-vec official repository](https://github.com/asg017/sqlite-vec)
- [LanceDB official repository](https://github.com/lancedb/lancedb)
- [Qdrant local quickstart](https://qdrant.tech/documentation/quick-start/)
- [FAISS installation](https://github.com/facebookresearch/faiss/blob/main/INSTALL.md)
- [Microsoft GraphRAG](https://microsoft.github.io/graphrag/)
- [Ragas metrics](https://docs.ragas.io/en/latest/concepts/metrics/available_metrics/)
- [OpenInference semantic conventions](https://github.com/Arize-ai/openinference/blob/main/spec/semantic_conventions.md)
