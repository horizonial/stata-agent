import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests/e2e",
  fullyParallel: false,
  workers: 1,
  timeout: 90_000,
  expect: { timeout: 15_000 },
  use: {
    baseURL: "http://127.0.0.1:8765",
    browserName: "chromium",
    channel: "chrome",
    headless: true,
    trace: "retain-on-failure",
  },
  webServer: [
    {
      command:
        "..\\.venv\\Scripts\\python.exe ..\\tests\\e2e\\m4_server.py --workspace-root ..\\.e2e-runtime",
      url: "http://127.0.0.1:8765/api/v1/meta",
      reuseExistingServer: false,
      timeout: 120_000,
    },
    {
      command:
        "..\\.venv\\Scripts\\python.exe ..\\tests\\e2e\\m5_session_server.py --workspace-root ..\\.e2e-session-runtime",
      url: "http://127.0.0.1:8766/api/v1/meta",
      reuseExistingServer: false,
      timeout: 120_000,
    },
  ],
});
