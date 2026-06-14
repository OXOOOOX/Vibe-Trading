import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { RouterProvider } from "react-router-dom";
import { Toaster } from "sonner";
import { ErrorBoundary } from "./components/common/ErrorBoundary";
import { router } from "./router";
import { I18nProvider } from "./lib/i18n";
import "highlight.js/styles/github-dark-dimmed.min.css";
import "./index.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ErrorBoundary>
      <I18nProvider>
        <RouterProvider router={router} />
      </I18nProvider>
      <Toaster position="bottom-right" richColors closeButton duration={3500} />
    </ErrorBoundary>
  </StrictMode>
);
