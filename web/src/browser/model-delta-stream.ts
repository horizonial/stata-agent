import { modelDeltaEventStreamUrl } from "../generated/api-v1";
import { ReconnectingApiEventStream } from "./api-event-stream";

export type ModelResponseDelta = {
  readonly schema_version: "model-response-delta/v1";
  readonly authoritative: false;
  readonly workspace_id: string;
  readonly turn_id: string;
  readonly provider_attempt_id: string;
  readonly sequence: number;
  readonly channel: "provider_content";
  readonly content: string;
};

export type ModelDeltaStreamCallbacks = {
  readonly onOpen: () => void;
  readonly onDelta: (delta: ModelResponseDelta) => void;
  readonly onReconnecting: () => void;
};

function parseDelta(
  raw: string,
  workspaceId: string,
  turnId: string,
): ModelResponseDelta | undefined {
  let candidate: unknown;
  try {
    candidate = JSON.parse(raw);
  } catch {
    return undefined;
  }
  if (candidate === null || typeof candidate !== "object") return undefined;
  const value = candidate as Record<string, unknown>;
  if (
    value.schema_version !== "model-response-delta/v1"
    || value.authoritative !== false
    || value.workspace_id !== workspaceId
    || value.turn_id !== turnId
    || typeof value.provider_attempt_id !== "string"
    || !Number.isInteger(value.sequence)
    || (value.sequence as number) < 1
    || value.channel !== "provider_content"
    || typeof value.content !== "string"
  ) {
    return undefined;
  }
  return value as ModelResponseDelta;
}

/**
 * Consumes the ephemeral per-Turn stream. These deltas improve perceived latency
 * only; callers must continue to read committed messages from Workspace state.
 */
export class ModelDeltaStream {
  readonly #stream = new ReconnectingApiEventStream();
  readonly #seen = new Set<string>();

  open(
    workspaceId: string,
    turnId: string,
    callbacks: ModelDeltaStreamCallbacks,
  ): void {
    this.close();
    this.#stream.open(modelDeltaEventStreamUrl("", workspaceId, turnId), {
      onOpen: callbacks.onOpen,
      onReconnecting: callbacks.onReconnecting,
      onEvent: (event) => {
        if (event.event !== "model_delta") return;
        const delta = parseDelta(event.data, workspaceId, turnId);
        if (delta === undefined) return;
        const identity = `${delta.provider_attempt_id}:${delta.sequence}`;
        if (this.#seen.has(identity)) return;
        this.#seen.add(identity);
        callbacks.onDelta(delta);
      },
    });
  }

  close(): void {
    this.#stream.close();
    this.#seen.clear();
  }
}
