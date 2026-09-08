# 自建 eval golden（你自己维护的评测集）

> 目标：把**你想验证的"理想行为"**固化成可跑的 golden——数字可复现、改了不崩。
> 用现成 hooks，别造新轮子；新增一个 golden = 在下面加一个用例。

## 三层可放 golden 的地方
1. **单元/行为（L0/L2）**：直接写 pytest，样式参考
   `tests/test_runner.py`、`test_recovery.py`、`test_compaction_memory.py`
   （确定性重放、断点续跑、压缩不丢真相、approval 门、goal 模式…）。
2. **防幻觉对抗（validator 必须抓）**：加进 `src/stata_agent/eval/checks.py` 的 `CHECKS`，
   或仿 `tests/test_writer_ground.py` / `test_writer_citation.py`（幻觉数字/假引用）。
3. **数值单元格（L3，复现论文主表）**：仿
   `eval.checks._c3_l3_numeric` 的"结构先 exact→数值容差→原因分类"；
   想跑真复现，用 `skills/engine.run_variants` + `tools/robustness` + `draft_multi`，
   把你的 golden 论文做成一个 `skills/<method>.md`（variants 配置），再把"期望单元格 vs 容差"写成用例。

## 快速加一个自己的用例
```python
# tests/test_my_golden.py
def test_my_signature_case(tmp_path):
    # … 跑你的 agent/engine，得到 machine 或 events …
    m = {...}                       # 你的 golden 期望来源结果
    assert abs(m["coef"] - 2.809943) <= 1e-3   # 容差你自己定
    assert m["N"] == 788                       # 结构 exact
```

## 检查你护住的核心承诺（每个 golden 至少碰一条）
- 数字可溯源（run→card→文本/表格）
- 确定性 / 可回放 / 断点续跑
- 写权分离（模型不能签 claim/card）
- 越权/注入被拦（mark_done、无来源数字、style 库当引文）
- goal 模式不越权（审批仍是停点）
- 同输入幂等复用 / 预算刹车 / 健康探针

## 怎么跑
`cd app && python -m pytest`（全量）；`STATA_LIVE=1` / 设好 key 时连 live 用例也跑。
加 live golden 的约定：跳过用 `@pytest.mark.skipif(not STATA_LIVE/not key)`，让 CI 默认离线绿。
