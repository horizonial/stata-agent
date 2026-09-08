# 设计审计：按 Stata 实证研究实际需求回看 SPEC v0.5 / DD-01 / DD-02

> 审计对象：`design/SPEC.md` v0.5、`design/dd-01-domain-events.md`、`design/dd-02-phase-machine-harness.md`。
> 审计视角：真实实证（社科/计量）研究者在 Stata 里到底怎么干活——数据清洗/构造、反复试 spec、出表、出图、复现。发现按"会不会在真实使用时咬我们一口"排序。
> 结论性质：审计 + 修补建议，未自动改动 SPEC/DD；每项给 严重度(S1 必修/S2 该修/S3 建议) 与 修补落点。

---

## 0. 一句话结论

方向仍成立，但有三处"真实使用会咬人"的洞：① 把"一次回归"当最小可复现单元，低估了**长数据 pipeline + 环境(ado/Stata 版本)依赖**对可复现的影响；② 只把数字当证据，**没把表格语义和图形当一等证据**；③ 面向"封闭本地 PDF 库"设计，**没有给"外部世界"(联网复现包/公开数据) 和"非文本模态"(图/扫描件/用户贴图) 明确的受控入口**。前两类是修 DD 就能关的洞，第三类是新能力方向，要开决策。

---

## 1. 两个能力问题的直接答复

### 1.1 能不能联网？
**现状：设计上不能（除 provider API 外全本地）。**
- SPEC v0.5 只有隐私三档门、`provider.fallback`、`files/网络`进工具副作用表里标"联网下载 Deferred"。**没有 agent 级 web_search / URL fetch 工具**。
- 真实需求：① 复现 benchmark 要拿**公开论文复现包**（作者 do-file + 数据，多数在期刊/NBER/网上）；② 可行性对话可能要看**新文献**（不在本地库）；③ 找公开数据。
- **建议：做成受控能力，不是默认开**。新增 web_search/fetch/download 工具，归 DD-04 权限表：`local_strict` 全禁；`approved_remote / mixed_sanitized` 允许，下载内容一律标 `retrieved_untrusted`（可能含注入），落盘带 `来源 URL + 内容 hash + 版本`（复用 artifact 链）。**绝不允许"下载后自动执行"**。作者复现 do-file 属于最高风险执行（见 §2.6）。

### 1.2 能不能输入图片？
**现状：设计上不能（全链路文本假设）。**
- SPEC/DD 无 modality。真实需求三类：① 学者贴图（发表表格截图 / 数据字典 / 论文图 / 想复现的目标表）当输入；② agent **读 Stata 自己产出的图**（event-study/margins/coefplot/placebo/balance）做解读与写稿、嵌 Word；③ 中文扫描 PDF 的 OCR。
- **建议：接受"图 = 一等外部输入 + 一等证据产物"**，但分两层：
  - **输入层**：用户贴图 = `retrieved_untrusted`（图里也可能夹 prompt 注入文本）；`ModelCapabilityProfile` 增 `vision: bool`；上下文投影层按模型能力裁剪（codex for_prompt 已借鉴）；`local_strict` 下无远端视觉 → 走本地 OCR/本地视觉模型，或明确该模式不支持贴图解析。
  - **证据层**：Stata 出的图落 artifact（.gph/.png，hash）→ 新增 `figure` EvidenceCard → writer 嵌 Word，caption 引 claim；图也要可复现（有对应 dofile）。深度见 §2.3/§2.5。
  - 中文扫描 PDF OCR 与"贴图解析"共用视觉能力，是同一块。

---

## 2. 审计发现（按域）

### A. 执行粒度与可复现性（S1：必修，改 DD-01/DD-02）

**A1｜"Run = 一次回归"粒度太细，真实可复现单元是"长 pipeline"**。实证中 90% 时间在数据构造：`加载→merge/append→panel 设定→generate/encode→winsor→跑回归`。若每个 Run 只记"最后一条回归命令"，重放必须能复现整条链。
- 修：`Run` 之上加 **`Pipeline`/`DataPrep` 语义**——ResearchSpec 明确 `prep`（从 raw → prepared dataset 的脚本/do），每 run 记 `input_data_signature = H(raw 文件集 + prep do + Stata/ado 版本)`；`sample_definition` 对 raw 定义、`run` 对 prepared 定义。**优先"每次从 prepared 快照重建，不依赖内存增量状态"**（Stata 内存数据集是顺序可变、非事务的，重放失败 do 会双重 append/漂移——MCP session reset+replay 只救会话，不救内存数据集污染）。
- 落点：DD-01 §2.3 ResearchSpec（加 `prep` 与 `run_from`）、§3.5 reconcile（write 判定含"内存数据集污染→必须从 prepared 重建"）。

