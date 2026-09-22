import {
  consumeApiEventStream,
  type ApiEventStreamEvent,
} from "../generated/api-v1";

export type ApiEventStreamCallbacks = {
  readonly onOpen: () => void;
  readonly onEvent: (event: ApiEventStreamEvent) => void;
  readonly onReconnecting: () => void;
};

export class ReconnectingApiEventStream {
  #controller: AbortController | undefined;

  open(url: string, callbacks: ApiEventStreamCallbacks): void {
    this.close();
    const controller = new AbortController();
    this.#controller = controller;
    void this.#run(url, callbacks, controller);
  }

  close(): void {
    this.#controller?.abort();
    this.#controller = undefined;
  }

  async #run(
    url: string,
    callbacks: ApiEventStreamCallbacks,
    controller: AbortController,
  ): Promise<void> {
    while (!controller.signal.aborted) {
      try {
        await consumeApiEventStream(url, callbacks, controller.signal);
      } catch {
        if (controller.signal.aborted) return;
      }
      if (controller.signal.aborted) return;
      callbacks.onReconnecting();
      await new Promise<void>((resolve) => {
        const timer = window.setTimeout(resolve, 1_000);
        controller.signal.addEventListener(
          "abort",
          () => {
            window.clearTimeout(timer);
            resolve();
          },
          { once: true },
        );
      });
    }
  }
}
