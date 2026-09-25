import { useEffect, useMemo, useRef, useState } from "react";

import {
  getArtifactContent,
  getAnalysisOutputIndex,
  getConversationDetail,
  getDocumentIndex,
  getEvidenceLineage,
  getJournalEntries,
  getResearchPlan,
  getResultIndex,
  getTurnOperationalEvaluation,
  getTurnUsage,
  recordTurnOutcomeFeedback,
  type AnalysisOutputIndexResponse,
  type ConversationDetailResponse,
  type DocumentIndexResponse,
  type EvidenceLineageResponse,
  type ErrorResponse,
  type JournalEntryPageResponse,
  type ResearchPlanResponse,
  type ResultIndexResponse,
  type TurnUsageResponse,
  type TurnOperationalEvaluationResponse,
} from "./generated/api-v1";
import { newCommandId } from "./browser/commands";
import { ModelDeltaStream, type ModelResponseDelta } from "./browser/model-delta-stream";
import { projectProviderText } from "./browser/provider-text-projection";
import { MarkdownContent } from "./MarkdownContent";

type QueryState<T> =
  | { readonly kind: "loading" }
  | { readonly kind: "error"; readonly error: ErrorResponse }
  | { readonly kind: "ready"; readonly value: T };

type ArtifactPreview =
  | {
      readonly artifactId: string;
      readonly kind: "text";
      readonly mediaType: string;
      readonly content: string;
    }
  | {
      readonly artifactId: string;
      readonly kind: "image" | "binary";
      readonly mediaType: string;
      readonly objectUrl: string;
    };

function LoadingView() {
  return <div className="view-loading">正在读取已提交研究事实…</div>;
}

function QueryError({ error }: { readonly error: ErrorResponse }) {
  return (
    <div className="inline-error">
      <strong>{error.error.code}</strong>
      <span>{error.error.message}</span>
    </div>
  );
}

type LiveMessageState = {
  readonly status: "connecting" | "streaming" | "reconnecting";
  readonly providerAttemptId?: string;
  readonly text: string;
};

function LiveAssistantMessage({
  workspaceId,
  turnId,
  committedContents,
}: {
  readonly workspaceId: string;
  readonly turnId: string;
  readonly committedContents: ReadonlySet<string>;
}) {
  const stream = useMemo(() => new ModelDeltaStream(), []);
  const rawByAttempt = useRef(new Map<string, string>());
  const currentState = useRef<LiveMessageState>({ status: "connecting", text: "" });
  const pendingState = useRef<LiveMessageState | undefined>(undefined);
  const frame = useRef<number | undefined>(undefined);
  const card = useRef<HTMLElement>(null);
  const followOutput = useRef(true);
  const [live, setLive] = useState<LiveMessageState>(currentState.current);

  useEffect(() => {
    const scrollContainer = card.current?.closest(".view-body");
    if (!(scrollContainer instanceof HTMLElement)) return;
    const updateFollowState = () => {
      followOutput.current = (
        scrollContainer.scrollHeight - scrollContainer.scrollTop - scrollContainer.clientHeight
      ) < 180;
    };
    updateFollowState();
    scrollContainer.addEventListener("scroll", updateFollowState, { passive: true });
    return () => scrollContainer.removeEventListener("scroll", updateFollowState);
  }, []);

  useEffect(() => {
    rawByAttempt.current.clear();
    pendingState.current = undefined;
    currentState.current = { status: "connecting", text: "" };
    setLive(currentState.current);

    const flush = () => {
      frame.current = undefined;
      const next = pendingState.current;
      if (next === undefined) return;
      pendingState.current = undefined;
      currentState.current = next;
      setLive(next);
    };
    const schedule = (next: LiveMessageState) => {
      pendingState.current = next;
      if (frame.current === undefined) frame.current = window.requestAnimationFrame(flush);
    };
    const acceptDelta = (delta: ModelResponseDelta) => {
      const raw = (rawByAttempt.current.get(delta.provider_attempt_id) ?? "") + delta.content;
      rawByAttempt.current.set(delta.provider_attempt_id, raw);
      const projection = projectProviderText(raw);
      schedule({
        status: "streaming",
        providerAttemptId: delta.provider_attempt_id,
        text: projection?.text ?? "",
      });
    };

    stream.open(workspaceId, turnId, {
      onOpen: () => {
        const current = pendingState.current ?? currentState.current;
        schedule({
          ...current,
          status: current.text === "" ? "connecting" : "streaming",
        });
      },
      onDelta: acceptDelta,
      onReconnecting: () => {
        const current = pendingState.current ?? currentState.current;
        schedule({ ...current, status: "reconnecting" });
      },
    });
    return () => {
      stream.close();
      if (frame.current !== undefined) window.cancelAnimationFrame(frame.current);
      frame.current = undefined;
    };
  }, [stream, workspaceId, turnId]);

  useEffect(() => {
    if (!followOutput.current) return;
    const animationFrame = window.requestAnimationFrame(() => {
      card.current?.scrollIntoView({ block: "end" });
    });
    return () => window.cancelAnimationFrame(animationFrame);
  }, [live.text]);

  if (live.text !== "" && committedContents.has(live.text)) return null;
  const visibleText = live.text;
  const statusText = live.status === "reconnecting"
    ? "流式连接恢复中"
    : visibleText === ""
      ? "Agent 正在思考"
      : "Agent 正在回复";
  return (
    <article
      aria-busy="true"
      aria-live="polite"
      className="message-card assistant streaming-message"
      ref={card}
    >
      <div className="message-role">Agent · 实时</div>
      {visibleText === ""
        ? <div className="stream-placeholder">{statusText}<span className="streaming-dots">…</span></div>
        : <MarkdownContent content={visibleText} />}
      <div className="stream-status">
        <span>{statusText}</span>
        <span aria-hidden="true" className="streaming-caret" />
      </div>
    </article>
  );
}

