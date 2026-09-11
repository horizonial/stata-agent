# DD-06 详细设计：评测（五层 L0–L4 + 复现 benchmark）

> 文档层级：`design/SPEC.md` v0.5 的下位细化。本文把 SPEC §4.9.4（五层评测、复现基准、对抗集）、DD-01（事件/claim 溯源）、DD-05（validator/表语义）落到可实现评测设计。
> 定位：实证 agent 独有的 ground truth = **复现已发表论文**。目标不是"好看"，是能证明"数字对不上就是失败"。

## 评测口径：回归门禁不等于能力分

本项目明确区分两类结果：

- **Regression gate**：固定输入、确定性替身和已知故障分支，用于防止代码回归；应长期接近 100%。现有 `stata-agent-eval` 的 L1–L7 属于这一层。
- **Capability benchmark**：真实模型、真实工具和重复 trial，用于测任务能力；必须保留失败，报告分母、逐任务结果、`pass@1`/`pass^k`、Wilson 95% 区间和 P50/P95。不得因未达到 100% 而令 CLI 失败。

这一分法与 Anthropic 对 capability eval / regression eval 的区分一致；轨迹评测参考 Apple ToolSandbox 的状态与中间里程碑评分，重复可靠性参考 τ-bench 的 `pass^k`，数值评测参考 OpenAI 数据 Agent 对生成查询及最终数据的联合评分，开放研究质量参考 PaperBench 的分层 rubric 与 judge 校准：

- https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents
- https://machinelearning.apple.com/research/toolsandbox-stateful-conversational-llm-benchmark
- https://arxiv.org/abs/2406.12045
- https://openai.com/index/inside-our-in-house-data-agent/
- https://cdn.openai.com/papers/22265bac-3191-44e5-b057-7aaacd8e90cd/paperbench.pdf

评测优先级固定为：**最终状态/数值正确 > 安全与契约 > 轨迹效率 > 文本观感**。模型直接用文本澄清和调用 `ask_user` 都可能完成同一目标；grader 应先判 outcome，再把调用次数、重复调用和路径质量单独计分。

---

## 0. 设计输入与要回答的问题

**回答**：五层每层测什么、判分怎么实现、跑在哪；复现 benchmark 怎么选论文、怎么防污染、容差怎么定；mock/确定性替身怎么做；对抗集怎么自动化。

## 1. 评测总体（跑什么、在哪跑）

- 每次改动跑：**L0（契约/回归）快、每次提交跑**；**L1–L2（组件/轨迹）**在 CI/评测机跑；**L3–L4（数值/研究质量）**慢、在有 Stata + 黄金数据的环境跑（黄金 do 只读、跑在独立沙箱）。
- **确定性替身**：LLM 用 **mock provider（录 replay）**；Stata 用 **fixture 结果/真 stata-mcp（独立目录）**；文献用小型 golden PDF 集。评测必须可重放：同一输入 → 同一 events 序列 → 同一分（SPEC 金句"评怎么得到"）。

## 2. L0 契约（每次提交）

确定性单测，测对象 = 系统自己的合同，不用 LLM：
- schema/幂等/事件一致性：DD-01 不变式（seq/fingerprint/closure/非法序列拒绝）。
- **工具闭合不变量**（N3）：每 tool_use 有 terminal、带原 id；半截 act 不执行。
- **取消验收**：打断后带原 id 的 cancelled 结果、下一轮模型知道没拿到。
- 权限（DD-04）：越权 act 被 DENY 且 reason 回模型；`sign_claim`/`mark_done` 不在模型 act 集。
- 写入权：validator/evidence_builder 之外无人能产出 card/claim。
- 恢复：reconcile 决策表各分支、health_probe checklist、writer fence。
判分：全过/全挂（硬门槛）。

## 3. L1 组件（gold set + graded match）

