"""Build a pinned, reproducible Agent interview-question audit inventory.

The source repositories are used as capability probes, not as product requirements.
Every extracted question is retained for traceability, while ``disposition`` decides
whether it belongs in the actionable Stata Research Agent checklist.
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "verification" / "audits" / "agent-interview-question-inventory.v1.json"
CHECKLIST_OUTPUT = ROOT / "verification" / "audits" / "AGENT-CAPABILITY-CHECKLIST.md"

GUIDE_COMMIT = "9a987322f2d82ddabe4c1aabc7b4795749fa90a2"
DATAWHALE_COMMIT = "f72d756fbd2c95c883e057806b2116ed44bcafc3"

GUIDE_FILES = (
    "01-基础概念.md",
    "02-核心框架.md",
    "03-RAG技术.md",
    "04-工具调用.md",
    "05-记忆系统.md",
    "06-多智能体.md",
    "07-大模型基础.md",
    "08-工程化实践.md",
    "09-Prompt工程.md",
)


@dataclass(frozen=True)
class Question:
    question_id: str
    source: str
    source_revision: str
    module: str
    source_number: str
    text: str
    disposition: str
    capability_cluster: str
    project_status: str
    evidence: tuple[str, ...]
    rationale: str


CLUSTERS: dict[str, dict[str, object]] = {
    "agent-boundary": {
        "status": "verified",
        "evidence": (
            "src/stata_research_agent/runtime/agent_turn_driver.py",
            "src/stata_research_agent/runtime/turn_worker.py",
            "tests/vertical/test_agent_turn_driver_vertical.py",
        ),
    },
    "planning-replanning": {
        "status": "partial",
        "evidence": (
            "src/stata_research_agent/runtime/agent_turn_driver.py",
            "src/stata_research_agent/runtime/research_workflow_executor.py",
        ),
    },
    "human-in-loop-control": {
        "status": "verified",
        "evidence": (
            "src/stata_research_agent/application/turn_interaction_service.py",
            "src/stata_research_agent/runtime/agent_turn_driver.py",
            "tests/vertical/test_agent_turn_driver_vertical.py",
        ),
    },
    "evaluation-reflection": {
        "status": "verified",
        "evidence": (
            "src/stata_research_agent/application/evaluation.py",
            "src/stata_research_agent/runtime/model_evaluation_coordinator.py",
            "tests/vertical/test_runtime_evaluation_vertical.py",
        ),
    },
    "loop-safety": {
        "status": "verified",
        "evidence": (
            "src/stata_research_agent/application/turn_driver.py",
            "src/stata_research_agent/runtime/agent_turn_driver.py",
            "src/stata_research_agent/persistence/turn_runtime_budget_store.py",
            "tests/vertical/test_model_gateway_vertical.py",
        ),
    },
    "critical-path-degradation": {
        "status": "partial",
        "evidence": (
            "src/stata_research_agent/runtime/application_runtime.py",
            "src/stata_research_agent/application/diagnostic_service.py",
        ),
    },
    "context-engineering": {
        "status": "verified",
        "evidence": (
            "src/stata_research_agent/application/context_compiler.py",
            "src/stata_research_agent/persistence/context_authority.py",
            "tests/integration/test_context_management.py",
        ),
    },
    "rag-pipeline": {
        "status": "verified",
        "evidence": (
            "src/stata_research_agent/application/knowledge_retrieval.py",
            "src/stata_research_agent/persistence/knowledge_store.py",
            "verification/runs/product-agentic-rag-20260921-v1/live-product-report.json",
        ),
    },
    "rag-ingestion-indexing": {
        "status": "partial",
        "evidence": (
            "src/stata_research_agent/interfaces/literature_catalog.py",
            "src/stata_research_agent/persistence/dense_knowledge_store.py",
            "verification/EMBEDDING-UPGRADE.md",
            "verification/runs/rag-adaptive-pdf-20260921-v4/adaptive-pdf-report.json",
        ),
    },
    "embedding-index-upgrade": {
        "status": "verified",
        "evidence": (
            "src/stata_research_agent/application/dense_retrieval.py",
            "verification/EMBEDDING-UPGRADE.md",
            "tests/unit/test_dense_index_upgrade.py",
        ),
    },
    "rag-retrieval-quality": {
        "status": "verified",
        "evidence": (
            "src/stata_research_agent/application/dense_retrieval.py",
            "src/stata_research_agent/application/rag_evaluation.py",
            "verification/runs/rag-real-scenarios-20260921-v6/rag-scenario-report.json",
        ),
    },
    "rag-agentic-multihop": {
        "status": "verified",
        "evidence": (
            "src/stata_research_agent/application/knowledge_retrieval.py",
            "tests/integration/test_canonical_knowledge_runtime.py",
            "verification/runs/product-agentic-rag-20260921-v1/live-product-report.json",
        ),
    },
    "rag-trust-security": {
        "status": "verified",
        "evidence": (
            "verification/RAG-TRUST-BOUNDARY.md",
            "verification/rag-prompt-injection-redteam.v1.json",
            "verification/runs/rag-prompt-injection-live-p0.json",
        ),
    },
    "tool-contract-admission": {
        "status": "verified",
        "evidence": (
            "src/stata_research_agent/application/tool_broker.py",
            "src/stata_research_agent/application/tool_broker_service.py",
            "tests/vertical/test_tool_broker_vertical.py",
        ),
    },
    "tool-selection-routing": {
        "status": "verified",
        "evidence": (
            "src/stata_research_agent/application/default_tool_contracts.py",
            "src/stata_research_agent/interfaces/production_turn_runner.py",
            "verification/tool-selection-gold.v2.json",
            "tests/unit/test_production_tool_catalog.py",
        ),
    },
    "tool-scheduling": {
        "status": "verified",
        "evidence": (
            "src/stata_research_agent/runtime/agent_turn_driver.py",
            "tests/unit/test_agent_tool_batch_execution.py",
            "tests/integration/test_cross_workspace_real_stata.py",
        ),
    },
    "tool-security-permissions": {
        "status": "verified",
        "evidence": (
            "src/stata_research_agent/runtime/sandbox_tool_executor.py",
            "src/stata_research_agent/interfaces/windows_sandbox_executor.py",
            "tests/integration/test_windows_sandbox_executor.py",
        ),
    },
    "mcp-integration": {
        "status": "verified",
        "evidence": (
            "src/stata_research_agent/stata/stdio_runtime.py",
            "tests/integration/test_real_stata_mcp_runtime.py",
        ),
    },
    "memory-architecture": {
        "status": "verified",
        "evidence": (
            "src/stata_research_agent/application/memory.py",
            "src/stata_research_agent/persistence/memory_store.py",
            "tests/integration/test_project_memory.py",
        ),
    },
    "memory-quality-lifecycle": {
        "status": "partial",
        "evidence": (
            "src/stata_research_agent/application/memory_curator.py",
            "src/stata_research_agent/persistence/memory_curator_store.py",
            "tests/integration/test_memory_curator.py",
            "tests/integration/test_memory_quality_lifecycle.py",
        ),
    },
    "multi-agent-decision": {
        "status": "not_planned_v0_1",
        "evidence": ("src/stata_research_agent/runtime/model_evaluation_coordinator.py",),
    },
    "model-configuration-budget": {
        "status": "verified",
        "evidence": (
            "src/stata_research_agent/application/model_configuration.py",
            "src/stata_research_agent/application/output_budget.py",
            "tests/unit/test_output_budget.py",
        ),
    },
    "provider-resilience-routing": {
        "status": "verified",
        "evidence": (
            "src/stata_research_agent/application/model_gateway_service.py",
            "tests/vertical/test_model_gateway_vertical.py",
        ),
    },
    "observability-audit": {
        "status": "verified",
        "evidence": (
            "src/stata_research_agent/application/diagnostics.py",
            "src/stata_research_agent/persistence/model_gateway_store.py",
            "src/stata_research_agent/persistence/tool_broker_store.py",
        ),
    },
    "observability-telemetry": {
        "status": "partial",
        "evidence": (
            "src/stata_research_agent/application/diagnostics.py",
            "src/stata_research_agent/interfaces/filesystem_diagnostics.py",
        ),
    },
    "streaming-performance": {
        "status": "partial",
        "evidence": (
            "src/stata_research_agent/application/streaming.py",
            "src/stata_research_agent/interfaces/api/app.py",
            "tests/contract/test_workspace_stream.py",
        ),
    },
    "cost-usage-monitoring": {
        "status": "partial",
        "evidence": (
            "src/stata_research_agent/persistence/model_gateway_store.py",
            "src/stata_research_agent/application/output_budget.py",
        ),
    },
    "recovery-idempotency": {
        "status": "verified",
        "evidence": (
            "src/stata_research_agent/application/recovery_service.py",
            "src/stata_research_agent/persistence/stata_recovery_scanner.py",
            "tests/vertical/test_stata_operation_vertical.py",
        ),
    },
    "prompt-versioning-output-validation": {
        "status": "verified",
        "evidence": (
            "src/stata_research_agent/application/model_gateway.py",
            "src/stata_research_agent/persistence/model_gateway_store.py",
            "src/stata_research_agent/interfaces/openai_compatible_transport.py",
        ),
    },
    "prompt-injection-redteam": {
        "status": "verified",
        "evidence": (
            "verification/rag-prompt-injection-redteam.v1.json",
            "tools/run_live_prompt_injection_redteam.py",
            "verification/runs/rag-prompt-injection-live-p0.json",
        ),
    },
    "prompt-data-privacy": {
        "status": "verified",
        "evidence": (
            "src/stata_research_agent/application/sensitive_output.py",
            "src/stata_research_agent/application/rag_evaluation.py",
            "tests/unit/test_rag_evaluation.py",
        ),
    },
    "multilingual-contract-integrity": {
        "status": "verified",
        "evidence": (
            "tests/unit/test_openai_compatible_transport.py",
            "tests/unit/test_table_document_rules.py",
        ),
    },
    "evaluation-regression": {
        "status": "partial",
        "evidence": (
            "src/stata_research_agent/application/rag_evaluation.py",
            "tools/run_real_product_stress.py",
            "verification/rag-real-questions.v1.json",
            "src/stata_research_agent/interfaces/release_evaluation_gate.py",
            "verification/runs/release-gate-p0-p1-full.json",
        ),
    },
    "local-release-operations": {
        "status": "verified",
        "evidence": (
            "tools/build_local_installer.py",
            "tools/audit_release_readiness.py",
        ),
    },
    "semantic-cache-decision": {
        "status": "not_planned_v0_1",
        "evidence": (),
    },
    "reasoning-trace-boundary": {
        "status": "not_planned_v0_1",
        "evidence": (
            "src/stata_research_agent/application/model_gateway.py",
            "src/stata_research_agent/persistence/model_gateway_store.py",
            "src/stata_research_agent/application/diagnostics.py",
        ),
    },
    "theory-only": {"status": "not_a_product_requirement", "evidence": ()},
    "out-of-scope-v0_1": {"status": "not_planned_v0_1", "evidence": ()},
}


def fetch(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "stata-agent-audit"})
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        return response.read().decode("utf-8")


def cluster_for(module: str, text: str, source: str) -> str:
    probe = f"{module} {text}".lower()
    if (
        "显式写出 thought" in probe
        or ("react 轨迹" in probe and "系统生成" in probe)
        or "cot 有哪些缺点" in probe
    ):
        return "reasoning-trace-boundary"
    if "人机在环" in probe:
        return "human-in-loop-control"
    if "max_iterations" in probe:
        return "loop-safety"
    if "何时不要用" in probe or ("docstring" in probe and "工具" in probe):
        return "tool-selection-routing"
    if "关键路径" in probe and "非关键路径" in probe:
        return "critical-path-degradation"
    if "embedding 模型升级" in probe:
        return "embedding-index-upgrade"
    if "few-shot" in probe and "隐私" in probe:
        return "prompt-data-privacy"
    if "个性化" in probe and "隐私" in probe:
        return "prompt-data-privacy"
    if "多语言混合 prompt" in probe:
        return "multilingual-contract-integrity"
    if "用 llm 给 llm 打分" in probe:
        return "evaluation-regression"
    if any(
        marker in probe
        for marker in (
            "langchain",
            "langgraph",
            "llamaindex",
            "crewai",
            "autogen",
            "metagpt",
            "dspy",
        )
    ):
        return "theory-only"
    if any(marker in probe for marker in ("微调过agent", "rag 与微调", "rag与微调")):
        return "theory-only"
    if any(marker in probe for marker in ("多模态 rag", "多模态rag", "graphrag")):
        return "out-of-scope-v0_1"
    if "语义缓存" in probe:
        return "semantic-cache-decision"
    if "opentelemetry" in probe:
        return "observability-telemetry"
    if source == "datawhale" and any(
        marker in module for marker in ("VLM", "RLHF", "前景", "其它")
    ):
        return "theory-only"
    if "多智能体" in module or any(
        marker in probe
        for marker in ("多 agent", "多agent", "多智能体", "crewai", "autogen", "metagpt", "a2a")
    ):
        return "multi-agent-decision"
    if "大模型基础" in module or (source == "datawhale" and "LLM 八股" in module):
        if any(
            marker in probe
            for marker in (
                "tokenizer",
                "prefill",
                "decode",
                "temperature",
                "top-k",
                "top-p",
                "闭源 api",
                "开源模型",
            )
        ):
            return "model-configuration-budget"
        return "theory-only"
    if any(marker in probe for marker in ("k8s", "kubernetes", "金丝雀", "蓝绿")):
        return "out-of-scope-v0_1"
    if any(
        marker in probe for marker in ("prompt 注入", "间接注入", "jailbreak", "越狱", "恶意内容")
    ):
        return (
            "rag-trust-security"
            if "rag" in probe or "检索" in probe
            else "prompt-injection-redteam"
        )
    if any(
        marker in probe
        for marker in (
            "rag",
            "检索",
            "chunk",
            "embedding",
            "faiss",
            "bm25",
            "rrf",
            "rerank",
            "向量",
            "hyde",
            "mmr",
        )
    ):
        if any(
            marker in probe
            for marker in ("agentic", "self-rag", "corrective", "多次检索", "自适应检索", "多跳")
        ):
            return "rag-agentic-multihop"
        if any(marker in probe for marker in ("评估", "ragas", "指标", "召回")):
            return "rag-retrieval-quality"
        if any(
            marker in probe
            for marker in ("chunk", "分块", "索引", "embedding", "faiss", "父子文档", "文档")
        ):
            return "rag-ingestion-indexing"
        if any(
            marker in probe
            for marker in ("bm25", "rrf", "rerank", "cross-encoder", "mmr", "混合检索")
        ):
            return "rag-retrieval-quality"
        return "rag-pipeline"
    if any(marker in probe for marker in ("mcp", "function calling", "tool_calls")):
        return "mcp-integration" if "mcp" in probe else "tool-contract-admission"
    if any(marker in probe for marker in ("工具", "tool", "json schema", "calculator", "eval`")):
        if any(
            marker in probe for marker in ("权限", "安全", "泄露", "敏感", "两阶段", "代码执行")
        ):
            return "tool-security-permissions"
        if any(marker in probe for marker in ("并行", "串行", "dag", "编排")):
            return "tool-scheduling"
        if any(marker in probe for marker in ("选错", "路由", "description", "docstring")):
            return "tool-selection-routing"
        if any(marker in probe for marker in ("10mb", "返回", "结构化")):
            return "context-engineering"
        return "tool-contract-admission"
    if any(marker in probe for marker in ("记忆", "memory", "摘要", "衰减", "情景", "语义记忆")):
        if any(
            marker in probe for marker in ("评测", "脏数据", "更新", "衰减", "升级", "事故", "误差")
        ):
            return "memory-quality-lifecycle"
        return "memory-architecture"
    if any(
        marker in probe
        for marker in ("熔断", "重试", "退避", "jitter", "模型路由", "多模型", "降级")
    ):
        return "provider-resilience-routing"
    if any(
        marker in probe
        for marker in ("trace", "日志", "span", "langsmith", "langfuse", "opentelemetry", "审计")
    ):
        return "observability-audit"
    if any(
        marker in probe
        for marker in ("streaming", "异步", "吞吐", "semaphore", "并发", "连接池", "流式")
    ):
        return "streaming-performance"
    if any(marker in probe for marker in ("成本", "token", "缓存")):
        return (
            "cost-usage-monitoring"
            if any(marker in probe for marker in ("成本", "计费", "监控"))
            else "model-configuration-budget"
        )
    if any(marker in probe for marker in ("恢复", "不中断", "幂等", "重放")):
        return "recovery-idempotency"
    if any(
        marker in probe
        for marker in ("版本化", "版本管理", "结构化输出", "后端校验", "system prompt")
    ):
        return "prompt-versioning-output-validation"
    if any(
        marker in probe
        for marker in ("评估", "回归测试", "llm-as-a-judge", "幻觉", "红队", "benchmark", "指标")
    ):
        return "evaluation-regression"
    if any(
        marker in probe
        for marker in ("re-plan", "replanning", "re-planning", "plan-and-execute", "规划", "计划")
    ):
        return "planning-replanning"
    if any(marker in probe for marker in ("reflexion", "reflection", "自检", "检查一遍", "评估器")):
        return "evaluation-reflection"
    if any(marker in probe for marker in ("死循环", "max_iterations", "停止", "迷失", "抖动")):
        return "loop-safety"
    if any(marker in probe for marker in ("context", "上下文", "lost in the middle", "噪声")):
        return "context-engineering"
    if any(marker in probe for marker in ("部署", "发布", "运维")):
        return "local-release-operations"
    if any(marker in probe for marker in ("agent", "react", "生命周期", "chatbot", "prompt chain")):
        return "agent-boundary"
    return "theory-only"


def disposition_for(cluster: str) -> str:
    if cluster == "theory-only":
        return "theory_only"
    if cluster in {
        "out-of-scope-v0_1",
        "multi-agent-decision",
        "semantic-cache-decision",
        "reasoning-trace-boundary",
    }:
        return "architecture_decision"
    return "project_capability"


def rationale_for(cluster: str, status: str) -> str:
    if cluster == "theory-only":
        return (
            "Useful interview knowledge, but not an application capability the local Agent "
            "must implement."
        )
    if cluster == "out-of-scope-v0_1":
        return "Cloud-scale release machinery is outside the local-first V0.1 deployment model."
    if cluster == "multi-agent-decision":
        return (
            "V0.1 intentionally uses one Research Agent with deterministic "
            "runtime/evaluator components."
        )
    if cluster == "reasoning-trace-boundary":
        return (
            "V0.1 records observable inputs, plans, actions, tool results, decisions and "
            "summaries; hidden chain-of-thought is neither a product trace nor an "
            "authoritative research fact."
        )
    if status == "missing":
        return (
            "The probe exposes a product-relevant capability with no direct implementation "
            "evidence."
        )
    if status == "partial":
        return (
            "Related mechanisms exist, but the full behavior or dedicated evaluation evidence "
            "is incomplete."
        )
    return "Implementation and test or live-run evidence were located."


def make_question(
    *, source: str, revision: str, module: str, source_number: str, text: str, ordinal: int
) -> Question:
    cluster = cluster_for(module, text, source)
    metadata = CLUSTERS[cluster]
    status = str(metadata["status"])
    return Question(
        question_id=f"{source}-{ordinal:03d}",
        source=source,
        source_revision=revision,
        module=module,
        source_number=source_number,
        text=text.strip(),
        disposition=disposition_for(cluster),
        capability_cluster=cluster,
        project_status=status,
        evidence=tuple(str(item) for item in metadata["evidence"]),
        rationale=rationale_for(cluster, status),
    )


def guide_questions() -> list[Question]:
    questions: list[Question] = []
    ordinal = 0
    for file_name in GUIDE_FILES:
        url = (
            "https://raw.githubusercontent.com/Horanluo/ai-agent-interview-guide/"
            f"{GUIDE_COMMIT}/docs/01-%E9%9D%A2%E8%AF%95%E5%85%AB%E8%82%A1%E6%96%87/"
            + urllib.parse.quote(file_name)
        )
        text = fetch(url)
        if file_name == "04-工具调用.md":
            pattern = re.compile(
                r"^\*\*面试 Q(\d+)[：:]\s*(.+?)\*\*|"
                r"^###\s+Q(\d+)(?:（.+?）)?[：:]\s*(.+?)$",
                re.MULTILINE,
            )
            matches = [
                (
                    match.group(1) or match.group(3),
                    match.group(2) or match.group(4),
                )
                for match in pattern.finditer(text)
            ]
        else:
            matches = [
                (match.group(1), match.group(2))
                for match in re.finditer(r"^\*\*Q(\d+)[：:]\s*(.+?)\*\*", text, re.MULTILINE)
            ]
        for number, question_text in matches:
            ordinal += 1
            questions.append(
                make_question(
                    source="guide",
                    revision=GUIDE_COMMIT,
                    module=file_name.removesuffix(".md"),
                    source_number=f"Q{number}",
                    text=question_text,
                    ordinal=ordinal,
                )
            )
    return questions


def datawhale_questions() -> list[Question]:
    url = (
        "https://raw.githubusercontent.com/datawhalechina/hello-agents/"
        f"{DATAWHALE_COMMIT}/Extra-Chapter/"
        "Extra01-%E9%9D%A2%E8%AF%95%E9%97%AE%E9%A2%98%E6%80%BB%E7%BB%93.md"
    )
    text = fetch(url)
    section = "unclassified"
    ordinal = 0
    output: list[Question] = []
    for line in text.splitlines():
        heading = re.match(r"^###\s+(.+?)\s*$", line)
        if heading:
            section = heading.group(1).strip()
            continue
        item = re.match(r"^\s*(\d+)\.\s+(.+?)\s*$", line)
        if not item:
            continue
        ordinal += 1
        output.append(
            make_question(
                source="datawhale",
                revision=DATAWHALE_COMMIT,
                module=section,
                source_number=item.group(1),
                text=item.group(2),
                ordinal=ordinal,
            )
        )
    return output


def main() -> None:
    questions = guide_questions() + datawhale_questions()
    guide_count = sum(item.source == "guide" for item in questions)
    datawhale_count = sum(item.source == "datawhale" for item in questions)
    if guide_count != 203:
        raise RuntimeError(f"guide extraction drifted: expected 203, got {guide_count}")
    if datawhale_count != 92:
        raise RuntimeError(f"Datawhale extraction drifted: expected 92, got {datawhale_count}")

    payload = {
        "schema_version": "agent-interview-gap-inventory/v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "purpose": "Capability-probe inventory for the local Stata Research Agent",
        "sources": [
            {
                "source": "guide",
                "repository": "https://github.com/Horanluo/ai-agent-interview-guide",
                "revision": GUIDE_COMMIT,
                "question_count": guide_count,
            },
            {
                "source": "datawhale",
                "repository": "https://github.com/datawhalechina/hello-agents",
                "revision": DATAWHALE_COMMIT,
                "question_count": datawhale_count,
            },
        ],
        "summary": {
            "total_questions": len(questions),
            "by_disposition": dict(Counter(item.disposition for item in questions)),
            "by_status": dict(Counter(item.project_status for item in questions)),
            "by_cluster": dict(Counter(item.capability_cluster for item in questions)),
        },
        "cluster_contracts": CLUSTERS,
        "questions": [asdict(item) for item in questions],
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    status_icon = {
        "verified": "✅",
        "partial": "🟡",
        "missing": "🔴",
        "not_planned_v0_1": "⚪",
        "not_a_product_requirement": "⚪",
    }
    actionable = [item for item in questions if item.disposition == "project_capability"]
    lines = [
        "# Agent 项目能力审计 Checklist",
        "",
        "> 由两套题库的固定 revision 自动生成。题目是能力探针，不是产品需求。",
        (
            f"> 主清单只展示可转化为产品能力的 {len(actionable)} 项；"
            "纯理论和架构非目标仍保留在 JSON 母表中。"
        ),
        "",
        f"- 主清单 revision：`{GUIDE_COMMIT}`（203 项）",
        f"- 压力题 revision：`{DATAWHALE_COMMIT}`（92 项）",
        "- 状态：✅ 已有证据；🟡 部分实现/证据不足；🔴 缺失",
        "",
    ]
    for cluster in CLUSTERS:
        cluster_items = [item for item in actionable if item.capability_cluster == cluster]
        if not cluster_items:
            continue
        metadata = CLUSTERS[cluster]
        status = str(metadata["status"])
        lines.extend(
            [
                f"## {status_icon[status]} `{cluster}` — {status}",
                "",
            ]
        )
        evidence = tuple(str(item) for item in metadata["evidence"])
        if evidence:
            lines.append("证据：" + "、".join(f"`{item}`" for item in evidence))
            lines.append("")
        for item in cluster_items:
            lines.append(
                f"- {status_icon[item.project_status]} `{item.question_id}` "
                f"[{item.module}/{item.source_number}] {item.text}"
            )
        lines.append("")
    CHECKLIST_OUTPUT.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print(json.dumps(payload["summary"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