**A2｜provenance 缺环境指纹（Stata 版本 / ado 依赖 / 机器）**。结果由 `reghdfe/esttab/coefplot` 等用户安装 ado 决定；不同 Stata 版本/平台可能微差。复现 eval（换机器跑作者 do）没有 env 指纹就说不清"是代码错还是环境错"。
- 修：`provenance`（DD-01 §2.5）加 **`env_sig = H(Stata 版本 + ado 清单(关键项) + OS)`**；stata-mcp 层能吐就吐（含 `about`/`which reghdfe`），不能则设计约束要求。
- 落点：DD-01 §2.5 + §4 场景、M2 复现 eval 容差判断。

**A3｜复现包(repro bundle)与 author do-file 是两类不同风险**。
- 修：L-D 交付物加 **`repro_manifest`**（raw 引用+prep+估计 do+结果哈希 → 一键重跑）；validator L-D 检 manifest。
- 作者 do-file（eval 用）是**外部不可信代码**：只准在 restricted + 显式审批 + 独立 sandbox 目录跑，**绝不碰学者真实数据/工作目录**；下载的复现包标 `retrieved_untrusted`。
- 落点：DD-04 权限表加"author_dofile=最高风险"；DD-01 事件目录加 `repro.manifest_built`。

### B. 表格与数字接地（S1：必修，改 DD-01 §2.6/§4.8 + writer/validator 设计）

**B1｜"表3-列2"的 locator 太粗；表格是 esttab 语义对象**。真实表 = 多 panel、row label、多列、括号 SE、星号、`Yes/No/X` 的 FE 行、`N/R²/聚类`尾行。格子的身份是 `(table, panel, row_label, column_组, statistic_型[coef/se/p/N])`，不是列序数。
- 修：NumericClaim.locator 用 **表语义键**；validator/writer 要有 **esttab→rtf/csv 的解析器**（把排版还原成 cell 语义），数字接地按 cell 语义比对，不是按位置猜。排版层值 vs 规范值 round-trip 反查。
- 落点：DD-01 §2.6 Locator 扩字段；新 DD（writer/validator）写 esttab 表解析。

**B2｜描述统计/平衡表是独立产物**。Table 1（mean/sd/组间差+t 检验）、balance test 常见且常进主稿；它们不是"回归系数"。
- 修：EvidenceCard.kind 已泛化 numeric 够用，但 writer 要有"描述/平衡表"模板；L3 复现评分要含这类表。落点：writer 模板清单。

**B3｜显示 vs 原始精度**已有（SPEC §4.8），但补 **round-trip 测试**：draft 里"0.038***"必须能反查到规范值 0.0376(p<0.01) 且格式一致。落点：validator 单测（对抗集已含小数点篡改，加格式层）。

### C. 图形作为证据（S2：该修；真实使用缺它活不下去）

**C1｜图没有进证据链**。Stata 的 event-study/margins/placebo/coefplot/balance 图是正文核心，目前 SPEC 只把"图"当 outputs 附件，writer 只谈表格 + rtf。
- 修：图 = artifact（.gph/.png，hash）→ `figure` EvidenceCard（数据点/CI/纵轴含义 locator）→ writer 嵌 Word + caption 引 claim → 复现=图的 dofile 可跑。**这依赖 vision 能力（§1.2）** 读图确认"图讲的是 claim 讲的事"。
- 落点：DD-01 证据链加 figure 卡；DD-03 上下文含图；writer 模板加图。

### D. 工作流形态（S2：该修，改 DD-02）

**D1｜"清洗+试 spec"是紧循环，不是线性门**。真实流程：跑个快速回归→发现变量问题→回去改构造→再跑，反复几十轮，先于任何正式 gate。若 DESIGN/DATA/ESTIMATION 门控过严、或 confirmatory lock 过早，会把探索卡死。
- 修：门控严格度做成**每阶段可配置**；进 ESTIMATION 初期默认"轻量探索环"（不请求人工门、不上锁），只有**声明主 spec / 转 ROBUSTNESS 才收紧**（DD-02 §2 已有 PhaseDef 数据驱动，明确调这个旋钮）。exploratory/confirmatory 显式标注已做（SPEC §4.9.5），别让 lock 变成探索期的默认。