function TurnUsageCard({
  workspaceId,
  turnId,
}: {
  readonly workspaceId: string;
  readonly turnId: string;
}) {
  const [state, setState] = useState<QueryState<TurnUsageResponse>>();

  function loadUsage() {
    if (state?.kind === "loading" || state?.kind === "ready") return;
    setState({ kind: "loading" });
    void getTurnUsage("", workspaceId, turnId).then((result) => {
      setState(
        result.ok
          ? { kind: "ready", value: result.data }
          : { kind: "error", error: result.error },
      );
    });
  }

  return (
    <article className="usage-card">
      <div className="usage-card-heading">
        <div>
          <strong>{turnId}</strong>
          {state?.kind === "ready" && <span>{state.value.data.turn_status}</span>}
        </div>
        <button type="button" onClick={loadUsage}>
          {state?.kind === "ready" ? "已加载" : "查看运行用量"}
        </button>
      </div>
      {state?.kind === "loading" && <div className="usage-loading">正在汇总运行事实…</div>}
      {state?.kind === "error" && <QueryError error={state.error} />}
      {state?.kind === "ready" && (
        <>
          <div className="usage-metrics">
            <div><span>Steps</span><strong>{state.value.data.step_count}</strong></div>
            <div><span>Provider calls</span><strong>{state.value.data.provider_attempts.length}</strong></div>
            <div><span>Input tokens</span><strong>{state.value.data.input_tokens_observed}</strong></div>
            <div><span>Output tokens</span><strong>{state.value.data.output_tokens_observed}</strong></div>
            <div><span>Cache hits</span><strong>{state.value.data.cached_input_tokens_observed}</strong></div>
            <div><span>Retry / fallback</span><strong>{state.value.data.retry_count} / {state.value.data.fallback_count}</strong></div>
          </div>
          <div className={`usage-cost ${state.value.data.monetary_estimate.quality}`}>
            <span>模型费用估算 · {state.value.data.monetary_estimate.pricing_revision}</span>
            <strong>
              {state.value.data.monetary_estimate.amount === null
                ? "无法完整估算"
                : `${state.value.data.monetary_estimate.currency} ${state.value.data.monetary_estimate.amount}`}
            </strong>
            {state.value.data.monetary_estimate.explanation !== null && (
              <small>{state.value.data.monetary_estimate.explanation}</small>
            )}
          </div>
          <details className="usage-details">
            <summary>Provider 与工具明细</summary>
            {state.value.data.provider_attempts.map((attempt) => (
              <div className="usage-attempt" key={attempt.provider_attempt_id}>
                <code>{attempt.provider_profile} · attempt {attempt.attempt_ordinal}</code>
                <span>{attempt.state} · {attempt.usage_quality}</span>
                <span>{attempt.input_tokens ?? "?"} in / {attempt.output_tokens ?? "?"} out</span>
                {(attempt.is_retry || attempt.is_fallback) && (
                  <b>{attempt.is_fallback ? "fallback" : "retry"}</b>
                )}
              </div>
            ))}
            {state.value.data.tool_operations.map((operation) => (
              <div className="usage-attempt" key={operation.operation_id}>
                <code>{operation.tool_name ?? operation.operation_kind}</code>
                <span>{operation.status}</span>
                <span>{operation.observed_duration_seconds ?? "?"}s</span>
              </div>
            ))}
          </details>
        </>
      )}
    </article>
  );
}

