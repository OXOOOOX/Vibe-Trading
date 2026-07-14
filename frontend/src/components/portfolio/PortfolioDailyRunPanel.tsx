import { useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  ArrowLeft,
  ArrowRight,
  Bot,
  Download,
  GripVertical,
  Loader2,
  RotateCcw,
  Save,
  ShieldCheck,
  Sparkles,
  Square,
  Swords,
  WalletCards,
} from "lucide-react";
import type {
  PortfolioAssignment,
  PortfolioDailyRun,
  PortfolioHolding,
  PortfolioMandate,
} from "@/lib/api";

const ACTIVE = new Set(["queued", "running", "cancelling"]);

interface Props {
  mandate: PortfolioMandate | null;
  holdings: PortfolioHolding[];
  currentCash: number | null;
  cashCurrency: string;
  run: PortfolioDailyRun | null;
  loading: boolean;
  saving: boolean;
  savingCash: boolean;
  starting: boolean;
  onSave: (mandate: PortfolioMandate) => Promise<boolean>;
  onSaveCash: (cash: number, currency: string) => Promise<void>;
  onAssign: (symbol: string, sleeveId: string) => Promise<PortfolioMandate | null>;
  onStart: () => Promise<void>;
  onRetry: () => Promise<void>;
  onRetryHolding: (symbol: string) => Promise<void>;
  onCancel: () => Promise<void>;
  artifactUrl: (runId: string, artifactId: string) => string;
}

function runStageLabel(run: PortfolioDailyRun, legacyDataLimited: boolean): string {
  if (run.stage === "skipped_data_unavailable") return "已跳过（数据不足）";
  if (legacyDataLimited) return "历史报告（数据受限）";
  if (run.stage === "completed") return "已完成";
  if (run.stage === "refreshing_data") return "正在获取数据";
  if (run.stage === "analyzing_holdings") return "正在分析持仓";
  return run.stage;
}

function numberOrNull(value: string): number | null {
  if (!value.trim()) return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : null;
}

function money(value: number): string {
  return new Intl.NumberFormat("zh-CN", {
    style: "currency",
    currency: "CNY",
    maximumFractionDigits: 0,
  }).format(value);
}

function holdingValue(holding: PortfolioHolding): number {
  const marketValue = holding.market_value === null || holding.market_value === undefined
    ? Number.NaN
    : Number(holding.market_value);
  if (Number.isFinite(marketValue) && marketValue >= 0) return marketValue;
  const quantity = Number(holding.quantity);
  const price = Number(holding.last_price ?? holding.cost_price);
  return Number.isFinite(quantity) && Number.isFinite(price) ? Math.max(0, quantity * price) : 0;
}

function symbolOf(holding: PortfolioHolding): string {
  return String(holding.symbol || holding.code || "").toUpperCase();
}

export interface AgentAllocationSuggestion {
  offensiveRatio: number;
  defensiveRatio: number;
  rationale: string;
}

export function getAgentAllocationSuggestion(
  holdings: PortfolioHolding[],
  assignments: Record<string, PortfolioAssignment>,
): AgentAllocationSuggestion {
  let totalValue = 0;
  let reliableValue = 0;
  let reliableOffensiveValue = 0;

  for (const holding of holdings) {
    const value = holdingValue(holding);
    totalValue += value;
    const assignment = assignments[symbolOf(holding)];
    const reliable = Boolean(assignment?.user_locked) || Number(assignment?.confidence || 0) >= 0.75;
    if (!reliable) continue;
    reliableValue += value;
    if (assignment.active_sleeve_id === "offensive") reliableOffensiveValue += value;
  }

  if (totalValue <= 0 || reliableValue / totalValue < 0.5) {
    return {
      offensiveRatio: 40,
      defensiveRatio: 60,
      rationale: "当前可靠分区不足，先采用均衡偏防守基线。",
    };
  }

  const observedOffensive = reliableOffensiveValue / reliableValue * 100;
  const blended = 40 * 0.6 + observedOffensive * 0.4;
  const offensiveRatio = Math.max(20, Math.min(70, Math.round(blended / 5) * 5));
  return {
    offensiveRatio,
    defensiveRatio: 100 - offensiveRatio,
    rationale: "综合已确认分区，并向均衡偏防守基线收敛。",
  };
}

