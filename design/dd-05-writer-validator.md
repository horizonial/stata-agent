# DD-05 详细设计：Writer + Validator（证据 → 实证初稿）

> 文档层级：`design/SPEC.md` v0.5 的下位细化。本文把 SPEC §4.7（Writer）、§4.8（EvidenceBundle/Claim/EvidenceCard/分级证据门槛）、§4.9.4（validator 相关）与审计 B1/B2/C1（esttab 表语义、描述/平衡表、figure 嵌入）落到可实现设计。数据契约以 **DD-01**（Claim/EvidenceCard/Locator）、上下文以 **DD-03** 为准。
> 目标产品：中文 Word 实证初稿，**数字/引用全可溯源、文风学顶刊、学者能在 Word 里继续编辑**。

---

## 0. 设计输入与要回答的问题

**回答**：证据怎么变成段落/表/图？esttab 表语义解析器怎么做（渲染 + 反查双向）？Validator 查什么、分级证据门槛 L-C/L-R/L-D 的检查项？图/描述/平衡表怎么进稿？Word 产出链路选什么？

## 1. 证据 → 初稿的管线（写入权分离落地）

```
Stata 结果 → EvidenceCard(validator 签发, 含 locator) → Claim(evidence_builder 合成)
   → 写作单元(NarrativeUnit) → 初稿(Word)
                      writer 只消费已签 Claim/Card；模型只做"组织与措辞"
```
- **NarrativeUnit**：最小写作单元 = 一个主张 + 其支撑（`claim_id` / 段落文本 / 引用表格或图的 `table_id`|`figure_id` / 引文 `citation_claims`）。整篇 = 有序 NarrativeUnit 树（方法/结果/解读/稳健性）。
- **render_policy 硬规则**：模型产出的草稿里任何统计数字、任何表格单元格、任何引文，**必须有对应已签 Claim/Card 的 id**；没有 → writer 在渲染期拒绝该句并回查。模型能"提议叙述"，不能"发明事实"（DD-01 §2.7）。

## 2. 表格 = esttab 语义对象（渲染与反查共用同一模型）

审计 B1：格子身份是 `(table, panel, row_label, col_group, stat_type)`，不是列序数。定义 **TableModel** 作为中间表示（esttab→TableModel→rtf/Word；反查 = 从稿中格值 → TableModel → 原 result_id）：

```python
class TableModel(BaseModel):
    table_id: str; title: str; notes: list[str]
    panels: list[Panel]            # 多 panel（A 面板…/机制分列…）
    columns: list[Col]             # 每列带 col_group 标签（如"处理×组A"）
    rows: list[TableRow]           # row_label + per-(col,stat_type) 的值

class Cell(BaseModel):
    stat_type: Literal["coef","se","p","N","r2","mean","sd","t","fe_flag","…"]
    raw_value: str | None          # 展示串（如 "0.038***" / "Yes" / "–"）
    canonical: ValueRef | None     # → NumericClaim/card（未接地=灰格，不许交付）
```
- **esttab 解析器（双向）**：读 esttab/estout 的 rtf/csv/txt → 还原成 TableModel（含星号、括号 SE、FE 行 "Yes/X/No"、尾行 N/R²/聚类）；反向把 TableModel 渲染成 Word/rtf 表。解析器是 validator 与 writer 的共同底层（不是只给 writer）。
- **round-trip 检查**：draft 里 `0.038***` → TableModel.cell(…) → canonical 0.0376(p<0.01) 数值+显著性一致才过（防小数点/星号/排版篡改）。显示值/原始值分离：canonical 存原始精度，显示串存展示精度。

## 3. 内容产出

**表**：
- 回归表：表语义键渲染（系数行、括号 SE、星号、FE/聚类/N/R² 尾行）。模板学顶刊（style_only）。
- **描述/平衡表（Table 1，审计 B2）**：mean/sd/组间差+t —— 独立模板，复用 EvidenceCard(kind=numeric) 语义键。

**图（figure，审计 C1）**：Stata 图已落 artifact（DD-01）→ figure EvidenceCard（含 x_var/est_point_ref/ci_ref）→ writer 嵌入 Word，caption 写"图 N：…"并引支撑 claim。**没被 figure 卡支撑的图不许进正文结论**（agent 不得空口说"图显示显著"）。

**方法/解读文字**：NarrativeUnit 由 claim 组织；"为什么用这个模型/样本"引用 ResearchState 的 decision/amendment（DD-01）与文献（citation）。

## 4. Validator 规则集（写稿前/稿中必跑）

