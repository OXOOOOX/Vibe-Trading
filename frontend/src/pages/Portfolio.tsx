import { Fragment, useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import {
  AlertTriangle,
  ArrowDown,
  ArrowUp,
  Bot,
  BriefcaseBusiness,
  ChartCandlestick,
  Check,
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  ChevronsUpDown,
  Database,
  Info,
  Loader2,
  Pencil,
  Radar,
  RefreshCw,
  Save,
  Trash2,
  X,
} from "lucide-react";
import { toast } from "sonner";
import {
  api,
  type PortfolioAnalysisScope,
  type PortfolioAnalysisSession,
  type MarketCacheRun,
  type PortfolioDailyRun,
  type PortfolioHolding,
  type PortfolioMandate,
  type PortfolioReview,
  type PortfolioTrade,
  type VerifiedMarketCacheRow,
} from "@/lib/api";
import { cn } from "@/lib/utils";
import MarketCacheKlineDialog from "@/components/portfolio/MarketCacheKlineDialog";
import PortfolioDailyRunPanel from "@/components/portfolio/PortfolioDailyRunPanel";
import PortfolioMonitorPanel from "@/components/portfolio/PortfolioMonitorPanel";
import PortfolioReconciliationPanel from "@/components/portfolio/PortfolioReconciliationPanel";

const ACTIVE_REFRESH_KEY = "vibe-trading:portfolio-market-refresh:v1";
const ANALYSIS_RUNS_STORAGE_KEY = "vibe-trading:portfolio-analysis-runs:v1";
const MONITOR_SELECTION_STORAGE_KEY = "vibe-trading:portfolio-monitor-selection:v1";
const MONITOR_LEVEL_STORAGE_KEY = "vibe-trading:portfolio-monitor-levels:v2";
const MONITOR_LEVEL_APPLY_DELAY_MS = 5_000;
const TERMINAL_REFRESH_STATUSES = new Set(["completed", "partial", "failed", "interrupted"]);
const ACTIVE_ANALYSIS_STATUSES = new Set(["queued", "running"]);
const ACTIVE_DAILY_RUN_STATUSES = new Set(["queued", "running", "cancelling"]);
const US_TICKER_PATTERN = /^[A-Z][A-Z0-9]{0,9}(?:\.[A-Z])?(?:\.US)?$/;

function isCompleteSecurityCode(value: string): boolean {
  const code = value.trim().toUpperCase();
  return /^\d{6}$/.test(code) || US_TICKER_PATTERN.test(code);
}

function sanitizeSecurityCode(value: string): string {
  return value.toUpperCase().replace(/[^A-Z0-9.]/g, "").slice(0, 16);
}

const HOLDING_SORT_COLUMNS = [
  { key: "name", label: "名称" },
  { key: "symbol", label: "证券标识" },
  { key: "quantity", label: "数量", numeric: true },
  { key: "cost_price", label: "成本价", numeric: true },
  { key: "last_price", label: "最新价", numeric: true },
  { key: "market_value", label: "市值", numeric: true },
  { key: "pnl", label: "盈亏", numeric: true },
  { key: "pnl_pct", label: "盈亏率", numeric: true },
  { key: "market_status", label: "行情状态" },
  { key: "market_verified_at", label: "校核时间" },
] as const;

type HoldingSortKey = (typeof HOLDING_SORT_COLUMNS)[number]["key"];
type HoldingSortDirection = "asc" | "desc";
type HoldingSort = { key: HoldingSortKey; direction: HoldingSortDirection };
type PortfolioMonitorLevel = "off" | "manual" | "autonomous";

const holdingTextCollator = new Intl.Collator("zh-CN", { numeric: true, sensitivity: "base" });
const shanghaiClockFormatter = new Intl.DateTimeFormat("en-GB", {
  timeZone: "Asia/Shanghai",
  weekday: "short",
  hour: "2-digit",
  minute: "2-digit",
  hourCycle: "h23",
});

export function getMarketAnalysisPhase(now = new Date()): "premarket" | "intraday" {
  const timeParts = Object.fromEntries(
    shanghaiClockFormatter.formatToParts(now).map((part) => [part.type, part.value]),
  );
  const minutes = Number(timeParts.hour) * 60 + Number(timeParts.minute);
  const weekday = timeParts.weekday;
  const isWeekday = weekday !== "Sat" && weekday !== "Sun";
  const isIntradayWindow = minutes >= 11 * 60 + 30 && minutes < 15 * 60;
  return isWeekday && isIntradayWindow ? "intraday" : "premarket";
}

export function getMarketAnalysisLabel(now = new Date()): "盘前分析" | "盘中分析" {
  return getMarketAnalysisPhase(now) === "premarket" ? "盘前分析" : "盘中分析";
}

function todayDateString(): string {
  const date = new Date();
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function shiftDate(value: string, days: number): string {
  const date = value ? new Date(`${value}T00:00:00`) : new Date();
  if (Number.isNaN(date.getTime())) return todayDateString();
  date.setDate(date.getDate() + days);
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function createEmptyTradeForm() {
  return {
    code: "",
    symbol: "",
    name: "",
    side: "buy" as "buy" | "sell",
    quantity: "",
    price: "",
    trade_date: todayDateString(),
    fees: "",
    taxes: "",
    broker_reported_pnl: "",
    notes: "",
  };
}

type TradeLookupState = {
  status: "idle" | "loading" | "resolved" | "error";
  code?: string;
  message?: string;
};

function fmtNumber(value: unknown, digits = 3): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "-";
  return value.toLocaleString(undefined, { maximumFractionDigits: digits });
}

function pnlTone(value: unknown): "gain" | "loss" | "neutral" {
  if (typeof value !== "number" || !Number.isFinite(value) || value === 0) return "neutral";
  return value > 0 ? "gain" : "loss";
}

function pnlColorClass(value: unknown): string {
  const tone = pnlTone(value);
  if (tone === "gain") return "text-red-500";
  if (tone === "loss") return "text-emerald-600 dark:text-emerald-400";
  return "text-muted-foreground";
}

function fmtText(value: unknown): string {
  if (value === null || value === undefined || value === "") return "-";
  return String(value);
}

function fmtDate(value: unknown): string {
  if (!value) return "-";
  const date = new Date(String(value));
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString();
}

function tradeSideLabel(value: unknown): string {
  const side = String(value || "").toLowerCase();
  if (side === "buy") return "买入";
  if (side === "sell") return "卖出";
  return fmtText(value);
}

function holdingCode(row: PortfolioHolding): string {
  const code = String(row.code || row.symbol || "").trim();
  return code.split(".")[0];
}

function holdingSymbol(row: PortfolioHolding): string {
  return String(row.symbol || row.code || "").trim();
}

function securityDisplayName(
  symbol: string,
  holdingNames: ReadonlyMap<string, string>,
  explicitName?: unknown,
): string {
  const normalizedSymbol = symbol.trim().toUpperCase();
  const code = normalizedSymbol.split(".")[0];
  const name = String(
    explicitName
    || holdingNames.get(normalizedSymbol)
    || holdingNames.get(code)
    || "",
  ).trim();
  return name ? `${name}（${code || normalizedSymbol}）` : normalizedSymbol || "-";
}

type StoredMonitorLevels = {
  levels: Map<string, PortfolioMonitorLevel>;
  hasPersistedValue: boolean;
};

const MONITOR_LEVEL_LABELS: Record<PortfolioMonitorLevel, string> = {
  off: "未监控",
  manual: "普通监控",
  autonomous: "AI 自主监控",
};

function nextMonitorLevel(level: PortfolioMonitorLevel): PortfolioMonitorLevel {
  if (level === "off") return "manual";
  if (level === "manual") return "autonomous";
  return "off";
}

function loadMonitorLevels(): StoredMonitorLevels {
  if (typeof window === "undefined") {
    return { levels: new Map(), hasPersistedValue: false };
  }
  try {
    const raw = window.localStorage.getItem(MONITOR_LEVEL_STORAGE_KEY);
    if (raw !== null) {
      const stored = JSON.parse(raw) as { version?: unknown; levels?: unknown };
      const levels = new Map<string, PortfolioMonitorLevel>();
      if (stored.version === 2 && stored.levels && typeof stored.levels === "object" && !Array.isArray(stored.levels)) {
        for (const [symbol, level] of Object.entries(stored.levels)) {
          if (level !== "manual" && level !== "autonomous") continue;
          const normalized = symbol.trim().toUpperCase();
          if (normalized) levels.set(normalized, level);
        }
      }
      return { levels, hasPersistedValue: true };
    }

    const legacyRaw = window.localStorage.getItem(MONITOR_SELECTION_STORAGE_KEY);
    if (legacyRaw === null) return { levels: new Map(), hasPersistedValue: false };
    const legacy = JSON.parse(legacyRaw) as { version?: unknown; symbols?: unknown };
    const levels = new Map<string, PortfolioMonitorLevel>();
    if (legacy.version === 1 && Array.isArray(legacy.symbols)) {
      for (const value of legacy.symbols) {
        if (typeof value !== "string") continue;
        const symbol = value.trim().toUpperCase();
        if (symbol) levels.set(symbol, "autonomous");
      }
    }
    return { levels, hasPersistedValue: true };
  } catch {
    return { levels: new Map(), hasPersistedValue: true };
  }
}

function persistMonitorLevels(levels: ReadonlyMap<string, PortfolioMonitorLevel>): void {
  if (typeof window === "undefined") return;
  try {
    const activeLevels = Object.fromEntries(
      Array.from(levels.entries())
        .filter(([, level]) => level !== "off")
        .sort(([left], [right]) => left.localeCompare(right)),
    );
    window.localStorage.setItem(MONITOR_LEVEL_STORAGE_KEY, JSON.stringify({
      version: 2,
      levels: activeLevels,
    }));
    const autonomousSymbols = Array.from(levels.entries())
      .filter(([, level]) => level === "autonomous")
      .map(([symbol]) => symbol)
      .sort();
    window.localStorage.setItem(MONITOR_SELECTION_STORAGE_KEY, JSON.stringify({
      version: 1,
      symbols: autonomousSymbols,
    }));
  } catch {
    // A blocked or full localStorage must not make the portfolio page unusable.
  }
}

function matchesHolding(row: PortfolioHolding, query: string): boolean {
  const needle = query.trim().toLowerCase();
  if (!needle) return false;
  const code = holdingCode(row).toLowerCase();
  const symbol = holdingSymbol(row).toLowerCase();
  const name = String(row.name || "").toLowerCase();
  return code.startsWith(needle) || symbol.startsWith(needle) || name.includes(needle);
}

function findExactHolding(holdings: PortfolioHolding[], codeOrSymbol: string): PortfolioHolding | undefined {
  const value = codeOrSymbol.trim().toLowerCase();
  if (!value) return undefined;
  return holdings.find((row) => holdingCode(row).toLowerCase() === value || holdingSymbol(row).toLowerCase() === value);
}

function holdingSortValue(row: PortfolioHolding, key: HoldingSortKey): string | number | null {
  switch (key) {
    case "name": {
      const value = String(row.name || "").trim();
      return value || null;
    }
    case "symbol": {
      const value = String(row.symbol || row.code || "").trim();
      return value || null;
    }
    case "quantity":
    case "cost_price":
    case "last_price":
    case "market_value":
    case "pnl":
    case "pnl_pct": {
      const value = row[key];
      return typeof value === "number" && Number.isFinite(value) ? value : null;
    }
    case "market_status": {
      const value = String(row.market_status || "").trim();
      return value || null;
    }
    case "market_verified_at": {
      const value = String(row.market_verified_at || "").trim();
      if (!value) return null;
      const timestamp = Date.parse(value);
      return Number.isNaN(timestamp) ? null : timestamp;
    }
  }
}

function compareHoldingRows(left: PortfolioHolding, right: PortfolioHolding, sort: HoldingSort): number {
  const leftValue = holdingSortValue(left, sort.key);
  const rightValue = holdingSortValue(right, sort.key);
  if (leftValue === null) return rightValue === null ? 0 : 1;
  if (rightValue === null) return -1;

  const difference = typeof leftValue === "number" && typeof rightValue === "number"
    ? leftValue - rightValue
    : holdingTextCollator.compare(String(leftValue), String(rightValue));
  return sort.direction === "asc" ? difference : -difference;
}

function analysisTargetKey(scope: PortfolioAnalysisScope, symbol?: string): string {
  if (scope === "portfolio") return "portfolio";
  if (scope === "market") return "market";
  return `holding:${String(symbol || "").toUpperCase()}`;
}

function loadSavedAnalysisRuns(): Record<string, PortfolioAnalysisSession> {
  try {
    const raw = localStorage.getItem(ANALYSIS_RUNS_STORAGE_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw) as unknown;
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return {};
    return Object.fromEntries(
      Object.entries(parsed).filter(([, value]) => (
        Boolean(value)
        && typeof value === "object"
        && typeof (value as PortfolioAnalysisSession).analysis_id === "string"
        && typeof (value as PortfolioAnalysisSession).session_id === "string"
      )),
    ) as Record<string, PortfolioAnalysisSession>;
  } catch {
    return {};
  }
}

function isActiveAnalysis(run: PortfolioAnalysisSession | undefined): boolean {
  return Boolean(run && ACTIVE_ANALYSIS_STATUSES.has(run.status));
}

export function Portfolio() {
  const navigate = useNavigate();
  const [initialMonitorLevels] = useState<StoredMonitorLevels>(loadMonitorLevels);
  const [review, setReview] = useState<PortfolioReview | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [refreshRun, setRefreshRun] = useState<MarketCacheRun | null>(null);
  const [holdingsText, setHoldingsText] = useState("");
  const [savingHoldings, setSavingHoldings] = useState(false);
  const [tradeForm, setTradeForm] = useState(createEmptyTradeForm);
  const [savingTrade, setSavingTrade] = useState(false);
  const [tradeLookup, setTradeLookup] = useState<TradeLookupState>({ status: "idle" });
  const [tradeSuggestionsOpen, setTradeSuggestionsOpen] = useState(false);
  const [deletingTradeId, setDeletingTradeId] = useState<string | null>(null);
  const [expandedCachePath, setExpandedCachePath] = useState<string | null>(null);
  const [chartCacheGroup, setChartCacheGroup] = useState<CacheGroup | null>(null);
  const [analysisRuns, setAnalysisRuns] = useState<Record<string, PortfolioAnalysisSession>>(loadSavedAnalysisRuns);
  const [startingAnalysisKey, setStartingAnalysisKey] = useState<string | null>(null);
  const [analysisClock, setAnalysisClock] = useState(Date.now);
  const [mandate, setMandate] = useState<PortfolioMandate | null>(null);
  const [dailyRun, setDailyRun] = useState<PortfolioDailyRun | null>(null);
  const [loadingMandate, setLoadingMandate] = useState(true);
  const [savingMandate, setSavingMandate] = useState(false);
  const [savingCash, setSavingCash] = useState(false);
  const [startingDailyRun, setStartingDailyRun] = useState(false);
  const [appliedMonitorLevels, setAppliedMonitorLevels] = useState<Map<string, PortfolioMonitorLevel>>(
    () => new Map(initialMonitorLevels.levels),
  );
  const [draftMonitorLevels, setDraftMonitorLevels] = useState<Map<string, PortfolioMonitorLevel>>(
    () => new Map(initialMonitorLevels.levels),
  );
  const [monitorLevelApplyAt, setMonitorLevelApplyAt] = useState<number | null>(null);
  const [monitorSelectionRevision, setMonitorSelectionRevision] = useState(0);
  const [monitorSelectionHydrationPending, setMonitorSelectionHydrationPending] = useState(
    () => !initialMonitorLevels.hasPersistedValue,
  );
  const completedRefreshRef = useRef<string | null>(null);
  const holdingNameByIdentifier = useMemo(() => {
    const names = new Map<string, string>();
    for (const holding of review?.portfolio_state.holdings || []) {
      const name = String(holding.name || "").trim();
      const symbol = holdingSymbol(holding).toUpperCase();
      const code = holdingCode(holding).toUpperCase();
      if (!name) continue;
      if (symbol) names.set(symbol, name);
      if (code) names.set(code, name);
    }
    return names;
  }, [monitorSelectionHydrationPending, review?.portfolio_state.holdings]);

  const appliedMonitorSymbols = useMemo(
    () => new Set(Array.from(appliedMonitorLevels.entries())
      .filter(([, level]) => level !== "off")
      .map(([symbol]) => symbol)),
    [appliedMonitorLevels],
  );
  const appliedManualMonitorSymbols = useMemo(
    () => new Set(Array.from(appliedMonitorLevels.entries())
      .filter(([, level]) => level === "manual")
      .map(([symbol]) => symbol)),
    [appliedMonitorLevels],
  );
  const appliedAutonomousMonitorSymbols = useMemo(
    () => new Set(Array.from(appliedMonitorLevels.entries())
      .filter(([, level]) => level === "autonomous")
      .map(([symbol]) => symbol)),
    [appliedMonitorLevels],
  );
  const pendingMonitorSymbols = useMemo(() => {
    const symbols = new Set([...draftMonitorLevels.keys(), ...appliedMonitorLevels.keys()]);
    return new Set(Array.from(symbols).filter((symbol) => (
      (draftMonitorLevels.get(symbol) || "off") !== (appliedMonitorLevels.get(symbol) || "off")
    )));
  }, [appliedMonitorLevels, draftMonitorLevels]);

  useEffect(() => {
    if (monitorSelectionHydrationPending) return;
    persistMonitorLevels(appliedMonitorLevels);
  }, [appliedMonitorLevels, monitorSelectionHydrationPending]);

  useEffect(() => {
    if (monitorLevelApplyAt === null) return;
    const timer = window.setTimeout(() => {
      setAppliedMonitorLevels(new Map(draftMonitorLevels));
      setMonitorSelectionRevision((current) => current + 1);
      setMonitorLevelApplyAt(null);
    }, Math.max(0, monitorLevelApplyAt - Date.now()));
    return () => window.clearTimeout(timer);
  }, [draftMonitorLevels, monitorLevelApplyAt]);

  useEffect(() => {
    const holdings = review?.portfolio_state.holdings;
    if (!holdings) return;
    const validSymbols = new Set(holdings.map(holdingSymbol).filter(Boolean));
    const pruneLevels = (current: Map<string, PortfolioMonitorLevel>) => {
      const next = new Map(Array.from(current.entries()).filter(([symbol]) => validSymbols.has(symbol)));
      return next.size === current.size ? current : next;
    };
    setAppliedMonitorLevels(pruneLevels);
    setDraftMonitorLevels(pruneLevels);
  }, [review?.portfolio_state.holdings]);

  const queueMonitorLevels = (symbols: string[], level: PortfolioMonitorLevel) => {
    setMonitorSelectionHydrationPending(false);
    const next = new Map(draftMonitorLevels);
    for (const rawSymbol of symbols) {
      const symbol = rawSymbol.trim().toUpperCase();
      if (!symbol) continue;
      if (level === "off") next.delete(symbol);
      else next.set(symbol, level);
    }
    setDraftMonitorLevels(next);
    const allSymbols = new Set([...next.keys(), ...appliedMonitorLevels.keys()]);
    const changed = Array.from(allSymbols).some((symbol) => (
      (next.get(symbol) || "off") !== (appliedMonitorLevels.get(symbol) || "off")
    ));
    setMonitorLevelApplyAt(changed ? Date.now() + MONITOR_LEVEL_APPLY_DELAY_MS : null);
  };

  const loadReview = useCallback(async (mode: "initial" | "refresh" = "refresh") => {
    if (mode === "initial") setLoading(true);
    else setRefreshing(true);
    try {
      const payload = await api.getPortfolioReview(50);
      setReview(payload);
      if (payload.active_market_refresh) {
        setRefreshRun(payload.active_market_refresh);
        localStorage.setItem(ACTIVE_REFRESH_KEY, payload.active_market_refresh.run_id);
      }
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "加载持仓检阅失败");
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  const loadMorningMeeting = useCallback(async () => {
    setLoadingMandate(true);
    try {
      const [nextMandate, runs] = await Promise.all([
        api.getPortfolioMandate(),
        api.listPortfolioDailyRuns(1),
      ]);
      setMandate(nextMandate);
      setDailyRun(runs.runs[0] || null);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "加载组合晨会设置失败");
    } finally {
      setLoadingMandate(false);
    }
  }, []);

  useEffect(() => {
    void Promise.all([loadReview("initial"), loadMorningMeeting()]);
  }, [loadReview, loadMorningMeeting]);

  useEffect(() => {
    if (!dailyRun || !ACTIVE_DAILY_RUN_STATUSES.has(dailyRun.status)) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const poll = async () => {
      try {
        const next = await api.getPortfolioDailyRun(dailyRun.run_id);
        if (cancelled) return;
        setDailyRun(next);
        if (!ACTIVE_DAILY_RUN_STATUSES.has(next.status)) {
          if (next.status === "completed") toast.success("组合晨会报告已生成。");
          else if (next.status === "completed_with_warnings") {
            toast.warning(
              next.stage === "skipped_data_unavailable"
                ? "数据覆盖不足，已跳过报告与模型分析。"
                : "组合晨会已完成，部分标的使用保守降级结果。",
            );
          }
          else if (next.status === "failed") toast.error(next.error || "组合晨会生成失败。");
          return;
        }
        timer = setTimeout(poll, 1500);
      } catch {
        if (!cancelled) timer = setTimeout(poll, 2500);
      }
    };
    timer = setTimeout(poll, 600);
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [dailyRun?.run_id, dailyRun?.status]);

  useEffect(() => {
    const code = tradeForm.code.trim().toUpperCase();
    if (!isCompleteSecurityCode(code)) {
      setTradeLookup({ status: "idle" });
      setTradeForm((current) => (
        current.symbol || current.name ? { ...current, symbol: "", name: "" } : current
      ));
      return;
    }

    const controller = new AbortController();
    let cancelled = false;
    setTradeLookup({ status: "loading", code });
    const timer = window.setTimeout(() => {
      void api.lookupPortfolioSecurity(code, controller.signal)
        .then((security) => {
          if (cancelled) return;
          setTradeForm((current) => (
            current.code.trim().toUpperCase() === code
              ? { ...current, code: security.code, symbol: security.symbol, name: security.name }
              : current
          ));
          setTradeSuggestionsOpen(false);
          setTradeLookup({ status: "resolved", code, message: `${security.name} · ${security.symbol}` });
        })
        .catch((error) => {
          if (cancelled || (error instanceof Error && error.name === "AbortError")) return;
          setTradeForm((current) => (
            current.code.trim().toUpperCase() === code ? { ...current, symbol: "", name: "" } : current
          ));
          setTradeLookup({
            status: "error",
            code,
            message: error instanceof Error ? error.message : "未找到该证券，请检查代码。",
          });
        });
    }, 300);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [tradeForm.code]);

  useEffect(() => {
    const timer = window.setInterval(() => setAnalysisClock(Date.now()), 60_000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    const storedRunId = localStorage.getItem(ACTIVE_REFRESH_KEY);
    if (!storedRunId) return;
    let cancelled = false;
    void api.getMarketCacheRun(storedRunId)
      .then((run) => {
        if (!cancelled) setRefreshRun(run);
      })
      .catch(() => {
        localStorage.removeItem(ACTIVE_REFRESH_KEY);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!refreshRun || TERMINAL_REFRESH_STATUSES.has(refreshRun.status)) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const poll = async () => {
      try {
        const next = await api.getMarketCacheRun(refreshRun.run_id);
        if (cancelled) return;
        setRefreshRun(next);
        if (TERMINAL_REFRESH_STATUSES.has(next.status)) {
          localStorage.removeItem(ACTIVE_REFRESH_KEY);
          if (completedRefreshRef.current !== next.run_id) {
            completedRefreshRef.current = next.run_id;
            await loadReview("refresh");
            const singleSymbol = next.profile === "symbol_detail" && next.symbols.length === 1
              ? next.symbols[0]
              : null;
            const singleSymbolDisplay = singleSymbol
              ? securityDisplayName(singleSymbol, holdingNameByIdentifier)
              : null;
            if (next.status === "completed") {
              toast.success(singleSymbolDisplay ? `${singleSymbolDisplay} 行情缓存已刷新完成。` : "持仓行情与分层缓存已刷新完成。");
            } else if (next.status === "partial") {
              toast.warning(singleSymbolDisplay ? `${singleSymbolDisplay} 刷新完成，但有部分数据源失败。` : "行情刷新已完成，但有部分数据源失败。");
            }
            else toast.error(next.error || "行情刷新未完成。");
          }
          return;
        }
        timer = setTimeout(poll, 1000);
      } catch {
        if (!cancelled) timer = setTimeout(poll, 2000);
      }
    };
    timer = setTimeout(poll, 500);
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [holdingNameByIdentifier, loadReview, refreshRun]);

  useEffect(() => {
    try {
      localStorage.setItem(ANALYSIS_RUNS_STORAGE_KEY, JSON.stringify(analysisRuns));
    } catch {
      // Analysis results remain reachable through the Agent session even when storage is unavailable.
    }
  }, [analysisRuns]);

  const activeAnalysisRuns = useMemo(
    () => Object.entries(analysisRuns).filter(([, run]) => isActiveAnalysis(run)),
    [analysisRuns],
  );
  const activeAnalysisSignature = activeAnalysisRuns
    .map(([key, run]) => `${key}:${run.analysis_id}:${run.status}`)
    .sort()
    .join("|");

  useEffect(() => {
    if (activeAnalysisRuns.length === 0) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const poll = async () => {
      const results = await Promise.all(
        activeAnalysisRuns.map(async ([key, run]) => {
          try {
            return [key, await api.getPortfolioAnalysis(run.analysis_id)] as const;
          } catch {
            return null;
          }
        }),
      );
      if (cancelled) return;
      setAnalysisRuns((current) => {
        let changed = false;
        const next = { ...current };
        for (const result of results) {
          if (!result) continue;
          const [key, run] = result;
          if (next[key]?.analysis_id !== run.analysis_id || next[key]?.status !== run.status || next[key]?.error !== run.error) {
            next[key] = run;
            changed = true;
          }
        }
        return changed ? next : current;
      });
      if (!cancelled) timer = setTimeout(poll, 1500);
    };

    void poll();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [activeAnalysisSignature]);

  const startMarketRefresh = async (symbol?: string) => {
    const count = review?.portfolio_state.holdings.length || 0;
    if (count === 0) {
      toast.error("请先保存持仓，再刷新行情。");
      return;
    }
    try {
      const payload = await api.startMarketCacheRefresh(
        symbol
          ? { symbols: [symbol], profile: "symbol_detail" }
          : { profile: "portfolio_default" },
      );
      setRefreshRun(payload.run);
      completedRefreshRef.current = null;
      localStorage.setItem(ACTIVE_REFRESH_KEY, payload.run_id);
      toast.success(
        payload.deduplicated
          ? "已恢复正在进行的行情刷新。"
          : symbol
            ? `已开始刷新 ${securityDisplayName(symbol, holdingNameByIdentifier)}。`
            : `已开始刷新 ${count} 个持仓标的。`,
      );
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "刷新行情失败");
    }
  };

  const refreshMarketData = () => void startMarketRefresh();

  const saveMorningMeetingMandate = async (nextMandate: PortfolioMandate) => {
    setSavingMandate(true);
    try {
      const saved = await api.updatePortfolioMandate(nextMandate);
      setMandate(saved);
      toast.success("组合目标已保存，下次晨会按新版本执行。");
      return true;
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "保存组合目标失败");
      return false;
    } finally {
      setSavingMandate(false);
    }
  };

  const savePortfolioCash = async (nextCash: number, currency: string) => {
    setSavingCash(true);
    try {
      const saved = await api.updatePortfolioCash({ cash: nextCash, cash_currency: currency });
      setReview(saved);
      toast.success("当前现金已更新，不会改动持仓。");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "更新现金失败");
    } finally {
      setSavingCash(false);
    }
  };

  const updateMorningMeetingAssignment = async (symbol: string, sleeveId: string) => {
    try {
      const saved = await api.updatePortfolioAssignment(symbol, sleeveId);
      toast.success(`${securityDisplayName(symbol, holdingNameByIdentifier)} 分区已锁定。`);
      return saved;
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "更新持仓分区失败");
      return null;
    }
  };

  const startMorningMeeting = async () => {
    setStartingDailyRun(true);
    try {
      const run = await api.startPortfolioDailyRun({ refresh_policy: "ensure_fresh" });
      setDailyRun(run);
      toast.success(run.deduplicated ? "已恢复今天相同输入的组合晨会。" : "组合晨会已启动。");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "启动组合晨会失败");
    } finally {
      setStartingDailyRun(false);
    }
  };

  const retryMorningMeeting = async () => {
    if (!dailyRun) return;
    setStartingDailyRun(true);
    try {
      const legacyDataLimited = (
        !dailyRun.analysis_gate
        && ["limited", "offline"].includes(dailyRun.data_status || "")
      );
      const run = legacyDataLimited
        ? await api.startPortfolioDailyRun({ refresh_policy: "ensure_fresh", force_new: true })
        : await api.retryPortfolioDailyRun(dailyRun.run_id);
      setDailyRun(run);
      toast.success("已重新获取数据；只有数据门禁通过后才会启动报告模型。");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "重试组合晨会失败");
    } finally {
      setStartingDailyRun(false);
    }
  };

  const retryMorningMeetingHolding = async (symbol: string) => {
    if (!dailyRun) return;
    setStartingDailyRun(true);
    try {
      const run = await api.retryPortfolioDailyRun(dailyRun.run_id, symbol);
      setDailyRun(run);
      toast.success(`${securityDisplayName(symbol, holdingNameByIdentifier)} 已进入单股重试；组合结论和综合 PDF 会同步生成新 revision。`);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "重试个股日报失败");
    } finally {
      setStartingDailyRun(false);
    }
  };

  const cancelMorningMeeting = async () => {
    if (!dailyRun) return;
    try {
      setDailyRun(await api.cancelPortfolioDailyRun(dailyRun.run_id));
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "取消组合晨会失败");
    }
  };

  const startAnalysis = async (scope: PortfolioAnalysisScope, holding?: PortfolioHolding) => {
    const symbol = scope === "holding" ? holdingSymbol(holding || {}) : undefined;
    const key = analysisTargetKey(scope, symbol);
    const existing = analysisRuns[key];
    if (existing && isActiveAnalysis(existing)) {
      navigate(`/agent?session=${encodeURIComponent(existing.session_id)}`);
      return;
    }
    if (scope === "holding" && !symbol) {
      toast.error("该持仓缺少可分析的证券标识。");
      return;
    }
    setStartingAnalysisKey(key);
    try {
      const run = await api.startPortfolioAnalysis({ scope, symbol });
      setAnalysisRuns((current) => ({ ...current, [key]: run }));
      const successMessage = scope === "portfolio"
        ? "全持仓报告已在后台启动。"
        : scope === "market"
          ? `${run.analysis_phase === "intraday" ? "盘中分析" : "盘前分析"}已在后台启动。`
          : "持仓分析已在后台启动。";
      toast.success(successMessage);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "启动持仓分析失败");
    } finally {
      setStartingAnalysisKey(null);
    }
  };

  const openAnalysis = (run: PortfolioAnalysisSession) => {
    navigate(`/agent?session=${encodeURIComponent(run.session_id)}`);
  };

  const refreshingMarket = Boolean(refreshRun && !TERMINAL_REFRESH_STATUSES.has(refreshRun.status));
  const refreshingSymbol = refreshingMarket
    && refreshRun?.profile === "symbol_detail"
    && refreshRun.symbols.length === 1
    ? refreshRun.symbols[0]
    : null;
  const portfolioAnalysisKey = analysisTargetKey("portfolio");
  const portfolioAnalysisRun = analysisRuns[portfolioAnalysisKey];
  const marketAnalysisKey = analysisTargetKey("market");
  const marketAnalysisRun = analysisRuns[marketAnalysisKey];
  const marketAnalysisLabel = getMarketAnalysisLabel(new Date(analysisClock));

  const summary = useMemo(() => {
    const holdings = review?.portfolio_state.holdings || [];
    const caches = cacheRowsForHoldings(review?.verified_market_cache || [], holdings);
    const totalMarketValue = holdings.reduce((sum, row) => {
      const value = typeof row.market_value === "number" ? row.market_value : 0;
      return sum + value;
    }, 0);
    return {
      holdingCount: holdings.length,
      tradeCount: review?.portfolio_state.recent_trades.length || 0,
      cacheCount: caches.length,
      conflictCount: caches.filter((row) => row.status === "unresolved_conflict").length,
      singleSourceCount: caches.filter((row) => row.status === "single_source").length,
      lagCount: caches.filter((row) => row.status === "source_lag").length,
      provisionalCount: caches.filter((row) => row.status === "provisional_mix").length,
      basisMismatchCount: caches.filter((row) => row.status === "basis_mismatch").length,
      totalMarketValue,
    };
  }, [review]);

  const tradeSuggestions = useMemo(() => {
    const holdings = review?.portfolio_state.holdings || [];
    const query = tradeForm.code.trim() || tradeForm.symbol.trim() || tradeForm.name.trim();
    if (!query) return [];
    const exact = findExactHolding(holdings, query);
    if (exact && tradeForm.symbol.trim() && tradeForm.name.trim()) return [];
    return holdings.filter((row) => matchesHolding(row, query)).slice(0, 6);
  }, [review, tradeForm.code, tradeForm.symbol, tradeForm.name]);

  const applyTradeSuggestion = (row: PortfolioHolding) => {
    setTradeForm((state) => ({
      ...state,
      code: holdingCode(row),
      symbol: holdingSymbol(row),
      name: String(row.name || ""),
    }));
    setTradeSuggestionsOpen(false);
  };

  const saveHoldings = async (event: FormEvent) => {
    event.preventDefault();
    if (!holdingsText.trim()) {
      toast.error("请先粘贴券商持仓表。");
      return;
    }
    setSavingHoldings(true);
    try {
      const payload = await api.updatePortfolioHoldings({
        raw_text: holdingsText,
      });
      setReview(payload);
      setHoldingsText("");
      toast.success(`持仓已保存到 ${payload.portfolio_path}`);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "保存持仓失败");
    } finally {
      setSavingHoldings(false);
    }
  };

  const saveTrade = async (event: FormEvent) => {
    event.preventDefault();
    const code = tradeForm.code.trim().toUpperCase();
    if (!isCompleteSecurityCode(code)) {
      toast.error("请输入完整的 6 位 A 股代码或美股 ticker。");
      return;
    }
    if (tradeLookup.status === "loading") {
      toast.error("证券信息正在补全，请稍候。");
      return;
    }
    if (!tradeForm.symbol.trim() || !tradeForm.name.trim()) {
      toast.error("证券代码尚未成功匹配名称，不能提交。");
      return;
    }
    const quantity = Number(tradeForm.quantity);
    const price = Number(tradeForm.price);
    if (!Number.isFinite(quantity) || quantity <= 0) {
      toast.error("请输入大于 0 的交易数量。");
      return;
    }
    if (!Number.isFinite(price) || price <= 0) {
      toast.error("请输入大于 0 的交易价格。");
      return;
    }
    setSavingTrade(true);
    try {
      const payload = await api.recordPortfolioTrade({
        code,
        symbol: tradeForm.symbol.trim(),
        name: tradeForm.name.trim(),
        side: tradeForm.side,
        quantity,
        price,
        trade_date: tradeForm.trade_date.trim() || undefined,
        fees: tradeForm.fees.trim() ? Number(tradeForm.fees) : undefined,
        taxes: tradeForm.taxes.trim() ? Number(tradeForm.taxes) : undefined,
        broker_reported_pnl: tradeForm.broker_reported_pnl.trim()
          ? Number(tradeForm.broker_reported_pnl)
          : undefined,
        idempotency_key: crypto.randomUUID(),
        notes: tradeForm.notes.trim() || undefined,
      });
      setReview(payload);
      setTradeForm(createEmptyTradeForm());
      setTradeLookup({ status: "idle" });
      setTradeSuggestionsOpen(false);
      toast.success("交易已记录，持仓已同步。");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "记录交易失败");
    } finally {
      setSavingTrade(false);
    }
  };

  const updateHolding = async (symbol: string, quantity: number, costPrice: number) => {
    try {
      const payload = await api.editPortfolioHolding(symbol, { quantity, cost_price: costPrice });
      setReview(payload);
      toast.success(`${securityDisplayName(symbol, holdingNameByIdentifier)} 的持有股数和成本已更新。`);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "更新持仓失败");
      throw error;
    }
  };

  const removeTrade = async (trade: PortfolioTrade) => {
    const tradeId = String(trade.trade_id || "");
    if (!tradeId) {
      toast.error("该交易事件缺少标识，无法追加冲销标记。");
      return;
    }
    const symbol = String(trade.symbol || trade.code || "");
    const displayName = symbol
      ? securityDisplayName(symbol, holdingNameByIdentifier, trade.name)
      : "该证券";
    if (!window.confirm(`确认给 ${displayName} 的这条交易追加冲销标记？\n\n历史事件不会删除；持仓调整仍需通过券商对账明确确认。`)) return;

    setDeletingTradeId(tradeId);
    try {
      const payload = await api.reversePortfolioTrade(tradeId);
      setReview(payload);
      toast.success("冲销标记已追加；历史事件和持仓审计轨迹均已保留。");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "追加冲销标记失败");
    } finally {
      setDeletingTradeId(null);
    }
  };

  return (
    <div className="min-h-full p-6 lg:p-8">
      <div className="mx-auto flex w-full max-w-7xl flex-col gap-6">
        <section className="flex flex-col gap-4 border-b pb-6 lg:flex-row lg:items-end lg:justify-between">
          <div className="space-y-3">
            <div className="inline-flex items-center gap-2 rounded-md border px-2.5 py-1 text-xs font-medium text-muted-foreground">
              <BriefcaseBusiness className="h-3.5 w-3.5" />
              组合 / 持仓
            </div>
            <div>
              <h1 className="text-3xl font-bold tracking-tight">组合 / 持仓</h1>
              <p className="mt-2 max-w-3xl text-sm text-muted-foreground">
                管理结构化持仓、最近交易记录，并检阅多源校核行情缓存与复权口径。
              </p>
            </div>
          </div>
          <div className="flex flex-wrap gap-2">
            <PortfolioAnalysisButton
              run={portfolioAnalysisRun}
              starting={startingAnalysisKey === portfolioAnalysisKey}
              empty={summary.holdingCount === 0}
              ariaLabel="全持仓报告"
              onStart={() => void startAnalysis("portfolio")}
              onOpen={openAnalysis}
              primary
              idleLabel="全持仓报告"
            />
            <PortfolioAnalysisButton
              run={marketAnalysisRun}
              starting={startingAnalysisKey === marketAnalysisKey}
              empty={summary.holdingCount === 0}
              ariaLabel={marketAnalysisLabel}
              onStart={() => void startAnalysis("market")}
              onOpen={openAnalysis}
              primary
              idleLabel={marketAnalysisLabel}
            />
            <button
              type="button"
              onClick={refreshMarketData}
              disabled={refreshingMarket || loading}
              className="inline-flex items-center gap-2 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition disabled:opacity-50"
            >
              {refreshingMarket ? <Loader2 className="h-4 w-4 animate-spin" /> : <Database className="h-4 w-4" />}
              刷新持仓行情
            </button>
            <button
              type="button"
              onClick={() => loadReview("refresh")}
              disabled={refreshing}
              className="inline-flex items-center gap-2 rounded-md border px-4 py-2 text-sm font-medium transition hover:bg-muted disabled:opacity-50"
            >
              {refreshing ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
              刷新页面
            </button>
          </div>
        </section>

        {refreshRun ? <RefreshProgress run={refreshRun} holdingNames={holdingNameByIdentifier} /> : null}

        <PortfolioDailyRunPanel
          mandate={mandate}
          holdings={review?.portfolio_state.holdings || []}
          currentCash={review?.portfolio_state.cash ?? null}
          cashCurrency={review?.portfolio_state.cash_currency || "CNY"}
          run={dailyRun}
          loading={loadingMandate}
          saving={savingMandate}
          savingCash={savingCash}
          starting={startingDailyRun}
          onSave={saveMorningMeetingMandate}
          onSaveCash={savePortfolioCash}
          onAssign={updateMorningMeetingAssignment}
          onStart={startMorningMeeting}
          onRetry={retryMorningMeeting}
          onRetryHolding={retryMorningMeetingHolding}
          onCancel={cancelMorningMeeting}
          artifactUrl={api.portfolioDailyRunArtifactUrl}
        />

        <PortfolioMonitorPanel
          holdings={review?.portfolio_state.holdings || []}
          selectedSymbols={appliedMonitorSymbols}
          manualSymbols={appliedManualMonitorSymbols}
          autonomousSymbols={appliedAutonomousMonitorSymbols}
          selectionRevision={monitorSelectionRevision}
          selectionHydrationPending={monitorSelectionHydrationPending}
          onHydrateSelection={({ manual, autonomous }) => {
            const hydrated = new Map<string, PortfolioMonitorLevel>();
            manual.forEach((symbol) => hydrated.set(symbol, "manual"));
            autonomous.forEach((symbol) => hydrated.set(symbol, "autonomous"));
            setAppliedMonitorLevels(hydrated);
            setDraftMonitorLevels(new Map(hydrated));
            setMonitorLevelApplyAt(null);
            setMonitorSelectionHydrationPending(false);
          }}
        />

        {loading ? (
          <div className="grid gap-3 md:grid-cols-4">
            {[1, 2, 3, 4].map((item) => (
              <div key={item} className="h-24 animate-pulse rounded-md border bg-muted/40" />
            ))}
          </div>
        ) : null}

        {!loading ? (
          <>
            <section className="grid gap-3 md:grid-cols-2 xl:grid-cols-10">
              <SummaryTile label="持仓数" value={String(summary.holdingCount)} />
              <SummaryTile label="现金" value={`${fmtNumber(review?.portfolio_state.cash, 2)} ${review?.portfolio_state.cash_currency || "CNY"}`} />
              <SummaryTile label="总市值" value={fmtNumber(summary.totalMarketValue, 2)} />
              <SummaryTile label="交易记录" value={String(summary.tradeCount)} />
              <SummaryTile label="缓存数" value={String(summary.cacheCount)} />
              <SummaryTile label="冲突缓存" value={String(summary.conflictCount)} danger={summary.conflictCount > 0} />
              <SummaryTile label="单一来源" value={String(summary.singleSourceCount)} />
              <SummaryTile label="来源延迟" value={String(summary.lagCount)} />
              <SummaryTile label="盘中旧值" value={String(summary.provisionalCount)} />
              <SummaryTile label="口径不符" value={String(summary.basisMismatchCount)} />
            </section>
            <section className="flex flex-wrap items-center gap-x-5 gap-y-2 rounded-md border bg-muted/20 px-3 py-2 text-xs text-muted-foreground">
              <span>账本版本：{review?.portfolio_state.revision ?? 0}</span>
              <span>权威存储：{String(review?.portfolio_state.provenance?.authoritative_store || "sqlite")}</span>
              <span>收益口径：{review?.portfolio_state.performance?.status || "unavailable"}</span>
              <span>券商报告盈亏：{fmtNumber(review?.portfolio_state.performance?.broker_reported_pnl, 2)}</span>
              <span>费用与税费：{fmtNumber(review?.portfolio_state.performance?.fees_and_taxes, 2)}</span>
            </section>

            <section className="grid gap-4 lg:grid-cols-[minmax(0,1.2fr)_minmax(320px,0.8fr)]">
              <form onSubmit={saveHoldings} className="rounded-md border p-4">
                <div className="mb-3 flex items-center justify-between gap-3">
                  <h2 className="text-sm font-semibold">粘贴持仓表</h2>
                  <span className="truncate text-xs text-muted-foreground">{review?.portfolio_path}</span>
                </div>
                <textarea
                  value={holdingsText}
                  onChange={(event) => setHoldingsText(event.target.value)}
                  rows={7}
                  placeholder="支持完整券商表，也支持：名称 证券代码 持仓数量 成本价；个股无ETF 会按已知别名补全代码。"
                  className="w-full resize-y rounded-md border bg-background px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-primary/30"
                />
                <div className="mt-3 flex justify-end">
                  <button
                    type="submit"
                    disabled={savingHoldings}
                    className="inline-flex items-center justify-center gap-2 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground disabled:opacity-50"
                  >
                    {savingHoldings ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
                    保存持仓
                  </button>
                </div>
              </form>

              <form onSubmit={saveTrade} className="rounded-md border p-4">
                <h2 className="mb-3 text-sm font-semibold">记录交易</h2>
                <div className="grid gap-2">
                  <div className="grid grid-cols-2 gap-2">
                    <div className={`relative ${tradeSuggestionsOpen && tradeSuggestions.length > 0 ? "z-30" : ""}`}>
                      <input
                        className="relative z-30 w-full rounded-md border bg-background px-3 py-2 pr-9 text-sm"
                        placeholder="代码，如 588870 或 AAPL"
                        aria-label="证券代码或美股 ticker"
                        inputMode="text"
                        maxLength={16}
                        value={tradeForm.code}
                        onFocus={() => setTradeSuggestionsOpen(true)}
                        onChange={(e) => {
                          const code = sanitizeSecurityCode(e.target.value);
                          setTradeForm((s) => ({ ...s, code, symbol: "", name: "" }));
                          setTradeSuggestionsOpen(true);
                        }}
                        onKeyDown={(e) => {
                          if (e.key === "Escape") setTradeSuggestionsOpen(false);
                        }}
                      />
                      {tradeLookup.status === "loading" ? (
                        <Loader2 aria-label="正在联网补全证券信息" className="absolute right-3 top-2.5 z-40 h-4 w-4 animate-spin text-muted-foreground" />
                      ) : tradeLookup.status === "resolved" ? (
                        <Check aria-label="证券信息已补全" className="absolute right-3 top-2.5 z-40 h-4 w-4 text-emerald-500" />
                      ) : null}
                      {tradeSuggestionsOpen && tradeSuggestions.length > 0 ? (
                        <>
                          <button
                            type="button"
                            aria-label="关闭代码候选"
                            className="fixed inset-0 z-20 cursor-default bg-background/70 backdrop-blur-[1px]"
                            onClick={() => setTradeSuggestionsOpen(false)}
                          />
                          <div className="absolute left-0 right-0 z-40 mt-1 max-h-56 overflow-auto rounded-md border bg-card p-1 shadow-xl">
                          {tradeSuggestions.map((row) => (
                            <button
                              type="button"
                              key={holdingSymbol(row)}
                              onClick={() => applyTradeSuggestion(row)}
                              className="flex w-full items-center justify-between gap-3 rounded px-2 py-1.5 text-left text-xs hover:bg-muted"
                            >
                              <span className="truncate">{fmtText(row.name)}</span>
                              <span className="shrink-0 font-mono text-muted-foreground">
                                {holdingCode(row)} {holdingSymbol(row) ? `(${holdingSymbol(row)})` : ""}
                              </span>
                            </button>
                          ))}
                          </div>
                        </>
                      ) : null}
                    </div>
                    <input className="rounded-md border bg-muted/30 px-3 py-2 text-sm" readOnly aria-label="自动补全的证券标识" placeholder="证券标识（自动补全）" value={tradeForm.symbol} />
                  </div>
                  <input className="rounded-md border bg-muted/30 px-3 py-2 text-sm" readOnly aria-label="自动补全的证券名称" placeholder="名称（自动补全）" value={tradeForm.name} />
                  {tradeLookup.status !== "idle" ? (
                    <p
                      className={cn(
                        "text-xs",
                        tradeLookup.status === "error" ? "text-destructive" : "text-muted-foreground",
                      )}
                      aria-live="polite"
                    >
                      {tradeLookup.status === "loading" ? "正在联网识别证券…" : tradeLookup.message}
                    </p>
                  ) : null}
                  <div className="grid grid-cols-3 gap-2">
                    <select className="rounded-md border bg-background px-3 py-2 text-sm" value={tradeForm.side} onChange={(e) => setTradeForm((s) => ({ ...s, side: e.target.value as "buy" | "sell" }))}>
                      <option value="buy">买入</option>
                      <option value="sell">卖出</option>
                    </select>
                    <input className="rounded-md border bg-background px-3 py-2 text-sm" type="number" min="0" step="any" required inputMode="decimal" placeholder="数量" value={tradeForm.quantity} onChange={(e) => setTradeForm((s) => ({ ...s, quantity: e.target.value }))} />
                    <input className="rounded-md border bg-background px-3 py-2 text-sm" type="number" min="0" step="any" required inputMode="decimal" placeholder="价格" value={tradeForm.price} onChange={(e) => setTradeForm((s) => ({ ...s, price: e.target.value }))} />
                  </div>
                  <div className="grid grid-cols-3 gap-2">
                    <input className="rounded-md border bg-background px-3 py-2 text-sm" type="number" min="0" step="any" placeholder="手续费（可选）" value={tradeForm.fees} onChange={(e) => setTradeForm((s) => ({ ...s, fees: e.target.value }))} />
                    <input className="rounded-md border bg-background px-3 py-2 text-sm" type="number" min="0" step="any" placeholder="税费（可选）" value={tradeForm.taxes} onChange={(e) => setTradeForm((s) => ({ ...s, taxes: e.target.value }))} />
                    <input className="rounded-md border bg-background px-3 py-2 text-sm" type="number" step="any" placeholder="券商净盈亏（可选）" value={tradeForm.broker_reported_pnl} onChange={(e) => setTradeForm((s) => ({ ...s, broker_reported_pnl: e.target.value }))} />
                  </div>
                  <div className="grid grid-cols-[1fr_auto] gap-2">
                    <input
                      className="rounded-md border bg-background px-3 py-2 text-sm"
                      type="date"
                      placeholder="交易日期"
                      value={tradeForm.trade_date}
                      onChange={(e) => setTradeForm((s) => ({ ...s, trade_date: e.target.value }))}
                    />
                    <div className="grid grid-rows-2 overflow-hidden rounded-md border">
                      <button
                        type="button"
                        className="flex h-5 w-8 items-center justify-center border-b text-muted-foreground hover:bg-muted hover:text-foreground"
                        title="日期加一天"
                        onClick={() => setTradeForm((s) => ({ ...s, trade_date: shiftDate(s.trade_date, 1) }))}
                      >
                        <ChevronUp className="h-3.5 w-3.5" />
                      </button>
                      <button
                        type="button"
                        className="flex h-5 w-8 items-center justify-center text-muted-foreground hover:bg-muted hover:text-foreground"
                        title="日期减一天"
                        onClick={() => setTradeForm((s) => ({ ...s, trade_date: shiftDate(s.trade_date, -1) }))}
                      >
                        <ChevronDown className="h-3.5 w-3.5" />
                      </button>
                    </div>
                  </div>
                  <textarea className="rounded-md border bg-background px-3 py-2 text-sm" rows={2} placeholder="备注" value={tradeForm.notes} onChange={(e) => setTradeForm((s) => ({ ...s, notes: e.target.value }))} />
                  <button type="submit" disabled={savingTrade || tradeLookup.status === "loading"} className="inline-flex items-center justify-center gap-2 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground disabled:opacity-50">
                    {savingTrade || tradeLookup.status === "loading" ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
                    保存交易
                  </button>
                </div>
              </form>
            </section>

            <PortfolioReconciliationPanel
              currentRevision={review?.portfolio_state.revision || 0}
              onCommitted={() => loadReview("refresh")}
            />

            <HoldingsTable
              review={review}
              monitorLevels={draftMonitorLevels}
              pendingMonitorSymbols={pendingMonitorSymbols}
              onCycleMonitorLevel={(symbol) => queueMonitorLevels(
                [symbol],
                nextMonitorLevel(draftMonitorLevels.get(symbol) || "off"),
              )}
              onSetAllMonitorLevels={queueMonitorLevels}
              analysisRuns={analysisRuns}
              startingAnalysisKey={startingAnalysisKey}
              onStart={(holding) => void startAnalysis("holding", holding)}
              onOpen={openAnalysis}
              onUpdate={updateHolding}
            />
            <TradesTable
              review={review}
              holdingNames={holdingNameByIdentifier}
              deletingTradeId={deletingTradeId}
              onDelete={(trade) => void removeTrade(trade)}
            />
            <CacheTable
              review={review}
              expandedCachePath={expandedCachePath}
              onToggle={(path) => setExpandedCachePath((current) => (current === path ? null : path))}
              onOpenChart={setChartCacheGroup}
              onOpenMarketDetails={(group) => navigate(
                `/data-center?symbol=${encodeURIComponent(group.symbol)}`
                + (group.name ? `&name=${encodeURIComponent(group.name)}` : ""),
              )}
              onRefreshSymbol={(symbol) => void startMarketRefresh(symbol)}
              refreshingSymbol={refreshingSymbol}
              refreshDisabled={refreshingMarket}
            />
          </>
        ) : null}
      </div>
      {chartCacheGroup ? (
        <MarketCacheKlineDialog
          key={chartCacheGroup.symbol}
          symbol={chartCacheGroup.symbol}
          name={chartCacheGroup.name}
          cacheRows={chartCacheGroup.rows}
          onClose={() => setChartCacheGroup(null)}
        />
      ) : null}
    </div>
  );
}

function SummaryTile({ label, value, danger = false }: { label: string; value: string; danger?: boolean }) {
  return (
    <div className={cn("rounded-md border p-4", danger && "border-destructive/40 bg-destructive/5")}>
      <div className="text-xs font-medium uppercase text-muted-foreground">{label}</div>
      <div className={cn("mt-2 truncate text-xl font-semibold", danger && "text-destructive")}>{value}</div>
    </div>
  );
}

function refreshStatusLabel(status: string): string {
  const labels: Record<string, string> = {
    queued: "等待中",
    running: "刷新中",
    fetching: "获取中",
    verifying: "校核中",
    completed: "已完成",
    partial: "部分完成",
    verified: "已校核",
    single_source: "单一来源",
    source_lag: "来源延迟",
    provisional_mix: "盘中旧值已隔离",
    basis_mismatch: "复权口径不符",
    unresolved_conflict: "未解决冲突",
    stale: "已过期",
    conflict: "旧版冲突",
    unresolved: "未解析",
    failed: "失败",
    interrupted: "已中断",
  };
  return labels[status] || status;
}

function RefreshProgress({
  run,
  holdingNames,
}: {
  run: MarketCacheRun;
  holdingNames: ReadonlyMap<string, string>;
}) {
  const active = !TERMINAL_REFRESH_STATUSES.has(run.status);
  return (
    <section className="border-b pb-5" aria-live="polite">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <div className="flex items-center gap-2 text-sm font-semibold">
            {active ? <Loader2 className="h-4 w-4 animate-spin" /> : <Database className="h-4 w-4" />}
            行情缓存刷新
            <StatusPill status={run.status} />
          </div>
          <p className="mt-1 text-xs text-muted-foreground">
            {run.current_symbol
              ? `正在处理 ${securityDisplayName(run.current_symbol, holdingNames)}`
              : `任务 ${run.run_id.slice(0, 8)}`}
            {run.current_source ? ` · 来源 ${run.current_source}` : ""}
          </p>
        </div>
        <div className="text-right text-xs text-muted-foreground">
          <div>{run.completed_items} / {run.total_items} 项</div>
          <div>{run.conflict_items} 个冲突 · {run.failed_items} 个失败</div>
        </div>
      </div>
      <div className="mt-3 h-1.5 overflow-hidden rounded bg-muted">
        <div
          className={cn("h-full transition-[width]", run.failed_items > 0 ? "bg-warning" : "bg-primary")}
          style={{ width: `${Math.max(0, Math.min(100, run.progress_pct || 0))}%` }}
        />
      </div>
      <div className="mt-3 max-h-52 overflow-auto rounded-md border">
        <table className="w-full min-w-[720px] text-left text-xs">
          <thead className="sticky top-0 bg-muted/90 text-[11px] text-muted-foreground backdrop-blur">
            <tr>
              <th className="px-3 py-2 font-medium">证券标识</th>
              <th className="px-3 py-2 font-medium">周期</th>
              <th className="px-3 py-2 font-medium">复权</th>
              <th className="px-3 py-2 font-medium">状态</th>
              <th className="px-3 py-2 font-medium">实际来源</th>
              <th className="px-3 py-2 text-right font-medium">写入行数</th>
            </tr>
          </thead>
          <tbody>
            {run.items.map((item) => (
              <tr key={`${item.symbol}-${item.interval}-${item.adjustment}`} className="border-t">
                <td className="px-3 py-2 font-medium">{securityDisplayName(item.symbol, holdingNames)}</td>
                <td className="px-3 py-2 font-mono">{item.interval}</td>
                <td className="px-3 py-2 font-mono">{item.adjustment}</td>
                <td className="px-3 py-2"><StatusPill status={item.status} /></td>
                <td className="px-3 py-2">{item.actual_sources.join(", ") || item.requested_sources.join(", ") || "-"}</td>
                <td className="px-3 py-2 text-right font-mono">{item.rows_written || 0}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function HoldingsTable({
  review,
  monitorLevels,
  pendingMonitorSymbols,
  onCycleMonitorLevel,
  onSetAllMonitorLevels,
  analysisRuns,
  startingAnalysisKey,
  onStart,
  onOpen,
  onUpdate,
}: {
  review: PortfolioReview | null;
  monitorLevels: ReadonlyMap<string, PortfolioMonitorLevel>;
  pendingMonitorSymbols: ReadonlySet<string>;
  onCycleMonitorLevel: (symbol: string) => void;
  onSetAllMonitorLevels: (symbols: string[], level: PortfolioMonitorLevel) => void;
  analysisRuns: Record<string, PortfolioAnalysisSession>;
  startingAnalysisKey: string | null;
  onStart: (holding: PortfolioHolding) => void;
  onOpen: (run: PortfolioAnalysisSession) => void;
  onUpdate: (symbol: string, quantity: number, costPrice: number) => Promise<void>;
}) {
  const holdings = review?.portfolio_state.holdings || [];
  const [sort, setSort] = useState<HoldingSort | null>(null);
  const [editingSymbol, setEditingSymbol] = useState<string | null>(null);
  const [draftQuantity, setDraftQuantity] = useState("");
  const [draftCostPrice, setDraftCostPrice] = useState("");
  const [savingEdit, setSavingEdit] = useState(false);
  const sortedHoldings = useMemo(() => {
    if (!sort) return holdings;
    return holdings
      .map((row, index) => ({ row, index }))
      .sort((left, right) => compareHoldingRows(left.row, right.row, sort) || left.index - right.index)
      .map(({ row }) => row);
  }, [holdings, sort]);
  const selectableSymbols = useMemo(() => holdings.map(holdingSymbol).filter(Boolean), [holdings]);
  const batchMonitorLevel = useMemo<PortfolioMonitorLevel | "mixed">(() => {
    if (!selectableSymbols.length) return "off";
    const levels = new Set(selectableSymbols.map((symbol) => monitorLevels.get(symbol) || "off"));
    return levels.size === 1 ? Array.from(levels)[0] : "mixed";
  }, [monitorLevels, selectableSymbols]);
  const nextBatchMonitorLevel = batchMonitorLevel === "manual"
    ? "autonomous"
    : batchMonitorLevel === "autonomous" ? "off" : "manual";

  const toggleSort = (key: HoldingSortKey) => {
    setSort((current) => {
      if (!current || current.key !== key) return { key, direction: "asc" };
      return { key, direction: current.direction === "asc" ? "desc" : "asc" };
    });
  };

  const beginEdit = (holding: PortfolioHolding) => {
    const symbol = holdingSymbol(holding);
    if (!symbol) return;
    setEditingSymbol(symbol);
    setDraftQuantity(typeof holding.quantity === "number" ? String(holding.quantity) : "");
    setDraftCostPrice(typeof holding.cost_price === "number" ? String(holding.cost_price) : "");
  };

  const cancelEdit = () => {
    setEditingSymbol(null);
    setDraftQuantity("");
    setDraftCostPrice("");
  };

  const saveEdit = async () => {
    if (!editingSymbol) return;
    const quantity = Number(draftQuantity);
    const costPrice = Number(draftCostPrice);
    if (!Number.isFinite(quantity) || quantity <= 0 || !Number.isFinite(costPrice) || costPrice <= 0) {
      toast.error("持有股数和成本价都必须大于 0。");
      return;
    }
    setSavingEdit(true);
    try {
      await onUpdate(editingSymbol, quantity, costPrice);
      cancelEdit();
    } catch {
      // Parent displays the API error; keep the row open for correction.
    } finally {
      setSavingEdit(false);
    }
  };

  return (
    <section id="portfolio-holdings" className="grid scroll-mt-6 gap-2">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-sm font-semibold">持仓矩阵</h2>
        <div className="flex flex-wrap items-center gap-3 text-[11px] text-muted-foreground" aria-label="监控等级颜色说明">
          <span><i className="mr-1 inline-block h-2 w-2 rounded-full bg-muted-foreground/50" />灰色 · 未监控</span>
          <span><i className="mr-1 inline-block h-2 w-2 rounded-full bg-blue-500" />蓝色 · 普通监控</span>
          <span><i className="mr-1 inline-block h-2 w-2 rounded-full bg-violet-500 shadow-sm shadow-violet-500" />紫色 · AI 自主</span>
          <span>连续点击循环切换，停止操作 5 秒后应用</span>
        </div>
      </div>
      <div className="overflow-auto rounded-md border">
        <table aria-label="持仓矩阵" className="w-full min-w-[1090px] text-left text-xs">
          <thead className="bg-muted/50 text-[11px] uppercase text-muted-foreground">
            <tr>
              {HOLDING_SORT_COLUMNS.map((column) => (
                <SortableHoldingHeader
                  key={column.key}
                  column={column}
                  sort={sort}
                  onSort={toggleSort}
                  monitorLevelControl={column.key === "name" ? {
                    level: batchMonitorLevel,
                    nextLevel: nextBatchMonitorLevel,
                    onChange: () => onSetAllMonitorLevels(selectableSymbols, nextBatchMonitorLevel),
                  } : undefined}
                />
              ))}
              <th scope="col" className="px-3 py-2 font-medium">Agent 分析 / 操作</th>
            </tr>
          </thead>
          <tbody>
            {holdings.length === 0 ? (
              <EmptyRow colSpan={11} text="暂无结构化持仓记录。" />
            ) : (
              sortedHoldings.map((row, index) => {
                const symbol = holdingSymbol(row);
                const isEditing = editingSymbol === symbol;
                const monitorLevel = monitorLevels.get(symbol) || "off";
                const nextLevel = nextMonitorLevel(monitorLevel);
                const monitorLevelPending = pendingMonitorSymbols.has(symbol);
                return (
                <tr key={`${row.symbol || row.code || index}`} className={cn("border-t", isEditing && "bg-muted/20")}>
                  <td className="px-3 py-2 font-medium">
                    <div className="relative flex items-center gap-2">
                    <button
                      type="button"
                      aria-label={`${symbol || fmtText(row.name)} 监控等级：${MONITOR_LEVEL_LABELS[monitorLevel]}；点击切换为${MONITOR_LEVEL_LABELS[nextLevel]}`}
                      disabled={!symbol}
                      title={`${MONITOR_LEVEL_LABELS[monitorLevel]}；点击切换为${MONITOR_LEVEL_LABELS[nextLevel]}`}
                      onClick={() => onCycleMonitorLevel(symbol)}
                      data-monitor-mode={monitorLevel}
                      data-monitor-pending={monitorLevelPending ? "true" : "false"}
                      className={cn(
                        "relative inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-full border transition-all disabled:cursor-not-allowed disabled:opacity-40",
                        monitorLevel === "manual"
                          ? "border-blue-500/70 bg-blue-500/15 text-blue-600 shadow-sm shadow-blue-500/30 dark:text-blue-300"
                          : monitorLevel === "autonomous"
                            ? "border-violet-400/80 bg-violet-500/20 text-violet-600 shadow-md shadow-violet-500/40 dark:text-violet-300"
                            : "border-muted-foreground/25 text-muted-foreground hover:border-blue-500/40 hover:bg-blue-500/5 hover:text-blue-600",
                      )}
                    >
                      <Radar aria-hidden="true" className="h-4 w-4" />
                      {monitorLevel === "autonomous" ? (
                        <span aria-hidden="true" className="pointer-events-none absolute inset-0">
                          <i className="absolute -right-0.5 -top-0.5 h-1.5 w-1.5 animate-ping rounded-full bg-fuchsia-400" />
                          <i className="absolute -bottom-1 left-0 h-1 w-1 animate-pulse rounded-full bg-violet-300" />
                          <i className="absolute -left-1 top-1 h-1 w-1 animate-pulse rounded-full bg-purple-400 [animation-delay:250ms]" />
                        </span>
                      ) : null}
                    </button>
                    <span>{fmtText(row.name)}</span>
                    {monitorLevelPending ? (
                      <span
                        role="status"
                        className={cn(
                          "pointer-events-none absolute bottom-[calc(100%+0.25rem)] left-0 z-30 whitespace-nowrap rounded border px-2 py-1 text-[10px] font-medium shadow-lg backdrop-blur-sm",
                          monitorLevel === "autonomous"
                            ? "border-violet-500/40 bg-violet-500/10 text-violet-700 dark:text-violet-200"
                            : monitorLevel === "manual"
                              ? "border-blue-500/40 bg-blue-500/10 text-blue-700 dark:text-blue-200"
                              : "border-muted bg-background text-muted-foreground",
                        )}
                      >
                        已切换到{MONITOR_LEVEL_LABELS[monitorLevel]} · 5 秒后应用
                      </span>
                    ) : null}
                    </div>
                  </td>
                  <td className="px-3 py-2 font-mono">{fmtText(row.symbol || row.code)}</td>
                  <td className="px-3 py-2 text-right font-mono">
                    {isEditing ? (
                      <input
                        aria-label={`${symbol} 当前持有股数`}
                        className="w-24 rounded border bg-background px-2 py-1 text-right font-mono"
                        type="number"
                        min="0"
                        step="any"
                        value={draftQuantity}
                        onChange={(event) => setDraftQuantity(event.target.value)}
                      />
                    ) : fmtNumber(row.quantity, 0)}
                  </td>
                  <td className="px-3 py-2 text-right font-mono">
                    {isEditing ? (
                      <input
                        aria-label={`${symbol} 当前成本价`}
                        className="w-24 rounded border bg-background px-2 py-1 text-right font-mono"
                        type="number"
                        min="0"
                        step="any"
                        value={draftCostPrice}
                        onChange={(event) => setDraftCostPrice(event.target.value)}
                      />
                    ) : fmtNumber(row.cost_price)}
                  </td>
                  <td className="px-3 py-2 text-right font-mono">{fmtNumber(row.last_price)}</td>
                  <td className="px-3 py-2 text-right font-mono">{fmtNumber(row.market_value, 2)}</td>
                  <td
                    className={cn("px-3 py-2 text-right font-mono font-medium", pnlColorClass(row.pnl))}
                    data-pnl-tone={pnlTone(row.pnl)}
                  >
                    {fmtNumber(row.pnl, 2)}
                  </td>
                  <td
                    className={cn("px-3 py-2 text-right font-mono font-medium", pnlColorClass(row.pnl_pct))}
                    data-pnl-tone={pnlTone(row.pnl_pct)}
                  >
                    {fmtNumber(row.pnl_pct, 2)}
                  </td>
                  <td className="px-3 py-2"><StatusPill status={String(row.market_status || "unresolved")} /></td>
                  <td className="px-3 py-2 font-mono">{fmtDate(row.market_verified_at)}</td>
                  <td className="px-3 py-2">
                    <div className="flex items-center gap-1.5">
                    <PortfolioAnalysisButton
                      run={analysisRuns[analysisTargetKey("holding", symbol)]}
                      starting={startingAnalysisKey === analysisTargetKey("holding", symbol)}
                      ariaLabel={`分析 ${symbol || fmtText(row.name)}`}
                      onStart={() => onStart(row)}
                      onOpen={onOpen}
                    />
                    {isEditing ? (
                      <>
                        <button
                          type="button"
                          aria-label={`保存 ${symbol} 持仓修改`}
                          disabled={savingEdit}
                          onClick={() => void saveEdit()}
                          className="inline-flex items-center rounded-md border px-2 py-1.5 text-emerald-600 hover:bg-muted disabled:opacity-50"
                        >
                          {savingEdit ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Check className="h-3.5 w-3.5" />}
                        </button>
                        <button
                          type="button"
                          aria-label={`取消 ${symbol} 持仓修改`}
                          disabled={savingEdit}
                          onClick={cancelEdit}
                          className="inline-flex items-center rounded-md border px-2 py-1.5 text-muted-foreground hover:bg-muted disabled:opacity-50"
                        >
                          <X className="h-3.5 w-3.5" />
                        </button>
                      </>
                    ) : (
                      <button
                        type="button"
                        aria-label={`编辑 ${symbol} 持仓`}
                        onClick={() => beginEdit(row)}
                        className="inline-flex items-center rounded-md border px-2 py-1.5 text-muted-foreground hover:bg-muted hover:text-foreground"
                      >
                        <Pencil className="h-3.5 w-3.5" />
                      </button>
                    )}
                    </div>
                  </td>
                </tr>
                );
              })
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function SortableHoldingHeader({
  column,
  sort,
  onSort,
  monitorLevelControl,
}: {
  column: (typeof HOLDING_SORT_COLUMNS)[number];
  sort: HoldingSort | null;
  onSort: (key: HoldingSortKey) => void;
  monitorLevelControl?: {
    level: PortfolioMonitorLevel | "mixed";
    nextLevel: PortfolioMonitorLevel;
    onChange: () => void;
  };
}) {
  const isActive = sort?.key === column.key;
  const direction = isActive ? sort.direction : null;
  const nextDirection = direction === "asc" ? "降序" : "升序";
  const SortIcon = direction === "asc" ? ArrowUp : direction === "desc" ? ArrowDown : ChevronsUpDown;
  const alignRight = Boolean("numeric" in column && column.numeric);
  return (
    <th
      scope="col"
      aria-sort={direction === "asc" ? "ascending" : direction === "desc" ? "descending" : "none"}
      className={cn("px-3 py-2 font-medium", alignRight && "text-right")}
    >
      <div className={cn("flex items-center gap-2", alignRight && "justify-end")}>
        {monitorLevelControl ? (
          <button
            type="button"
            aria-label={`批量监控等级：${monitorLevelControl.level === "mixed" ? "混合" : MONITOR_LEVEL_LABELS[monitorLevelControl.level]}；点击全部切换为${MONITOR_LEVEL_LABELS[monitorLevelControl.nextLevel]}`}
            title={`全部切换为${MONITOR_LEVEL_LABELS[monitorLevelControl.nextLevel]}，5 秒后应用`}
            onClick={monitorLevelControl.onChange}
            data-monitor-mode={monitorLevelControl.level}
            className={cn(
              "relative inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-full border transition",
              monitorLevelControl.level === "manual"
                ? "border-blue-500/70 bg-blue-500/15 text-blue-600 dark:text-blue-300"
                : monitorLevelControl.level === "autonomous"
                  ? "border-violet-400/80 bg-violet-500/20 text-violet-600 shadow-sm shadow-violet-500/40 dark:text-violet-300"
                  : "border-muted-foreground/25 text-muted-foreground hover:border-blue-500/40 hover:text-blue-600",
            )}
          >
            <Radar aria-hidden="true" className="h-3.5 w-3.5" />
            {monitorLevelControl.level === "autonomous" ? (
              <i aria-hidden="true" className="absolute -right-0.5 -top-0.5 h-1.5 w-1.5 animate-ping rounded-full bg-fuchsia-400" />
            ) : null}
          </button>
        ) : null}
        <button
          type="button"
          className={cn(
            "inline-flex min-w-0 flex-1 items-center gap-1.5 rounded-sm transition hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40",
            alignRight ? "justify-end" : "justify-start",
          )}
          aria-label={`按${column.label}${nextDirection}排序`}
          title={`按${column.label}${nextDirection}排序`}
          onClick={() => onSort(column.key)}
        >
          <span>{column.label}</span>
          <SortIcon aria-hidden="true" className={cn("h-3 w-3", isActive ? "text-foreground" : "opacity-55")} />
        </button>
      </div>
    </th>
  );
}

function PortfolioAnalysisButton({
  run,
  starting,
  empty = false,
  ariaLabel,
  onStart,
  onOpen,
  primary = false,
  idleLabel = "分析",
}: {
  run?: PortfolioAnalysisSession;
  starting: boolean;
  empty?: boolean;
  ariaLabel: string;
  onStart: () => void;
  onOpen: (run: PortfolioAnalysisSession) => void;
  primary?: boolean;
  idleLabel?: string;
}) {
  const baseClass = primary
    ? "inline-flex items-center gap-2 rounded-md border px-4 py-2 text-sm font-medium transition hover:bg-muted disabled:opacity-50"
    : "inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1.5 text-xs font-medium transition hover:bg-muted disabled:cursor-not-allowed disabled:opacity-50";
  if (starting) {
    return (
      <button type="button" disabled className={baseClass} aria-label={`${ariaLabel} 启动中`}>
        <Loader2 className="h-3.5 w-3.5 animate-spin" />
        启动中
      </button>
    );
  }
  if (run && run.status === "completed") {
    return (
      <span className="inline-flex items-center gap-1">
        <button type="button" onClick={() => onOpen(run)} className={baseClass} aria-label={`${ariaLabel} 查看报告`}>
          <Bot className="h-3.5 w-3.5" />
          查看报告
        </button>
        <button
          type="button"
          onClick={onStart}
          className="inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1.5 text-xs font-medium transition hover:bg-muted"
          aria-label={`${ariaLabel} 重新分析`}
        >
          <RefreshCw className="h-3.5 w-3.5" />
          重新分析
        </button>
      </span>
    );
  }
  if (run && isActiveAnalysis(run)) {
    const label = run.status === "queued" ? "排队中" : "分析中";
    return (
      <button type="button" onClick={() => onOpen(run)} className={baseClass} aria-label={`${ariaLabel} ${label}`}>
        {run.status === "running" ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Bot className="h-3.5 w-3.5" />}
        {label}
      </button>
    );
  }
  const retry = run?.status === "failed" || run?.status === "cancelled";
  return (
    <button type="button" onClick={onStart} disabled={empty} className={baseClass} aria-label={`${ariaLabel} ${retry ? "重试" : "启动"}`}>
      <Bot className="h-3.5 w-3.5" />
      {retry ? "重试" : idleLabel}
    </button>
  );
}

function TradesTable({
  review,
  holdingNames,
  deletingTradeId,
  onDelete,
}: {
  review: PortfolioReview | null;
  holdingNames: ReadonlyMap<string, string>;
  deletingTradeId: string | null;
  onDelete: (trade: PortfolioTrade) => void;
}) {
  const trades = review?.portfolio_state.recent_trades || [];
  return (
    <section className="grid gap-2">
      <h2 className="text-sm font-semibold">最近交易记录</h2>
      <div className="overflow-auto rounded-md border">
        <table aria-label="最近交易记录" className="w-full min-w-[760px] text-left text-xs">
          <thead className="bg-muted/50 text-[11px] uppercase text-muted-foreground">
            <tr>
              <th className="px-3 py-2 font-medium">证券标识</th>
              <th className="px-3 py-2 font-medium">方向</th>
              <th className="px-3 py-2 text-right font-medium">数量</th>
              <th className="px-3 py-2 text-right font-medium">价格</th>
              <th className="px-3 py-2 font-medium">交易日期</th>
              <th className="px-3 py-2 font-medium">备注</th>
              <th className="px-3 py-2 font-medium">记录时间</th>
              <th className="px-3 py-2 text-right font-medium">操作</th>
            </tr>
          </thead>
          <tbody>
            {trades.length === 0 ? (
              <EmptyRow colSpan={8} text="暂无最近交易记录。" />
            ) : (
              trades.map((row, index) => {
                const symbol = String(row.symbol || row.code || "");
                const displayName = securityDisplayName(symbol, holdingNames, row.name);
                return (
                <tr key={`${row.recorded_at || row.trade_date || "trade"}-${row.symbol || row.code || index}-${index}`} className="border-t">
                  <td className="px-3 py-2 font-medium">{displayName}</td>
                  <td className="px-3 py-2">{tradeSideLabel(row.side)}</td>
                  <td className="px-3 py-2 text-right font-mono">{fmtNumber(row.quantity, 0)}</td>
                  <td className="px-3 py-2 text-right font-mono">{fmtNumber(row.price)}</td>
                  <td className="px-3 py-2 font-mono">{fmtText(row.trade_date)}</td>
                  <td className="px-3 py-2">{fmtText(row.notes)}</td>
                  <td className="px-3 py-2 font-mono">{fmtDate(row.recorded_at)}</td>
                  <td className="px-3 py-2 text-right">
                    <button
                      type="button"
                      aria-label={`删除交易 ${displayName} ${fmtText(row.trade_date)}`}
                      disabled={!row.trade_id || deletingTradeId === row.trade_id}
                      onClick={() => onDelete(row)}
                      className="inline-flex items-center rounded-md border px-2 py-1.5 text-destructive hover:bg-destructive/10 disabled:opacity-50"
                    >
                      {deletingTradeId === row.trade_id
                        ? <Loader2 className="h-3.5 w-3.5 animate-spin" />
                        : <Trash2 className="h-3.5 w-3.5" />}
                    </button>
                  </td>
                </tr>
                );
              })
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function CacheTable({
  review,
  expandedCachePath,
  onToggle,
  onOpenChart,
  onOpenMarketDetails,
  onRefreshSymbol,
  refreshingSymbol,
  refreshDisabled,
}: {
  review: PortfolioReview | null;
  expandedCachePath: string | null;
  onToggle: (path: string) => void;
  onOpenChart: (group: CacheGroup) => void;
  onOpenMarketDetails: (group: CacheGroup) => void;
  onRefreshSymbol: (symbol: string) => void;
  refreshingSymbol: string | null;
  refreshDisabled: boolean;
}) {
  const caches = review?.verified_market_cache || [];
  const holdings = review?.portfolio_state.holdings || [];
  const groups = useMemo(() => groupCacheRows(caches, holdings), [caches, holdings]);
  const [groupExpansionOverrides, setGroupExpansionOverrides] = useState<Record<string, boolean>>({});
  return (
    <section className="grid gap-2">
      <div className="flex items-center justify-between gap-3">
        <div>
          <h2 className="flex items-center gap-2 text-sm font-semibold">
            <Database className="h-4 w-4" />
            校核行情缓存
          </h2>
          <p className="mt-1 text-xs text-muted-foreground">仅显示当前持仓；其他历史缓存仍保留在数据中心。</p>
        </div>
        <span className="truncate text-xs text-muted-foreground">{review?.market_cache_db || review?.verified_cache_dir}</span>
      </div>
      <div className="overflow-auto rounded-md border">
        <table className="w-full min-w-[1180px] text-left text-xs">
          <thead className="bg-muted/50 text-[11px] uppercase text-muted-foreground">
            <tr>
              <th className="w-9 px-2 py-2" />
              <th className="px-3 py-2 font-medium">证券标识</th>
              <th className="px-3 py-2 font-medium">周期</th>
              <th className="px-3 py-2 font-medium">状态</th>
              <th className="px-3 py-2 font-medium">实际复权</th>
              <th className="px-3 py-2 text-right font-medium">最新价</th>
              <th className="px-3 py-2 text-right font-medium">价差 %</th>
              <th className="px-3 py-2 text-right font-medium">成交量</th>
              <th className="px-3 py-2 text-right font-medium">成交额</th>
              <th className="px-3 py-2 font-medium">来源</th>
              <th className="px-3 py-2 font-medium">缓存范围</th>
              <th className="px-3 py-2 font-medium">校核时间</th>
            </tr>
          </thead>
          <tbody>
            {groups.length === 0 ? (
              <EmptyRow colSpan={12} text="暂无校核行情缓存。" />
            ) : (
              groups.map((group) => {
                const groupVerified = group.status === "verified";
                const groupExpanded = !groupVerified || groupExpansionOverrides[group.symbol] === true;
                const groupLabel = `${group.name ? `${group.name} ` : ""}${group.symbol}`;
                return <Fragment key={group.symbol}>
                  <tr className={cn("border-t-2 bg-muted/40", group.status === "unresolved_conflict" && "bg-destructive/10")} data-cache-group={group.symbol}>
                    <td colSpan={12} className="px-3 py-2.5">
                      <div className="flex flex-wrap items-center justify-between gap-3">
                        <div className="flex min-w-0 flex-wrap items-center gap-2.5">
                          {groupVerified ? (
                            <button
                              type="button"
                              onClick={() => setGroupExpansionOverrides((current) => ({
                                ...current,
                                [group.symbol]: !groupExpanded,
                              }))}
                              className="inline-flex h-7 w-7 items-center justify-center rounded border bg-background text-muted-foreground hover:text-foreground"
                              aria-label={`${groupLabel} ${groupExpanded ? "收起" : "展开"}校核行情`}
                              aria-expanded={groupExpanded}
                              title={groupExpanded ? "收起该标的" : "展开该标的"}
                            >
                              <ChevronDown className={cn("h-4 w-4 transition-transform", !groupExpanded && "-rotate-90")} />
                            </button>
                          ) : <span className="inline-block h-7 w-7" aria-hidden="true" />}
                          <span className="font-mono text-sm font-semibold">{group.symbol}</span>
                          {group.name ? <span className="truncate text-xs text-muted-foreground">{group.name}</span> : null}
                          <StatusPill status={group.status} />
                          <span className="text-[11px] text-muted-foreground">
                            {group.rows.length} 个缓存 · {group.intervals.join(" / ")}
                            {!groupExpanded && group.status === "verified" ? " · 全部校核通过，已折叠" : ""}
                          </span>
                        </div>
                        <div className="flex items-center gap-2">
                          <button
                            type="button"
                            onClick={() => onRefreshSymbol(group.symbol)}
                            disabled={refreshDisabled}
                            className="inline-flex items-center gap-1.5 rounded-md border bg-background px-2.5 py-1.5 text-xs font-medium hover:bg-muted disabled:opacity-50"
                            aria-label={refreshingSymbol === group.symbol
                              ? `${groupLabel} 行情刷新中`
                              : `刷新 ${groupLabel} 行情`}
                            title={`只刷新 ${groupLabel}`}
                          >
                            {refreshingSymbol === group.symbol
                              ? <Loader2 className="h-3.5 w-3.5 animate-spin" />
                              : <RefreshCw className="h-3.5 w-3.5" />}
                            {refreshingSymbol === group.symbol ? "刷新中" : "刷新行情"}
                          </button>
                          <button
                            type="button"
                            onClick={() => onOpenChart(group)}
                            className="inline-flex items-center gap-1.5 rounded-md border bg-background px-2.5 py-1.5 text-xs font-medium hover:bg-muted"
                            aria-label={`查看 ${group.symbol} K线`}
                            title={`查看 ${group.symbol} 已缓存K线`}
                          >
                            <ChartCandlestick className="h-3.5 w-3.5" />
                            查看K线
                          </button>
                          <button
                            type="button"
                            onClick={() => onOpenMarketDetails(group)}
                            className="inline-flex items-center gap-1.5 rounded-md border bg-background px-2.5 py-1.5 text-xs font-medium hover:bg-muted"
                            aria-label={`${group.name ? `${group.name} ` : ""}${group.symbol} 行情详情`}
                            title={`查看 ${group.name ? `${group.name} ` : ""}${group.symbol} 的行情详情`}
                          >
                            <Info className="h-3.5 w-3.5" />
                            行情详情
                          </button>
                        </div>
                      </div>
                    </td>
                  </tr>
                  {groupExpanded ? <>
                  {group.rows.map((row) => {
                    const expanded = expandedCachePath === row.path;
                    return (
                      <Fragment key={row.path}>
                        <tr className={cn("border-t bg-background", row.status === "unresolved_conflict" && "bg-destructive/5")}>
                          <td className="px-2 py-2">
                            <button
                              type="button"
                              onClick={() => onToggle(row.path)}
                              className="inline-flex h-6 w-6 items-center justify-center rounded border text-muted-foreground hover:text-foreground"
                              title="展开/收起详情"
                            >
                              <ChevronDown className={cn("h-3.5 w-3.5 transition-transform", expanded && "rotate-180")} />
                            </button>
                          </td>
                          <td className="px-3 py-2 text-muted-foreground">
                            <span className="inline-block h-4 w-3 border-b border-l border-border" aria-hidden="true" />
                          </td>
                          <td className="px-3 py-2 font-mono font-medium">{fmtText(row.interval)}</td>
                          <td className="px-3 py-2">
                            <StatusPill status={row.status} />
                          </td>
                          <td className="px-3 py-2 font-mono">{fmtText(row.actual_adjustment || row.requested_adjustment)}</td>
                          <td className="px-3 py-2 text-right font-mono">{fmtNumber(row.consensus_close)}</td>
                          <td className="px-3 py-2 text-right font-mono">{fmtNumber(row.spread_pct, 4)}</td>
                          <td className="px-3 py-2 text-right font-mono">{fmtNumber(row.volume, 0)}</td>
                          <td className="px-3 py-2 text-right font-mono">{fmtNumber(row.amount, 2)}</td>
                          <td className="px-3 py-2">{(row.sources || []).join(", ") || "-"} ({row.source_count || 0})</td>
                          <td className="px-3 py-2 font-mono">{fmtText(row.start_date)}{" 至 "}{fmtText(row.end_date)}</td>
                          <td className="px-3 py-2 font-mono">{fmtDate(row.verified_at || row.modified_at)}</td>
                        </tr>
                        {expanded ? (
                          <tr className="border-t bg-muted/20">
                            <td colSpan={12} className="px-4 py-3">
                              <CacheDetails row={row} />
                            </td>
                          </tr>
                        ) : null}
                      </Fragment>
                    );
                  })}
                  </> : null}
                </Fragment>;
              })
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}

interface CacheGroup {
  symbol: string;
  name?: string;
  status: string;
  intervals: string[];
  rows: VerifiedMarketCacheRow[];
}

function groupCacheRows(caches: VerifiedMarketCacheRow[], holdings: PortfolioHolding[]): CacheGroup[] {
  const intervalOrder = new Map([["1m", 0], ["5m", 1], ["1D", 2]]);
  const adjustmentOrder = new Map([["raw", 0], ["qfq", 1]]);
  const holdingOrder = new Map<string, number>();
  const holdingNames = new Map<string, string>();
  holdings.forEach((holding, index) => {
    const symbol = holdingSymbol(holding).toUpperCase();
    if (!symbol) return;
    holdingOrder.set(symbol, index);
    if (holding.name) holdingNames.set(symbol, String(holding.name));
  });

  const grouped = new Map<string, VerifiedMarketCacheRow[]>();
  for (const row of caches) {
    const symbol = String(row.symbol || row.file_name || "未知证券").toUpperCase();
    if (!holdingOrder.has(symbol)) continue;
    const rows = grouped.get(symbol) || [];
    rows.push(row);
    grouped.set(symbol, rows);
  }

  return [...grouped.entries()]
    .map(([symbol, rows]) => {
      rows.sort((left, right) => {
        const leftInterval = String(left.interval || "");
        const rightInterval = String(right.interval || "");
        const intervalDiff = (intervalOrder.get(leftInterval) ?? 99) - (intervalOrder.get(rightInterval) ?? 99);
        if (intervalDiff !== 0) return intervalDiff;
        const leftAdjustment = String(left.actual_adjustment || left.requested_adjustment || "");
        const rightAdjustment = String(right.actual_adjustment || right.requested_adjustment || "");
        return (adjustmentOrder.get(leftAdjustment) ?? 99) - (adjustmentOrder.get(rightAdjustment) ?? 99);
      });
      const statuses = new Set(rows.map((row) => row.status || "unknown"));
      const allVerified = rows.every((row) => row.status === "verified");
      const status = statuses.has("unresolved_conflict")
        ? "unresolved_conflict"
        : statuses.has("basis_mismatch")
          ? "basis_mismatch"
          : statuses.has("source_lag")
            ? "source_lag"
            : statuses.has("provisional_mix")
              ? "provisional_mix"
              : statuses.has("unresolved")
                ? "unresolved"
                : statuses.has("stale")
                  ? "stale"
                  : statuses.has("single_source")
                    ? "single_source"
                    : allVerified
                      ? "verified"
                      : String(rows[0]?.status || "unknown");
      return {
        symbol,
        name: holdingNames.get(symbol),
        status,
        intervals: [...new Set(rows.map((row) => String(row.interval || "-")))],
        rows,
      };
    })
    .sort((left, right) => {
      const leftOrder = holdingOrder.get(left.symbol) ?? Number.MAX_SAFE_INTEGER;
      const rightOrder = holdingOrder.get(right.symbol) ?? Number.MAX_SAFE_INTEGER;
      return leftOrder - rightOrder || left.symbol.localeCompare(right.symbol);
    });
}

function cacheRowsForHoldings(
  caches: VerifiedMarketCacheRow[],
  holdings: PortfolioHolding[],
): VerifiedMarketCacheRow[] {
  const holdingSymbols = new Set(
    holdings.map((holding) => holdingSymbol(holding).toUpperCase()).filter(Boolean),
  );
  return caches.filter((row) => holdingSymbols.has(String(row.symbol || "").toUpperCase()));
}

function CacheDetails({ row }: { row: VerifiedMarketCacheRow }) {
  const observations = row.observations || [];
  if (observations.length === 0) {
    return <div className="text-sm text-muted-foreground">暂无来源级观测记录。</div>;
  }
  return (
    <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-3">
      {observations.map((obs, index) => (
        <div key={`${obs.actual_source || obs.source || index}`} className="rounded-md border bg-background p-3">
          <div className="mb-2 flex items-center justify-between gap-2">
            <span className="font-mono text-sm font-medium">{fmtText(obs.actual_source || obs.source)}</span>
            <span className="rounded bg-muted px-1.5 py-0.5 font-mono text-[11px] text-muted-foreground">
              {fmtText(obs.actual_adjustment || obs.adjustment)}
            </span>
          </div>
          <div className="grid grid-cols-2 gap-2 text-xs">
            <KeyValue label="请求来源" value={fmtText(obs.requested_source)} />
            <KeyValue label="实际来源" value={fmtText(obs.actual_source || obs.source)} />
            <KeyValue label="开 / 高" value={`${fmtNumber(obs.open)} / ${fmtNumber(obs.high)}`} />
            <KeyValue label="低 / 收" value={`${fmtNumber(obs.low)} / ${fmtNumber(obs.close)}`} />
            <KeyValue label="成交量" value={`${fmtNumber(obs.volume, 0)} ${fmtText(obs.volume_unit)}`} />
            <KeyValue label="成交额" value={fmtNumber(obs.amount, 2)} />
            <KeyValue label="VWAP" value={fmtNumber(obs.vwap)} />
            <KeyValue label="时间" value={fmtDate(obs.date)} />
            <KeyValue label="获取时间" value={fmtDate(obs.retrieved_at)} />
            <KeyValue label="刷新批次" value={fmtText(obs.batch_id)} />
            <KeyValue label="置信度" value={fmtText(obs.adjustment_confidence)} />
            <KeyValue label="获取方式" value={fmtText(obs.acquisition_mode)} />
            <KeyValue label="纳入共识" value={obs.included_in_consensus ? "是" : "否"} />
          </div>
        </div>
      ))}
    </div>
  );
}

function StatusPill({ status }: { status?: string }) {
  const value = status || "unknown";
  const tone = value === "verified" || value === "completed"
    ? "success"
    : value === "unresolved_conflict" || value === "conflict" || value === "failed"
      ? "danger"
      : ["single_source", "source_lag", "provisional_mix", "basis_mismatch", "stale", "partial", "interrupted"].includes(value)
        ? "warning"
        : "neutral";
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded px-2 py-0.5 font-mono text-[11px]",
        tone === "success" && "bg-success/10 text-success",
        tone === "danger" && "bg-destructive/10 text-destructive",
        tone === "warning" && "bg-warning/10 text-amber-700 dark:text-amber-300",
        tone === "neutral" && "bg-muted text-muted-foreground",
      )}
    >
      {tone === "success" ? <CheckCircle2 className="h-3 w-3" /> : tone === "danger" ? <AlertTriangle className="h-3 w-3" /> : null}
      {refreshStatusLabel(value)}
    </span>
  );
}

function KeyValue({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="text-[10px] uppercase text-muted-foreground">{label}</div>
      <div className="font-mono text-xs">{value}</div>
    </div>
  );
}

function EmptyRow({ colSpan, text }: { colSpan: number; text: string }) {
  return (
    <tr>
      <td colSpan={colSpan} className="px-3 py-8 text-center text-muted-foreground">
        {text}
      </td>
    </tr>
  );
}
