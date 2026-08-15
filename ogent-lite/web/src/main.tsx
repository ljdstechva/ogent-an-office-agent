import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./App";
import { ErrorBoundary } from "./components/ErrorBoundary";
import "./styles/foundation.css";
import "./styles/intelligence.css";
import "./styles/preview.css";
import "./styles/chat.css";
import "./styles/overlays.css";
import "./styles/responsive.css";

const root = document.getElementById("root");
if (!root) {
  throw new Error("Ogent could not find its application root.");
}

createRoot(root).render(
  <StrictMode>
    <ErrorBoundary>
      <App />
    </ErrorBoundary>
  </StrictMode>,
);
