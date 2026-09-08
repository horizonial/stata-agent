---
name: did_minimum_wage_fte
version: 0.1.0
role: methodology
triggers:
  - needs: [did]
requires:
  ados: [reghdfe, esttab]
  stata_min: "17"
---

# did_minimum_wage_fte

> 由一次稳定 episode 自动沉淀（skill 自进化）。来源：Card-Krueger 1994 复现。
> 方法学=用 处理×期后 交互识别（DID）；变量映射(treat/post/wave)由调用方注入，本 skill 只声明 y 与 cluster。　稳健性目标：稳定。

## variants（沉淀自该次跑通配置，可再编辑）
```json
[
  {
    "id": "main",
    "label": "主回归",
    "y": "fte",
    "cluster": "sheet",
    "reason": "FTE(含管理者) 对 处理×期后，聚类到店"
  },
  {
    "id": "robust_no_mgr",
    "label": "稳健：不含管理者",
    "y": "fte_nm",
    "cluster": "sheet",
    "reason": "换因变量定义：不含管理者的 FTE"
  },
  {
    "id": "robust_cluster_chain",
    "label": "稳健：按连锁聚类",
    "y": "fte",
    "cluster": "chain",
    "reason": "换聚类层级到连锁"
  }
]
```

## rules（继承默认）
- forbid: 单期无事件研究却宣称平行趋势
- forbid: 多 spec 只挑显著报（全 family 留痕，选主结果须给理由）
- require: 进稿数字来自 run→card；报告注明聚类口径

## 证据门槛
- L-R：rc=0 + 结构化(coef/SE/N) + 可复现 do + env_sig。
- 主结果入选须带理由（防只记录喜欢规格）。
