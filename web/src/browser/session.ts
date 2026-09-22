import {
  exchangeBrowserSession,
  getMeta,
  setBrowserSessionToken,
} from "../generated/api-v1";

export class BrowserSessionBootstrapError extends Error {}

function takeBootstrapNonce(): string | undefined {
  const fragment = new URLSearchParams(window.location.hash.slice(1));
  const nonce = fragment.get("bootstrap") ?? undefined;
  if (window.location.hash.length > 0) {
    window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}`);
  }
  return nonce;
}

export async function initializeBrowserSession(baseUrl = ""): Promise<void> {
  const nonce = takeBootstrapNonce();
  const meta = await getMeta(baseUrl);
  if (!meta.browser_session_required) return;
  if (nonce === undefined || nonce.length === 0) {
    throw new BrowserSessionBootstrapError(
      "This browser page must be opened by the Stata Research Agent launcher.",
    );
  }
  try {
    const receipt = await exchangeBrowserSession(baseUrl, nonce);
    setBrowserSessionToken(receipt.browser_session_token);
  } catch {
    throw new BrowserSessionBootstrapError(
      "The browser launch capability expired or was already used. Open the app again.",
    );
  }
}