function savedOffensiveRatio(mandate: PortfolioMandate): number | null {
  const offensive = mandate.sleeves.find((item) => item.id === "offensive")?.target_amount || 0;
  const defensive = mandate.sleeves.find((item) => item.id === "defensive")?.target_amount || 0;
  const total = offensive + defensive;
  return total > 0 ? Math.round(offensive / total * 100) : null;
}

function roundMoney(value: number): number {
  return Math.round(value * 100) / 100;
}

export function applyAllocationRatio(
  mandate: PortfolioMandate,
  offensiveRatio: number,
  allocationBase: number,
  _currentCash: number | null,
): PortfolioMandate {
  const next = structuredClone(mandate);
  const offensiveShare = Math.max(0, Math.min(100, offensiveRatio)) / 100;
  const shares: Record<string, number> = {
    offensive: offensiveShare,
    defensive: 1 - offensiveShare,
  };
  const bandShare = 0.1;

  for (const sleeve of next.sleeves) {
    const share = shares[sleeve.id];
    if (share === undefined) continue;
    sleeve.configured = true;
    sleeve.target_amount = roundMoney(allocationBase * share);
    sleeve.min_amount = roundMoney(allocationBase * Math.max(0, share - bandShare));
    sleeve.max_amount = roundMoney(allocationBase * Math.min(1, share + bandShare));
    sleeve.rebalance_band_amount = roundMoney(allocationBase * bandShare);
  }

  if (next.cash_policy.configured) {
    // Actual cash is maintained as portfolio fact data.  The mandate cash
    // target represents the reserve kept outside the two investable sleeves.
    next.cash_policy.target_amount = next.cash_policy.min_amount;
    if (
      next.cash_policy.max_amount !== null
      && next.cash_policy.max_amount < next.cash_policy.target_amount
    ) {
      next.cash_policy.max_amount = null;
    }
  }
  return next;
}