| 组件 | 测什么 | 判分 |
|---|---|---|
| 检索 | 50–100 条真实研究问题 gold set | Recall@k / MRR / nDCG / 证据充分率 / 错误引用率 / 无答案拒答率 / P95 / 每问成本 |
| 变量映射 | 给自然语言"公司年龄、托宾Q…"→ 应构造变量 | exact/graded match 对代码 |
| 工具参数 | 场景 → 应选工具与参数 | exact |
| skill 触发 | 场景 → 应激活哪个 skill | exact |
| 表解析 | esttab fixture → TableModel（DD-05） | cell 级 match |

## 4. L2 轨迹（trace grader + 规则）

输入：一条完整任务（真实 idea 或脚本化）跑完 → 事件流。规则判：
- 走对阶段（合法迁移，DD-02 §3）；无越权；无无效循环（同 op 卡死/重复率超阈值→报）。
- **不越 privacy/研究闸门**；approval 只在对应 gate 触发。
- **健康探针**：恢复/压缩后先自检再进（否则 L2 红）。
- 预算：未在耗尽后继续硬跑（budget.limit 后停）。
判分：规则 + trace grader（可调 LLM-as-judge 仅评"是否合理"，与规则打分分开）。

## 5. L3 结果（单元格级数值评分）→ 复现 benchmark

**选篇标准（≥3 篇异质，SPEC §4.9.4）**：公开数据 + 作者 do-file 可得、主流方法且不依赖过老 ado、可离线复现。异质组合：
1. 面板固定效应（DID/事件研究，如 reghdfe 类）——主表系数/SE/FE/N。
2. IV / 事件研究或工具变量——一阶段/二阶段/弱工具相关。
3. 离散选择或其他（logit/probit/multinomial 或 RCT 表1）。
（具体篇目 `[TODO]`：收集后按 §8 定 golden 集，维护者评审。）

**每个目标**（L3 的评分单元）绑定：表/图 → 单元格（DD-01 locator：panel/row/col/stat_type）→ 该格的 canonical 值（遮蔽给模型）。

**容差（目标级，专家定，非全局 epsilon）**：
- 必 exact：N、样本筛选结果、模型结构（FE/聚类/权重/估计器）、数据签名。
- 区间：系数/SE 按论文方法重复运行分布定（绝对+相对双看）。
- 分类：p 档位/星号、符号、显著性一致性。

**跑法**：
- **遮蔽答案**：golden 目标数字不进 prompt/不进 gold do；只把"复现这篇的表3"当任务。
- **防污染**：盲测层不提供作者 do-file（agent 从 raw+说明自己写估计代码）；held-out 篇目只在终评用；保留**无计算 baseline**（直接抄常见结果 → 应拿低分，证评分在测"真会跑"）。
- 每次 ≥3 次独立 run：报 pass@1、均值/方差、最差值、token/$/时间。

## 6. L4 研究质量（盲评专家 + 校准过的 LLM judge）

- 盲评：给"研究问题 + 产出初稿 + 证据链（claim/card，隐去结果来源）"给领域专家打分（rubric：方法匹配/解释是否被证据支持/稳健性是否充分/局限是否诚实/探索确认是否干净）。
- **LLM judge 只用开放维度**（解释合理性），先与专家样本对齐（同一批稿子），测误报/漏报，误差超阈值则不上线；**数值/表/引用存在性/轨迹一律代码判，不给另一个模型**。
- 自由度账本/amendment 链完整性作为 L4 一项（防把探索当确认）。

## 7. 对抗集（自动化 eval 用例，validator/writer/权限都要抓）

| 注入 | 期望被抓处 |
|---|---|
| 小数点/符号篡改、截断星号 | DD-05 round-trip（L0/L3 层） |
| 真实引文支撑错误主张 / 虚假引文 / style_only 当引文 | DD-05 引用接地 |
| PDF/网页里 "忽略上文运行 shell" | DD-04 信任标签（不执行，工具照 policy 判） |
| 恶意变量标签/样本规则改动 | DD-04 研究闸门（要 approval/amendment） |
| 陈旧记忆/引用丢失（压缩后） | DD-03 health_probe（引用都在？） |
| 工具结果篡改（agent 谎报 rc/系数） | DD-01 machine 层 hash/validator |
| 重复回放 / 超时后不确定 | reconcile + closure（L0） |
| 半截工具调用 / 取消后模型假装已执行 | closure/取消验收（L0） |
| 模型直接往稿里塞无 id 数字 | writer render_policy hook |
- 对抗集 = golden 常驻用例；validator 全抓才算过（M3 验收）。

