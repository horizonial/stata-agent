import { useEffect, useMemo, useRef, useState } from "react";

import type {
  GlobalAttentionResponse,
  ConversationMemoryPolicyResponse,
  KnowledgeIndexResponse,
  MemoryIndexResponse,
  ProviderProfileIndexResponse,
  SkillEvolutionIndexResponse,
  MessageSubmitEnvelope,
  ResearchPathBranchEnvelope,
  TurnPauseRequestEnvelope,
  WaitingAnswerEnvelope,
  WorkspaceBootstrapResponse,
  WorkspaceCreateEnvelope,
  WorkspaceDataCatalogResponse,
  WorkspaceModelConfigurationResponse,
} from "./generated/api-v1";
import {
  createProviderCredential,
  activateMemory,
  activateSkillChange,
  activateSkillEvolutionCandidate,
  deactivateWorkspaceSkill,
  evaluateWorkspaceSkill,
  getConversationMemoryPolicy,
  getKnowledgeIndex,
  getMemoryIndex,
  getProviderProfiles,
  getSkillEvolutionCandidates,
  getWorkspaceAttention,
  getWorkspaceDataCatalog,
  getWorkspaceModelConfiguration,
  materializeSkillChange,
  putWorkspaceModelConfiguration,
  putConversationMemoryPolicy,
  retractMemory,
  rejectSkillChange,
  rejectSkillEvolutionCandidate,
  rollbackWorkspaceSkill,
  reviseMemory,
} from "./generated/api-v1";
import { GlobalAttentionStream } from "./browser/attention-stream";
import { WorkspaceShell, type ClientSyncState } from "./browser/shell";
import {
  newCommandId,
  PendingCommandCoordinator,
  type PendingCommandState,
} from "./browser/commands";
import { ConversationDetailView, ResultsView, TraceView, WordView } from "./ResearchViews";

type ViewName =
  | "conversation"
  | "results"
  | "trace"
  | "word"
  | "knowledge"
  | "memory"
  | "settings";

const VIEWS: ReadonlyArray<{ readonly id: ViewName; readonly label: string }> = [
  { id: "conversation", label: "对话" },
  { id: "results", label: "结果" },
  { id: "trace", label: "Trace" },
  { id: "word", label: "Word" },
  { id: "knowledge", label: "资料" },
  { id: "memory", label: "记忆" },
  { id: "settings", label: "设置" },
];

function initialWorkspace(): string {
  const candidate = new URLSearchParams(window.location.search).get("workspace");
  return candidate?.startsWith("ws_") ? candidate : "ws_demo";
}

function statusText(state: ClientSyncState): string {
  switch (state.kind) {
    case "cold":
      return "未连接";
    case "bootstrapping":
      return "正在读取工作区";
    case "ready":
      return "已同步";
    case "reconnecting":
      return "连接恢复中 · 数据可能不是最新";
    case "resyncing":
      return "正在重新同步";
    case "unavailable":
      return "工作区不可用";
  }
}

