import {
  getWorkspaceStreamHead,
  workspaceEventStreamUrl,
  type DurableNotificationResponse,
} from "../generated/api-v1";
import { ReconnectingApiEventStream } from "./api-event-stream";

export type WorkspaceStreamCallbacks = {
  readonly onDurable: (notification: DurableNotificationResponse) => void;
  readonly onOpen: () => void;
  readonly onReconnecting: () => void;
  readonly onResyncRequired: () => void;
};

export class WorkspaceStream {
  readonly #baseUrl: string;
  readonly #source = new ReconnectingApiEventStream();
  #generation = 0;
  #seenEventIds = new Set<string>();

  constructor(baseUrl = "") {
    this.#baseUrl = baseUrl;
  }

  close(): void {
    this.#generation += 1;
    this.#source.close();
    this.#seenEventIds.clear();
  }

  async open(
    workspaceId: string,
    afterCursor: string,
    callbacks: WorkspaceStreamCallbacks,
  ): Promise<void> {
    this.close();
    const generation = this.#generation;
    const probe = await getWorkspaceStreamHead(this.#baseUrl, workspaceId, afterCursor);
    if (generation !== this.#generation) return;
    if (!probe.ok) {
      if (probe.error.error.code === "RESYNC_REQUIRED") callbacks.onResyncRequired();
      else callbacks.onReconnecting();
      return;
    }

    this.#source.open(workspaceEventStreamUrl(this.#baseUrl, workspaceId, afterCursor), {
      onOpen: () => {
        if (generation === this.#generation) callbacks.onOpen();
      },
      onReconnecting: () => {
        if (generation === this.#generation) callbacks.onReconnecting();
      },
      onEvent: (event) => {
        if (generation !== this.#generation) return;
        if (event.event === "resync_required") {
          this.#source.close();
          callbacks.onResyncRequired();
          return;
        }
        if (event.event !== "durable") return;
        const notification = JSON.parse(event.data) as DurableNotificationResponse;
        if (
          notification.event_class !== "durable" ||
          notification.workspace_id !== workspaceId ||
          this.#seenEventIds.has(notification.event_id)
        ) {
          return;
        }
        this.#seenEventIds.add(notification.event_id);
        callbacks.onDurable(notification);
      },
    });
  }
}
