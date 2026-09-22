import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import App from "./App";
import {
  BrowserSessionBootstrapError,
  initializeBrowserSession,
} from "./browser/session";
import "./styles.css";

const root = document.getElementById("root");
if (root === null) throw new Error("Missing #root mount point");

try {
  await initializeBrowserSession();
  createRoot(root).render(
    <StrictMode>
      <App />
    </StrictMode>,
  );
} catch (error) {
  root.setAttribute("role", "alert");
  root.textContent =
    error instanceof BrowserSessionBootstrapError
      ? error.message
      : "The local Stata Research Agent service is unavailable.";
}