function TurnEvaluationCard({
  workspaceId,
  turnId,
  turnStatus,
}: {
  readonly workspaceId: string;
  readonly turnId: string;
  readonly turnStatus: string;
}) {
  const [state, setState] = useState<QueryState<TurnOperationalEvaluationResponse>>();
  const [feedbackState, setFeedbackState] = useState<"idle" | "saving" | "saved">("idle");
  const terminal = ["succeeded", "partial", "paused", "failed"].includes(turnStatus);

  function loadEvaluation(force = false) {
    if (!force && (state?.kind === "loading" || state?.kind === "ready")) return;
    setState({ kind: "loading" });
    void getTurnOperationalEvaluation("", workspaceId, turnId).then((result) => {
      setState(
        result.ok
          ? { kind: "ready", value: result.data }
          : { kind: "error", error: result.error },
      );
    });
  }

  function saveFeedback(disposition: "accepted" | "needs_revision" | "rejected") {
    if (feedbackState === "saving") return;
    setFeedbackState("saving");
    void recordTurnOutcomeFeedback("", workspaceId, turnId, {
      command_id: newCommandId(),
      disposition,
      ratings: [],
      issue_codes: [],
      comment: "",
      policy_revision: "turn-outcome-feedback/v1",
    }).then((result) => {
      if (result.ok) {
        setFeedbackState("saved");
        loadEvaluation(true);
      } else {
        setFeedbackState("idle");
        setState({ kind: "error", error: result.error });
      }
    });
  }

  return (
    <article className="evaluation-card">
      <div className="usage-card-heading">
        <div>
          <strong>运行体检</strong>
          <span>来自本次真实 Turn 的权威事实，不是 Benchmark 分数</span>
        </div>
        <button type="button" onClick={() => loadEvaluation()}>
          {state?.kind === "ready" ? "已加载" : "查看体检"}
        </button>
      </div>
      {state?.kind === "loading" && <div className="usage-loading">正在计算 L1 / L2 / L3…</div>}
      {state?.kind === "error" && <QueryError error={state.error} />}
      {state?.kind === "ready" && (
        <>
          <div className="evaluation-layers">
            {state.value.data.layers.map((layer) => (
              <div className={`evaluation-layer ${layer.status}`} key={layer.layer}>
                <strong>{layer.layer}</strong>
                <span>{layer.status}</span>
                <small>{layer.hard_failure_count} hard fail · {layer.warning_count} warning</small>
              </div>
            ))}
          </div>
          <div className="evaluation-config">
            <span>Model: {state.value.data.configuration.model_names.join(", ") || "尚未调用"}</span>
            <span>Skill: {state.value.data.configuration.main_skill_name ?? "尚未冻结"} {state.value.data.configuration.main_skill_revision ?? ""}</span>
            <span>Policy: {state.value.data.policy_revision}</span>
          </div>
          <details className="usage-details">
            <summary>指标与失败分类</summary>
            {state.value.data.layers.flatMap((layer) => layer.metrics)
              .filter((metric) => metric.status === "fail" || metric.status === "warn")
              .map((metric) => (
                <div className="evaluation-finding" key={metric.metric_id}>
                  <code>{metric.metric_id}</code>
                  <b>{metric.status}</b>
                  <span>{metric.explanation}</span>
                </div>
              ))}
            {state.value.data.breakdowns
              .filter((breakdown) => breakdown.items.length > 0)
              .map((breakdown) => (
                <div className="evaluation-breakdown" key={breakdown.breakdown_id}>
                  <strong>{breakdown.breakdown_id}</strong>
                  <span>{breakdown.items.map((item) => `${item.key} ${item.count}`).join(" · ")}</span>
                </div>
              ))}
          </details>
          {terminal && (
            <div className="outcome-feedback">
              <span>这次结果对你是否有用？可选，不影响研究事实。</span>
              <div>
                <button type="button" onClick={() => saveFeedback("accepted")}>直接采用</button>
                <button type="button" onClick={() => saveFeedback("needs_revision")}>继续修改</button>
                <button type="button" onClick={() => saveFeedback("rejected")}>不采用</button>
              </div>
              {feedbackState === "saved" && <small>已记录；后续反馈会追加保留历史。</small>}
            </div>
          )}
        </>
      )}
    </article>
  );
}

