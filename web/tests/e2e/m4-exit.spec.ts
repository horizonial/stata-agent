import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

type Bootstrap = {
  readonly data: {
    readonly execution: {
      readonly turns: ReadonlyArray<{
        readonly status: string;
        readonly turn_id: string;
      }>;
    };
  };
};

type JournalPage = {
  readonly items: ReadonlyArray<{
    readonly event_type: string;
    readonly payload_json: string;
  }>;
};

async function createWorkspace(request: APIRequestContext, workspaceId: string): Promise<void> {
  const response = await request.post("/api/v1/commands", {
    data: {
      schema_version: "1",
      command_id: `cmd_create_${workspaceId}`,
      workspace_id: workspaceId,
      command_type: "workspace.create",
      payload: {},
      preconditions: {},
    },
  });
  expect(response.ok()).toBeTruthy();
}

async function bootstrap(request: APIRequestContext, workspaceId: string): Promise<Bootstrap> {
  const response = await request.get(`/api/v1/workspaces/${workspaceId}/bootstrap`);
  expect(response.ok()).toBeTruthy();
  return (await response.json()) as Bootstrap;
}

async function openWorkspace(page: Page, workspaceId: string): Promise<void> {
  await page.locator("#workspace").fill(workspaceId);
  await page.getByRole("button", { name: "打开", exact: true }).click();
  await expect(page.locator(".workspace-header .eyebrow")).toHaveText(workspaceId);
  await expect(page.locator(".sidebar-footer")).toContainText("已同步");
}

async function submitInstruction(page: Page, content: string): Promise<void> {
  await page.locator("#new-instruction").fill(content);
  await page.locator(".instruction-form button[type=submit]").click();
  await expect(page.locator(".command-state.accepted")).toBeVisible();
}

async function sessionIds(
  request: APIRequestContext,
  workspaceId: string,
): Promise<ReadonlyArray<string>> {
  const response = await request.get(
    `/api/v1/workspaces/${workspaceId}/journal-entries?page_size=200`,
  );
  expect(response.ok()).toBeTruthy();
  const journal = (await response.json()) as JournalPage;
  return journal.items
    .filter((item) => item.event_type === "tool.handoff_committed")
    .map((item) => String((JSON.parse(item.payload_json) as { session_id: string }).session_id));
}

async function journalEvents(
  request: APIRequestContext,
  workspaceId: string,
): Promise<ReadonlyArray<string>> {
  const response = await request.get(
    `/api/v1/workspaces/${workspaceId}/journal-entries?page_size=200`,
  );
  expect(response.ok()).toBeTruthy();
  const journal = (await response.json()) as JournalPage;
  return journal.items.map((item) => item.event_type);
}

test("browser commands run two real Workspace Stata sessions in parallel and drain one queue", async ({
  page,
  request,
}) => {
  const suffix = `${Date.now()}`;
  const workspaceA = `ws_browser_a_${suffix}`;
  const workspaceB = `ws_browser_b_${suffix}`;
  await createWorkspace(request, workspaceA);
  await createWorkspace(request, workspaceB);

  await page.goto(`/?workspace=${workspaceA}`);
  await expect(page.locator(".workspace-header .eyebrow")).toHaveText(workspaceA);
  const startedAt = Date.now();
  await submitInstruction(page, "Workspace A browser parallel regression");
  await openWorkspace(page, workspaceB);
  await submitInstruction(page, "Workspace B browser parallel regression");

  await expect
    .poll(async () => {
      const [a, b] = await Promise.all([
        bootstrap(request, workspaceA),
        bootstrap(request, workspaceB),
      ]);
      return [a.data.execution.turns[0]?.status, b.data.execution.turns[0]?.status];
    })
    .toEqual(["running", "running"]);

  await openWorkspace(page, workspaceA);
  await submitInstruction(page, "queued Workspace A follow-up");
  await expect(page.locator(".queue-strip")).toContainText("#1");
  await expect(page.locator(".attention-link", { hasText: workspaceB })).toContainText(
    "Agent running",
  );

  await expect
    .poll(async () => {
      const [eventsA, eventsB] = await Promise.all([
        journalEvents(request, workspaceA),
        journalEvents(request, workspaceB),
      ]);
      return [eventsA, eventsB].every(
        (events) =>
          events.includes("tool.handoff_committed") && !events.includes("tool.completed"),
      );
    })
    .toBeTruthy();

  await expect
    .poll(
      async () => {
        const [a, b] = await Promise.all([
          bootstrap(request, workspaceA),
          bootstrap(request, workspaceB),
        ]);
        return [
          a.data.execution.turns.map((turn) => turn.status),
          b.data.execution.turns.map((turn) => turn.status),
        ];
      },
      { timeout: 30_000 },
    )
    .toEqual([["succeeded", "succeeded"], ["succeeded"]]);

  const elapsedSeconds = (Date.now() - startedAt) / 1000;
  expect(elapsedSeconds).toBeLessThan(24);
  const sessionsA = await sessionIds(request, workspaceA);
  const sessionsB = await sessionIds(request, workspaceB);
  expect(new Set(sessionsA).size).toBe(1);
  expect(new Set(sessionsB).size).toBe(1);
  expect(sessionsA).toHaveLength(2);
  expect(sessionsB).toHaveLength(1);
  expect(sessionsA[0]).not.toBe(sessionsB[0]);

  await expect(page.locator(".attention-link", { hasText: workspaceB })).toContainText("0 排队");
  await page.getByRole("button", { name: "Trace" }).click();
  await expect(page.locator(".trace-row").first()).toBeVisible();
  await expect(page.locator(".trace-view")).not.toContainText(sessionsB[0]!);
  const firstPageCount = await page.locator(".trace-row").count();
  expect(firstPageCount).toBe(30);
  await page.getByRole("button", { name: "加载更早记录" }).click();
  await expect.poll(async () => page.locator(".trace-row").count()).toBeGreaterThan(firstPageCount);
  const journalIds = await page.locator(".trace-row .item-meta").allTextContents();
  expect(new Set(journalIds).size).toBe(journalIds.length);
});

