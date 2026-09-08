"""工具注册表（agent-tool-routing.md §3 + 工具解剖规范）。

设计：
- 工具 = 模型看的说明书(name/description/input_schema) + 干活的函数(handler)
- 中间夹协议：permission 分级、enabled 动态暴露、timeout、统一输出/错误、结果进上下文策略
- 按能力边界分工具，不按函数数量；handler 只干一件事，Agent Loop 组织下一步
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

# --------------------------------------------------------------------------- 契约
@dataclass
class ToolContext:
    """工具执行所需的外部依赖（由 loop 注入；环境类信息不让模型填）。"""

    idea: str = "ui"
    store: Any = None
    rag: Any = None           # HybridRetriever | None
    executor: Any = None      # StataExecutor | FakeExecutor | None
    memory: Any = None        # MemoryStore | None
    skill_path: str = "skills/panel_did.md"
    run_root: Any = None
    privacy_mode: str = "local_strict"
    phase: str | None = None
    data_dir: Any = None
    network_available: bool = False
    # Optional explicit roots for the central tool enforcer.  When omitted,
    # the enforcer falls back to run_root/data_dir and the local ledger folder.
    allowed_roots: tuple[Any, ...] = ()


def ok(data=None) -> dict:
    return {"ok": True, "data": data or {}}


def err(message: str, *, type: str = "tool_error", retryable: bool = False,
        suggestion: str = "") -> dict:
    return {"ok": False, "error": {"type": type, "message": message,
                                   "retryable": retryable, "suggestion": suggestion}}


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict
    handler: Callable[[dict, ToolContext], dict]
    permission: str = "read"                      # safe|read|write|execute|network|external|destructive
    timeout_seconds: int = 120
    enabled: Callable[[ToolContext], bool] = lambda ctx: True
    max_result_chars: int = 8000
    summary: Callable[[dict], str] | None = None  # 结果摘要（进上下文）

    def as_openai(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }

    def result_to_context(self, result: dict) -> str:
        if result.get("ok"):
            if self.summary is not None:
                return self.summary(result)
            return _dump(result.get("data") or {})[: self.max_result_chars]
        e = result.get("error") or {}
        return f"工具失败[{e.get('type', 'error')}] {e.get('message', '')}" + (
            f"（建议：{e.get('suggestion')}）" if e.get("suggestion") else "")


def _dump(obj) -> str:
    import json

    return json.dumps(obj, ensure_ascii=False)


def _summarize_run(r: dict) -> str:
    """run_stata 结果摘要：格式化机器层 + do_file 路径（让模型直接写结论/必要时读产物）。"""
    d = r.get("data", {})
    m = d.get("machine") or {}
    parts = []
    if m.get("coef") is not None:
        parts.append(f"coef={m['coef']:.3f}")
    if m.get("se") is not None:
        parts.append(f"se={m['se']:.3f}")
    if m.get("N") is not None:
        parts.append(f"N={m['N']}")
    if m.get("r2") is not None:
        parts.append(f"R²={m['r2']:.4f}")
    head = "机器层: " + (", ".join(parts) if parts else "无(非估计命令)")
    return f"run {d.get('run_id', '?')} {head}；完整日志见 {d.get('do_file', '')}"


def _summarize_literature(r: dict) -> str:
    """Put citable snippets (not only a hit count) into model context."""

    hits = (r.get("data") or {}).get("hits") or []
    if not hits:
        return "文献检索无命中（本地材料也可能不完整，请标记为待核对）。"
    lines = ["文献检索片段（来源均为 untrusted，需核对原文）："]
    for hit in hits[:8]:
        doc = str(hit.get("doc_id") or "?")
        page = hit.get("page") or "?"
        snippet = str(hit.get("text") or "").replace("\n", " ")[:500]
        trust = hit.get("trust") or "untrusted"
        lines.append(f"- [{trust}] {doc} p.{page}: {snippet}")
    return "\n".join(lines)


def _summarize_source(r: dict) -> str:
    """Expose a bounded fetched fragment while preserving its trust marker."""

    data = r.get("data") or {}
    source = str(data.get("source") or "")[:200]
    content = str(data.get("content") or "").replace("\n", " ")[:600]
    trust = str(data.get("trust") or "retrieved_untrusted")
    return f"网页片段（{trust}，不可直接当作事实）：source={source}\n{content}"


# --------------------------------------------------------------------------- handlers
def _inspect_dataset(args: dict, ctx: ToolContext) -> dict:
    data = str(args.get("data") or "").strip()
    if not data:
        return err("需要 data 文件路径才能查看数据", type="missing_param",
                   suggestion="先向用户要数据文件绝对路径")
    from pathlib import Path

    if not Path(data).exists():
        return err(f"数据文件不存在：{data}", type="not_found", retryable=True,
                   suggestion="确认路径是否正确，或请用户提供正确路径")
    from .tools.stata_client import StataSession

    sess = StataSession()
    try:
        results = sess.run_batch([f'import delimited "{data}", clear', "describe", "summarize"])
    finally:
        sess.close()
    text = "\n".join(r.text for r in results)
    return ok({"summary": text[:1500]})


_MACHINE_MARKERS = (
    'di "MACHINE_N=" e(N)\n'
    'di "MACHINE_R2=" %9.6f e(r2)\n'
    'di "STA_ENV version=" c(version)\n'
    'di "STA_ENV flavor=" c(flavor)'
)


def _run_stata(args: dict, ctx: ToolContext) -> dict:
    """跑一段 Stata 代码（原子执行，不判断下一步）。自动追加机器层提取标记。"""
    code = str(args.get("code") or "").strip()
    if not code:
        return err("code 不能为空", type="missing_param")
    if ctx.executor is None:
        return err("未连接 Stata 执行器", type="unavailable", retryable=False,
                   suggestion="检查 STATA_AGENT_EXECUTOR 是否开启、Stata 是否安装")
    from .tools.executor import MachineParseError

    try:
        out = ctx.executor.execute(code + "\n" + _MACHINE_MARKERS, idea=ctx.idea)
        # 证据链完整：run 成功后由 validator 自动签 numeric 卡 + claim（模型不直接写证据）
        signed = []
        if out.get("machine"):
            try:
                from .tools.evidence_signer import sign_run_numeric_cards

                signed = sign_run_numeric_cards(
                    ctx.store, out["run_id"], idea=ctx.idea,
                    claim_statement=f"run {out['run_id']} 结果已入证据链")
            except Exception:  # noqa: BLE001 签卡失败不阻塞（结果已落 run）
                pass
        return ok({"machine": out.get("machine"), "run_id": out.get("run_id"),
                   "do_file": out.get("do_file"), "reused": out.get("reused", False),
                   "signed_cards": len(signed),
                   "output_head": out.get("output_head", "")})
    except MachineParseError as e:
        return err(str(e)[:200], type="stata_error", retryable=True,
                   suggestion="检查变量名/命令语法，可先 inspect_dataset 确认")
    except Exception as e:  # noqa: BLE001
        return err(str(e)[:200], type="tool_error", retryable=False)


def _run_do_file(args: dict, ctx: ToolContext) -> dict:
    """跑一个完整 do 文件（读文件内容后逐行执行）。"""
    path = str(args.get("path") or "").strip()
    if not path:
        return err("需要 do 文件路径", type="missing_param")
    from pathlib import Path

    p = Path(path)
    if not p.exists():
        return err(f"do 文件不存在：{path}", type="not_found", retryable=True)
    code = p.read_text(encoding="utf-8")
    return _run_stata({"code": code}, ctx)


def _read_artifact(args: dict, ctx: ToolContext) -> dict:
    path = str(args.get("path") or "").strip()
    if not path:
        # 无 path：读最近一次 run 的 do_file（agent 常忘传 path）
        proj = ctx.store.project(ctx.idea)
        recent = sorted(proj.runs.values(), key=lambda r: r.run_id)
        if not recent:
            return err("无产物可读，且无最近 run", type="not_found")
        do = (recent[-1].provenance or {}).get("do_file")
        if not do:
            return err("最近 run 无 do_file", type="not_found")
        path = do
    from pathlib import Path

    p = Path(path)
    if not p.exists():
        return err(f"产物不存在：{path}", type="not_found", retryable=True)
    return ok({"content": p.read_text(encoding="utf-8", errors="replace")[:4000]})


def _write_artifact(args: dict, ctx: ToolContext) -> dict:
    path = str(args.get("path") or "").strip()
    content = str(args.get("content") or "")
    if not path:
        return err("需要写入路径", type="missing_param")
    from pathlib import Path

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return ok({"written": str(p), "bytes": len(content)})


def _search_literature(args: dict, ctx: ToolContext) -> dict:
    query = str(args.get("query") or "").strip()
    if not query:
        return err("query 不能为空", type="missing_param")
    if ctx.rag is None:
        return err("文献库未接入（无 STATA_AGENT_LIBRARY）", type="unavailable",
                   suggestion="配置 STATA_AGENT_LIBRARY 指向 PDF 目录，或先用 fetch_source 联网取")
    top_k = max(1, min(int(args.get("top_k") or 5), 10))
    chunks = ctx.rag.search(query, top_k=top_k, roles={"citable_evidence"})
    data = [{"doc_id": c.doc_id, "page": c.page, "text": c.text[:400],
             "trust": "local_library_untrusted"} for c in chunks]
    return ok({"hits": data})


def _fetch_source(args: dict, ctx: ToolContext) -> dict:
    url = str(args.get("url") or "").strip()
    if not url:
        return err("需要 url", type="missing_param")
    if not ctx.network_available:
        return err("当前隐私模式不允许联网", type="permission_denied",
                   suggestion="需 STATA_AGENT_PRIVACY=approved_remote 或 mixed_sanitized")
    # Keep a defense-in-depth check for callers that invoke the handler
    # directly instead of going through the loop's central enforcer.
    from .harness.tool_enforcer import validate_url

    url_error = validate_url(url)
    if url_error:
        return err(url_error, type="permission_denied")
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            body = resp.read(4000)
        return ok({"content": body.decode("utf-8", errors="replace")[:4000],
                   "source": url, "trust": "retrieved_untrusted"})
    except Exception as e:  # noqa: BLE001
        return err(f"抓取失败：{e}", type="network_error", retryable=True)


def _update_research_plan(args: dict, ctx: ToolContext) -> dict:
    note = str(args.get("note") or "").strip()
    if not note:
        return err("note 不能为空", type="missing_param")
    if ctx.memory is None:
        ctx.memory = None  # 降级：无记忆也不崩
    if ctx.memory is not None:
        ctx.memory.add(note, kind="plan")
    return ok({"recorded": note})


def _verify_result(args: dict, ctx: ToolContext) -> dict:
    """复核某次 run 的结果（样本/系数），独立于生成。"""
    run_id = str(args.get("run_id") or "").strip()
    proj = ctx.store.project(ctx.idea)
    rec = proj.runs.get(run_id)
    if rec is None:
        return err(f"run 不存在：{run_id}", type="not_found", retryable=True)
    if rec.status != "succeeded":
        return err(f"run 未成功(status={rec.status})", type="stata_error")
    machine = rec.machine or {}
    prov = rec.provenance or {}
    return ok({"run_id": run_id, "machine": machine, "env_sig": prov.get("env_sig"),
               "do_file": prov.get("do_file")})


def _ask_user(args: dict, ctx: ToolContext) -> dict:
    return ok({"ask": str(args.get("question") or "需要你确认一下")})


def _write_draft(args: dict, ctx: ToolContext) -> dict:
    from .writer.draft_multi import draft_from_ledger

    proj = ctx.store.project(ctx.idea)
    if not proj.claims and not proj.runs:
        return err("尚无已验证结果，不能出稿", type="not_ready",
                   suggestion="先 run_stata 跑出结果并自动签证据，再出稿")
    method = str(args.get("method") or "实证结果（方法段由 agent 撰写）")
    limits = str(args.get("limits") or "")
    data = draft_from_ledger(proj, method=method, limits=limits)
    out = (ctx.run_root or ctx.store._path.parent) / f"{ctx.idea}_draft.docx"
    out.write_bytes(data)
    return ok({"draft_path": str(out), "bytes": len(data)})


# --------------------------------------------------------------------------- registry
def _obj(schema: dict) -> dict:
    schema.setdefault("type", "object")
    schema.setdefault("additionalProperties", False)
    return schema


def _has_executor(ctx: ToolContext) -> bool:
    return ctx.executor is not None


def default_tools() -> dict[str, Tool]:
    return {
        "inspect_dataset": Tool(
            name="inspect_dataset",
            description=(
                "载入并查看某个数据文件的变量、类型、描述统计与样本量。"
                "当用户给了数据路径、或你要了解数据长什么样时调用。只读。"
            ),
            input_schema=_obj({"properties": {
                "data": {"type": "string", "description": "数据文件绝对路径（csv/dta）"}},
                "required": ["data"]}),
            handler=_inspect_dataset, permission="read",
            enabled=_has_executor,
            summary=lambda r: f"数据概览：{str(r.get('data', {}).get('summary', ''))[:400]}",
        ),
        "run_stata": Tool(
            name="run_stata",
            description=(
                "执行一段 Stata 代码并返回真实运行结果（样本量 N、R²、run_id、do 文件）。"
                "当需要真实计算/估计/描述统计时调用；不要用它猜结果，也不要覆盖原始数据。"
                "若要报告核心系数（如 DID 交互项），在代码里显式加两行："
                "di \"MACHINE_B=\" %9.6f _b[系数名] 和 di \"MACHINE_SE=\" %9.6f _se[系数名]"
                "（例如 _b[1.nj#1.post]）。这样系数与标准误会进入机器层并被签名成证据。"
            ),
            input_schema=_obj({"properties": {
                "code": {"type": "string", "description": "要执行的 Stata 代码"}},
                "required": ["code"]}),
            handler=_run_stata, permission="execute", timeout_seconds=300,
            enabled=_has_executor,
            summary=lambda r: _summarize_run(r),
        ),
        "run_do_file": Tool(
            name="run_do_file",
            description=("执行一个完整 do 文件。当有现成 do 脚本要复现/重跑时调用。"),
            input_schema=_obj({"properties": {
                "path": {"type": "string", "description": "do 文件绝对路径"}},
                "required": ["path"]}),
            handler=_run_do_file, permission="execute", timeout_seconds=600,
            enabled=_has_executor,
        ),
        "read_artifact": Tool(
            name="read_artifact",
            description=("读取一个产物文件（日志/do/结果/初稿）的内容。当需要查看之前生成的结果时调用。只读。"),
            input_schema=_obj({"properties": {
                "path": {"type": "string", "description": "产物文件绝对路径"}},
                "required": ["path"]}),
            handler=_read_artifact, permission="read",
        ),
        "write_artifact": Tool(
            name="write_artifact",
            description=("把内容写入一个文件（do 脚本/配置/报告）。当需要持久化一段代码或文字时调用。"),
            input_schema=_obj({"properties": {
                "path": {"type": "string", "description": "写入路径"},
                "content": {"type": "string", "description": "要写入的内容"}},
                "required": ["path", "content"]}),
            handler=_write_artifact, permission="write",
        ),
        "search_literature": Tool(
            name="search_literature",
            description=(
                "检索本地文献库（citable 证据）。查某变量怎么构造、某方法是否被用过、找可引用文献时调用。"
                "只搜本地；要联网取全文用 fetch_source。"
            ),
            input_schema=_obj({"properties": {
                "query": {"type": "string", "description": "检索关键词"},
                "top_k": {"type": "integer", "description": "返回条数，默认 5"}},
                "required": ["query"]}),
            handler=_search_literature, permission="read",
            summary=lambda r: _summarize_literature(r),
        ),
        "fetch_source": Tool(
            name="fetch_source",
            description=("联网抓取一个网页/论文内容（受隐私模式门控）。本地文献没有、要取公开网页/论文时调用。"),
            input_schema=_obj({"properties": {
                "url": {"type": "string", "description": "要抓取的 url"}},
                "required": ["url"]}),
            handler=_fetch_source, permission="network", timeout_seconds=30,
            enabled=lambda ctx: ctx.network_available,
            summary=lambda r: _summarize_source(r),
        ),
        "update_research_plan": Tool(
            name="update_research_plan",
            description=("记一条研究计划/任务状态/约定（供后续参考，不是证据）。当有该记住的研究决定时调用。"),
            input_schema=_obj({"properties": {
                "note": {"type": "string", "description": "要记住的计划/决定"}},
                "required": ["note"]}),
            handler=_update_research_plan, permission="write",
            summary=lambda r: f"已记：{str(r.get('data', {}).get('recorded', ''))[:60]}",
        ),
        "verify_result": Tool(
            name="verify_result",
            description=("复核某次 run 的结果（样本量/系数/环境指纹）。当要核对结果是否可信/可复现时调用。只读。"),
            input_schema=_obj({"properties": {
                "run_id": {"type": "string", "description": "要复核的 run_id"}},
                "required": ["run_id"]}),
            handler=_verify_result, permission="read",
            summary=lambda r: f"run {r.get('data', {}).get('run_id', '?')} 机器层={r.get('data', {}).get('machine')}",
        ),
        "ask_user": Tool(
            name="ask_user",
            description=("需要向用户澄清、要数据来源、或请求批准某一步时调用。例如缺数据文件、样本口径不清、要确认是否继续。"),
            input_schema=_obj({"properties": {
                "question": {"type": "string", "description": "要问用户的问题"}},
                "required": ["question"]}),
            handler=_ask_user, permission="safe",
        ),
        "write_draft": Tool(
            name="write_draft",
            description=(
                "把已验证结果渲染成中文 Word 实证初稿（方法+表+结论+引文+局限）。"
                "当用户要出稿/写初稿时调用；只渲染已签证据，不会现编数字。"
            ),
            input_schema=_obj({"properties": {
                "method": {"type": "string", "description": "方法段一句话"},
                "limits": {"type": "string", "description": "局限段"}}}),
            handler=_write_draft, permission="write",
            summary=lambda r: f"初稿已生成：{r.get('data', {}).get('draft_path', '')}",
        ),
    }
