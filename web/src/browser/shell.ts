import {
  getWorkspaceBootstrap,
  type ErrorResponse,
  type WorkspaceBootstrapResponse,
} from "../generated/api-v1";
import { WorkspaceStream } from "./workspace-stream";

export type ClientSyncState =
  | { readonly kind: "cold" }
  | { readonly kind: "bootstrapping"; readonly workspaceId: string }
  | {
      readonly kind: "ready";
      readonly workspaceId: string;
      readonly snapshot: WorkspaceBootstrapResponse;
    }
  | {
      readonly kind: "reconnecting";
      readonly workspaceId: string;
      readonly snapshot: WorkspaceBootstrapResponse;
    }
  | { readonly kind: "resyncing"; readonly workspaceId: string }
  | {
      readonly kind: "unavailable";
      readonly workspaceId: string;
      readonly error: ErrorResponse;
    };

export type ShellListener = (state: ClientSyncState) => void;

export class WorkspaceShell {
  readonly #baseUrl: string;
  readonly #snapshots = new Map<string, WorkspaceBootstrapResponse>();
  readonly #listeners = new Set<ShellListener>();
  readonly #stream: WorkspaceStream;
  #requestGeneration = 0;
  #refreshGeneration = 0;
  #activeRequest: AbortController | undefined;
  #state: ClientSyncState = { kind: "cold" };

  constructor(baseUrl = "") {
    this.#baseUrl = baseUrl;
    this.#stream = new WorkspaceStream(baseUrl);
  }

  get state(): ClientSyncState {
    return this.#state;
  }

  subscribe(listener: ShellListener): () => void {
    this.#listeners.add(listener);
    listener(this.#state);
    return () => this.#listeners.delete(listener);
  }

  snapshotFor(workspaceId: string): WorkspaceBootstrapResponse | undefined {
    return this.#snapshots.get(workspaceId);
  }

  async openWorkspace(workspaceId: string): Promise<void> {
    const normalized = workspaceId.trim();
    if (!normalized.startsWith("ws_")) {
      throw new Error("Workspace identity must start with ws_");
    }
    this.#requestGeneration += 1;
    this.#refreshGeneration += 1;
    const generation = this.#requestGeneration;
    this.#activeRequest?.abort();
    this.#stream.close();
    const controller = new AbortController();
    this.#activeRequest = controller;
    this.#setState({ kind: "bootstrapping", workspaceId: normalized });

    const result = await getWorkspaceBootstrap(
      this.#baseUrl,
      normalized,
      controller.signal,
    );
    if (generation !== this.#requestGeneration) return;
    this.#activeRequest = undefined;
    if (!result.ok) {
      this.#setState({
        kind: "unavailable",
        workspaceId: normalized,
        error: result.error,
      });
      return;
    }

    // Server Query responses replace a Workspace partition. The browser never
    // patches this snapshot to manufacture a newer authoritative revision.
    this.#snapshots.set(normalized, result.data);
    this.#setState({
      kind: "ready",
      workspaceId: normalized,
      snapshot: result.data,
    });
    void this.#stream.open(normalized, result.data.durable_stream_cursor, {
      onDurable: () => void this.#refreshFromDurableNotification(normalized, generation),
      onOpen: () => {
        if (
          generation === this.#requestGeneration &&
          this.#state.kind === "reconnecting" &&
          this.#state.workspaceId === normalized
        ) {
          this.#setState({ kind: "ready", workspaceId: normalized, snapshot: this.#state.snapshot });
        }
      },
      onReconnecting: () => {
        if (generation !== this.#requestGeneration) return;
        this.markReconnecting();
      },
      onResyncRequired: () => {
        if (generation !== this.#requestGeneration) return;
        void this.resync();
      },
    });
  }

  markReconnecting(): void {
    if (this.#state.kind !== "ready") return;
    this.#setState({
      kind: "reconnecting",
      workspaceId: this.#state.workspaceId,
      snapshot: this.#state.snapshot,
    });
  }

  async resync(): Promise<void> {
    if (
      this.#state.kind !== "ready" &&
      this.#state.kind !== "reconnecting" &&
      this.#state.kind !== "unavailable"
    ) {
      return;
    }
    const workspaceId = this.#state.workspaceId;
    this.#setState({ kind: "resyncing", workspaceId });
    await this.openWorkspace(workspaceId);
  }

  async #refreshFromDurableNotification(
    workspaceId: string,
    generation: number,
  ): Promise<void> {
    const refreshGeneration = ++this.#refreshGeneration;
    const result = await getWorkspaceBootstrap(this.#baseUrl, workspaceId);
    if (
      generation !== this.#requestGeneration ||
      refreshGeneration !== this.#refreshGeneration ||
      !result.ok
    ) return;
    this.#snapshots.set(workspaceId, result.data);
    this.#setState({ kind: "ready", workspaceId, snapshot: result.data });
  }

  #setState(state: ClientSyncState): void {
    this.#state = state;
    for (const listener of this.#listeners) listener(state);
  }
}
