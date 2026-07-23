import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";
import path from "path";

export const PROXY_PATHS = [
  "/sessions",
  "/swarm/presets",
  "/swarm/runs",
  "/settings/llm",
  "/settings/data-sources",
  "/settings/research",
  "/settings/feishu-delivery",
  "/settings/codex-cli",
  "/channels",
  "/portfolio/review",
  "/portfolio/cash",
  "/portfolio/mandate",
  "/portfolio/monitor",
  "/portfolio/daily-runs",
  "/portfolio/weekly-runs",
  "/portfolio/holdings",
  "/portfolio/trades",
  "/portfolio/reconciliation",
  "/portfolio/refresh-market-data",
  "/portfolio/analysis-sessions",
  "/market-cache",
  "/data",
  "/mandate",
  "/live",
  "/upload",
  "/report-library",
  "/shadow-reports",
];

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const apiTarget = env.VITE_API_URL || "http://127.0.0.1:8899";
  const apiProxy = { target: apiTarget, changeOrigin: true };
  const apiProxyWithHtmlFallback = {
    ...apiProxy,
    bypass(req: { headers: { accept?: string }; url?: string }) {
      if (req.headers.accept?.includes("text/html")) {
        return "/index.html";
      }
    },
  };

  const previewProxy = {
    ...Object.fromEntries(PROXY_PATHS.map((p) => [p, apiProxy])),
    "/reports": apiProxyWithHtmlFallback,
    "^/runs/[^/]+/?$": apiProxyWithHtmlFallback,
    "/runs": apiProxy,
    "/correlation": apiProxyWithHtmlFallback,
    "^/alpha(?:/|$)": apiProxy,
  };

  return {
    plugins: [react()],
    resolve: {
      alias: { "@": path.resolve(__dirname, "./src") },
    },
    server: {
      port: 5899,
      proxy: {
        ...Object.fromEntries(PROXY_PATHS.map((p) => [p, apiProxy])),
        "/reports": apiProxyWithHtmlFallback,
        // SPA RunDetail page — only the two-segment ``/runs/{id}``
        // form should fall back to ``index.html`` on browser navigation.
        // ``/runs/{id}/code`` and ``/runs/{id}/pine`` are API-only and
        // must keep proxying to the backend even when Accept is text/html.
        "^/runs/[^/]+/?$": apiProxyWithHtmlFallback,
        "/runs": apiProxy,
        "/correlation": apiProxyWithHtmlFallback,
        "^/alpha(?:/|$)": apiProxy,
      },
    },
    preview: {
      port: 5899,
      proxy: previewProxy,
    },
    build: {
      rollupOptions: {
        output: {
          manualChunks: {
            "vendor-react": ["react", "react-dom", "react-router-dom"],
            "vendor-charts": ["echarts"],
          },
        },
      },
    },
  };
});
