# 代码缺口审计（2026-09-08）

> **状态更新（同日，A–E 分批已补）**：A1 表语义✅ A2 引文接地✅ A3 figure✅ ·
> B1 阶段门禁自动推进✅ B2 幂等复用✅ B3 分支切片✅（并行活分支留给编排）·
> C 长会话：estimate_tokens✅ 压缩感知上下文+token_cap✅ auto_compact✅ 大输出 head✅ ·
> D 向量化：Embedder 接口+HashEmbedder✅ VectorIndex+Hybrid(RRF)✅ 摄取缓存幂等✅ ·
> E：eval checks(数字/引文对抗+L3 结构先数值)✅ 隐私三档边界✅ Telemetry 两本账✅。
> 仍未做/半做见下（诚实）

> 对照：SPEC v0.5 + DD-01…07 + impl-plan + 前端契约。分四级：✅已做(可运行/有测试) · 🟡半做(模块在、没接全/没真跑) · ⬜未做 · ⚠演示hack。

## A. 出稿 / 证据链（核心，缺的最影响"可信初稿"）
| 项 | 状态 | 说明 |
|---|---|---|
| 数字接地结果句 + round-trip | ✅ | writer/ground.py，测试全 |
| **esttab 表语义解析器（TableModel）** | ⬜ | DD-05 §2 核心；现在只有结果句，无表格行/列反查 |
| **引文接地 / CitationClaim（文献块→正文）** | ⬜ | 只有 numeric 卡；写作引用 citable 块的链路没有 |
| 图证据（figure card + 嵌入 Word） | ⬜ | schema kind 有 "figure"，但签卡/渲染都没实现 |
| 描述/平衡表（Table1）模板 | ⬜ | 无 |
| Word：表格/隐藏批注溯源 | ⬜ | 段落 docx 有；表格与旁注无 |

## B. Stata / 研究执行
| 项 | 状态 | 说明 |
|---|---|---|
| 真 Stata 回归→事件链 | ✅ | live 验证（executor/session/adofile/env_sig） |
| 10 工具策略映射 metadata | ✅ | strategy.py（data 表） |
| 10 工具真实驱动（load/inspect/graph…） | 🟡 | 只真用过 run + 内置 auto |
| **prepared 快照纪律** | 🟡 | 原则写进 executor；没有"prepared dataset 缓存 + input_data_sig 复用"，仍是逐会话跑 |
| require_ados 真预检 | 🟡 | ado.py 有 + live-skip 测试，未在 UI/run 中真触发 |
| 分支语义（换 spec=fork/leaf） | ⬜ | schema 有 branch 列，reducer/runner 恒 "main" |
| phase 门禁流转（IDEA→…→ESTIMATION 由编排器提交） | 🟡 | 有迁移表+门控点但 runner 不自动推进；UI DEMO 是 seed hack(⚠) |

## C. 上下文 / 记忆 / 长会话
| 项 | 状态 | 说明 |
|---|---|---|
| build_context 最小(L1/L2)+extra(文献/记忆) | ✅ | runner cycle |
| 压缩=语义 checkpoint | ✅ | 模块+测试；**未自动触发**（显式调用） |
| token 预算（meter/auto-compact buffer） | 🟡 | Budget 只按"步数"；无 LLM token 计量与提前压缩 |
| 大工具结果摘要→按需读 | ⬜ | 没做（Stata 输出直接整段进 text 由前端截断） |
| MemoryStore(决策/偏好) | ✅ | 本地文件 + usage/prune；接入 approval note→context |
| 跨 idea 研究者档案 / codex 两阶段 | ⬜ | 无 |

## D. RAG / 文献
| 项 | 状态 | 说明 |
|---|---|---|
| PDF 摄取(角色/chunk_id/扫描探测) + 词法检索 | ✅ | 真实中文库验证 |
| embedding/向量库(bge-m3/Chroma)、rerank | ⬜ | 无；gold set 消融无 |
| 持久索引/增量摄取 | 🟡 | 每次重解析，无缓存；幂等没做文件级 |
| OCR（扫描中文 PDF） | ⬜ | 探测有，OCR 无 |

## E. 评测 / 观测 / 隐私
| 项 | 状态 | 说明 |
|---|---|---|
| L0 单测（事件/幂等/fence/写权/压缩/记忆…） | ✅ | 规模可观 |
| L1–L4 harness、mock replay fixtures、对抗集、复现 benchmark | ⬜ | eval/ 未建（golden PARKED） |
| 隐私三档落地（approved/mixed 配置、内容审计） | 🟡 | local_strict 语义在；只实现"禁"与"假门控"，无 approved/mixed 真正配置 |
| OTel/两本账遥测 | ⬜ | 无 exporter |

## F. UI / 杂项
| 项 | 状态 | 说明 |
|---|---|---|
| 聊天/状态/证据/运行/trace/审批/续跑/draft | ✅ | 后端+前端（codex 稿）冒烟通；DEMO 可全流程 |
| stop（安全中断） | ⬜ | 诚实 501；要真做需 MCP stata_break 接线 + 任务取消 |
| 附件/图片输入 | ⬜ | 按钮 disabled；vision/OCR 无 |
| 门禁"改要求"（decision revision） | ⬜ | capabilities 标 False；只能靠聊天 |
| 分支 trace 可视化 / 自动压缩触发 / 图预览 | ⬜ | 无 |
| 工程卫生：lint/typecheck/CI、pyproject 依赖 pin | ⬜ | 无 |

## 收口决策（2026-09-08，排掉）
| 项 | 处置 | 理由 / 触发条件 |
|---|---|---|
| lint/typecheck/CI | ✅ 已配 | pyproject `[tool.ruff]/[tool.mypy]` + `.github/workflows/ci.yml`；本地 `pip install ruff mypy && ruff check src` |
| 真 embedding（bge-m3/API）+ rerank | ⏸ PARK | 需装模型/网络；接口(Embedder/VectorIndex)已留，HashEmbedder 召回不够再换 |
| 并行活分支折叠（多 leaf 同进 ResearchState） | ⏸ PARK | 需折叠路径/合并策略设计；触发=同 idea 并行稳健性实测有收益（可连带多 agent）时 |
| eval fixtures 建库 / OTel exporter | ⏸ PARK 后置 | 现单测 + eval checks 顶 L0/对抗；OTel 需 exporter 与观测后端 |
| UI stop（安全中断） | ⏸ PARK | 需 MCP `stata_break` 真接线 + 任务取消；现诚实 501 |
| UI 附件（图片/OCR） | ⏸ PARK | 需 vision/OCR（SPEC §4.10 D 切片）；按钮 disabled 诚实标注 |
| deepseek live key | ⏸ 按需 | 设 `DEEPSEEK_API_KEY` 即自动切 deepseek（registry 已就绪） |

