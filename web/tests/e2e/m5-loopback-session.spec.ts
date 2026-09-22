import { expect, test, type APIRequestContext } from "@playwright/test";

const securedOrigin = "http://127.0.0.1:8766";
const launcherSecret = `m5_e2e_launcher_${"a".repeat(40)}`;

async function issueBootstrapNonce(request: APIRequestContext): Promise<string> {
  const response = await request.post(`${securedOrigin}/api/v1/launcher/browser-bootstrap`, {
    headers: { "x-stata-launcher-capability": launcherSecret },
  });
  expect(response.ok()).toBeTruthy();
  const body = (await response.json()) as { readonly bootstrap_nonce: string };
  return body.bootstrap_nonce;
}

test("launcher nonce becomes a memory-only browser session for API and fetch-stream", async ({
  page,
  request,
}) => {
  const denied = await request.get(`${securedOrigin}/api/v1/workspace-attention`);
  expect(denied.status()).toBe(401);

  const nonce = await issueBootstrapNonce(request);
  const observedHeaders: Array<{ readonly url: string; readonly token: string | undefined }> = [];
  page.on("request", (outbound) => {
    if (!outbound.url().startsWith(`${securedOrigin}/api/v1/workspace-attention`)) return;
    observedHeaders.push({
      url: outbound.url(),
      token: outbound.headers()["x-stata-browser-session"],
    });
  });

  await page.goto(`${securedOrigin}/#bootstrap=${encodeURIComponent(nonce)}`);
  await expect(page.locator(".app-shell")).toBeVisible();
  await expect.poll(() => observedHeaders.length).toBeGreaterThanOrEqual(2);
  expect(page.url()).toBe(`${securedOrigin}/`);
  expect(observedHeaders.every((item) => item.token !== undefined && item.token.length >= 32)).toBe(
    true,
  );
  expect(observedHeaders.every((item) => !item.url.includes(observedHeaders[0]?.token ?? ""))).toBe(
    true,
  );
  await page.reload();
  await expect(page.getByRole("alert")).toContainText("opened by the Stata Research Agent launcher");
});

test("a browser bootstrap nonce cannot be replayed", async ({ browser, request }) => {
  const nonce = await issueBootstrapNonce(request);
  const first = await browser.newPage();
  await first.goto(`${securedOrigin}/#bootstrap=${encodeURIComponent(nonce)}`);
  await expect(first.locator(".app-shell")).toBeVisible();

  const replay = await browser.newPage();
  await replay.goto(`${securedOrigin}/#bootstrap=${encodeURIComponent(nonce)}`);
  await expect(replay.getByRole("alert")).toContainText("expired or was already used");
  await first.close();
  await replay.close();
});
