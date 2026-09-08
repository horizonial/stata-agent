# stata-agent 产品化路线图

## 产品边界

首个可发布版本定位为 **本地优先、单用户的实证研究桌面 Agent**。默认仅监听本机，研究数据、事件账本与运行产物保留在本地；远端模型和真实 Stata 都必须显式启用。多租户、互联网公开部署、组织权限体系不属于 v1。

## 当前基线

- `main` 工作树干净，离线测试 187 passed / 4 skipped。
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

