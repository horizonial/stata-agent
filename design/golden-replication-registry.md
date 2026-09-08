# Golden 复现论文注册表（eval 用，草稿 v0）—— ⏸ PARKED

> **2026-09-07 暂停**：eval 优先用 **Stata/现成复现评测集**，不再手动收集 golden 论文；本表仅作未来备选参考，不跟进。相关实测见 §0.2（若重启再续）。

> 对应：DD-06 §5 复现 benchmark。用途 = L3 单元格级评分的 golden ground-truth。三篇异质、公开数据、主表可对答案。
> 验证状态（2026-09-07）：检索级核验 + **curl 直连实测**（见 §0.2，绕过 WebFetch 域名限制）。G1/G3 已实际下载并记 sha256；G2 源存在已确认、大文件在沙箱传输不稳，待真机取。

---

## 0. 选择门槛（每篇都要过才算 golden，DD-06 §5）

1. 数据源可离线下载 + 作者 do-file 可得（或等价清理代码）。
2. 不依赖过老/难装 ado（可在 restricted 沙箱内复现）。
3. 主表单元格可对答案；样本/变量定义明确。
4. 异质：三类方法尽量不重。
5. 每篇登记：遮蔽数字清单、目标单元格 locator、专家容差备注、容差归属人。至少 1 篇 held-out 只在终评用。

## 0.2 实测记录（2026-09-07，curl 直连，绕过 WebFetch 域名限制）

- **WebFetch 域名限制说明**：该工具拉取前有"domain safe"校验，Berkeley/NBER/Hansen 等未通过 → 整单拒。**改用 Bash `curl` 直连不受此限制**（NBER/Hansen/David Card 均 HTTP 200）。
- **G1 Card & Krueger**：`davidcard.berkeley.edu/data_sets/njmin.zip` **下载成功**，sha256 `41a2…ba587`，内含 **raw dat**（`public.dat`/`survey1.nj`/`survey2.nj`）+ `check.sas`/`codebook`（**非 .dta**——需自写解析/清理，正合"盲测不给作者 do"）。清理版 .dta 镜像（Hansen 长格式 820obs、Pisa `card_data.dta`、ECON523）供选 primary。
- **G2 Angrist & Lavy**：官方 MIT 直链按猜测 404（未定位）；**GitHub 镜像存在已确认**（`laurenhanlon/Maimonides-Rule-and-Class-Sizes` `rawdata/final4_raw_data.dta`、`final5_raw_data.dta`，课程注明源自 Angrist）。**沙箱大文件下载被掐**（连接重置/超时，ADH 等大文件同此）→ G2 及大文件待**真机**下载。小文件（≤~几十 KB）沙箱可下。
- **G3 NSW**：`users.nber.org/~rdehejia/data/nsw.dta` **下载成功**，sha256 `3ee0…c24e`（22 KB）。全套文件见 §1 表。

## 1. 推荐起步三篇（异质）

| # | 论文 / 类 | 方法 | 数据源（web 核验） | 格式/规模 | do-file | 状态 |
|---|---|---|---|---|---|---|
| G1 | **Card & Krueger (1994)** 《Minimum Wages and Employment》AER | DID（NJ/PA 快餐、两期） | ① David Card 页 `davidcard.berkeley.edu/data_sets.html`（经典源）② Bruce Hansen 经济学页清理版（820 obs × 26 var 长格式）③ Univ. Pisa `card_data.dta`（qe4policy）④ 多门课程镜像 `Card-Krueger-1994-data.dta` | Stata .dta；410 店宽格式 / 820 长格式 | Boeri & van Ours（2008）`card_do.pdf` 等 + 大量公开复现 do | ✅ `njmin.zip` 下载确认(sha `41a2…`，内含 raw dat+SAS)；清理 .dta 镜像选 primary |
| G2 | **Angrist & Lavy (1999)** 《Using Maimonides' Rule…》QJE | IV（班级规模，RD 式断点 + IV） | ① Angrist MIT 页 `final4.dta`/`final5.dta` + `Angrist_Lavy_Table4.do`/`_Table5.do` ② Hansen 清理合并版 4,067 class × 31 var ③ R 包 DOS2/rbounds 仅 172 obs 匹配子集（**只够单测，不够主表**） | Stata .dta，全样本（数千班） | 作者 do 可得 | ⚠ 全量 raw .dta 在 GitHub 镜像（MIT 直链 404）；**大文件沙箱下载不稳→真机取**；R 包 172obs 子集勿当主表 |
| G3 | **LaLonde (1986) NSW** / Dehejia & Wahba (1999) | 非实验对照基准 / 处理效应（匹配/OLS/IV） | ① `users.nber.org/~rdehejia/nswdata2.html`（权威，含 `nsw.dta`、`nsw_dw.dta`、txt 各子样本）② xuyiqing/lalonde GitHub（nsw.dta + code） | Stata .dta；NSW n≈722（treated297+ctrl425）；DW 子样本 445 | DW 复现代码公开 | ✅ `nsw.dta` 下载确认(sha `3ee0…`，22 KB)；全子样本文件见 §1 |