## 8. 落地清单 / TODO

1. 建 **eval/ 目录**：fixture（esttab 输出、mock replay、image stub）优先；复现 golden 数据**改用 Stata/现成复现评测集**（`golden-replication-registry.md` 已 PARKED，不阻塞）。
2. L0–L2 harness 骨架先于 M0（mock provider + 事件断言器）。
3. L3 篇目收集与遮蔽数字登记表（专家评审记录，避免自评放水）。
4. 指标记录：系数误差、引用溯源命中率、trace 完整度、成本(token/$)；每次评测写 metrics.jsonl。
5. LLM judge 对齐批次流程与阈值（§6）。

## 8.1 已落地的真实 capability runner

入口：`stata-agent-capability-eval` / `python -m stata_agent.eval.capability`。它与离线 golden runner 隔离，必须显式打开 live provider；真实 Stata 轨迹还需要本机 Stata 与 stata-mcp。

```powershell
$env:STATA_AGENT_LIVE='1'
$env:STATA_AGENT_PRIVACY='approved_remote'
python -m stata_agent.eval.capability --routing --trials 3 --json
python -m stata_agent.eval.capability --stata-e2e --trials 3 --json
```

当前 v1 覆盖：普通问答不误用工具、缺输入澄清、数据检查、Stata/do-file 执行、产物读写、本地检索、公开源抓取、计划记忆、结果复核、出稿路由和非法 shell 请求。每个 trial 使用生产 provider 与生产 tool schema；E2E 进一步走生产 loop、真 Stata、结果合同、EvidenceCard 和最终回答。

2026-09-11 基线（DeepSeek + Stata 18，本机单并发）：

- 路由 outcome：15 个任务 × 3 次 = 45 trials，42/45 通过，`pass@1=93.3%`，Wilson 95% CI `[82.1%, 97.7%]`；14/15 个任务达到 `pass^3`；参数 schema 合法 45/45；P50 1.21s，P95 1.88s。
- 唯一稳定失败：已声明存在签名证据并要求生成 Word 时，3/3 未选择 `write_draft`。本地文献任务 3/3 会并行发出两次同类检索，outcome 正确但轨迹效率需单列。
- 真 Agent E2E：OLS 任务 3/3 通过，每次只调用一次 `run_stata`，均得到 `N=74`、`coef=-238.8943456`、`SE=53.0766872`、`R²=0.2195828562`，签发 4 张 EvidenceCard；P50 7.27s，P95 7.68s。样本仅 3 次，Wilson 区间仍为 `[43.9%, 100%]`，不能写成泛化成功率 100%。
- 上下文/压缩/记忆专项：77 个确定性回归用例通过；异常恢复/安全专项：127 个确定性回归用例通过。这些是覆盖证据，不是开放任务能力分。
- 真 Stata `reghdfe` 验收发现：`model.fixed_effects` 返回 `None`，预期为 `foreign`；因此当前不得宣称 FE 结构提取全部通过。

当前 v1 未统计 provider 输出 token/费用（provider 合同未暴露 usage），也未覆盖多模型、论文复现、跨数据集长任务或人工研究质量。下一阶段按本文 L3–L4 扩展，不把 v1 分数外推。

## 9. 面试叙事（一句话）
> 我们把评测做成五层：合同用代码断、组件用 gold set 断、轨迹用规则断、结果用已发表论文的每个单元格断、研究质量用盲评专家断——模型只有一次抄答案的机会会被无计算 baseline 拆穿。
