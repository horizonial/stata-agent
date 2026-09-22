import { expect, test } from "@playwright/test";

test("first-run UI can create a Workspace and keeps the URL authoritative", async ({ page }) => {
  const workspaceId = `ws_ui_onboarding_${Date.now()}`;
  await page.goto("/");
  await page.locator("#workspace").fill(workspaceId);
  await page.getByRole("button", { name: "新建", exact: true }).click();

  await expect(page.locator(".workspace-header .eyebrow")).toHaveText(workspaceId);
  await expect(page.locator(".sidebar-footer")).toContainText("已同步");
  await expect(page).toHaveURL(new RegExp(`workspace=${workspaceId}$`));

  await page.getByRole("button", { name: "Word", exact: true }).click();
  await expect(page.getByRole("heading", { name: "当前路径还没有文章版本" })).toBeVisible();
  await expect
    .poll(async () => (await page.locator(".path-picker").boundingBox())?.height ?? 999)
    .toBeLessThan(64);
  await page.getByRole("button", { name: "设置", exact: true }).click();
  await expect(page.getByText("当前工作区尚未配置模型。")).toBeVisible();
  await expect(page.getByRole("heading", { name: "新增 Provider 凭据" })).toBeVisible();
});

test("compact viewport has no horizontal document overflow", async ({ page }) => {
  const workspaceId = `ws_ui_compact_${Date.now()}`;
  await page.setViewportSize({ width: 600, height: 900 });
  await page.goto("/");
  await expect(page.locator(".sidebar-body")).toBeHidden();
  await page.getByRole("button", { name: "工作区", exact: true }).click();
  await expect(page.locator(".sidebar-body")).toBeVisible();
  await page.locator("#workspace").fill(workspaceId);
  await page.getByRole("button", { name: "新建", exact: true }).click();
  await expect(page.locator(".workspace-header .eyebrow")).toHaveText(workspaceId);
  await expect(page.locator(".sidebar-body")).toBeHidden();
  await expect
    .poll(() => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth))
    .toBeTruthy();
  await expect(page.locator("#new-instruction")).toBeVisible();
});