function fileSize(size: number): string {
  if (size < 1024 * 1024) return `${Math.max(1, Math.round(size / 1024))} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

function ConversationView({
  snapshot,
  conversationId,
}: {
  readonly snapshot: WorkspaceBootstrapResponse;
  readonly conversationId: string;
}) {
  const activeTurn = snapshot.data.execution.turns.find(
    (turn) => turn.turn_id === snapshot.data.execution.active_write_turn_id,
  );
  const conversationActiveTurn = activeTurn?.conversation_id === conversationId
    && activeTurn.status === "running"
    && snapshot.data.open_waiting_request === null
    ? activeTurn
    : undefined;
  return (
    <div className="conversation-view">
      <ConversationDetailView
        workspaceId={snapshot.workspace_id}
        conversationId={conversationId}
        queryRevision={snapshot.authoritative_revision}
        {...(conversationActiveTurn === undefined
          ? {}
          : { activeTurnId: conversationActiveTurn.turn_id })}
      />
      {snapshot.data.open_waiting_request !== null && (
        <article className="waiting-card">
          <div className="eyebrow">等待你的决定</div>
          <h2>{snapshot.data.open_waiting_request.prompt}</h2>
          <p>回答后同一 Turn 将以新的 revision 继续；Waiting 期间仍持有 Workspace 写执行 lane。</p>
        </article>
      )}
    </div>
  );
}

function WorkspaceControls({
  snapshot,
  conversationId,
  researchPathId,
  coordinator,
  commandStates,
  onSettled,
  onConversationCreated,
}: {
  readonly snapshot: WorkspaceBootstrapResponse;
  readonly conversationId: string | undefined;
  readonly researchPathId: string | undefined;
  readonly coordinator: PendingCommandCoordinator;
  readonly commandStates: ReadonlyMap<string, PendingCommandState>;
  readonly onSettled: () => Promise<void>;
  readonly onConversationCreated: () => void;
}) {
  const [instruction, setInstruction] = useState("");
  const [deliverWord, setDeliverWord] = useState(conversationId === undefined);
  const [waitingAnswer, setWaitingAnswer] = useState("");
  const [toolDecision, setToolDecision] = useState<"approve" | "deny" | "cancel" | undefined>();
  const activeTurn = snapshot.data.execution.turns.find(
    (turn) => turn.turn_id === snapshot.data.execution.active_write_turn_id,
  );
  const waiting = snapshot.data.open_waiting_request;
  const queuedTurns = snapshot.data.execution.turns.filter(
    (turn) => turn.execution_mode === "write" && turn.status === "queued",
  );
  const latestCommand = Array.from(commandStates.values()).at(-1);

  useEffect(() => {
    setDeliverWord(conversationId === undefined);
  }, [conversationId]);

  return (
    <section className="workspace-controls">
      {queuedTurns.length > 0 && (
        <div className="queue-strip">
          <strong>执行队列</strong>
          {queuedTurns.map((turn, index) => (
            <span key={turn.turn_id}>
              #{index + 1} · {turn.turn_id} · enqueue {turn.enqueue_ordinal}
            </span>
          ))}
        </div>
      )}
      {snapshot.data.active_pause_intent !== null && (
        <div className="pause-banner">
          <strong>正在安全暂停</strong>
          <span>{snapshot.data.active_pause_intent.status} · {snapshot.data.active_pause_intent.reason}</span>
        </div>
      )}
      {waiting !== null && (
        <form
          className="decision-form"
          onSubmit={(event) => {
            event.preventDefault();
            if (waitingAnswer.trim() === "") return;
            const envelope: WaitingAnswerEnvelope = {
              schema_version: "1",
              command_id: newCommandId(),
              workspace_id: snapshot.workspace_id,
              command_type: "waiting.answer",
              payload: {
                waiting_request_id: waiting.waiting_request_id,
                turn_id: waiting.turn_id,
                waiting_revision: waiting.created_turn_revision,
                answer: waitingAnswer,
                tool_decision: toolDecision ?? null,
              },
              preconditions: {},
              client_context: { interaction_id: waiting.waiting_request_id },
            };
            void coordinator.submit(envelope).then(async () => {
              setWaitingAnswer("");
              await onSettled();
            });
          }}
        >
          <label htmlFor="waiting-answer">回答当前 Waiting 决策</label>
          <textarea
            id="waiting-answer"
            value={waitingAnswer}
            onChange={(event) => setWaitingAnswer(event.target.value)}
          />
          {waiting.wait_reason === "user_confirmation" && (
            <div className="decision-options">
              {(["approve", "deny", "cancel"] as const).map((decision) => (
                <button
                  className={toolDecision === decision ? "active" : ""}
                  key={decision}
                  onClick={() => setToolDecision(decision)}
                  type="button"
                >
                  {decision}
                </button>
              ))}
            </div>
          )}
          <button type="submit">提交回答</button>
        </form>
      )}
      <form
        className="instruction-form"
        onSubmit={(event) => {
          event.preventDefault();
          if (instruction.trim() === "") return;
          const envelope: MessageSubmitEnvelope = {
            schema_version: "1",
            command_id: newCommandId(),
            workspace_id: snapshot.workspace_id,
            command_type: "message.submit",
            payload: {
              content: instruction,
              conversation_id: conversationId ?? null,
              research_path_id: conversationId === undefined ? researchPathId ?? null : null,
              execution_mode: "write",
              goal_mode: deliverWord ? "deliver_word" : "research_loop",
            },
            preconditions: {},
            client_context: { interaction_id: conversationId ?? snapshot.workspace_id },
          };
            void coordinator.submit(envelope).then(async () => {
              setInstruction("");
              setDeliverWord(false);
              await onSettled();
              if (conversationId === undefined) onConversationCreated();
            });
        }}
      >
        <label htmlFor="new-instruction">
          {activeTurn === undefined ? "新研究指令" : "新指令 · 将进入 Workspace 队列"}
        </label>
        <label className="goal-mode-control">
          <input
            checked={deliverWord}
            onChange={(event) => setDeliverWord(event.target.checked)}
            type="checkbox"
          />
          自动继续到可核查的 Word 初稿
        </label>
        <div>
          <textarea
            id="new-instruction"
            value={instruction}
            onChange={(event) => setInstruction(event.target.value)}
          />
          <button type="submit">发送</button>
        </div>
      </form>
      {activeTurn !== undefined && snapshot.data.active_pause_intent === null && (
        <button
          className="pause-button"
          type="button"
          onClick={() => {
            const envelope: TurnPauseRequestEnvelope = {
              schema_version: "1",
              command_id: newCommandId(),
              workspace_id: snapshot.workspace_id,
              command_type: "turn.pause.request",
              payload: {
                turn_id: activeTurn.turn_id,
                expected_turn_revision: activeTurn.turn_revision,
                reason: "user_requested_from_browser",
              },
              preconditions: {},
              client_context: { interaction_id: activeTurn.turn_id },
            };
            void coordinator.submit(envelope).then(onSettled);
          }}
        >
          在安全边界暂停
        </button>
      )}
      {latestCommand !== undefined && (
        <div className={`command-state ${latestCommand.kind}`}>
          {latestCommand.kind === "submitting" && "正在提交命令…"}
          {latestCommand.kind === "delivery_unknown" && (
            <>
              命令送达状态未知
              <button
                type="button"
                onClick={() => void coordinator.retry(latestCommand.envelope.command_id)}
              >
                使用同一 command_id 重试
              </button>
            </>
          )}
          {latestCommand.kind === "accepted" && `已接纳 · R${latestCommand.receipt.commit_revision}`}
          {latestCommand.kind === "rejected" && latestCommand.error.error.code}
        </div>
      )}
    </section>
  );
}

function SettingsView({ workspaceId }: { readonly workspaceId: string }) {
  const secret = useRef<HTMLInputElement>(null);
  const [providerKind, setProviderKind] = useState("deepseek");
  const [endpoint, setEndpoint] = useState("https://api.deepseek.com/chat/completions");
  const [modelName, setModelName] = useState("deepseek-chat");
  const [reasoningEffort, setReasoningEffort] = useState<"none" | "low" | "medium" | "high">("medium");
  const [permissionMode, setPermissionMode] = useState<"workspace_only" | "full_access">(
    "workspace_only",
  );
  const [configuration, setConfiguration] = useState<WorkspaceModelConfigurationResponse>();
  const [profiles, setProfiles] = useState<ProviderProfileIndexResponse>();
  const [selectedProfileId, setSelectedProfileId] = useState("");
  const [status, setStatus] = useState<string>();

  useEffect(() => {
    const controller = new AbortController();
    void getWorkspaceModelConfiguration("", workspaceId, controller.signal).then((result) => {
      if (result.ok) {
        setConfiguration(result.data);
        setSelectedProfileId(result.data.provider_profile_id);
        setReasoningEffort(result.data.reasoning_effort);
        setPermissionMode(result.data.permission_mode);
      }
    });
    void getProviderProfiles("", controller.signal).then((result) => {
      if (result.ok) {
        setProfiles(result.data);
        const firstEnabled = result.data.items.find((item) => item.status === "enabled");
        if (firstEnabled !== undefined) {
          setSelectedProfileId((current) => current || firstEnabled.provider_profile_id);
        }
      } else {
        setStatus(result.error.error.message);
      }
    });
    return () => controller.abort();
  }, [workspaceId]);

  const confirmPermission = () =>
    permissionMode !== "full_access" ||
    window.confirm("完全访问允许 Agent 使用工作区外的已注册工具能力。确认用于当前工作区？");

  const bindProfile = async (providerProfileId: string) => {
    if (!confirmPermission()) return;
    setStatus("正在绑定模型配置…");
    const selected = await putWorkspaceModelConfiguration("", workspaceId, {
      provider_profile_id: providerProfileId,
      model_name: modelName,
      reasoning_effort: reasoningEffort,
      permission_mode: permissionMode,
    });
    if (selected.ok) {
      setConfiguration(selected.data);
      setSelectedProfileId(selected.data.provider_profile_id);
      setStatus("模型配置已启用");
    } else {
      setStatus(selected.error.error.message);
    }
  };

  return (
    <section className="settings-view">
      <div className="eyebrow">Workspace Model</div>
      <h2>模型与凭据</h2>
      <p>API Key 只写入 Windows Credential Manager；工作区账本只保存版本化引用。</p>
      {configuration !== undefined && (
        <div className="settings-current">
          <strong>{configuration.model_name}</strong>
          <span>{configuration.provider_kind} · revision {configuration.configuration_revision}</span>
        </div>
      )}
      {configuration === undefined && <div className="settings-warning">当前工作区尚未配置模型。</div>}
      {profiles !== undefined && profiles.items.some((item) => item.status === "enabled") && (
        <form
          className="settings-form saved-profile-form"
          onSubmit={(event) => {
            event.preventDefault();
            if (selectedProfileId !== "") void bindProfile(selectedProfileId);
          }}
        >
          <h3>使用已安全保存的 Provider</h3>
          <label>
            Provider Profile
            <select
              value={selectedProfileId}
              onChange={(event) => setSelectedProfileId(event.target.value)}
            >
              {profiles.items.filter((item) => item.status === "enabled").map((item) => (
                <option key={item.provider_profile_id} value={item.provider_profile_id}>
                  {item.account_label ?? item.provider_kind} · {item.provider_kind}
                </option>
              ))}
            </select>
          </label>
          <label>Model<input value={modelName} onChange={(event) => setModelName(event.target.value)} /></label>
          <label>
            推理强度
            <select value={reasoningEffort} onChange={(event) => setReasoningEffort(event.target.value as typeof reasoningEffort)}>
              {(["none", "low", "medium", "high"] as const).map((item) => <option key={item}>{item}</option>)}
            </select>
          </label>
          <label>
            执行权限
            <select value={permissionMode} onChange={(event) => setPermissionMode(event.target.value as typeof permissionMode)}>
              <option value="workspace_only">工作区权限</option>
              <option value="full_access">完全访问</option>
            </select>
          </label>
          <button type="submit">用于当前工作区</button>
        </form>
      )}
      <form
        className="settings-form"
        onSubmit={(event) => {
          event.preventDefault();
          const value = secret.current?.value ?? "";
          if (value === "") {
            setStatus("请输入 API Key，或选择上方已保存的 Provider。");
            return;
          }
          setStatus("正在安全保存并绑定模型…");
          void createProviderCredential("", {
            provider_kind: providerKind,
            endpoint,
            account_label: `${workspaceId} default`,
            secret: value,
          }).then(async (created) => {
            if (secret.current !== null) secret.current.value = "";
            if (!created.ok) {
              setStatus(created.error.error.message);
              return;
            }
            await bindProfile(created.data.provider_profile_id);
          });
        }}
      >
        <h3>新增 Provider 凭据</h3>
        <label>Provider<input value={providerKind} onChange={(event) => setProviderKind(event.target.value)} /></label>
        <label>Endpoint<input value={endpoint} onChange={(event) => setEndpoint(event.target.value)} /></label>
        <label>Model<input value={modelName} onChange={(event) => setModelName(event.target.value)} /></label>
        <label>
          推理强度
          <select value={reasoningEffort} onChange={(event) => setReasoningEffort(event.target.value as typeof reasoningEffort)}>
            {(["none", "low", "medium", "high"] as const).map((item) => <option key={item}>{item}</option>)}
          </select>
        </label>
        <label>
          执行权限
          <select
            value={permissionMode}
            onChange={(event) => setPermissionMode(event.target.value as typeof permissionMode)}
          >
            <option value="workspace_only">工作区权限</option>
            <option value="full_access">完全访问</option>
          </select>
        </label>
        <label>API Key<input ref={secret} type="password" autoComplete="new-password" /></label>
        <button type="submit">保存并用于当前工作区</button>
      </form>
      {status !== undefined && <div className="command-state accepted" role="status" aria-live="polite">{status}</div>}
    </section>
  );
}

function MemoryView({
  workspaceId,
  conversationId,
  queryRevision,
}: {
  readonly workspaceId: string;
  readonly conversationId: string | undefined;
  readonly queryRevision: number;
}) {
  const [index, setIndex] = useState<MemoryIndexResponse>();
  const [policy, setPolicy] = useState<ConversationMemoryPolicyResponse>();
  const [skills, setSkills] = useState<SkillEvolutionIndexResponse>();
  const [status, setStatus] = useState<string>();

  const refresh = async (signal?: AbortSignal) => {
    const memoryResult = await getMemoryIndex("", workspaceId, signal);
    if (memoryResult.ok) setIndex(memoryResult.data);
    const skillResult = await getSkillEvolutionCandidates("", workspaceId, signal);
    if (skillResult.ok) setSkills(skillResult.data);
    if (conversationId !== undefined) {
      const policyResult = await getConversationMemoryPolicy(
        "",
        workspaceId,
        conversationId,
        signal,
      );
      if (policyResult.ok) setPolicy(policyResult.data);
    }
  };

  useEffect(() => {
    const controller = new AbortController();
    void refresh(controller.signal);
    return () => controller.abort();
  }, [workspaceId, conversationId, queryRevision]);

  const updatePolicy = (nextUse: boolean, nextContribute: boolean) => {
    if (conversationId === undefined || policy === undefined) return;
    setStatus("正在更新当前对话的记忆权限…");
    void putConversationMemoryPolicy("", workspaceId, conversationId, {
      command_id: newCommandId(),
      use_memory: nextUse,
      contribute_memory: nextContribute,
      expected_policy_revision: policy.policy_revision,
    }).then(async (result) => {
      if (result.ok) {
        setPolicy(result.data);
        setStatus("记忆权限已更新");
        await refresh();
      } else {
        setStatus(result.error.error.message);
      }
    });
  };

  return (
    <section className="memory-view">
      <header className="memory-header">
        <div>
          <div className="eyebrow">Project Memory</div>
          <h2>研究记忆</h2>
          <p>仅用于延续项目上下文；不是 Evidence，也不会替代当前研究状态。</p>
        </div>
        {policy !== undefined && (
          <div className="memory-policy">
            <label>
              <input
                checked={policy.use_memory}
                onChange={(event) => updatePolicy(event.target.checked, policy.contribute_memory)}
                type="checkbox"
              />
              本对话读取记忆
            </label>
            <label>
              <input
                checked={policy.contribute_memory}
                onChange={(event) => updatePolicy(policy.use_memory, event.target.checked)}
                type="checkbox"
              />
              本对话贡献记忆
            </label>
          </div>
        )}
      </header>
      {status !== undefined && <div className="command-state accepted">{status}</div>}
      {index?.summaries.map((summary) => (
        <details className="memory-index" key={`${summary.scope_kind}:${summary.scope_object_id}`}>
          <summary>
            {summary.scope_kind === "workspace" ? "工作区摘要" : "研究分支摘要"}
            <small>R{summary.source_revision} · projection {summary.projection_revision}</small>
          </summary>
          <pre>{summary.summary_text}</pre>
        </details>
      ))}
      <div className="memory-grid">
        {index?.items.map((item) => (
          <article className={`memory-card ${item.lifecycle}`} key={item.memory_item_id}>
            <div className="memory-card-meta">
              <span>{item.kind.replaceAll("_", " ")}</span>
              <span>{item.lifecycle}</span>
              <span>{item.scope_kind}</span>
            </div>
            <h3>{item.title}</h3>
            <p>{item.content}</p>
            <small>
              {item.origin} · pointer {item.pointer_revision} · revision {item.updated_revision}
              {` · recalled ${item.recall_count}`}
            </small>
            {item.quality_flags.length > 0 && (
              <div className="memory-quality-flags">
                {item.quality_flags.map((flag) => <span key={flag}>{flag.replaceAll("_", " ")}</span>)}
              </div>
            )}
            <div className="memory-sources">
              {item.sources.map((source) => (
                <code key={`${source.object_type}:${source.object_id}:${source.role}`}>
                  {source.role} · {source.object_id}@{source.object_revision}
                </code>
              ))}
            </div>
            <div className="memory-actions">
              {item.lifecycle !== "retracted" && (
                <button
                  type="button"
                  onClick={() => {
                    const title = window.prompt("修正记忆标题", item.title);
                    if (title === null) return;
                    const content = window.prompt("修正记忆内容", item.content);
                    if (content === null) return;
                    setStatus("正在保存新的记忆版本…");
                    void reviseMemory("", workspaceId, item.memory_item_id, {
                      command_id: newCommandId(),
                      expected_pointer_revision: item.pointer_revision,
                      title,
                      content,
                      lifecycle: "active",
                    }).then(async (result) => {
                      setStatus(result.ok ? "新记忆版本已保存" : result.error.error.message);
                      await refresh();
                    });
                  }}
                >
                  修正
                </button>
              )}
              {item.lifecycle === "proposed" && (
                <button
                  type="button"
                  onClick={() => {
                    setStatus("正在确认记忆…");
                    void activateMemory("", workspaceId, item.memory_item_id, {
                      command_id: newCommandId(),
                      memory_revision_id: item.memory_revision_id,
                      expected_pointer_revision: item.pointer_revision,
                    }).then(async (result) => {
                      setStatus(result.ok ? "记忆已确认" : result.error.error.message);
                      await refresh();
                    });
                  }}
                >
                  确认采用
                </button>
              )}
              {item.lifecycle === "active" && (
                <button
                  type="button"
                  onClick={() => {
                    setStatus("正在停用记忆…");
                    void retractMemory("", workspaceId, item.memory_item_id, {
                      command_id: newCommandId(),
                      expected_pointer_revision: item.pointer_revision,
                      reason: "user_retracted_from_memory_view",
                    }).then(async (result) => {
                      setStatus(result.ok ? "记忆已停用" : result.error.error.message);
                      await refresh();
                    });
                  }}
                >
                  停用
                </button>
              )}
            </div>
          </article>
        ))}
      </div>
      {index?.items.length === 0 && (
        <div className="empty-state compact"><h2>还没有项目记忆</h2></div>
      )}
      <section className="skill-evolution-section">
        <div className="eyebrow">Controlled self-evolution</div>
        <h2>已采用 Skill</h2>
        <p>这里展示当前版本和历史版本。反馈只是关联观察，不会自动改写或替换 Skill。</p>
        <div className="memory-grid">
          {(skills?.adoptions ?? []).map((adoption) => (
            <article className={`memory-card ${adoption.lifecycle}`} key={adoption.skill_name}>
              <div className="memory-card-meta">
                <span>{adoption.skill_name}</span>
                <span>{adoption.lifecycle}</span>
                <span>pointer {adoption.pointer_revision}</span>
              </div>
              <h3>
                {adoption.current_skill_version_id === null
                  ? "当前未启用"
                  : `当前 ${adoption.current_skill_version_id}`}
              </h3>
              <div className="memory-sources">
                {adoption.versions.map((version) => (
                  <div key={version.skill_version_id}>
                    <code>
                      {version.version_label} · {version.skill_version_id}
                      {` · feedback observations ${version.outcome_observation_count}`}
                    </code>
                    {(adoption.lifecycle === "deactivated"
                      || adoption.current_skill_version_id !== version.skill_version_id) && (
                      <button
                        type="button"
                        onClick={() => {
                          const reason = window.prompt("回退原因", "恢复较早且更合适的 Skill 版本");
                          if (reason === null) return;
                          setStatus("正在回退 Skill 版本…");
                          void rollbackWorkspaceSkill("", workspaceId, adoption.skill_name, {
                            command_id: newCommandId(),
                            target_skill_version_id: version.skill_version_id,
                            expected_pointer_revision: adoption.pointer_revision,
                            reason,
                          }).then(async (result) => {
                            setStatus(result.ok ? "Skill 已回退" : result.error.error.message);
                            await refresh();
                          });
                        }}
                      >
                        {adoption.lifecycle === "deactivated" ? "重新启用此版本" : "回退到此版本"}
                      </button>
                    )}
                  </div>
                ))}
              </div>
              {adoption.lifecycle === "active" && (
                <div className="memory-actions">
                  <button
                    type="button"
                    onClick={() => {
                      setStatus("正在独立评估当前 Skill 版本…");
                      void evaluateWorkspaceSkill("", workspaceId, adoption.skill_name, {
                        command_id: newCommandId(),
                        candidate_skill_version_id: adoption.current_skill_version_id ?? null,
                        baseline_skill_version_id: null,
                      }).then(async (result) => {
                        setStatus(result.ok ? "独立 Skill 评估已记录" : result.error.error.message);
                        await refresh();
                      });
                    }}
                  >
                    独立评估
                  </button>
                  <button
                    type="button"
                    onClick={() => {
                      const reason = window.prompt("停用原因", "暂时停用该 Skill");
                      if (reason === null) return;
                      setStatus("正在停用 Skill…");
                      void deactivateWorkspaceSkill("", workspaceId, adoption.skill_name, {
                        command_id: newCommandId(),
                        expected_pointer_revision: adoption.pointer_revision,
                        reason,
                      }).then(async (result) => {
                        setStatus(result.ok ? "Skill 已停用" : result.error.error.message);
                        await refresh();
                      });
                    }}
                  >
                    停用
                  </button>
                </div>
              )}
            </article>
          ))}
        </div>
        {((skills?.evaluations ?? []).length > 0) && <>
          <h2>Skill 评估记录</h2>
          <p>评估只生成建议。使用结果与反馈是相关观察，不是对 Skill 的因果归因。</p>
          <div className="memory-grid">
            {(skills?.evaluations ?? []).map((evaluation) => (
              <article className="memory-card" key={evaluation.run_id}>
                <div className="memory-card-meta">
                  <span>{evaluation.skill_name}</span>
                  <span>{evaluation.status}</span>
                  <span>{evaluation.verdict ?? "等待结果"}</span>
                </div>
                <h3>{evaluation.candidate_skill_version_id}</h3>
                {evaluation.baseline_skill_version_id !== null && (
                  <small>对照 {evaluation.baseline_skill_version_id}</small>
                )}
                <p>{evaluation.rationale ?? "评估尚未完成。"}</p>
                <small>
                  候选：{evaluation.candidate_use_turn_count} 次 Turn 使用 / {evaluation.candidate_feedback_count} 条反馈
                  {evaluation.baseline_use_turn_count === null ? "" : ` · 对照：${evaluation.baseline_use_turn_count} 次使用 / ${evaluation.baseline_feedback_count ?? 0} 条反馈`}
                </small>
                {(evaluation.limitations ?? []).length > 0 && (
                  <details>
                    <summary>证据限制</summary>
                    <ul>{(evaluation.limitations ?? []).map((item) => <li key={item}>{item}</li>)}</ul>
                  </details>
                )}
                {(evaluation.proposals ?? []).map((proposal) => {
                  const existingChange = (skills?.change_candidates ?? []).find(
                    (candidate) => candidate.source_proposal_id === proposal.proposal_id,
                  );
                  const adoption = (skills?.adoptions ?? []).find(
                    (item) => item.skill_name === evaluation.skill_name,
                  );
                  return <details key={proposal.proposal_id}>
                    <summary>{proposal.kind} · {proposal.title}</summary>
                    <p>{proposal.rationale}</p>
                    {proposal.suggested_instruction_body !== null && (
                      <pre>{proposal.suggested_instruction_body}</pre>
                    )}
                    <small>建议本身不会修改 Skill；生成候选和正式发布是两个独立的人审动作。</small>
                    <div className="memory-actions">
                      {(proposal.kind === "revise" || proposal.kind === "merge") && (
                        <button
                          type="button"
                          disabled={existingChange !== undefined}
                          onClick={() => {
                            setStatus("正在生成可审核的 Skill 版本候选…");
                            void materializeSkillChange("", workspaceId, proposal.proposal_id, {
                              command_id: newCommandId(),
                            }).then(async (result) => {
                              setStatus(result.ok ? "Skill 版本候选已生成，尚未发布" : result.error.error.message);
                              await refresh();
                            });
                          }}
                        >
                          {existingChange === undefined ? "生成版本候选" : "已生成候选"}
                        </button>
                      )}
                      {proposal.kind === "retire" && adoption?.lifecycle === "active" && (
                        <button
                          type="button"
                          onClick={() => {
                            const reason = window.prompt("停用原因", proposal.rationale);
                            if (reason === null) return;
                            setStatus("正在停用 Skill…");
                            void deactivateWorkspaceSkill("", workspaceId, adoption.skill_name, {
                              command_id: newCommandId(),
                              expected_pointer_revision: adoption.pointer_revision,
                              reason,
                            }).then(async (result) => {
                              setStatus(result.ok ? "Skill 已停用" : result.error.error.message);
                              await refresh();
                            });
                          }}
                        >
                          确认停用
                        </button>
                      )}
                    </div>
                  </details>;
                })}
              </article>
            ))}
          </div>
        </>}
        {((skills?.change_candidates ?? []).length > 0) && <>
          <h2>评估建议生成的版本候选</h2>
          <p>候选内容已经固定并通过确定性检查，但当前 Skill 仍未改变。请审阅全文后单独发布或拒绝。</p>
          <div className="memory-grid">
            {(skills?.change_candidates ?? []).map((candidate) => (
              <article className={`memory-card ${candidate.lifecycle}`} key={candidate.candidate_id}>
                <div className="memory-card-meta">
                  <span>{candidate.change_kind} · {candidate.skill_name}@{candidate.proposed_version}</span>
                  <span>{candidate.lifecycle}</span>
                  <span>{candidate.validation_status}</span>
                </div>
                <h3>{candidate.description}</h3>
                <p>{candidate.rationale}</p>
                <details>
                  <summary>审阅完整 Skill 指导</summary>
                  <pre>{candidate.instruction_body}</pre>
                </details>
                <small>基于 {candidate.base_skill_version_id}</small>
                {candidate.merge_source_skill_version_id !== null && (
                  <small> · 合并来源 {candidate.merge_source_skill_version_id}</small>
                )}
                {candidate.validation_findings.length > 0 && (
                  <div className="memory-quality-flags">
                    {candidate.validation_findings.map((finding) => <span key={finding}>{finding}</span>)}
                  </div>
                )}
                {candidate.lifecycle === "proposed" && (
                  <div className="memory-actions">
                    <button
                      type="button"
                      disabled={candidate.validation_status !== "passed"}
                      onClick={() => {
                        setStatus("正在发布并采用 Skill 新版本…");
                        void activateSkillChange("", workspaceId, candidate.candidate_id, {
                          command_id: newCommandId(),
                          expected_pointer_revision: candidate.pointer_revision,
                        }).then(async (result) => {
                          setStatus(result.ok ? "Skill 新版本已发布并采用" : result.error.error.message);
                          await refresh();
                        });
                      }}
                    >
                      审核通过并发布
                    </button>
                    <button
                      type="button"
                      onClick={() => {
                        const reason = window.prompt("拒绝原因", "该版本建议不适合采用");
                        if (reason === null) return;
                        void rejectSkillChange("", workspaceId, candidate.candidate_id, {
                          command_id: newCommandId(),
                          expected_pointer_revision: candidate.pointer_revision,
                          reason,
                        }).then(async (result) => {
                          setStatus(result.ok ? "Skill 版本候选已拒绝" : result.error.error.message);
                          await refresh();
                        });
                      }}
                    >
                      拒绝
                    </button>
                  </div>
                )}
                {candidate.activated_skill_version_id !== null && (
                  <code>已发布 {candidate.activated_skill_version_id}</code>
                )}
              </article>
            ))}
          </div>
        </>}
        <h2>Skill 候选</h2>
        <p>Agent 可以从多条稳定记忆中提出可复用规则，但只有你确认后才会安装。</p>
        <div className="memory-grid">
          {skills?.items.map((candidate) => (
            <article className={`memory-card ${candidate.lifecycle}`} key={candidate.candidate_id}>
              <div className="memory-card-meta">
                <span>{candidate.skill_name}@{candidate.proposed_version}</span>
                <span>{candidate.lifecycle}</span>
                <span>{candidate.validation_status}</span>
              </div>
              <h3>{candidate.description}</h3>
              <p>{candidate.rationale}</p>
              <details>
                <summary>查看将要安装的指导</summary>
                <pre>{candidate.instruction_body}</pre>
              </details>
              <small>来源：{candidate.source_memory_item_ids.join(" · ")}</small>
              {candidate.validation_findings.length > 0 && (
                <div className="memory-quality-flags">
                  {candidate.validation_findings.map((finding) => <span key={finding}>{finding}</span>)}
                </div>
              )}
              {candidate.relative_skill_path !== null && <code>{candidate.relative_skill_path}</code>}
              {(candidate.lifecycle === "proposed" || candidate.lifecycle === "approved") && (
                <div className="memory-actions">
                  <button
                    type="button"
                    disabled={candidate.validation_status !== "passed"}
                    onClick={() => {
                      setStatus("正在版本化安装 Skill…");
                      void activateSkillEvolutionCandidate("", workspaceId, candidate.candidate_id, {
                        command_id: newCommandId(),
                        expected_pointer_revision: candidate.pointer_revision,
                      }).then(async (result) => {
                        setStatus(result.ok ? "Skill 已安装并激活" : result.error.error.message);
                        await refresh();
                      });
                    }}
                  >
                    {candidate.lifecycle === "approved" ? "继续安装" : "审核通过并安装"}
                  </button>
                  {candidate.lifecycle === "proposed" && <button
                    type="button"
                    onClick={() => {
                      const reason = window.prompt("拒绝原因", "不适合作为长期 Skill");
                      if (reason === null) return;
                      void rejectSkillEvolutionCandidate("", workspaceId, candidate.candidate_id, {
                        command_id: newCommandId(),
                        expected_pointer_revision: candidate.pointer_revision,
                        reason,
                      }).then(async (result) => {
                        setStatus(result.ok ? "Skill 候选已拒绝" : result.error.error.message);
                        await refresh();
                      });
                    }}
                  >
                    拒绝
                  </button>}
                </div>
              )}
            </article>
          ))}
        </div>
        {skills?.items.length === 0 && <p className="empty-copy">尚无稳定模式形成 Skill 候选。</p>}
      </section>
    </section>
  );
}

function KnowledgeView({
  workspaceId,
  queryRevision,
}: {
  readonly workspaceId: string;
  readonly queryRevision: number;
}) {
  const [index, setIndex] = useState<KnowledgeIndexResponse>();
  const [error, setError] = useState<string>();

  const refresh = async (signal?: AbortSignal) => {
    const result = await getKnowledgeIndex("", workspaceId, signal);
    if (result.ok) {
      setIndex(result.data);
      setError(undefined);
    } else {
      setError(result.error.error.message);
    }
  };

  useEffect(() => {
    const controller = new AbortController();
    void refresh(controller.signal);
    return () => controller.abort();
  }, [workspaceId, queryRevision]);

  return (
    <section className="memory-view">
      <header className="memory-header">
        <div>
          <div className="eyebrow">Workspace Literature RAG</div>
          <h2>研究资料</h2>
          <p>
            将 PDF、DOCX、Markdown 或文本放入 <code>literature/</code>。Agent Turn
            开始时会增量索引，并可按当前问题主动检索。
          </p>
        </div>
        <button type="button" onClick={() => void refresh()}>刷新目录</button>
      </header>
      <div className="knowledge-boundary">
        检索意图只是上下文提示，不会替你选择研究方法；资料片段也不是 Stata 数值 Evidence。
        被模型实际使用的片段会保留文件 hash、页码、revision 和 chunk 身份。
      </div>
      {index?.last_index_policy_revision !== null && index?.last_index_policy_revision !== undefined && (
        <div className="memory-card-meta">
          <span>ingestion {index.last_index_policy_revision}</span>
          <span>revision {index.authoritative_revision}</span>
        </div>
      )}
      {error !== undefined && <div className="command-state rejected">{error}</div>}
      <div className="memory-grid">
        {index?.documents.map((document) => (
          <article className={`memory-card ${document.availability}`} key={document.relative_path}>
            <div className="memory-card-meta">
              <span>{document.availability.replaceAll("_", " ")}</span>
              {document.page_count !== null && <span>{document.page_count} 页</span>}
              {document.parser_profile !== null && <span>{document.parser_profile}</span>}
            </div>
            <h3>{document.relative_path}</h3>
            <code title={document.content_sha256}>{document.content_sha256.slice(0, 16)}…</code>
            <p>
              {document.structured_node_count ?? 0} 个结构节点
              {(document.enrichment_finding_count ?? 0) > 0
                ? ` · ${document.enrichment_finding_count ?? 0} 个疑难页诊断`
                : " · 无疑难页升级"}
            </p>
            {document.canonical_ir_version !== null && (
              <small>{document.canonical_ir_version}</small>
            )}
          </article>
        ))}
      </div>
      {index?.documents.length === 0 && (
        <div className="empty-state compact">
          <h2>literature/ 中还没有已索引资料</h2>
          <p>加入资料后发起下一轮对话，Agent 会在推理前完成增量扫描。</p>
        </div>
      )}
    </section>
  );
}

export default function App() {
  const shell = useMemo(() => new WorkspaceShell(""), []);
  const coordinator = useMemo(() => new PendingCommandCoordinator(""), []);
  const attentionStream = useMemo(() => new GlobalAttentionStream(""), []);
  const [state, setState] = useState<ClientSyncState>(shell.state);
  const [commandStates, setCommandStates] = useState<ReadonlyMap<string, PendingCommandState>>(
    new Map(),
  );
  const [attention, setAttention] = useState<GlobalAttentionResponse | undefined>();
  const [dataCatalog, setDataCatalog] = useState<WorkspaceDataCatalogResponse | undefined>();
  const [dataRefresh, setDataRefresh] = useState(0);
  const [workspaceInput, setWorkspaceInput] = useState(initialWorkspace);
  const [activeView, setActiveView] = useState<ViewName>("conversation");
  const [selectedConversationId, setSelectedConversationId] = useState<string | undefined>();
  const [newConversationMode, setNewConversationMode] = useState(false);
  const [selectedResearchPathId, setSelectedResearchPathId] = useState<string | undefined>();
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const snapshot =
    state.kind === "ready" || state.kind === "reconnecting" ? state.snapshot : undefined;

  const openWorkspace = (workspaceId: string) => {
    const next = new URL(window.location.href);
    next.searchParams.set("workspace", workspaceId);
    window.history.replaceState({}, "", next);
    setWorkspaceInput(workspaceId);
    setSelectedConversationId(undefined);
    setSelectedResearchPathId(undefined);
    setNewConversationMode(false);
    void shell.openWorkspace(workspaceId);
  };

  const createWorkspace = async () => {
    const candidate = workspaceInput.trim();
    const workspaceId = candidate.startsWith("ws_") ? candidate : `ws_${candidate}`;
    if (!/^ws_[A-Za-z0-9._-]+$/.test(workspaceId)) return;
    const envelope: WorkspaceCreateEnvelope = {
      schema_version: "1",
      command_id: newCommandId(),
      workspace_id: workspaceId,
      command_type: "workspace.create",
      payload: {},
      preconditions: {},
      client_context: { interaction_id: workspaceId },
    };
    await coordinator.submit(envelope);
    openWorkspace(workspaceId);
    setSidebarOpen(false);
  };

  useEffect(() => shell.subscribe(setState), [shell]);
  useEffect(() => coordinator.subscribe(setCommandStates), [coordinator]);
  useEffect(() => {
    void shell.openWorkspace(initialWorkspace());
  }, [shell]);
  useEffect(() => {
    let active = true;
    let generation = 0;
    const refresh = () => {
      const requestGeneration = ++generation;
      void getWorkspaceAttention("").then((result) => {
        if (active && requestGeneration === generation && result.ok) {
          setAttention(result.data);
        }
      });
    };
    refresh();
    attentionStream.open(refresh);
    return () => {
      active = false;
      attentionStream.close();
    };
  }, [attentionStream]);
  useEffect(() => {
    if (snapshot === undefined) {
      setDataCatalog(undefined);
      return;
    }
    const controller = new AbortController();
    void getWorkspaceDataCatalog("", snapshot.workspace_id, controller.signal).then((result) => {
      if (result.ok) setDataCatalog(result.data);
    });
    return () => controller.abort();
  }, [snapshot?.workspace_id, dataRefresh]);

  const projectionLag = snapshot?.data.projection_watermarks.some(
    (watermark) => watermark.projection_lag > 0,
  );
  const validSelectedConversationId = snapshot?.data.conversations.some(
    (item) => item.conversation_id === selectedConversationId,
  )
    ? selectedConversationId
    : undefined;
  const validSelectedResearchPathId = snapshot?.data.research_paths.some(
    (item) => item.research_path_id === selectedResearchPathId,
  )
    ? selectedResearchPathId
    : undefined;
  const activeConversationId = newConversationMode
    ? undefined
    : validSelectedConversationId ?? snapshot?.data.active_conversation_id ?? undefined;
  const activeResearchPathId =
    validSelectedResearchPathId ?? snapshot?.data.research_paths[0]?.research_path_id;

  return (
    <main className="app-shell">
      <aside className={sidebarOpen ? "sidebar open" : "sidebar"}>
        <div className="brand">
          <div className="brand-mark">S</div>
          <div>
            <strong>Stata Agent</strong>
            <span>Research Workspace</span>
          </div>
          <button
            aria-expanded={sidebarOpen}
            className="mobile-sidebar-toggle"
            onClick={() => setSidebarOpen((current) => !current)}
            type="button"
          >
            {sidebarOpen ? "收起" : "工作区"}
          </button>
        </div>
        <div className="sidebar-body">
          <form
            className="workspace-picker"
            onSubmit={(event) => {
              event.preventDefault();
              openWorkspace(workspaceInput);
              setSidebarOpen(false);
            }}
          >
            <label htmlFor="workspace">工作区</label>
            <div className="workspace-input-row">
              <input
                id="workspace"
                value={workspaceInput}
                onChange={(event) => setWorkspaceInput(event.target.value)}
                spellCheck={false}
              />
              <button type="submit">打开</button>
              <button type="button" onClick={() => void createWorkspace()}>新建</button>
            </div>
          </form>
          <nav className="conversation-list" aria-label="Conversations">
            <div className="section-heading">
              <div className="section-label">对话</div>
              <button
                type="button"
                onClick={() => {
                  setSelectedConversationId(undefined);
                  setNewConversationMode(true);
                  setActiveView("conversation");
                  setSidebarOpen(false);
                }}
              >
                新对话
              </button>
            </div>
            {snapshot?.data.conversations.map((conversation) => (
              <button
                className={
                  !newConversationMode && conversation.conversation_id === activeConversationId
                    ? "conversation-link active"
                    : "conversation-link"
                }
                key={conversation.conversation_id}
                onClick={() => {
                  setSelectedConversationId(conversation.conversation_id);
                  setNewConversationMode(false);
                  setSidebarOpen(false);
                }}
                type="button"
              >
                <span>{conversation.latest_message_preview ?? "新研究对话"}</span>
                <small>{conversation.conversation_id}</small>
              </button>
            ))}
          </nav>
          <div className="data-list">
            <div className="section-heading">
              <div className="section-label">数据</div>
              <button type="button" onClick={() => setDataRefresh((value) => value + 1)}>
                刷新
              </button>
            </div>
            {dataCatalog?.items.length === 0 && <small>当前工作区没有 .dta</small>}
            {dataCatalog?.items.map((item) => (
              <div className="data-file" key={item.relative_path} title={item.relative_path}>
                <span>{item.display_name}</span>
                <small>{fileSize(item.size_bytes)}</small>
              </div>
            ))}
          </div>
          <div className="attention-list">
            <div className="section-label">工作区动态</div>
            {attention?.workspaces.map((workspace) => (
              <button
                className="attention-link"
                key={workspace.workspace_id}
                onClick={() => {
                  setWorkspaceInput(workspace.workspace_id);
                  openWorkspace(workspace.workspace_id);
                  setSidebarOpen(false);
                }}
                type="button"
              >
                <span className={`attention-dot ${workspace.highest_severity.toLowerCase()}`} />
                <span>{workspace.workspace_id}</span>
                <small>
                  {workspace.requires_action
                    ? `${workspace.attention_refs.length} 项需要处理`
                    : workspace.active_write_turn_status !== null
                      ? `Agent ${workspace.active_write_turn_status}`
                      : `${workspace.queued_write_count} 排队`}
                </small>
              </button>
            ))}
          </div>
          <div className="sidebar-footer">
            <span className={`sync-dot ${state.kind}`} />
            {statusText(state)}
          </div>
        </div>
      </aside>

      <section className="workspace-panel">
        <header className="workspace-header">
          <div>
            <div className="eyebrow">{snapshot?.workspace_id ?? workspaceInput}</div>
            <h1>研究工作区</h1>
          </div>
          <div className="revision-cluster">
            {projectionLag === true && <span className="lag-badge">Projection 正在追赶</span>}
            <span>R{snapshot?.authoritative_revision ?? "—"}</span>
          </div>
        </header>
        <nav className="view-tabs" aria-label="Workspace views">
          {VIEWS.map((view) => (
            <button
              className={activeView === view.id ? "view-tab active" : "view-tab"}
              key={view.id}
              onClick={() => setActiveView(view.id)}
              type="button"
            >
              {view.label}
            </button>
          ))}
        </nav>
        {snapshot !== undefined && snapshot.data.research_paths.length > 0 && (
          <div className="path-picker" aria-label="Research paths">
            {snapshot.data.research_paths.map((path) => (
              <button
                className={activeResearchPathId === path.research_path_id ? "active" : ""}
                key={path.research_path_id}
                onClick={() => setSelectedResearchPathId(path.research_path_id)}
                type="button"
              >
                {path.canonical_key}
              </button>
            ))}
            <button
              disabled={snapshot.data.execution.active_write_turn_id !== null}
              onClick={() => {
                if (activeResearchPathId === undefined) return;
                const displayName = window.prompt("新研究方向名称", "新的研究方向");
                if (displayName === null || displayName.trim() === "") return;
                const reason = window.prompt("从当前节点分支的原因", "尝试另一种研究设定");
                if (reason === null || reason.trim() === "") return;
                const canonical = `branch.${Date.now().toString(36)}`;
                const envelope: ResearchPathBranchEnvelope = {
                  schema_version: "1",
                  command_id: newCommandId(),
                  workspace_id: snapshot.workspace_id,
                  command_type: "research_path.branch",
                  payload: {
                    source_research_path_id: activeResearchPathId,
                    conversation_id: activeConversationId ?? null,
                    canonical_key: canonical,
                    display_name: displayName.trim(),
                    branch_reason: reason.trim(),
                    expected_workspace_revision: snapshot.authoritative_revision,
                  },
                  preconditions: {},
                  client_context: { interaction_id: activeResearchPathId },
                };
                void coordinator.submit(envelope).then(() => shell.resync());
              }}
              title={
                snapshot.data.execution.active_write_turn_id === null
                  ? "从当前研究状态创建独立方向"
                  : "当前有写入 Turn，暂停或完成后可创建分支"
              }
              type="button"
            >
              ＋ 从此分支
            </button>
          </div>
        )}
        <div className="view-body">
          {state.kind === "bootstrapping" || state.kind === "resyncing" ? (
            <div className="empty-state"><h2>正在建立一致读视图…</h2></div>
          ) : state.kind === "unavailable" ? (
            <div className="error-state">
              <div className="eyebrow">{state.error.error.code}</div>
              <h2>无法打开 {state.workspaceId}</h2>
              <p>{state.error.error.message}</p>
              <button type="button" onClick={() => void shell.resync()}>重新读取</button>
              <button type="button" onClick={() => void createWorkspace()}>创建此工作区</button>
            </div>
          ) : snapshot === undefined ? (
            <div className="empty-state"><h2>选择一个工作区</h2></div>
          ) : activeView === "conversation" && activeConversationId !== undefined ? (
            <ConversationView snapshot={snapshot} conversationId={activeConversationId} />
          ) : activeView === "conversation" ? (
            <div className="empty-state compact"><h2>输入研究想法，开始第一个对话</h2></div>
          ) : activeView === "results" && activeResearchPathId !== undefined ? (
            <ResultsView
              workspaceId={snapshot.workspace_id}
              researchPathId={activeResearchPathId}
              queryRevision={snapshot.authoritative_revision}
            />
          ) : activeView === "trace" ? (
            <TraceView
              workspaceId={snapshot.workspace_id}
              queryRevision={snapshot.authoritative_revision}
            />
          ) : activeView === "word" && activeResearchPathId !== undefined ? (
            <WordView
              workspaceId={snapshot.workspace_id}
              researchPathId={activeResearchPathId}
              queryRevision={snapshot.authoritative_revision}
            />
          ) : activeView === "memory" ? (
            <MemoryView
              workspaceId={snapshot.workspace_id}
              conversationId={activeConversationId}
              queryRevision={snapshot.authoritative_revision}
            />
          ) : activeView === "knowledge" ? (
            <KnowledgeView
              workspaceId={snapshot.workspace_id}
              queryRevision={snapshot.authoritative_revision}
            />
          ) : activeView === "settings" ? (
            <SettingsView workspaceId={snapshot.workspace_id} />
          ) : (
            <div className="empty-state compact"><h2>当前视图没有可用的研究对象</h2></div>
          )}
          {snapshot !== undefined && activeView === "conversation" && (
            <WorkspaceControls
              snapshot={snapshot}
              conversationId={activeConversationId}
              researchPathId={activeResearchPathId}
              coordinator={coordinator}
              commandStates={commandStates}
              onSettled={() => shell.resync()}
              onConversationCreated={() => setNewConversationMode(false)}
            />
          )}
        </div>
      </section>
    </main>
  );
}