export function ConversationDetailView({
  workspaceId,
  conversationId,
  queryRevision,
  activeTurnId,
}: {
  readonly workspaceId: string;
  readonly conversationId: string;
  readonly queryRevision: number;
  readonly activeTurnId?: string;
}) {
  const [state, setState] = useState<QueryState<ConversationDetailResponse>>({
    kind: "loading",
  });
  useEffect(() => {
    const controller = new AbortController();
    setState({ kind: "loading" });
    void getConversationDetail("", workspaceId, conversationId, controller.signal).then(
      (result) => {
        if (controller.signal.aborted) return;
        setState(result.ok ? { kind: "ready", value: result.data } : { kind: "error", error: result.error });
      },
    );
    return () => controller.abort();
  }, [workspaceId, conversationId, queryRevision]);

  if (state.kind === "loading") return <LoadingView />;
  if (state.kind === "error") return <QueryError error={state.error} />;
  if (state.value.data.timeline.length === 0 && activeTurnId === undefined) {
    return <div className="empty-state compact"><h2>这个对话还没有已提交内容</h2></div>;
  }
  const committedContents = new Set(
    state.value.data.timeline
      .filter((item) => item.role === "assistant" && item.turn_id === activeTurnId)
      .map((item) => item.content),
  );
  return (
    <div className="conversation-view">
      <div className="view-watermark">一致读 R{state.value.authoritative_revision}</div>
      {state.value.data.timeline.map((item) => (
        <article className={`message-card ${item.role}`} key={item.item_id}>
          <div className="message-role">{item.role === "user" ? "研究者" : "Agent"}</div>
          <MarkdownContent content={item.content} />
          <div className="item-meta">{item.item_id} · R{item.created_revision}</div>
        </article>
      ))}
      {activeTurnId !== undefined && (
        <LiveAssistantMessage
          committedContents={committedContents}
          turnId={activeTurnId}
          workspaceId={workspaceId}
        />
      )}
      <section className="usage-section">
        <div className="usage-section-heading">
          <div><div className="eyebrow">运行记录</div><h2>用量与体检</h2></div>
          <span>按 Turn 汇总 · 不属于研究证据</span>
        </div>
        {state.value.data.turns.map((turn) => (
          <details className="turn-observability" key={turn.turn_id}>
            <summary>
              <span>
                <strong>Turn {turn.status}</strong>
                <small>{turn.turn_id}</small>
              </span>
              <span>展开运行详情</span>
            </summary>
            <div className="turn-observability-body">
              <TurnUsageCard workspaceId={workspaceId} turnId={turn.turn_id} />
              <TurnEvaluationCard
                workspaceId={workspaceId}
                turnId={turn.turn_id}
                turnStatus={turn.status}
              />
            </div>
          </details>
        ))}
      </section>
    </div>
  );
}