test("workspace switching rejects late detail state and delivery-unknown retry is idempotent", async ({
  page,
  request,
}) => {
  const suffix = `${Date.now()}`;
  const workspaceA = `ws_switch_a_${suffix}`;
  const workspaceB = `ws_switch_b_${suffix}`;
  await createWorkspace(request, workspaceA);
  await createWorkspace(request, workspaceB);

  let delayed = false;
  await page.route(`**/api/v1/workspaces/${workspaceA}/bootstrap`, async (route) => {
    if (!delayed) {
      delayed = true;
      await new Promise((resolve) => setTimeout(resolve, 750));
    }
    await route.continue();
  });
  await page.goto(`/?workspace=${workspaceA}`);
  await page.locator("#workspace").fill(workspaceB);
  await page.getByRole("button", { name: "打开", exact: true }).click();
  await expect(page.locator(".workspace-header .eyebrow")).toHaveText(workspaceB);
  await page.waitForTimeout(1_000);
  await expect(page.locator(".workspace-header .eyebrow")).toHaveText(workspaceB);

  let droppedCommandId: string | undefined;
  await page.route("**/api/v1/commands", async (route) => {
    const body = route.request().postDataJSON() as { command_id?: string; command_type?: string };
    if (body.command_type === "message.submit" && droppedCommandId === undefined) {
      droppedCommandId = body.command_id;
      await route.fetch();
      await route.abort("failed");
      return;
    }
    await route.continue();
  });
  await page.locator("#new-instruction").fill("queued delivery unknown profile");
  await page.locator(".instruction-form button[type=submit]").click();
  await expect(page.locator(".command-state.delivery_unknown")).toBeVisible();
  await page.getByRole("button", { name: "使用同一 command_id 重试" }).click();
  await expect(page.locator(".command-state.accepted")).toContainText("已接纳");

  await expect
    .poll(async () => (await bootstrap(request, workspaceB)).data.execution.turns.length)
    .toBe(1);
  expect(droppedCommandId).toBeTruthy();
});

test("durable cursor gap forces a full authoritative browser resync", async ({ page, request }) => {
  const workspaceId = `ws_resync_${Date.now()}`;
  await createWorkspace(request, workspaceId);
  let bootstrapCount = 0;
  await page.route(`**/api/v1/workspaces/${workspaceId}/bootstrap`, async (route) => {
    bootstrapCount += 1;
    await route.continue();
  });
  let rejectedHead = false;
  await page.route(`**/api/v1/workspaces/${workspaceId}/stream-head?**`, async (route) => {
    if (!rejectedHead) {
      rejectedHead = true;
      await route.fulfill({
        status: 409,
        contentType: "application/json",
        body: JSON.stringify({
          schema_version: "1",
          error: {
            code: "RESYNC_REQUIRED",
            message: "Injected cursor gap",
            next_action: "bootstrap",
          },
        }),
      });
      return;
    }
    await route.continue();
  });

  await page.goto(`/?workspace=${workspaceId}`);
  await expect(page.locator(".workspace-header .eyebrow")).toHaveText(workspaceId);
  await expect(page.locator(".sidebar-footer")).toContainText("已同步");
  await expect.poll(() => bootstrapCount).toBeGreaterThanOrEqual(2);
  expect(rejectedHead).toBeTruthy();
});
