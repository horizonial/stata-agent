# DD-07 详细设计：文献管线（双 RAG）与 skill 规范

> 文档层级：`design/SPEC.md` v0.5 的下位细化。本文把 SPEC §4.5（双 RAG/信任角色/引用纪律）、§4.6（skill）、§4.10（联网补文献）落到可实现设计；评测配合 DD-06 L1（检索 gold set）。
> 术语以 SPEC/DD-01（CitationClaim/source_role/chunk_id）为准。

---

## 0. 设计输入与要回答的问题

**回答**：摄取怎么保证稳定/可溯源/可重建？中文 PDF 与扫描件怎么处理（OCR 接线）？混合检索怎么落地、rerank 要不要？检索结果怎么约束到 citable 纪律？skill 文件长什么样、怎么激活/预检？

## 1. 双库与信任角色（回顾，本设计给落地）

- `top_journals/`（style_only：文风/结构，**不得产生引用**）
- `project_papers/`（citable_evidence：变量/方法/可引用，带 bibtex）
- `background_only`：理解用，终稿不可直接引用。
角色是**摄取时打到 chunk/文献上的类型字段**，检索与 writer 都按它过滤（不是靠 prompt 提醒）。

## 2. 摄取版本链（可溯源、可重建、可重跑）

```
file_hash → parser_version → page/section/bbox → chunk_id(稳定)
          → source_role → 文本/图 → embedding_version → index_version
```
- **稳定 chunk_id**：`sha256(doc_id|parser_version|locator|first~last)`，内容变了 chunk_id 变，旧 chunk **失效保留历史**（不覆盖）——文献更新不污染历史引用（DD-01 事件引用旧 chunk 仍可查）。
- **canonical/derived**：原始 PDF/文本 = canonical（哈希存储，只读）；分块、embedding、向量索引 = derived（可重建、可重灌）。
- **摄取记账**：每次摄取写事件（可复用 DD-01 `artifact.stored` + 索引清单），失败可重放（幂等：同 file_hash 同 parser 不重复建块）。
- 抽取 QA（审计 A 精神）：每批抽查页序/表格/脚注/双栏/错页；parser 版本升级必须回归（L1 表解析/抽取 fixture）。

## 3. 解析管线（含中文/扫描）

1. 文本 PDF：pymupdf/pdfplumber → 页/bbox → 去页眉页脚/脚注归属。
2. 表格/图：detect → 表格单独成块（供 DD-05 esttab 对照/表引用），图/公式成图块（后续可 OCR/视觉）。
3. **扫描件（中文）**：走 OCR（接 DD-03 §7 视觉能力；local_strict 用本地 OCR，禁止因"要解析文献"把文献发远端）。OCR 结果 = derived，标 `ocr:true + 置信度`，引用 span 允许但标注为 OCR 转写。
4. 分块策略：按节/段优先（保语义），块带 `doc_id + page/section/bbox + role`；中英混排不硬切。

## 4. 混合检索 + rerank（落地）

```python
class Retriever(Protocol):
    def search(query, *, top_k, roles) -> list[Chunk]: ...
```
- 内部 = 向量召回（Embedder→VectorDB）+ 词法精确（BM25/FTS 于 chunk 文本，grep 于"确定词/变量名/表名"）并查 → 合并去重 → （可选）rerank。
- **角色过滤在检索参数**：写作/可行性取证只能 `roles=[citable_evidence]`；文风查询 `roles=[style_only]`；二者永不混。
- **rerank 是否上**由 gold set 消融定（DD-06 L1 指标），先不加，数据说话。
- 每块带回 `doc_id + source_role + page/section/span`；检索结果本身是 untrusted（DD-04 信任标签），不能当指令。

## 5. citable 纪律（引用溯源）

- 写作引用只能来自 `citable_evidence` 的块，且建 CitationClaim（chunk_id+span+版本）——**A 文结论不许安到 B 文**（DD-01 locator）。
- 检索到的候选若被引用，writer/validator 用 CitationClaim 校验存在且支持该句主张；不支持 → 回检索/拒引。

## 6. 联网补文献（§4.10 接线，可选）

- `web_fetch/download` 拉到新 PDF（NBER/期刊开放版）→ 走同一摄取链（来源 URL + hash + parser 版本），标 `retrieved_untrusted` → 入 citable（需角色/许可判断）或 background。
- 本地没有、确认是开放文献 → approved_remote/mixed 才拉；local_strict 禁。
- **拉文献 ≠ 拉进证据**：下载后是否可引用由"来源角色 + 学者确认"定，不自动升级为证据。

## 7. skill 规范（文件化方法论 = 版本化政策包）

目录 `skills/<slug>/SKILL.md`，用 frontmatter + 正文：

```markdown
---
name: panel_did
version: 1.2.0
role: methodology            # 或 robustness / mechanism / writing
triggers:                    # 命中条件（由 harness 评估当前 task/ResearchState）
  - phase: ESTIMATION
  - needs: [did, event-study, parallel-trend]
requires:                    # 审计 D2
  ados: [reghdfe, esttab, coefplot]
  stata_min: "17"
prechecks:
  - data.has_panel_keys
  - data.has_variable(treat)
steps:                       # 有序；每步可带 → evidence/act
  - 平行趋势：先事件研究/安慰剂再进 DID
  - 主 spec：fe + cluster(id)，过研究闸门
rules:                       # 方法学硬规则（禁/允）
  - forbid: 单期 DID 无 parallel-trend 证据却宣称因果
  - forbid: 把探索性多 spec 只挑显著报
examples:                    # 每个给"输入→应产出"可判例
  - run a: …
disabled_when: ...
```

- **skill = 政策包**（claw 学习册 + SPEC §4.6）：给"什么时候用、先检查什么、步骤、哪些不许"——不是 prompt 片段，是可判的规则（L1 skill 触发评测）。
- **激活流程**：harness（DD-02 §6）按 phase + triggers 从 Registry 选 → preflight 查 requires（`which <ado>`，缺则提示安装/不静默跑）→ 注入 DD-03 L1 稳定规则层。
- 变更 = 新版本；skill 回归（跑它的 examples）在 L1/L2。

## 8. 目录/接口

```
libraries/top_journals/{pdf, chunk/, index_v/}     # role=style_only
libraries/project_papers/{pdf, bibtex.json, chunk/, index_v/}  # role=citable_evidence
skills/<slug>/SKILL.md
```
检索接口：`Retriever.search(query, top_k, roles, filters)`；摄取接口：`IngestPipeline.ingest(lib, path, parser_ver)`（幂等）；skill Registry：`skills.load(slug) / match(ctx)`。

## 9. 开放决策 / TODO

1. 中文 PDF 是文本版还是扫描版——先抽样验证，决定 OCR 优先级（SPEC §7 #7 落地）。
2. embedding/向量库具体（bge-m3 本地 vs API；Chroma 起步）；在 privacy 三档内定。
3. chunk 大小/重叠与"表格块"切法（影响引用粒度）——用 gold set 消融。
4. rerank 上不上、用什么。
5. 文献去重（同文多版本、工作论文 vs 发表版）与"失效保留历史"的展示规则。

## 10. 面试叙事（一句话）
> 文献不按"篇"存，按"带角色与页码的块"存，写作只能引用 citable 块的原文 span——把"防编造引用"从口号变成检索与渲染的类型约束；skill 是带前置检查和禁用条件的可判政策包，不是会背的 prompt。