export default function PortfolioDailyRunPanel(props: Props) {
  const [draft, setDraft] = useState<PortfolioMandate | null>(props.mandate);
  const [cashDraft, setCashDraft] = useState(props.currentCash === null ? "" : String(props.currentCash));
  const [allocationRatio, setAllocationRatio] = useState(40);
  const [settingsDirty, setSettingsDirty] = useState(false);
  const [draggingSymbol, setDraggingSymbol] = useState<string | null>(null);
  const [dropTarget, setDropTarget] = useState<string | null>(null);
  const [movingSymbol, setMovingSymbol] = useState<string | null>(null);

  const holdingsValue = useMemo(
    () => props.holdings.reduce((total, holding) => total + holdingValue(holding), 0),
    [props.holdings],
  );
  const agentSuggestion = useMemo(
    () => getAgentAllocationSuggestion(props.holdings, props.mandate?.assignments || {}),
    [props.holdings, props.mandate?.assignments],
  );

  useEffect(() => {
    setDraft(props.mandate);
    if (!props.mandate) return;
    setAllocationRatio(savedOffensiveRatio(props.mandate) ?? agentSuggestion.offensiveRatio);
    setSettingsDirty(false);
  }, [props.mandate]);

  useEffect(() => {
    setCashDraft(props.currentCash === null ? "" : String(props.currentCash));
  }, [props.currentCash]);

  useEffect(() => {
    if (!draft || savedOffensiveRatio(draft) !== null || settingsDirty) return;
    setAllocationRatio(agentSuggestion.offensiveRatio);
  }, [agentSuggestion.offensiveRatio, draft, settingsDirty]);

  if (props.loading || !draft) {
    return <section className="h-48 animate-pulse rounded-md border bg-muted/30" aria-label="组合晨会加载中" />;
  }

  const active = Boolean(props.run && ACTIVE.has(props.run.status));
  const skippedForData = props.run?.stage === "skipped_data_unavailable";
  const legacyDataLimited = Boolean(
    props.run
    && !props.run.analysis_gate
    && ["limited", "offline"].includes(props.run.data_status || ""),
  );
  const dataStopped = skippedForData || legacyDataLimited;
  const coveragePercent = Math.round((props.run?.analysis_gate?.coverage_ratio || 0) * 100);
  const cashFloor = draft.cash_policy.configured ? draft.cash_policy.min_amount : 0;
  const allocationBase = holdingsValue + Math.max(0, (props.currentCash || 0) - cashFloor);
  const offensiveTarget = allocationBase * allocationRatio / 100;
  const defensiveTarget = allocationBase - offensiveTarget;
  const parsedCash = numberOrNull(cashDraft);
  const holdingsBySleeve: Record<"offensive" | "defensive", PortfolioHolding[]> = {
    offensive: [],
    defensive: [],
  };
  for (const holding of props.holdings) {
    const symbol = symbolOf(holding);
    const sleeveId = draft.assignments[symbol]?.active_sleeve_id === "defensive"
      ? "defensive"
      : "offensive";
    holdingsBySleeve[sleeveId].push(holding);
  }

  const updateCashFloor = (value: string) => {
    setSettingsDirty(true);
    setDraft((current) => {
      if (!current) return current;
      const next = structuredClone(current);
      const floor = numberOrNull(value) ?? 0;
      next.cash_policy.configured = true;
      next.cash_policy.min_amount = floor;
      next.cash_policy.target_amount = floor;
      if (next.cash_policy.max_amount !== null && next.cash_policy.max_amount < floor) {
        next.cash_policy.max_amount = null;
      }
      return next;
    });
  };

  const toggleCashFloor = (configured: boolean) => {
    setSettingsDirty(true);
    setDraft((current) => current ? {
      ...current,
      cash_policy: { ...current.cash_policy, configured },
    } : current);
  };

  const saveSettings = async (ratio = allocationRatio) => {
    const saved = await props.onSave(
      applyAllocationRatio(draft, ratio, allocationBase, props.currentCash),
    );
    if (saved) setSettingsDirty(false);
    return saved;
  };

  const applyAgentSuggestion = async () => {
    const ratio = agentSuggestion.offensiveRatio;
    setAllocationRatio(ratio);
    setSettingsDirty(true);
    await saveSettings(ratio);
  };

  const startWithLatestSettings = async () => {
    if (settingsDirty || savedOffensiveRatio(draft) === null) {
      const saved = await saveSettings();
      if (!saved) return;
    }
    await props.onStart();
  };

  const moveHolding = async (symbol: string, sleeveId: "offensive" | "defensive") => {
    const previous = draft.assignments[symbol];
    if (previous?.active_sleeve_id === sleeveId || movingSymbol) return;

    setMovingSymbol(symbol);
    setDraft((current) => current ? {
      ...current,
      assignments: {
        ...current.assignments,
        [symbol]: {
          ...previous,
          active_sleeve_id: sleeveId,
          assigned_by: "user",
          confidence: 1,
          user_locked: true,
        },
      },
    } : current);

    const saved = await props.onAssign(symbol, sleeveId);
    setMovingSymbol(null);
    if (saved) {
      setDraft((current) => current ? {
        ...current,
        version: saved.version,
        suggestion_revision: saved.suggestion_revision,
        assignments: saved.assignments,
        classification_history: saved.classification_history,
        updated_at: saved.updated_at,
      } : saved);
      return;
    }

    setDraft((current) => {
      if (!current) return current;
      const assignments = { ...current.assignments };
      if (previous) assignments[symbol] = previous;
      else delete assignments[symbol];
      return { ...current, assignments };
    });
  };

  const dropHolding = (event: React.DragEvent<HTMLElement>, sleeveId: "offensive" | "defensive") => {
    event.preventDefault();
    const symbol = (event.dataTransfer.getData("text/plain") || draggingSymbol || "").toUpperCase();
    setDraggingSymbol(null);
    setDropTarget(null);
    if (symbol) void moveHolding(symbol, sleeveId);
  };

  return (
    <section className="rounded-md border bg-card p-4" aria-label="组合晨会">
      <div className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
        <div>
          <div className="flex items-center gap-2">
            <Bot className="h-5 w-5 text-primary" />
            <h2 className="text-lg font-semibold">组合晨会</h2>
            <span className="rounded-full bg-muted px-2 py-0.5 text-xs text-muted-foreground">
              策略版本 v{draft.version}
            </span>
          </div>
          <p className="mt-1 text-sm text-muted-foreground">
            一次刷新全部持仓，生成今日观察重点、组合约束后的调整建议，以及综合 PDF 与个股附录。
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            onClick={() => void saveSettings()}
            disabled={props.saving || active}
            className="inline-flex items-center gap-2 rounded-md border px-3 py-2 text-sm font-medium disabled:opacity-50"
          >
            {props.saving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
            保存组合设置
          </button>
          {active ? (
            <button
              type="button"
              onClick={() => void props.onCancel()}
              className="inline-flex items-center gap-2 rounded-md border border-destructive/40 px-3 py-2 text-sm font-medium text-destructive"
            >
              <Square className="h-4 w-4" />取消
            </button>
          ) : (
            <button
              type="button"
              onClick={() => void startWithLatestSettings()}
              disabled={props.starting || props.saving || props.holdings.length === 0}
              className="inline-flex items-center gap-2 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground disabled:opacity-50"
            >
              {props.starting ? <Loader2 className="h-4 w-4 animate-spin" /> : <Bot className="h-4 w-4" />}
              一键生成今日组合晨会
            </button>
          )}
        </div>
      </div>

      {props.run ? (
        <div className="mt-4 rounded-md border bg-muted/20 p-3" aria-live="polite">
          <div className="flex items-center justify-between gap-3 text-sm">
            <span className="font-medium">{props.run.market_date} · {runStageLabel(props.run, legacyDataLimited)}</span>
            {!dataStopped ? <span>{props.run.progress?.percent ?? 0}%</span> : null}
          </div>
          {!dataStopped ? (
            <div className="mt-2 h-2 overflow-hidden rounded-full bg-muted">
              <div className="h-full bg-primary transition-all" style={{ width: `${props.run.progress?.percent ?? 0}%` }} />
            </div>
          ) : null}
          {props.run.error ? <p className="mt-2 text-sm text-destructive">{props.run.error}</p> : null}
          {dataStopped ? (
            <div
              className="mt-3 rounded-md border border-amber-500/30 bg-amber-500/10 p-3 text-amber-700 dark:text-amber-300"
              role="status"
              aria-label={legacyDataLimited ? "历史报告数据受限" : "报告因数据不足已跳过"}
            >
              <div className="flex items-start gap-2">
                <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
                <div>
                  <p className="text-sm font-medium">
                    {legacyDataLimited ? "历史报告数据受限，请勿使用" : "数据覆盖不足，已停止报告流程"}
                  </p>
                  {legacyDataLimited ? (
                    <p className="mt-1 text-xs">
                      这次运行发生在数据门禁上线前，PDF 已隐藏。重新运行时，若仍大面积缺数，系统会在模型分析前停止。
                    </p>
                  ) : (
                    <p className="mt-1 text-xs">
                      可用标的 {props.run.analysis_gate?.eligible_count || 0}/{props.run.analysis_gate?.total_count || props.holdings.length}
                      （{coveragePercent}%）。未启动个股研究模型 Session，也未生成 PDF。
                    </p>
                  )}
                  <button
                    type="button"
                    onClick={() => void props.onRetry()}
                    disabled={props.starting}
                    className="mt-2 inline-flex items-center gap-1.5 rounded-md border border-amber-500/40 bg-background px-2.5 py-1.5 text-xs font-medium disabled:opacity-50"
                  >
                    {props.starting ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RotateCcw className="h-3.5 w-3.5" />}
                    {legacyDataLimited ? "按新门禁重新获取" : "数据恢复后重试"}
                  </button>
                </div>
              </div>
            </div>
          ) : props.run.warnings?.map((warning) => (
            <p key={warning} className="mt-2 text-xs text-amber-600">{warning}</p>
          ))}
          {props.run.input_outdated ? (
            <div className="mt-3 flex flex-wrap items-center justify-between gap-2 rounded-md border border-amber-500/30 bg-amber-500/10 p-2.5 text-xs text-amber-700 dark:text-amber-300">
              <span>运行期间持仓或组合设置已变化；当前报告仍严格基于冻结快照。</span>
              <button
                type="button"
                onClick={() => void startWithLatestSettings()}
                disabled={props.starting || active}
                className="inline-flex items-center gap-1.5 rounded-md border border-amber-500/40 bg-background px-2.5 py-1.5 font-medium disabled:opacity-50"
              >
                <RotateCcw className="h-3.5 w-3.5" />按最新配置重新生成
              </button>
            </div>
          ) : null}
          {!dataStopped && props.run.artifacts?.length ? (
            <div className="mt-3 flex flex-wrap gap-2">
              {props.run.artifacts
                .filter((item) => item.media_type === "application/pdf" && !item.expired && !item.superseded)
                .map((item) => (
                  <div key={item.artifact_id} className="inline-flex overflow-hidden rounded-md border bg-background">
                    <a
                      href={props.artifactUrl(props.run!.run_id, item.artifact_id)}
                      className="inline-flex items-center gap-1.5 px-2.5 py-1.5 text-xs font-medium hover:bg-muted"
                    >
                      <Download className="h-3.5 w-3.5" />
                      {item.kind === "master_pdf" ? "综合报告 PDF" : `${item.symbol || "个股"} PDF`}
                    </a>
                    {item.kind === "holding_daily_pdf" && item.symbol ? (
                      <button
                        type="button"
                        aria-label={`重试 ${item.symbol} 个股日报`}
                        title="只重试这只股票，并重算组合报告"
                        onClick={() => void props.onRetryHolding(item.symbol!)}
                        disabled={props.starting}
                        className="inline-flex items-center border-l px-2 text-muted-foreground hover:bg-muted hover:text-foreground disabled:opacity-50"
                      >
                        <RotateCcw className="h-3.5 w-3.5" />
                      </button>
                    ) : null}
                  </div>
                ))}
            </div>
          ) : null}
        </div>
      ) : null}

      <div className="mt-5 grid gap-3 xl:grid-cols-[minmax(280px,0.8fr)_minmax(0,2fr)]">
        <div className="rounded-md border bg-background/50 p-4">
          <div className="flex items-start justify-between gap-3">
            <div>
              <div className="flex items-center gap-2 text-sm font-semibold">
                <WalletCards className="h-4 w-4 text-emerald-500" />
                当前现金
              </div>
              <p className="mt-1 text-xs text-muted-foreground">直接维护，不再和导入持仓绑定。</p>
            </div>
            <span className="text-xs text-muted-foreground">{props.cashCurrency || "CNY"}</span>
          </div>
          <label className="mt-4 block text-xs text-muted-foreground">
            券商当前可用现金
            <div className="mt-1 grid gap-2 sm:grid-cols-[minmax(0,1fr)_auto]">
              <input
                aria-label="当前可用现金"
                data-testid="portfolio-current-cash"
                type="number"
                min="0"
                step="100"
                value={cashDraft}
                onChange={(event) => setCashDraft(event.target.value)}
                placeholder="输入当前现金"
                className="min-w-0 flex-1 rounded-md border bg-background px-3 py-2 text-base font-semibold text-foreground outline-none focus:ring-2 focus:ring-emerald-500/30"
              />
              <button
                type="button"
                onClick={() => parsedCash !== null && void props.onSaveCash(parsedCash, props.cashCurrency || "CNY")}
                disabled={parsedCash === null || props.savingCash || active}
                className="inline-flex items-center justify-center gap-1.5 rounded-md bg-emerald-600 px-3 py-2 text-sm font-medium text-white disabled:opacity-50"
              >
                {props.savingCash ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
                更新现金
              </button>
            </div>
          </label>
          <div className="mt-4 border-t pt-3">
            <label className="flex items-center justify-between gap-3 text-xs text-muted-foreground">
              <span>
                <span className="block font-medium text-foreground">晨会最低保留</span>
                高于底线的现金才纳入可分配资金
              </span>
              <input
                aria-label="启用现金最低保留"
                type="checkbox"
                checked={draft.cash_policy.configured}
                onChange={(event) => toggleCashFloor(event.target.checked)}
              />
            </label>
            <input
              aria-label="现金最低保留金额"
              type="number"
              min="0"
              step="100"
              value={draft.cash_policy.min_amount}
              onChange={(event) => updateCashFloor(event.target.value)}
              disabled={!draft.cash_policy.configured}
              className="mt-2 w-full rounded-md border bg-background px-3 py-2 text-sm text-foreground disabled:opacity-50"
            />
          </div>
        </div>

        <div className="rounded-md border bg-background/50 p-4">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
            <div>
              <div className="flex items-center gap-2 text-sm font-semibold">
                <Sparkles className="h-4 w-4 text-primary" />
                进攻 / 防守目标比例
              </div>
              <p className="mt-1 text-xs text-muted-foreground">AI 先给起点；你只需要左右拖动分界线。</p>
            </div>
            <button
              type="button"
              onClick={() => void applyAgentSuggestion()}
              disabled={props.saving || active}
              className="inline-flex items-center gap-1.5 self-start rounded-md border px-2.5 py-1.5 text-xs font-medium hover:bg-muted"
            >
              <RotateCcw className="h-3.5 w-3.5" />
              采用 AI 建议 {agentSuggestion.offensiveRatio}/{agentSuggestion.defensiveRatio}
            </button>
          </div>

          <div className="mt-5 grid grid-cols-2 gap-3">
            <div>
              <div className="text-xs font-medium text-red-500">进攻型</div>
              <div className="mt-1 text-2xl font-semibold tabular-nums text-red-500">{allocationRatio}%</div>
              <div className="text-xs text-muted-foreground">目标 {money(offensiveTarget)}</div>
            </div>
            <div className="text-right">
              <div className="text-xs font-medium text-blue-500">防守型</div>
              <div className="mt-1 text-2xl font-semibold tabular-nums text-blue-500">{100 - allocationRatio}%</div>
              <div className="text-xs text-muted-foreground">目标 {money(defensiveTarget)}</div>
            </div>
          </div>

          <div className="relative mt-4 py-3">
            <input
              aria-label="进攻型目标比例"
              data-testid="portfolio-allocation-slider"
              type="range"
              min="0"
              max="100"
              step="1"
              value={allocationRatio}
              onChange={(event) => {
                setAllocationRatio(Number(event.target.value));
                setSettingsDirty(true);
              }}
              className="h-3 w-full cursor-ew-resize appearance-none rounded-full outline-none focus-visible:ring-2 focus-visible:ring-primary/50 [&::-moz-range-thumb]:h-6 [&::-moz-range-thumb]:w-3 [&::-moz-range-thumb]:rounded-full [&::-moz-range-thumb]:border-2 [&::-moz-range-thumb]:border-background [&::-moz-range-thumb]:bg-white [&::-webkit-slider-thumb]:h-6 [&::-webkit-slider-thumb]:w-3 [&::-webkit-slider-thumb]:appearance-none [&::-webkit-slider-thumb]:rounded-full [&::-webkit-slider-thumb]:border-2 [&::-webkit-slider-thumb]:border-background [&::-webkit-slider-thumb]:bg-white [&::-webkit-slider-thumb]:shadow-md"
              style={{
                background: `linear-gradient(to right, #ef4444 0%, #ef4444 ${allocationRatio}%, #3b82f6 ${allocationRatio}%, #3b82f6 100%)`,
              }}
            />
          </div>

          <div className="mt-3 flex flex-col gap-2 border-t pt-3 text-xs text-muted-foreground sm:flex-row sm:items-center sm:justify-between">
            <span>AI 建议：{agentSuggestion.rationale}</span>
            <span className="shrink-0">可分配资金 {money(allocationBase)} · 自动设置 ±10% 调仓带</span>
          </div>
        </div>
      </div>

      <div className="mt-5 border-t pt-4">
        <div className="flex flex-col gap-2 sm:flex-row sm:items-end sm:justify-between">
          <div>
            <h3 className="text-sm font-semibold">持仓左右分区</h3>
            <p className="mt-1 text-xs text-muted-foreground">
              Agent 已先放好建议位置；按住股票卡片，拖过中线即可修改并锁定分类。
            </p>
          </div>
          <div className="text-xs text-muted-foreground">
            红色进攻 · 蓝色防守 · 箭头同样可以移动
          </div>
        </div>

        <div className="relative mt-3 grid grid-cols-2 gap-2 md:gap-4">
          {(["offensive", "defensive"] as const).map((sleeveId) => {
            const offensive = sleeveId === "offensive";
            const holdings = holdingsBySleeve[sleeveId];
            const total = holdings.reduce((sum, holding) => sum + holdingValue(holding), 0);
            const targetActive = dropTarget === sleeveId && Boolean(draggingSymbol);
            return (
              <section
                key={sleeveId}
                aria-label={`${offensive ? "进攻型" : "防守型"}持仓分区`}
                data-testid={`holding-zone-${sleeveId}`}
                onDragEnter={(event) => {
                  event.preventDefault();
                  if (draggingSymbol) setDropTarget(sleeveId);
                }}
                onDragOver={(event) => {
                  event.preventDefault();
                  event.dataTransfer.dropEffect = "move";
                }}
                onDrop={(event) => dropHolding(event, sleeveId)}
                className={`min-w-0 overflow-hidden rounded-lg border transition-colors ${
                  offensive
                    ? `border-red-500/30 bg-red-500/[0.04] ${targetActive ? "border-red-500 bg-red-500/10" : ""}`
                    : `border-blue-500/30 bg-blue-500/[0.04] ${targetActive ? "border-blue-500 bg-blue-500/10" : ""}`
                }`}
              >
                <div className={`h-1 w-full ${offensive ? "bg-red-500" : "bg-blue-500"}`} />
                <div className="flex items-center justify-between gap-2 border-b px-2.5 py-2.5 md:px-3">
                  <div className="flex min-w-0 items-center gap-2">
                    {offensive
                      ? <Swords className="h-4 w-4 shrink-0 text-red-500" />
                      : <ShieldCheck className="h-4 w-4 shrink-0 text-blue-500" />}
                    <div className="min-w-0">
                      <div className={`text-sm font-semibold ${offensive ? "text-red-500" : "text-blue-500"}`}>
                        {offensive ? "进攻型" : "防守型"}
                      </div>
                      <div className="truncate text-[11px] text-muted-foreground">
                        {holdings.length} 只 · {money(total)}
                      </div>
                    </div>
                  </div>
                  <span className="hidden text-[11px] text-muted-foreground sm:inline">
                    {offensive ? "争取收益弹性" : "控制组合波动"}
                  </span>
                </div>

                <div role="list" className="min-h-36 space-y-2 p-2 md:min-h-44 md:p-3">
                  {holdings.length === 0 ? (
                    <div className={`flex min-h-28 items-center justify-center rounded-md border border-dashed px-2 text-center text-xs ${
                      offensive ? "border-red-500/25 text-red-500/70" : "border-blue-500/25 text-blue-500/70"
                    }`}>
                      把股票拖到这里
                    </div>
                  ) : holdings.map((holding) => {
                    const symbol = symbolOf(holding);
                    const assignment = draft.assignments[symbol];
                    const targetSleeve = offensive ? "defensive" : "offensive";
                    const moving = movingSymbol === symbol;
                    return (
                      <article
                        key={symbol}
                        role="listitem"
                        draggable={!moving && !active}
                        data-testid={`holding-card-${symbol}`}
                        data-sleeve={sleeveId}
                        aria-busy={moving}
                        onDragStart={(event) => {
                          event.dataTransfer.effectAllowed = "move";
                          event.dataTransfer.setData("text/plain", symbol);
                          setDraggingSymbol(symbol);
                        }}
                        onDragEnd={() => {
                          setDraggingSymbol(null);
                          setDropTarget(null);
                        }}
                        className={`group flex min-w-0 items-center gap-1.5 rounded-md border bg-background px-2 py-2 shadow-sm transition ${
                          draggingSymbol === symbol ? "opacity-45" : "hover:-translate-y-0.5 hover:shadow-md"
                        }`}
                        title="按住并拖到另一侧，修改这只股票的分区"
                      >
                        <GripVertical className="hidden h-4 w-4 shrink-0 cursor-grab text-muted-foreground/60 group-active:cursor-grabbing sm:block" />
                        <div className="min-w-0 flex-1">
                          <div className="line-clamp-2 break-words text-[11px] font-medium leading-4 sm:block sm:truncate sm:text-sm">
                            {holding.name || symbol}
                          </div>
                          <div className="mt-0.5 flex min-w-0 items-center gap-1 text-[10px] text-muted-foreground sm:text-[11px]">
                            <span className="truncate">{symbol}</span>
                            <span className="hidden sm:inline" aria-hidden="true">·</span>
                            <span className="hidden shrink-0 sm:inline">
                              {assignment?.user_locked
                                ? "你的分类"
                                : `Agent 建议${assignment?.confidence ? ` ${Math.round(assignment.confidence * 100)}%` : ""}`}
                            </span>
                          </div>
                        </div>
                        <button
                          type="button"
                          aria-label={`将 ${symbol} 移至${offensive ? "防守型" : "进攻型"}`}
                          title={`移至${offensive ? "防守型" : "进攻型"}`}
                          disabled={moving || active}
                          onClick={() => void moveHolding(symbol, targetSleeve)}
                          className={`inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-md border transition-colors disabled:opacity-50 sm:h-7 sm:w-7 ${
                            offensive
                              ? "border-blue-500/30 text-blue-500 hover:bg-blue-500/10"
                              : "border-red-500/30 text-red-500 hover:bg-red-500/10"
                          }`}
                        >
                          {moving
                            ? <Loader2 className="h-3.5 w-3.5 animate-spin" />
                            : offensive
                              ? <ArrowRight className="h-3.5 w-3.5" />
                              : <ArrowLeft className="h-3.5 w-3.5" />}
                        </button>
                      </article>
                    );
                  })}
                </div>
              </section>
            );
          })}
          <div
            aria-hidden="true"
            className="pointer-events-none absolute bottom-2 left-1/2 top-2 hidden w-px -translate-x-1/2 bg-gradient-to-b from-red-500 via-muted-foreground/30 to-blue-500 md:block"
          />
        </div>
      </div>
    </section>
  );
}