export function ResultsView({
  workspaceId,
  researchPathId,
  queryRevision,
}: {
  readonly workspaceId: string;
  readonly researchPathId: string;
  readonly queryRevision: number;
}) {
  const [state, setState] = useState<QueryState<ResultIndexResponse>>({ kind: "loading" });
  const [planState, setPlanState] = useState<QueryState<ResearchPlanResponse>>({
    kind: "loading",
  });
  const [analysisState, setAnalysisState] = useState<QueryState<AnalysisOutputIndexResponse>>({
    kind: "loading",
  });
  const [previewError, setPreviewError] = useState<string>();
  const [artifactPreview, setArtifactPreview] = useState<ArtifactPreview>();
  const [lineage, setLineage] = useState<QueryState<EvidenceLineageResponse> | undefined>();
  useEffect(() => {
    const controller = new AbortController();
    setState({ kind: "loading" });
    void getResultIndex("", workspaceId, researchPathId, controller.signal).then((result) => {
      if (controller.signal.aborted) return;
      setState(result.ok ? { kind: "ready", value: result.data } : { kind: "error", error: result.error });
    });
    setPlanState({ kind: "loading" });
    void getResearchPlan("", workspaceId, researchPathId, controller.signal).then((result) => {
      if (controller.signal.aborted) return;
      setPlanState(
        result.ok
          ? { kind: "ready", value: result.data }
          : { kind: "error", error: result.error },
      );
    });
    void getAnalysisOutputIndex("", workspaceId, controller.signal).then((result) => {
      if (controller.signal.aborted) return;
      setAnalysisState(
        result.ok
          ? { kind: "ready", value: result.data }
          : { kind: "error", error: result.error },
      );
    });
    return () => controller.abort();
  }, [workspaceId, researchPathId, queryRevision]);
  useEffect(
    () => () => {
      if (artifactPreview?.kind === "image" || artifactPreview?.kind === "binary") {
        URL.revokeObjectURL(artifactPreview.objectUrl);
      }
    },
    [artifactPreview],
  );

  function inspectElement(resultElementId: string) {
    setLineage({ kind: "loading" });
    void getEvidenceLineage(
      "",
      workspaceId,
      "result_element",
      resultElementId,
    ).then((result) => {
      setLineage(
        result.ok
          ? { kind: "ready", value: result.data }
          : { kind: "error", error: result.error },
      );
    });
  }

  async function previewArtifact(artifactId: string) {
    setPreviewError(undefined);
    const response = await getArtifactContent("", workspaceId, artifactId);
    if (!response.ok) {
      setPreviewError(`预览失败：${response.status}`);
      return;
    }
    const mediaType = response.headers.get("content-type")?.split(";", 1)[0] ?? "application/octet-stream";
    const blob = await response.blob();
    if (mediaType === "application/json" || mediaType.startsWith("text/")) {
      setArtifactPreview({ artifactId, kind: "text", mediaType, content: await blob.text() });
      return;
    }
    const objectUrl = URL.createObjectURL(blob);
    setArtifactPreview({
      artifactId,
      kind: mediaType.startsWith("image/") ? "image" : "binary",
      mediaType,
      objectUrl,
    });
  }

  if (state.kind === "loading") return <LoadingView />;
  if (state.kind === "error") return <QueryError error={state.error} />;
  return (
    <section className="research-index">
      <div className="view-intro">
        <div><div className="eyebrow">Adaptive research plan</div><h2>当前研究方案</h2></div>
        {planState.kind === "ready" && planState.value.data.plan_revision_id !== null && (
          <span>
            revision {planState.value.data.revision_number} · pointer {planState.value.data.pointer_revision}
          </span>
        )}
      </div>
      {planState.kind === "loading" && <LoadingView />}
      {planState.kind === "error" && <QueryError error={planState.error} />}
      {planState.kind === "ready" && planState.value.data.plan_revision_id === null && (
        <div className="empty-state compact"><h2>Agent 尚未提交研究方案</h2></div>
      )}
      {planState.kind === "ready" && planState.value.data.plan_revision_id !== null && (
        <article className="plan-panel">
          <div className="card-topline">
            <span>{planState.value.data.change_kind ?? "当前采用"}</span>
            <b title={planState.value.data.plan_revision_id ?? undefined}>
              Plan v{planState.value.data.revision_number}
            </b>
          </div>
          <h3>{planState.value.data.summary}</h3>
          <div className="plan-node-list">
            {planState.value.data.nodes.map((node) => (
              <div className={node.completed_run_count > 0 ? "plan-node completed" : "plan-node"} key={node.plan_node_id}>
                <span>{node.ordinal}</span>
                <div><strong>{node.canonical_key}</strong><small>{node.node_kind}</small></div>
                <b>{node.completed_run_count > 0 ? `${node.completed_run_count} run` : "planned"}</b>
              </div>
            ))}
          </div>
          {planState.value.data.dependencies.length > 0 && (
            <details>
              <summary>查看节点依赖</summary>
              {planState.value.data.dependencies.map((dependency) => (
                <div className="item-meta" key={`${dependency.upstream_node_key}:${dependency.downstream_node_key}`}>
                  {dependency.upstream_node_key} → {dependency.downstream_node_key} · {dependency.dependency_kind}
                </div>
              ))}
            </details>
          )}
        </article>
      )}
      <div className="view-intro">
        <div><div className="eyebrow">Current adoptions</div><h2>当前研究结果</h2></div>
        <span title={researchPathId}>当前分支 · R{state.value.authoritative_revision}</span>
      </div>
      {state.value.data.items.length === 0 ? (
        <div className="empty-state compact"><h2>当前路径尚未采用正式 Result</h2><p>探索结果不会自动出现在这里。</p></div>
      ) : (
        <div className="index-grid">
          {state.value.data.items.map((item) => (
            <article className="research-card" key={item.result_slot_id}>
              <div className="card-topline"><span>{item.slot_display_name}</span><b>{item.result_kind}</b></div>
              <h3>{item.slot_key}</h3>
              <dl>
                <div><dt>Result</dt><dd>{item.result_id}</dd></div>
                <div><dt>Stata Run</dt><dd>{item.producing_stata_run_id}</dd></div>
                <div><dt>Elements</dt><dd>{item.element_count}</dd></div>
                <div><dt>Evidence</dt><dd>{item.evidence_count}</dd></div>
              </dl>
              <details className="lineage-element-list">
                <summary>逐项核查数字（{item.result_element_ids.length}）</summary>
                <div className="lineage-actions">
                  {item.result_element_ids.map((elementId, index) => (
                    <button type="button" key={elementId} onClick={() => inspectElement(elementId)}>
                      数字 {index + 1}
                    </button>
                  ))}
                </div>
              </details>
              <footer>pointer v{item.pointer_revision} · adopted R{item.adopted_revision}</footer>
            </article>
          ))}
        </div>
      )}
      {lineage?.kind === "loading" && <LoadingView />}
      {lineage?.kind === "error" && <QueryError error={lineage.error} />}
      {lineage?.kind === "ready" && (
        <aside className="lineage-inspector">
          <div className="view-intro">
            <div><div className="eyebrow">Verified lineage</div><h2>数字来源</h2></div>
            <button type="button" onClick={() => setLineage(undefined)}>关闭</button>
          </div>
          <dl>
            <div><dt>数值</dt><dd>{lineage.value.data.canonical_decimal_text}</dd></div>
            <div><dt>语义</dt><dd>{lineage.value.data.semantic_key}</dd></div>
            <div><dt>Data Version</dt><dd>{lineage.value.data.data_version_id}</dd></div>
            <div><dt>Stata Run</dt><dd>{lineage.value.data.stata_run_id}</dd></div>
            <div><dt>Operation</dt><dd>{lineage.value.data.operation_id}</dd></div>
          </dl>
          <h3>完整 Stata 数据状态链</h3>
          <div className="data-state-chain">
            {lineage.value.data.data_state_steps.map((step, index) => (
              <article key={step.operation_id}>
                <div className="card-topline">
                  <span>{index + 1}. {step.execution_purpose}</span>
                  <b>generation {step.session_generation}</b>
                </div>
                <pre>{step.command_text}</pre>
                <div className="item-meta">
                  {step.operation_id} · manifest {step.completion_manifest_id}
                </div>
              </article>
            ))}
          </div>
          <details>
            <summary>查看原始定位器与来源指纹</summary>
            <pre>{JSON.stringify({
              locator: JSON.parse(lineage.value.data.locator_json) as unknown,
              source_chain_sha256: lineage.value.data.source_chain_sha256,
              completion_manifest_id: lineage.value.data.completion_manifest_id,
            }, null, 2)}</pre>
          </details>
        </aside>
      )}
      <div className="view-intro analysis-output-heading">
        <div>
          <div className="eyebrow">Python / Shell exploration</div>
          <h2>探索输出与确认状态</h2>
        </div>
        <span>分类不等于正式采用</span>
      </div>
      {analysisState.kind === "loading" && <LoadingView />}
      {analysisState.kind === "error" && <QueryError error={analysisState.error} />}
      {analysisState.kind === "ready" && analysisState.value.data.items.length === 0 && (
        <div className="empty-state compact">
          <h2>当前没有已分类的 Python/Shell 输出</h2>
        </div>
      )}
      {analysisState.kind === "ready" && analysisState.value.data.items.length > 0 && (
        <div className="index-grid analysis-output-grid">
          {analysisState.value.data.items.map((item) => (
            <article className="research-card" key={item.analysis_output_id}>
              <div className="card-topline">
                <span>{item.runtime_kind}</span>
                <b>{item.output_kind}</b>
              </div>
              <h3>{item.adoption_id === null ? "等待研究者确认" : "已明确采用"}</h3>
              <p>{item.method_summary}</p>
              <dl>
                <div><dt>Analysis Output</dt><dd>{item.analysis_output_id}</dd></div>
                <div><dt>Fingerprint</dt><dd>{item.output_fingerprint}</dd></div>
                <div>
                  <dt>正式资格</dt>
                  <dd>{item.document_eligible ? "确认后可进入正式链" : "不可直接进入正式文档"}</dd>
                </div>
              </dl>
              <div className="analysis-artifacts">
                {item.artifacts.map((artifact) => (
                  <details key={artifact.artifact_id}>
                    <summary>{artifact.role} · {artifact.artifact_kind} · {artifact.media_type}</summary>
                    <div className="item-meta">
                      {artifact.artifact_id}<br />sha256 {artifact.content_sha256}<br />
                      receipt {artifact.verification_receipt_id ?? "—"}
                    </div>
                    <button type="button" onClick={() => void previewArtifact(artifact.artifact_id)}>
                      查看精确产物
                    </button>
                  </details>
                ))}
              </div>
              <footer>created R{item.created_revision} · {item.evidence_record_id ?? "no evidence yet"}</footer>
            </article>
          ))}
        </div>
      )}
      {previewError !== undefined && <div className="inline-error">{previewError}</div>}
      {artifactPreview !== undefined && (
        <aside className="artifact-preview">
          <div className="view-intro">
            <div>
              <div className="eyebrow">Exact managed artifact</div>
              <h2>精确产物预览</h2>
            </div>
            <button type="button" onClick={() => setArtifactPreview(undefined)}>关闭</button>
          </div>
          <div className="item-meta">{artifactPreview.artifactId} · {artifactPreview.mediaType}</div>
          {artifactPreview.kind === "text" && <pre>{artifactPreview.content}</pre>}
          {artifactPreview.kind === "image" && (
            <img src={artifactPreview.objectUrl} alt={`Artifact ${artifactPreview.artifactId}`} />
          )}
          {artifactPreview.kind === "binary" && (
            <a href={artifactPreview.objectUrl} download={artifactPreview.artifactId}>
              下载并使用本地应用核查
            </a>
          )}
        </aside>
      )}
    </section>
  );
}

