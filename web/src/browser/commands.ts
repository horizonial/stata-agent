import {
  submitCommand,
  type CommandEnvelope,
  type CommandReceiptResponse,
  type ErrorResponse,
} from "../generated/api-v1";

export type PendingCommandState =
  | { readonly kind: "submitting"; readonly envelope: CommandEnvelope }
  | { readonly kind: "delivery_unknown"; readonly envelope: CommandEnvelope }
  | {
      readonly kind: "accepted";
      readonly envelope: CommandEnvelope;
      readonly receipt: CommandReceiptResponse;
    }
  | {
      readonly kind: "rejected";
      readonly envelope: CommandEnvelope;
      readonly error: ErrorResponse;
    };

export type PendingCommandListener = (
  states: ReadonlyMap<string, PendingCommandState>,
) => void;

export class PendingCommandCoordinator {
  readonly #baseUrl: string;
  readonly #states = new Map<string, PendingCommandState>();
  readonly #listeners = new Set<PendingCommandListener>();

  constructor(baseUrl = "") {
    this.#baseUrl = baseUrl;
  }

  subscribe(listener: PendingCommandListener): () => void {
    this.#listeners.add(listener);
    listener(new Map(this.#states));
    return () => this.#listeners.delete(listener);
  }

  async submit(envelope: CommandEnvelope): Promise<void> {
    this.#states.set(envelope.command_id, { kind: "submitting", envelope });
    this.#emit();
    try {
      const result = await submitCommand(this.#baseUrl, envelope);
      this.#states.set(
        envelope.command_id,
        result.ok
          ? { kind: "accepted", envelope, receipt: result.data }
          : { kind: "rejected", envelope, error: result.error },
      );
    } catch {
      // The server may already have committed. Preserve the exact immutable
      // envelope so the only retry path reuses its original command_id.
      this.#states.set(envelope.command_id, { kind: "delivery_unknown", envelope });
    }
    this.#emit();
  }

  async retry(commandId: string): Promise<void> {
    const existing = this.#states.get(commandId);
    if (existing?.kind !== "delivery_unknown") return;
    await this.submit(existing.envelope);
  }

  #emit(): void {
    const snapshot = new Map(this.#states);
    for (const listener of this.#listeners) listener(snapshot);
  }
}

export function newCommandId(): string {
  return `cmd_${crypto.randomUUID().replaceAll("-", "")}`;
}
