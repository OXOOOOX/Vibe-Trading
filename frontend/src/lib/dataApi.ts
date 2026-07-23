import { ApiError } from "@/lib/api";
import { authHeaders } from "@/lib/apiAuth";

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const { headers, ...rest } = options ?? {};
  const response = await fetch(path, {
    ...rest,
    headers: { "Content-Type": "application/json", ...authHeaders(), ...headers },
  });
  if (!response.ok) {
    let message = `HTTP ${response.status}`;
    try {
      const body = await response.json();
      message = body.detail || body.message || message;
    } catch { /* use the status fallback */ }
    throw new ApiError(message, response.status);
  }
  return response.json() as Promise<T>;
}

export type DataStatus = "live" | "partial" | "offline" | "ok";
export interface WatchlistEntry { symbol: string; added_at: string; note: string | null }
export interface CoverageRow { symbol: string; name?: string | null; actual_source: string; interval: string; actual_adjustment: string; min_bar_time: string; max_bar_time: string; row_count: number; last_success_at: string }
export interface SourceHealth {
  source: string;
  requested_source: string;
  actual_source: string | null;
  upstream_source: string | null;
  capability: string;
  consecutive_failures: number;
  circuit_open: boolean;
  circuit_open_until: string | null;
  last_status: string;
  effective_status: string;
  stale: boolean;
  last_latency_ms: number | null;
  error_category: string | null;
  last_error: string | null;
  updated_at: string;
}
export interface StorageEntry { kind: string; path: string; bytes: number }
export interface PrewarmStatus { enabled: boolean; running: boolean; timezone: string; calendar_mode: string; slots: { phase: string; time: string }[]; last_run: { status?: string; at?: string } | null }

export const dataApi = {
  coverage: () => request<{ status: string; coverage: CoverageRow[]; watchlist: WatchlistEntry[]; retention: Record<string, unknown> }>("/data/coverage"),
  sources: () => request<{ status: string; sources: SourceHealth[]; quorum: string }>("/data/sources"),
  storage: () => request<{ status: string; entries: StorageEntry[]; total_bytes: number; soft_limit_bytes: number; evict_at_bytes: number; retention: Record<string, unknown> }>("/data/storage"),
  watchlist: () => request<{ status: string; watchlist: WatchlistEntry[] }>("/data/watchlist"),
  addWatchlist: (symbol: string, note?: string) => request<{ status: string; entry: WatchlistEntry }>("/data/watchlist", { method: "POST", body: JSON.stringify({ symbol, note: note || null }) }),
  removeWatchlist: (symbol: string) => request<{ status: string; deleted: string }>(`/data/watchlist/${encodeURIComponent(symbol)}`, { method: "DELETE" }),
  prewarm: (phase: "premarket" | "intraday" = "premarket") => request<{ status: DataStatus; request_id?: string }>("/data/prewarm", { method: "POST", body: JSON.stringify({ phase }) }),
  prewarmStatus: () => request<PrewarmStatus>("/data/prewarm/status"),
};