export function TraceView({
  workspaceId,
  queryRevision,
}: {
  readonly workspaceId: string;
  readonly queryRevision: number;
}) {
  const [pages, setPages] = useState<ReadonlyArray<JournalEntryPageResponse>>([]);
  const [state, setState] = useState<"loading" | "ready" | "error">("loading");
  const [error, setError] = useState<ErrorResponse | undefined>();
  const [refreshToken, setRefreshToken] = useState(0);
  const [filter, setFilter] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    setState("loading");
    setPages([]);
    void getJournalEntries("", workspaceId, { pageSize: 30 }, controller.signal).then((result) => {
      if (controller.signal.aborted) return;
      if (result.ok) {
        setPages([result.data]);
        setState("ready");
      } else {
        setError(result.error);
        setState("error");
      }
    });
    return () => controller.abort();
  }, [workspaceId, refreshToken]);

  if (state === "loading") return <LoadingView />;
  if (state === "error" && error !== undefined) return <QueryError error={error} />;
  const lastPage = pages.at(-1);
  const items = pages.flatMap((page) => page.items);
  const normalizedFilter = filter.trim().toLocaleLowerCase();
  const visibleItems = normalizedFilter === ""
    ? items
    : items.filter((item) =>
      [item.event_type, item.summary, item.object_type, item.object_id]
        .some((value) => value.toLocaleLowerCase().includes(normalizedFilter)),
    );
  return (
    <section className="trace-view">
      <div className="view-intro">
        <div><div className="eyebrow">Interaction Journal</div><h2>原始 Trace</h2></div>
        <span>冻结窗口 R{pages[0]?.as_of_workspace_revision ?? "—"}</span>
      </div>
      {(lastPage?.newer_matching_entries_available === true ||
        (pages[0] !== undefined && queryRevision > pages[0].as_of_workspace_revision)) && (
        <div className="trace-notice">
          冻结窗口之后已有新记录。
          <button type="button" onClick={() => setRefreshToken((current) => current + 1)}>
            读取最新 Trace
          </button>
        </div>
      )}
      <div className="trace-toolbar">
        <label htmlFor="trace-filter">筛选当前已加载记录</label>
        <input
          id="trace-filter"
          onChange={(event) => setFilter(event.target.value)}
          placeholder="输入事件、对象或关键词"
          type="search"
          value={filter}
        />
        <span>{visibleItems.length} / {items.length}</span>
      </div>
      <div className="trace-list">
        {visibleItems.map((item) => (
          <details className="trace-row" key={item.journal_entry_id}>
            <summary>
              <span className="trace-key">R{item.workspace_revision}.{item.ordinal}</span>
              <span className="trace-summary">
                <strong>{item.summary}</strong>
                <small>{item.event_type}</small>
              </span>
            </summary>
            <pre>{JSON.stringify(JSON.parse(item.payload_json) as unknown, null, 2)}</pre>
            <div className="item-meta">{item.journal_entry_id}</div>
          </details>
        ))}
        {visibleItems.length === 0 && (
          <div className="empty-state compact">
            <h2>当前已加载记录中没有匹配项</h2>
            <p>清空筛选词，或继续加载更早记录。</p>
          </div>
        )}
      </div>
      {lastPage?.next_cursor !== null && lastPage?.next_cursor !== undefined && (
        <button
          className="load-more"
          type="button"
          onClick={() => {
            setState("loading");
            void getJournalEntries("", workspaceId, {
              cursor: lastPage.next_cursor!,
              pageSize: lastPage.page_size,
              sort: lastPage.sort_direction,
            }).then((result) => {
              if (result.ok) {
                setPages((current) => [...current, result.data]);
                setState("ready");
              } else {
                setError(result.error);
                setState("error");
              }
            });
          }}
        >
          加载更早记录
        </button>
      )}
    </section>
  );
}