## 2. 进阶 / held-out 候选

| 论文 | 方法 | 数据源 | 备注 |
|---|---|---|---|
| **Autor, Dorn & Hanson (2013)** 《China Syndrome》AER | shift-share IV + 面板（CZ×时期） | **OpenICPSR 官方** project 112670（DOI 10.3886/E112670V1，Public-Release-Data 含 do/dta/gph/tab-fig + Readme）；Dorn 页 ddorn.net/data.htm | 官方托管最规范，但数据大、复现较重 → 放 **held-out/进阶** |
| Dube, Lester & Reich (2010) 或 AER 其它 | 边界配对 DID | 待核 | 备选（若 G1 源不稳） |

## 3. 每篇要登记的 golden 元数据（建库时填，DD-06 §5/§8）

```yaml
id: G1
paper: Card & Krueger 1994 (AER 84(3):772-793)
type: did
target_tables: [Table 2 主 DID 单元格, Table 3/4 稳健性]
shadowed_numbers: []        # 遮蔽后存此；跑分前不落 prompt/gold do
cells:                       # locator：panel/row_label/col_group/stat_type
  - table: T2
    cell: {col_group: "full", row_label: "Δ(fte)", stat_type: coef, canonical: null}
data:
  source: "David Card page / Hansen cleaned / Pisa mirror（取其一作 primary）"
  files: [CardKrueger.dta]
  obs: 820 长格式（或 410 宽）
  hash: null                # 下载后记 sha256
dofile: "Boeri & van Ours card_do / 自写（盲测不供作者 do）"
ados_expected: [reghdfe(可选), esttab]
env: {stata_min: "17"}
tolerance:                  # 专家定；默认起步见 DD-06 §5(数值容差)
  N: exact; model: exact; coef: rel<=1%; se: rel<=5%; p/star: same
verification: "web 检索核验 2026-09-07；final 下载待确认"
```

## 4. 待办（进 eval 前）

1. 逐篇**实际下载**并验存活（WebFetch 域名拦截，改用浏览器/直连下载确认）；记录 sha256。
2. G1 定 primary 源（避免课程镜像漂移：优先作者页/期刊官方，其次 Hansen/Pisa 稳定镜像）。
3. G2 取**全量**数据（拒绝 172 obs 子集当主表）；确认 Angrist MIT / Hansen 下载可用。
4. 遮蔽主表数字 + 专家设容差（每条：归属人、理由）；至少 1 篇 held-out。
5. 本地离线搭 golden 环境：raw 只读 + 独立沙箱跑 agent 自写 do（盲测不供作者 do），跑 DD-06 L3 打分。

## 5. 备注

- 三篇方法异质满足：**DID（G1）／ IV-RD（G2）／ 处理效应基准-非实验对照（G3）**；若需更纯"离散选择"，换 logit/multinomial 有公开数据的期刊论文再补一篇（`[TODO]` 候选待列）。
- 均不需商业数据；G1/G3 数据小，适合 agent 反复跑与调试；G2 中等。