| 检查 | 对象 | 规则 | 不过 |
|---|---|---|---|
| 数字接地 | 每格/每数 | 命中已签 NumericClaim/card；round-trip（显示↔canonical 值+显著性） | 回写/回 ESTIMATION |
| 引用接地 | 每条引文 | 溯源到 citable_evidence 真实块（chunk_id+span）；style_only/background 不可当引文 | 回检索 |
| 复现 | 每表/每图 | 对应 dofile 可跑：data_sig+env_sig+command_hash 一致（DD-01 Provenance） | 回 ESTIMATION |
| 样本/模型一致 | ResearchState | 稿里样本口径/变量/识别与 ResearchState/amendment 一致（防止探索当确认） | 回 VALIDATION 初段 |
| 图 claim 一致 | figure | caption 主张与 figure 卡一致（读图，DD-03 §7） | 回解读 |
| 完整性 | 结构 | 方法/结果/解读/局限齐全；局限用研究者自由度账本（何时看过结果） | 回 WRITING |
| 对抗注入 | 全稿 | 幻觉数字/假引用/截断星号/陈旧 Claim 抓出 | 回 WRITING |

**分级证据门槛实现（SPEC §4.8 的 L-C/L-R/L-D）**：

| 等级 | 检查项最小集 | 放行给谁 |
|---|---|---|
| L-C 可行性 | 有据文献 + 变量可构造 + 缺项清单 + 学者确认 | DESIGN 出口 |
| L-R 单次运行 | rc=0 + 结构化结果 + env_sig/data_sig + 校验过 + 可复现 | ESTIMATION 内 claim 可签 |
| L-D 可交付 | 每表/图有 dofile 可跑 + 数字/引用全溯源 + 复现 manifest + 对抗全抓 + 结构完整 | 出 Word |

## 5. Writer 流程（分节，学顶刊）

1. **取料**：从 DD-01 投影取"被选定用于初稿"的 Claim 集（main_result 那支 + robustness 变体）+ 文风样本（style_only 库，仅表达约束）。
2. **分节提案**：模型给 NarrativeUnit 树（方法/结果/解读/稳健性/局限），每 unit 挂 claim/card id；writer 校验引用完备。
3. **逐段写作**：模型措辞，writer 拦截"无 id 的数字/引文"（写一个 hook 在句子级扫）。
4. **渲染**：表走 esttab→TableModel→Word；图嵌 figure；描述/平衡表模板。
5. **validator**（§4）全绿 → 出 Word；任何一格灰（未接地）都不许交付（宁缺勿错）。

**引用纪律**：只能引 `citable_evidence`；style_only 只影响"怎么说"不改"说什么"（SPEC §4.5）。

## 6. Word 产出链路（选型仍开放，给倾向）

目标：学者能继续编辑；表/图嵌入；数字可溯源（必要时旁注/批注隐藏引用）。
- 候选 A：结构化 → 中间 markdown/HTML（带 claim id 属性）→ pandoc → docx（样式少、自动化顺）。
- 候选 B：python-docx 直出（样式控制细，但表格/图+溯源批注工作量大）。
- 候选 C：rtf 表直出 + Word 主文档引用。
- **倾向 A + 溯源旁注**：NarrativeUnit 渲染成 docx 时把 claim/card id 写成**隐藏文字或批注**（学者平时不见、评审可见），保"Word 可编辑"+"数字可追"。`[TODO]` 选型在 M3 前用样例定（拿一篇真稿双跑 A/C 比可编辑性）。

## 7. 接线

- 输入：DD-01 的 claim_index/card/result_index + ResearchState（main_result 选择）；DD-03 只负责把"要写哪些 claim"放进程；writer 不读对话。
- 事件：产出时追加（写作/交付相关在 DD-01 事件目录补 `draft.rendered`/`draft.validated`，或复用已有；见 §8 TODO）。
- validator 失败回 WRITING/VALIDATION（DD-02 §3 回退边已含）。

## 8. 开放决策 / TODO

1. esttab 解析器对**多种 esttab 方言/分隔符/编码(GBK)** 的健壮性（M3 前用真 esttab 输出建 fixture 集）。
2. Word 溯源旁注形式：docx 批注 vs 隐藏文字 vs 单独 reference map（学者可编辑性/评审可查性取舍）。
3. 表/图编号与正文交叉引用管理（TableModel.figure_id 全局表）。
4. 长稿分节写作的并行（结果多张表时先方法→表逐个，不并行；稳健性可 batch）——与 DD-02 并行规则一致。
5. "局限"章节自动收集：自由度账本条目 → 草拟局限（学者确认后进稿）。

## 9. 面试叙事（一句话）
> 我们把 esttab 表格还原成语义格子、把每格数值接回某次 Stata 结果，写作器只认带 id 的证据——模型能帮忙写字，但一个没来路的数字都进不了 Word。
