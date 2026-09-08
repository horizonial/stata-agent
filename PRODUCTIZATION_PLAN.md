# stata-agent 产品化路线图

## 产品边界

首个可发布版本定位为 **本地优先、单用户的实证研究桌面 Agent**。默认仅监听本机，研究数据、事件账本与运行产物保留在本地；远端模型和真实 Stata 都必须显式启用。多租户、互联网公开部署、组织权限体系不属于 v1。

## 当前基线

- `main` 工作树干净，离线测试基线为 191 passed / 4 skipped（195 collected；live Stata/远端模型默认跳过）。
- 事件账本、工具策略、隐私模式、证据链、SSE、多工作区、RAG、Skill 与 wheel 打包已有实现。
- 当前主要风险集中在运行取消与资源生命周期、真实环境稳定性、交互状态表达，以及可重复的产品级评测/发布门禁。

## P0：首轮产品化（当前执行）

### A. 运行可靠性与控制面

- 引入线程安全的请求取消/停止原语，贯穿 agent loop、工具执行和 Stata transport。
- 外部执行必须有可验证超时、取消结果和 terminal event；关闭 UI/请求时释放 executor/session/store。
- 增加并发请求、断连、租约、超时、重复停止与 shutdown 回归测试。
- 健康与 capability 输出必须区分“已配置、可连接、已验证”，不得把 key 存在等同于服务可用。

### B. 交互可信度与可用性

- 工具开始/完成/失败使用结构化节点卡片，不再混入普通文本。
- Markdown 表格使用 DOM 构建并保持禁止 `innerHTML` 的 XSS 契约。
- Stop、SSE 断开、审批拒绝/修改要求的前后端状态保持一致且可恢复。
- 为关键交互增加前端静态契约测试与 FastAPI API 回归测试。

### C. 评测、质量与发布门禁

- 把 L1–L4 变成可运行的离线 scenario harness：工具选择、状态机、证据接地、恢复和端到端 golden trace。
- CI 增加 coverage 阈值、关键安全/隐私测试分组、wheel 独立安装 smoke 与构建产物检查。
- 收敛 Ruff/Mypy 基线；新增代码不得扩大豁免。
- 写清 release checklist、版本策略、真实 Stata/远端模型的可选手工验收步骤。

## P1：P0 合并后的下一轮

- 真实 Stata 长会话 soak、崩溃注入和 Windows 机器验收。
- 工作区导入/导出、备份恢复、SQLite schema migration/versioning。
- 附件上传与数据预览的沙箱、大小限制、格式嗅探和恶意文件测试。
- 可观测性：结构化日志、运行指标、隐私安全的诊断包。
- Windows 安装包、升级/回滚、签名与发布渠道。

## v1 发布退出标准

1. 离线全量测试、Ruff、Mypy、coverage 和 wheel smoke 全部通过。
2. FakeExecutor 只能显式启用，任何演示结果都不能冒充真实证据。
3. Stop/断连/超时都会产生可审计终态，不遗留不可解释的运行中操作。
4. golden scenarios 能覆盖一次完整的“消息 → 工具 → Stata/Fake → card → claim → draft”链路。
5. 默认配置不联网、不公开监听、不泄露本地路径或凭据。

## 发布门禁与验收清单

在 `app/` 下执行以下离线门禁；CI 的四个 job 与本地命令保持一致：

```bash
python -m pytest -q
python -m stata_agent.eval --json
python -m ruff check src tests
python -m mypy src
python -m coverage run --branch -m pytest -q
python -m coverage report
python -m build --wheel
```

`product-eval` 的退出码为 0/1/2：全部 scenario 通过、scenario 失败、评测配置/黄金文件错误。JSON 只含稳定字段；不得把 UUID、时间戳、临时目录或绝对路径写入 golden。当前实测 coverage 基线为 `75%`（branch coverage），门槛锁定为 `75%`；新增代码不得通过扩大忽略项来掩盖下降。

版本策略：`app/pyproject.toml` 的 `project.version` 是唯一发布版本源；发版时同步变更日志和 Git tag（`v<version>`），不在运行时从工作树推断版本。0.x 允许兼容性调整但仍须更新 release notes；进入 1.0 后遵循 SemVer。

真实环境是发布前的人工验收，不在默认 CI 中启用：

1. 在隔离 Windows 账户配置真实 Stata + `stata-mcp`，设置 `STATA_AGENT_EXECUTOR=stata`，运行一个内置 auto 回归；确认 run 事件为 `requested → call → result → succeeded`，do 文件、命令 hash、Stata 版本/ flavor 和机器层值均可复核。
2. 注入一次 Stata transport timeout/断连；确认账本出现 `run.uncertain`，重启后 `reconcile_uncertain` 只产生一次 `system.restored`，不会静默重跑写操作。
3. 远端模型验收必须由用户显式设置 `STATA_AGENT_PRIVACY=approved_remote`（或明确同意的 `mixed_sanitized`），检查请求中无本地绝对路径、凭据和原始研究文本泄露；`local_strict` 下应明确拒绝。
4. 从独立 wheel 安装目录启动 UI，确认 `index.html`、静态 JS/CSS、默认 `SKILL.md` 可读取；再手工打开一个工作区，验证 stop、审批、断线恢复和 draft 下载。

发布记录至少保留：Git commit/tag、四个 CI job 链接、pytest/Ruff/Mypy/coverage/wheel 输出、真实 Stata 与远端模型验收者和日期、已知遗留项及回滚版本。