export function WordView({
  workspaceId,
  researchPathId,
  queryRevision,
}: {
  readonly workspaceId: string;
  readonly researchPathId: string;
  readonly queryRevision: number;
}) {
  const [state, setState] = useState<QueryState<DocumentIndexResponse>>({ kind: "loading" });
  const [downloadState, setDownloadState] = useState<string>();
  useEffect(() => {
    const controller = new AbortController();
    setState({ kind: "loading" });
    void getDocumentIndex("", workspaceId, researchPathId, controller.signal).then((result) => {
      if (controller.signal.aborted) return;
      setState(result.ok ? { kind: "ready", value: result.data } : { kind: "error", error: result.error });
    });
    return () => controller.abort();
  }, [workspaceId, researchPathId, queryRevision]);

  if (state.kind === "loading") return <LoadingView />;
  if (state.kind === "error") return <QueryError error={state.error} />;

  async function downloadDocument(artifactId: string, slotKey: string) {
    setDownloadState("正在核验并准备 DOCX…");
    const response = await getArtifactContent("", workspaceId, artifactId);
    if (!response.ok) {
      setDownloadState(`DOCX 获取失败：HTTP ${response.status}`);
      return;
    }
    const blob = await response.blob();
    const objectUrl = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = objectUrl;
    anchor.download = `${slotKey.endsWith("delivery") ? "delivery" : "working"}-research-draft.docx`;
    document.body.append(anchor);
    anchor.click();
    anchor.remove();
    URL.revokeObjectURL(objectUrl);
    setDownloadState("DOCX 已下载，可用 Word 或兼容软件打开。");
  }

  return (
    <section className="research-index">
      <div className="view-intro">
        <div><div className="eyebrow">Working / Delivery</div><h2>文章版本</h2></div>
        <span title={researchPathId}>当前分支 · R{state.value.authoritative_revision}</span>
      </div>
      {downloadState !== undefined && <div className="command-state accepted" role="status" aria-live="polite">{downloadState}</div>}
      <div className="document-grid">
        {state.value.data.items.map((item) => (
          <article className="document-card" key={item.document_slot_id}>
            <div className="card-topline">
              <span>{item.slot_key.endsWith("working") ? "工作稿" : "正式稿"}</span>
              <b className={item.delivery_gate_verdict === "pass" ? "pass" : ""}>
                {item.delivery_gate_verdict === "pass"
                  ? "已通过"
                  : item.delivery_gate_verdict ?? "未核验"}
              </b>
            </div>
            {item.document_revision_id === null ? (
              <p>此槽位还没有采用文档版本。</p>
            ) : (
              <>
                <dl>
                  <div><dt>Revision</dt><dd>{item.document_revision_id}</dd></div>
                  <div><dt>Origin</dt><dd>{item.origin_kind}</dd></div>
                  <div><dt>DOCX Artifact</dt><dd>{item.docx_artifact_id}</dd></div>
                  <div><dt>Pointer</dt><dd>v{item.pointer_revision} · R{item.adopted_revision}</dd></div>
                </dl>
                {item.docx_artifact_id !== null && (
                  <button
                    className="document-download"
                    type="button"
                    onClick={() => void downloadDocument(item.docx_artifact_id!, item.slot_key)}
                  >
                    下载 DOCX
                  </button>
                )}
              </>
            )}
          </article>
        ))}
      </div>
      {state.value.data.items.every((item) => item.document_revision_id === null) && (
        <div className="empty-state compact"><h2>当前路径还没有文章版本</h2><p>选择“自动继续到可核查的 Word 初稿”后，Agent 会在交付门通过时生成文档。</p></div>
      )}
    </section>
  );
}
