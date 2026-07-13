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
  Loader2,
  Pencil,
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
  type PortfolioHolding,
  type PortfolioReview,
  type PortfolioTrade,
  type VerifiedMarketCacheRow,
} from "@/lib/api";
import { cn } from "@/lib/utils";
import MarketCacheKlineDialog from "@/components/portfolio/MarketCacheKlineDialog";

const ACTIVE_REFRESH_KEY = "vibe-trading:portfolio-market-refresh:v1";
const ANALYSIS_RUNS_STORAGE_KEY = "vibe-trading:portfolio-analysis-runs:v1";
const TERMINAL_REFRESH_STATUSES = new Set(["completed", "partial", "failed", "interrupted"]);
const ACTIVE_ANALYSIS_STATUSES = new Set(["queued", "running"]);

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
  const [review, setReview] = useState<PortfolioReview | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [refreshRun, setRefreshRun] = useState<MarketCacheRun | null>(null);
  const [holdingsText, setHoldingsText] = useState("");
  const [cash, setCash] = useState("");
  const [cashCurrency, setCashCurrency] = useState("CNY");
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
  const completedRefreshRef = useRef<string | null>(null);

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

  useEffect(() => {
    void loadReview("initial");
  }, [loadReview]);

  useEffect(() => {
    const code = tradeForm.code.trim();
    if (!/^\d{6}$/.test(code)) {
      setTradeLookup({ status: "idle" });
      setTradeForm((current) => (
        current.symbol || current.name ? { ...current, symbol: "", name: "" } : current
      ));
      return;
    }

    const controller = new AbortController();
    let cancelled = false;
    setTradeLookup({ status: "loading", code });
    void api.lookupPortfolioSecurity(code, controller.signal)
      .then((security) => {
        if (cancelled) return;
        setTradeForm((current) => (
          current.code.trim() === code
            ? { ...current, code: security.code, symbol: security.symbol, name: security.name }
            : current
        ));
        setTradeSuggestionsOpen(false);
        setTradeLookup({ status: "resolved", code, message: `${security.name} · ${security.symbol}` });
      })
      .catch((error) => {
        if (cancelled || (error instanceof Error && error.name === "AbortError")) return;
        setTradeForm((current) => (
          current.code.trim() === code ? { ...current, symbol: "", name: "" } : current
        ));
        setTradeLookup({
          status: "error",
          code,
          message: error instanceof Error ? error.message : "未找到该证券，请检查代码。",
        });
      });
    return () => {
      cancelled = true;
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
            if (next.status === "completed") toast.success("持仓行情与分层缓存已刷新完成。");
            else if (next.status === "partial") toast.warning("行情刷新已完成，但有部分数据源失败。");
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
  }, [loadReview, refreshRun]);

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

  const refreshMarketData = async () => {
    const count = review?.portfolio_state.holdings.length || 0;
    if (count === 0) {
      toast.error("请先保存持仓，再刷新行情。");
      return;
    }
    try {
      const payload = await api.startMarketCacheRefresh({ profile: "portfolio_default" });
      setRefreshRun(payload.run);
      completedRefreshRef.current = null;
      localStorage.setItem(ACTIVE_REFRESH_KEY, payload.run_id);
      toast.success(payload.deduplicated ? "已恢复正在进行的行情刷新。" : `已开始刷新 ${count} 个持仓标的。`);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "刷新行情失败");
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
  const portfolioAnalysisKey = analysisTargetKey("portfolio");
  const portfolioAnalysisRun = analysisRuns[portfolioAnalysisKey];
  const marketAnalysisKey = analysisTargetKey("market");
  const marketAnalysisRun = analysisRuns[marketAnalysisKey];
  const marketAnalysisLabel = getMarketAnalysisLabel(new Date(analysisClock));

  const summary = useMemo(() => {
    const holdings = review?.portfolio_state.holdings || [];
    const caches = review?.verified_market_cache || [];
    const totalMarketValue = holdings.reduce((sum, row) => {
      const value = typeof row.market_value === "number" ? row.market_value : 0;
      return sum + value;
    }, 0);
    return {
      holdingCount: holdings.length,
      tradeCount: review?.portfolio_state.recent_trades.length || 0,
      cacheCount: caches.length,
      conflictCount: caches.filter((row) => row.status === "conflict").length,
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
        cash: cash.trim() ? Number(cash) : undefined,
        cash_currency: cashCurrency.trim() || "CNY",
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
    const code = tradeForm.code.trim();
    if (!/^\d{6}$/.test(code)) {
      toast.error("请输入完整的 6 位证券代码。");
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
      toast.success(`${symbol} 的持有股数和成本已更新。`);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "更新持仓失败");
      throw error;
    }
  };

  const removeTrade = async (trade: PortfolioTrade) => {
    const tradeId = String(trade.trade_id || "");
    if (!tradeId) {
      toast.error("该交易记录缺少标识，无法删除。");
      return;
    }
    const symbol = String(trade.symbol || trade.code || "该证券");
    if (!window.confirm(`确认删除 ${symbol} 的这条交易记录？\n\n只删除记录，不会撤销它已经造成的持仓变化。`)) return;

    setDeletingTradeId(tradeId);
    try {
      const payload = await api.deletePortfolioTrade(tradeId);
      setReview(payload);
      toast.success("交易记录已删除，持仓未回滚。");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "删除交易记录失败");
    } finally {
      setDeletingTradeId(null);
    }
  };

  return (
    <div className="min-h-screen p-6 lg:p-8">
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

        {refreshRun ? <RefreshProgress run={refreshRun} /> : null}

        {loading ? (
          <div className="grid gap-3 md:grid-cols-4">
            {[1, 2, 3, 4].map((item) => (
              <div key={item} className="h-24 animate-pulse rounded-md border bg-muted/40" />
            ))}
          </div>
        ) : null}

        {!loading ? (
          <>
            <section className="grid gap-3 md:grid-cols-6">
              <SummaryTile label="持仓数" value={String(summary.holdingCount)} />
              <SummaryTile label="现金" value={`${fmtNumber(review?.portfolio_state.cash, 2)} ${review?.portfolio_state.cash_currency || "CNY"}`} />
              <SummaryTile label="总市值" value={fmtNumber(summary.totalMarketValue, 2)} />
              <SummaryTile label="交易记录" value={String(summary.tradeCount)} />
              <SummaryTile label="缓存数" value={String(summary.cacheCount)} />
              <SummaryTile label="冲突缓存" value={String(summary.conflictCount)} danger={summary.conflictCount > 0} />
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
                <div className="mt-3 grid gap-3 sm:grid-cols-[1fr_8rem_auto]">
                  <input
                    value={cash}
                    onChange={(event) => setCash(event.target.value)}
                    inputMode="decimal"
                    placeholder="现金"
                    className="rounded-md border bg-background px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-primary/30"
                  />
                  <input
                    value={cashCurrency}
                    onChange={(event) => setCashCurrency(event.target.value)}
                    placeholder="CNY"
                    className="rounded-md border bg-background px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-primary/30"
                  />
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
                        placeholder="代码，如 588870 或 588"
                        aria-label="6位证券代码"
                        inputMode="numeric"
                        maxLength={6}
                        value={tradeForm.code}
                        onFocus={() => setTradeSuggestionsOpen(true)}
                        onChange={(e) => {
                          const code = e.target.value.replace(/\D/g, "").slice(0, 6);
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

            <HoldingsTable
              review={review}
              analysisRuns={analysisRuns}
              startingAnalysisKey={startingAnalysisKey}
              onStart={(holding) => void startAnalysis("holding", holding)}
              onOpen={openAnalysis}
              onUpdate={updateHolding}
            />
            <TradesTable review={review} deletingTradeId={deletingTradeId} onDelete={(trade) => void removeTrade(trade)} />
            <CacheTable
              review={review}
              expandedCachePath={expandedCachePath}
              onToggle={(path) => setExpandedCachePath((current) => (current === path ? null : path))}
              onOpenChart={setChartCacheGroup}
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
    stale: "已过期",
    conflict: "有冲突",
    unresolved: "未解析",
    failed: "失败",
    interrupted: "已中断",
  };
  return labels[status] || status;
}

function RefreshProgress({ run }: { run: MarketCacheRun }) {
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
            {run.current_symbol ? `正在处理 ${run.current_symbol}` : `任务 ${run.run_id.slice(0, 8)}`}
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
                <td className="px-3 py-2 font-mono">{item.symbol}</td>
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
  analysisRuns,
  startingAnalysisKey,
  onStart,
  onOpen,
  onUpdate,
}: {
  review: PortfolioReview | null;
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
    <section className="grid gap-2">
      <h2 className="text-sm font-semibold">持仓矩阵</h2>
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
                return (
                <tr key={`${row.symbol || row.code || index}`} className={cn("border-t", isEditing && "bg-muted/20")}>
                  <td className="px-3 py-2 font-medium">{fmtText(row.name)}</td>
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
}: {
  column: (typeof HOLDING_SORT_COLUMNS)[number];
  sort: HoldingSort | null;
  onSort: (key: HoldingSortKey) => void;
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
      <button
        type="button"
        className={cn(
          "inline-flex w-full items-center gap-1.5 rounded-sm transition hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40",
          alignRight ? "justify-end" : "justify-start",
        )}
        aria-label={`按${column.label}${nextDirection}排序`}
        title={`按${column.label}${nextDirection}排序`}
        onClick={() => onSort(column.key)}
      >
        <span>{column.label}</span>
        <SortIcon aria-hidden="true" className={cn("h-3 w-3", isActive ? "text-foreground" : "opacity-55")} />
      </button>
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
  deletingTradeId,
  onDelete,
}: {
  review: PortfolioReview | null;
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
              trades.map((row, index) => (
                <tr key={`${row.recorded_at || row.trade_date || "trade"}-${row.symbol || row.code || index}-${index}`} className="border-t">
                  <td className="px-3 py-2 font-mono">{fmtText(row.symbol || row.code)}</td>
                  <td className="px-3 py-2">{tradeSideLabel(row.side)}</td>
                  <td className="px-3 py-2 text-right font-mono">{fmtNumber(row.quantity, 0)}</td>
                  <td className="px-3 py-2 text-right font-mono">{fmtNumber(row.price)}</td>
                  <td className="px-3 py-2 font-mono">{fmtText(row.trade_date)}</td>
                  <td className="px-3 py-2">{fmtText(row.notes)}</td>
                  <td className="px-3 py-2 font-mono">{fmtDate(row.recorded_at)}</td>
                  <td className="px-3 py-2 text-right">
                    <button
                      type="button"
                      aria-label={`删除交易 ${fmtText(row.symbol || row.code)} ${fmtText(row.trade_date)}`}
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
              ))
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
}: {
  review: PortfolioReview | null;
  expandedCachePath: string | null;
  onToggle: (path: string) => void;
  onOpenChart: (group: CacheGroup) => void;
}) {
  const caches = review?.verified_market_cache || [];
  const holdings = review?.portfolio_state.holdings || [];
  const groups = useMemo(() => groupCacheRows(caches, holdings), [caches, holdings]);
  return (
    <section className="grid gap-2">
      <div className="flex items-center justify-between gap-3">
        <h2 className="flex items-center gap-2 text-sm font-semibold">
          <Database className="h-4 w-4" />
          校核行情缓存
        </h2>
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
              groups.map((group) => (
                <Fragment key={group.symbol}>
                  <tr className={cn("border-t-2 bg-muted/40", group.status === "conflict" && "bg-destructive/10")} data-cache-group={group.symbol}>
                    <td colSpan={12} className="px-3 py-2.5">
                      <div className="flex flex-wrap items-center justify-between gap-3">
                        <div className="flex min-w-0 flex-wrap items-center gap-2.5">
                          <span className="font-mono text-sm font-semibold">{group.symbol}</span>
                          {group.name ? <span className="truncate text-xs text-muted-foreground">{group.name}</span> : null}
                          <StatusPill status={group.status} />
                          <span className="text-[11px] text-muted-foreground">
                            {group.rows.length} 个缓存 · {group.intervals.join(" / ")}
                          </span>
                        </div>
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
                      </div>
                    </td>
                  </tr>
                  {group.rows.map((row) => {
                    const expanded = expandedCachePath === row.path;
                    return (
                      <Fragment key={row.path}>
                        <tr className={cn("border-t bg-background", row.status === "conflict" && "bg-destructive/5")}>
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
                </Fragment>
              ))
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
      const status = statuses.has("conflict")
        ? "conflict"
        : statuses.has("unresolved")
          ? "unresolved"
          : statuses.has("stale")
            ? "stale"
            : statuses.has("single_source")
              ? "single_source"
              : "verified";
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
    : value === "conflict" || value === "failed"
      ? "danger"
      : ["single_source", "stale", "partial", "interrupted"].includes(value)
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
