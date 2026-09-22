import { workspaceAttentionEventStreamUrl } from "../generated/api-v1";
import { ReconnectingApiEventStream } from "./api-event-stream";

export class GlobalAttentionStream {
  readonly #baseUrl: string;
  readonly #source = new ReconnectingApiEventStream();

  constructor(baseUrl = "") {
    this.#baseUrl = baseUrl;
  }

  open(onInvalidated: () => void): void {
    this.close();
    this.#source.open(workspaceAttentionEventStreamUrl(this.#baseUrl), {
      onOpen: onInvalidated,
      onReconnecting: () => undefined,
      onEvent: (event) => {
        if (event.event === "attention_invalidated") onInvalidated();
      },
    });
  }

  close(): void {
    this.#source.close();
  }
}