**D2｜skill 要声明依赖（ado/Stata 版本）**。panel_did 依赖 `reghdfe`、`esttab`、`coefplot` 等；机器没装就跑不过，且会污染"代码 vs 环境"归因。
- 修：skill 文件加 `requires: {ados:[], stata_min:"…"}`；skill 激活时 preflight 检查已安装（`which reghdfe`），缺则先建议装/提示，不静默跑错。
- 落点：DD-01 不做；SPEC §4.6 skill 格式 + 未来 DD-skill。

**D3｜运行目录与内存数据集漂移**：见 A1；补一句路径纪律——每 run 独立运行目录 + prepared 快照，避免长会话内共享 .dta 增量污染。

### E. 外部世界入口（S2→设计决策；见 §1.1）

**E1**：web/fetch/download 受控工具（隐私门 + retrieved_untrusted + 来源/版本 artifact）。**E2**：复现数据获取放 approved/mixed；本地库没有的文献在线拉取可选。都在 DD-04 权限 + DD-01 artifact 链。

### F. 输入模态（S2→设计决策；见 §1.2）

**F1** vision 输入（用户贴图/模型读自产图）分层：ModelCapabilityProfile.vision + 投影按能力裁剪 + 图=untrusted data + local_strict 本地视觉/OCR。**F2** 中文扫描 PDF OCR 并入同一视觉能力。**F3** mock/eval 支持 image fixture（防评测退化文本化）。

---

## 3. 修补动作清单（按严重度）

| # | 动作 | 落点 | 性质 |
|---|---|---|---|
| 1 | ResearchSpec 加 `prep`（raw→prepared 脚本）+ run 记 input_data_sig + "从 prepared 快照重建"纪律 | DD-01 §2.3/§3.5 | S1 修 |
| 2 | provenance 加 `env_sig`（Stata 版本+ado+OS） | DD-01 §2.5 | S1 修 |
| 3 | NumericClaim.locator 升"表语义键"+ 规划 esttab 表解析器 + round-trip | DD-01 §2.6 + 未来 writer/validator DD | S1 修 |
| 4 | Figure evidence：figure artifact→FigureCard→writer 嵌 Word + 复现 | DD-01/DD-03 + writer 模板 | S2 修 |
| 5 | 门控严格度可配置 + 探索环默认轻、锁只在声明主 spec 收紧 | DD-02 §2/§4 | S2 修 |
| 6 | skill 声明 requires(ados/stata) + preflight 检查 | SPEC §4.6 | S2 修 |
| 7 | repro_manifest 进 L-D + author_dofile 最高风险执行策略 | DD-01 + DD-04 | S2 修 |
| 8 | web/fetch/download 受控工具 + 联网复现包策略 | SPEC/DD-04 权限 + 隐私 | S2 新增能力 |
| 9 | vision/OCR 输入层 + 图证据层 + ModelCapabilityProfile.vision | SPEC §4.2 + DD-03 | S2 新增能力 |
| 10 | 描述/平衡表模板 + L3 评分含表1 | writer 模板 | S3 建议 |

**评审口吻**：审计不推翻架构；A/B 两类是"现有 DD 该补的字段与算法"，C/D/E/F 是"要新增的能力与文档"。是否把 1–3 立刻修进 DD-01（小 diff），4–6 修进 DD-02/SPEC（中 diff），8–9 作为"能力决策"开 SPEC 小节（新方向），7/10 随对应 DD——见开放问题。

## 4. 开放问题（要决策）

1. 联网能力：纳入哪一档隐私？（建议 approved_remote 及以上；local_strict 全禁）用途先锁"复现包/公开数据/在线文献"，不做自由 browsing。
2. 图片：以"读自产图 + 学者贴图 + 中文扫描 OCR"为范围；deepseek 若无 vision 能力，local_strict 下视觉走本地模型——接受这个拆法吗？
3. 是否把 audit 的 S1(1–3) 直接修进 DD-01，还是先只存档本笔记等 DD-03/04 一起改？
