import { useCallback, useEffect, useId, useMemo, useRef, useState, type CSSProperties, type ReactNode } from "react";
import { createPortal } from "react-dom";
import {
  Activity,
  AlertTriangle,
  ArrowDownRight,
  ArrowRight,
  ArrowUpRight,
  BellRing,
  BrainCircuit,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  Clock3,
  Copy,
  Database,
  Eye,
  Info,
  KeyRound,
  Loader2,
  MessageSquare,
  Pause,
  Play,
  RefreshCw,
  Settings2,
  ShieldCheck,
  Volume2,
  VolumeX,
  Zap,
  X,
} from "lucide-react";
import { toast } from "sonner";

import {
  api,
  type MonitorDeliveryBindingAttempt,
  type MonitorDeliveryTarget,
  type MonitorDecisionChoice,
  type MonitorEffectAvailability,
  type MonitorEvent,
  type MonitorAutopilotConfig,
  type MonitorAutopilotRun,
  type MonitorRecommendation,
  type MonitorRecommendationFeedback,
  type MonitorPlan,
  type MonitorPlanVersion,
  type MonitorPlannerJob,
  type MonitorPriceVolumePolicy,
  type MonitorPriceVolumeSnapshot,
  type MonitorProfile,
  type MonitorReportCandidate,
  type MonitorRule,
  type MonitorTargetAssessment,
  type MonitorTargetIntent,
  type MonitorTargetMonitoringCard,
  type PortfolioHolding,
  type PortfolioMonitoringStatus,
} from "@/lib/api";
import { cn } from "@/lib/utils";
import { usePortfolioMonitorEffects } from "@/components/portfolio/PortfolioMonitorEffectsProvider";
import { MonitorUsagePanel } from "@/components/portfolio/MonitorUsagePanel";


const STATUS_LABELS: Record<string, string> = {
  drafting: "数据待补齐",
  pending_review: "待审核",
  active: "监控中",
  paused: "已暂停",
  expired: "已到期",
  closed: "已关闭",
  superseded: "已被新草案替代",
};

const RULE_LABELS: Record<string, string> = {
  price_cross_above: "价格向上突破",
  price_cross_below: "价格向下跌破",
  price_zone_enter: "进入价格区间",
  price_zone_exit: "离开价格区间",
  intraday_pct_change_above: "日内涨幅超过",
  intraday_pct_change_below: "日内跌幅低于",
  volume_ratio_above: "成交量比率超过",
};

const TARGET_INTENT_LABELS: Record<MonitorTargetIntent, string> = {
  buy_point: "买入点",
  add_position: "加仓点",
  stop_loss: "止损点",
  take_profit: "止盈点",
  watch: "观察点",
  breakout: "突破点",
};

const PRICE_VOLUME_REGIME_LABELS: Record<string, string> = {
  bullish_expansion: "上涨放量",
  bullish_contraction: "上涨缩量",
  bearish_expansion: "下跌放量",
  bearish_contraction: "下跌缩量",
  high_volume_absorption: "放量承接",
  high_volume_rejection: "放量受阻",
  high_volume_stall: "放量滞涨",
  low_volume_balance: "缩量平衡",
  neutral: "量价中性",
};

const PRICE_VOLUME_BIAS_LABELS: Record<string, string> = {
  bullish: "偏多",
  bearish: "偏空",
  mixed: "多空分歧",
  neutral: "中性",
};

const PRICE_VOLUME_CONFIDENCE_LABELS: Record<string, string> = {
  high: "高",
  medium: "中",
  low: "低",
};

const VOLUME_STATE_LABELS: Record<string, string> = {
  contraction: "缩量",
  contracted: "缩量",
  normal: "正常量能",
  expansion: "放量",
  expanded: "放量",
};

const PRICE_VOLUME_REASON_LABELS: Record<string, string> = {
  price_volume_mode_off: "量价功能尚未投递",
  price_volume_policy_disabled: "当前计划已关闭量价分析",
  bar_cache_query_failed: "K 线缓存读取失败",
  no_closed_5m_bar: "缺少闭合 5 分钟 K 线",
  no_closed_1m_bar: "缺少闭合 1 分钟 K 线",
  no_actionable_closed_bar: "缺少质量合格的闭合 K 线",
  stale_price_volume_bar: "最近合格的量价 K 线已过期",
  insufficient_same_time_baseline: "历史同时间桶样本不足",
  insufficient_recent_bars: "连续闭合 5 分钟 K 线不足",
  recent_bar_values_invalid: "近期 K 线价格或成交量无效",
  volume_conflict: "成交量来源冲突",
  volume_status_not_actionable: "成交量尚未形成可用共识",
  volume_unavailable: "成交量来源不可用",
  volume_unit_unknown: "成交量单位未知",
  volume_unit_conflict: "成交量单位冲突",
  volume_missing: "成交量缺失",
  close_missing: "收盘价缺失",
  bar_status_not_actionable: "K 线数据状态不可用",
  source_signature_missing: "行情来源签名缺失",
  source_signature_mismatch: "历史与当前来源不一致",
  no_price_volume_policy: "计划未配置量价分析",
  disabled_by_policy: "计划已关闭量价分析",
};

const TARGET_ASSESSMENT_LABELS: Record<MonitorTargetAssessment["decision"], string> = {
  supports_action: "量价支持",
  no_confirmation: "等待确认",
  opposes_add: "不宜补仓",
  insufficient_data: "量价数据不足",
};

type MonitorBoostParticleStyle = CSSProperties & {
  "--boost-left": string;
  "--boost-size": string;
  "--boost-delay": string;
  "--boost-duration": string;
  "--boost-drift": string;
  "--boost-opacity": string;
};

const MONITOR_BOOST_PARTICLES: MonitorBoostParticleStyle[] = [
  { "--boost-left": "5%", "--boost-size": "3px", "--boost-delay": "-0.1s", "--boost-duration": "1.5s", "--boost-drift": "18px", "--boost-opacity": "0.75" },
  { "--boost-left": "13%", "--boost-size": "5px", "--boost-delay": "-0.8s", "--boost-duration": "2.1s", "--boost-drift": "-12px", "--boost-opacity": "0.5" },
  { "--boost-left": "22%", "--boost-size": "2px", "--boost-delay": "-1.2s", "--boost-duration": "1.7s", "--boost-drift": "24px", "--boost-opacity": "0.9" },
  { "--boost-left": "31%", "--boost-size": "4px", "--boost-delay": "-0.4s", "--boost-duration": "2.3s", "--boost-drift": "-20px", "--boost-opacity": "0.6" },
  { "--boost-left": "40%", "--boost-size": "3px", "--boost-delay": "-1.5s", "--boost-duration": "1.9s", "--boost-drift": "15px", "--boost-opacity": "0.8" },
  { "--boost-left": "49%", "--boost-size": "2px", "--boost-delay": "-0.7s", "--boost-duration": "1.4s", "--boost-drift": "-16px", "--boost-opacity": "0.95" },
  { "--boost-left": "58%", "--boost-size": "5px", "--boost-delay": "-1.8s", "--boost-duration": "2.4s", "--boost-drift": "26px", "--boost-opacity": "0.45" },
  { "--boost-left": "67%", "--boost-size": "3px", "--boost-delay": "-0.2s", "--boost-duration": "1.8s", "--boost-drift": "-22px", "--boost-opacity": "0.8" },
  { "--boost-left": "76%", "--boost-size": "4px", "--boost-delay": "-1.1s", "--boost-duration": "2s", "--boost-drift": "14px", "--boost-opacity": "0.65" },
  { "--boost-left": "84%", "--boost-size": "2px", "--boost-delay": "-0.6s", "--boost-duration": "1.6s", "--boost-drift": "-10px", "--boost-opacity": "0.95" },
  { "--boost-left": "91%", "--boost-size": "4px", "--boost-delay": "-1.4s", "--boost-duration": "2.2s", "--boost-drift": "20px", "--boost-opacity": "0.55" },
  { "--boost-left": "96%", "--boost-size": "3px", "--boost-delay": "-0.9s", "--boost-duration": "1.7s", "--boost-drift": "-18px", "--boost-opacity": "0.85" },
];

function MonitorBoostParticles({ direction }: { direction: "up" | "down" }) {
  return (
    <div
      data-boost-particles="true"
      data-direction={direction}
      className="monitor-boost-particles pointer-events-none absolute inset-0 z-[1] overflow-hidden"
      aria-hidden="true"
    >
      {MONITOR_BOOST_PARTICLES.map((style, index) => (
        <span
          key={`boost-particle-${index}`}
          className="monitor-boost-particle absolute rounded-full"
          style={style}
        />
      ))}
    </div>
  );
}

function blockedReasonLabel(reason: string): string {
  const labels: Record<string, string> = {
    "quote_not_actionable:stale": "行情缓存已过期，尚未取得足够新的双源数据",
    "quote_not_actionable:single_source": "当前仍只有单一来源",
    "quote_not_actionable:source_lag": "行情来源时间不同步，暂不能用于监控计划",
    "quote_not_actionable:provisional_mix": "最新行情仍是临时混合结果，等待来源对齐",
    verified_quote_missing: "缺少可校验的最新行情",
    fresh_verified_quote_unavailable: "本轮未取得新鲜且已校验的行情",
    fresh_quote_unavailable: "本轮刷新后仍未取得可用行情",
    fresh_refresh_failed: "主动刷新行情失败，请稍后重试",
    fresh_refresh_interrupted: "主动刷新超过时间预算，请稍后重试",
    fresh_refresh_partial: "主动刷新只完成了一部分，当前证据仍不足",
    source_report_review_due: "来源周报已到复核时间，监控评估已暂停，等待新周报或人工复核",
    raw_price_basis_unavailable: "缺少可比较的原始价格口径",
    quote_provenance_missing: "行情缺少可追溯来源",
    verified_price_missing: "已校验行情中缺少有效价格",
    structural_report_refresh_failed_validation: "刷新后的结构化研究仍未通过监控点位门禁",
    structural_report_refresh_retry_exhausted: "结构化研究的一次自动修复额度已耗尽",
    structural_report_refresh_queue_failed: "结构化研究任务入队失败",
    report_has_no_qualified_monitoring_points: "报告没有形成合格的监控点位",
    deterministic_market_repair_requires_verified_market: "自动修复未取得双源校验行情",
    deterministic_market_repair_insufficient_daily_history: "自动修复缺少至少 20 个交易日的双源日线",
    deterministic_market_repair_no_reproducible_range: "自动修复无法形成可复算的近 20 日价格区间",
    deterministic_market_repair_no_plan: "自动修复未能生成确定性监控计划",
    price_series_discontinuity_unverified: "检测到未验证的价格折算断点",
    adjustment_factor_unverified: "折算因子尚未通过官方或双源校验",
    insufficient_post_event_history: "断点后的同口径历史数据不足",
    volume_unit_conflict: "成交量单位或双源口径仍有冲突",
    no_qualified_level: "多方法引擎尚未形成合格点位",
    ai_selection_invalid: "AI 选择未通过候选编号与数字白名单校验",
    recovery_circuit_open: "相同输入连续受阻，自动修复熔断已打开",
  };
  return labels[reason] || reason;
}

function SingleSourceWarning({ compact = false }: { compact?: boolean }) {
  return (
    <span
      role="status"
      aria-label="单源数据警告"
      className={cn(
        "inline-flex items-center gap-1 text-amber-700 dark:text-amber-300",
        compact
          ? "rounded border border-amber-500/40 bg-amber-500/10 px-1.5 py-0.5 text-[10px]"
          : "rounded-md border border-amber-500/40 bg-amber-500/10 px-2.5 py-2 text-xs",
      )}
    >
      <AlertTriangle className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
      此数据为单源，可能不准确
    </span>
  );
}

function statusClass(status: string): string {
  if (status === "active") return "border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300";
  if (status === "pending_review") return "border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-300";
  if (status === "closed" || status === "expired") return "border-muted text-muted-foreground";
  return "border-blue-500/30 bg-blue-500/10 text-blue-700 dark:text-blue-300";
}

function selectPlanVersion(
  profile: MonitorProfile,
  preferredVersion?: number | null,
): MonitorPlanVersion | undefined {
  const plans = profile.plans || [];
  if (preferredVersion != null) {
    const preferred = plans.find((version) => version.version === preferredVersion);
    if (preferred) return preferred;
  }
  if (profile.active_plan_version != null) {
    const active = plans.find((version) => version.version === profile.active_plan_version);
    if (active) return active;
  }
  return plans.find((version) => version.status === "pending_review")
    || plans.find((version) => version.status === "active")
    || plans[0];
}

function mergeMonitorProfileSummaries(
  current: MonitorProfile[],
  incoming: MonitorProfile[],
): MonitorProfile[] {
  const currentById = new Map(current.map((profile) => [profile.profile_id, profile]));
  return incoming.map((profile) => {
    const cached = currentById.get(profile.profile_id);
    if ((profile.plans?.length || 0) > 0 || !cached?.plans?.length) return profile;
    return { ...profile, plans: cached.plans };
  });
}

function formatDate(value?: string | null): string {
  if (!value) return "-";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
}

function formatBytes(value?: number | null): string {
  if (value == null || !Number.isFinite(value)) return "-";
  if (value < 1024) return `${value} B`;
  if (value < 1024 ** 2) return `${(value / 1024).toFixed(1)} KB`;
  if (value < 1024 ** 3) return `${(value / 1024 ** 2).toFixed(1)} MB`;
  return `${(value / 1024 ** 3).toFixed(2)} GB`;
}

function formatMilliseconds(value?: number | null): string {
  if (value == null || !Number.isFinite(value)) return "尚无样本";
  return value < 1000 ? `${Math.round(value)} ms` : `${(value / 1000).toFixed(1)} s`;
}

function formatRelativeTime(value: string | null | undefined, nowMs: number): string {
  if (!value) return "尚无记录";
  const parsed = new Date(value).getTime();
  if (!Number.isFinite(parsed)) return value;
  const deltaSeconds = Math.round((parsed - nowMs) / 1000);
  const absolute = Math.abs(deltaSeconds);
  if (absolute < 10) return deltaSeconds > 0 ? "即将" : "刚刚";
  if (absolute < 60) return deltaSeconds > 0 ? `${absolute} 秒后` : `${absolute} 秒前`;
  const minutes = Math.round(absolute / 60);
  if (minutes < 60) return deltaSeconds > 0 ? `${minutes} 分钟后` : `${minutes} 分钟前`;
  const hours = Math.round(minutes / 60);
  return deltaSeconds > 0 ? `${hours} 小时后` : `${hours} 小时前`;
}

function formatScheduleTime(value: string | null | undefined, nowMs: number): string {
  if (!value) return "尚未排程";
  const parsed = new Date(value).getTime();
  if (!Number.isFinite(parsed)) return value;
  const deltaSeconds = Math.round((parsed - nowMs) / 1000);
  if (deltaSeconds >= -10 && deltaSeconds <= 10) return "即将检查";
  const absolute = Math.abs(deltaSeconds);
  const duration = absolute < 60
    ? `${absolute} 秒`
    : absolute < 3600
      ? `${Math.round(absolute / 60)} 分钟`
      : `${Math.round(absolute / 3600)} 小时`;
  return deltaSeconds > 0 ? `${duration}后` : `已延迟 ${duration}`;
}

function quoteTierLabel(tier: MonitorPlan["quote_tier"]): string {
  if (tier === "active") return "每 1 分钟";
  if (tier === "normal") return "每 5 分钟";
  return "每 15 分钟";
}

function quoteTierSeconds(tier: MonitorPlan["quote_tier"]): number {
  if (tier === "active") return 60;
  if (tier === "normal") return 300;
  return 900;
}

function formatMonitorPrice(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "--";
  return value.toLocaleString(undefined, {
    minimumFractionDigits: value < 10 ? 3 : 2,
    maximumFractionDigits: value < 10 ? 4 : 2,
  });
}

function formatSignedBps(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "--";
  const rounded = Math.round(value);
  return `${rounded > 0 ? "+" : ""}${rounded}`;
}

function priceVolumeRegimeLabel(snapshot: MonitorPriceVolumeSnapshot): string {
  if (snapshot.accelerated_decline) return "放量加速下跌";
  if (!snapshot.regime) return snapshot.status === "ready" ? "量价中性" : "等待形成";
  return PRICE_VOLUME_REGIME_LABELS[snapshot.regime] || snapshot.regime;
}

function priceVolumeReasonText(reasonCodes: string[]): string {
  return reasonCodes.map((reason) => {
    const normalized = reason.replace(/^current_bar_/, "");
    return PRICE_VOLUME_REASON_LABELS[reason] || PRICE_VOLUME_REASON_LABELS[normalized] || reason;
  }).join("；");
}

function targetAssessmentTone(decision: MonitorTargetAssessment["decision"]): string {
  if (decision === "supports_action") {
    return "border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300";
  }
  if (decision === "opposes_add") {
    return "border-red-500/40 bg-red-500/10 text-red-700 dark:text-red-300";
  }
  if (decision === "no_confirmation") {
    return "border-amber-500/40 bg-amber-500/10 text-amber-800 dark:text-amber-200";
  }
  return "border-muted bg-muted/30 text-muted-foreground";
}

function monitorProfileDisplayName(
  profile: MonitorProfile,
  holdingNames: ReadonlyMap<string, string>,
): string {
  const normalizedSymbol = profile.symbol.trim().toUpperCase();
  const code = normalizedSymbol.split(".")[0];
  return holdingNames.get(normalizedSymbol) || holdingNames.get(code) || profile.symbol;
}

function monitorAutopilotRunDisplay(run: MonitorAutopilotRun, holdingNames: ReadonlyMap<string, string>) {
  const normalizedSymbol = run.symbol.trim().toUpperCase();
  const code = normalizedSymbol.split(".")[0];
  const payloadName = typeof run.payload.holding_name === "string"
    ? run.payload.holding_name.trim()
    : "";
  const name = payloadName || holdingNames.get(normalizedSymbol) || holdingNames.get(code) || "";
  return {
    code: code || run.symbol,
    name,
  };
}

const AUTOPILOT_TRIGGER_LABELS: Record<string, string> = {
  report_ready: "发现新报告",
  holdings_changed: "持仓或监控范围变化",
  scheduled_close: "收盘复核",
  approaching: "临近关键位",
  invalidated: "原方案失效",
  material_evidence_changed: "可信证据变化",
};

const AUTOPILOT_RUN_STATUS_LABELS: Record<string, string> = {
  queued: "等待中",
  running: "处理中",
  completed: "已更新",
  blocked: "门禁拦截",
  failed: "运行失败",
  cancelled: "已取消",
};

const AUTOPILOT_RUN_PREVIEW_LIMIT = 2;
const RECENT_EVENT_PREVIEW_LIMIT = 2;

type MonitorCarouselTarget =
  | {
    key: string;
    kind: "profile";
    symbol: string;
    profile: MonitorProfile;
    card: MonitorTargetMonitoringCard | null;
  }
  | {
    key: string;
    kind: "pending_autopilot";
    symbol: string;
    run: MonitorAutopilotRun | null;
    card: MonitorTargetMonitoringCard | null;
  };

function MonitorDecisionCard({
  card,
  busy,
  onChoice,
  onValidateDraft,
  onCancelDraft,
}: {
  card: MonitorTargetMonitoringCard;
  busy: string | null;
  onChoice: (choice: MonitorDecisionChoice) => void;
  onValidateDraft: (draftId: string) => void;
  onCancelDraft: (draftId: string) => void;
}) {
  const brief = card.decision_brief;
  const risk = card.risk_assessment;
  const draft = card.latest_draft;
  const riskTone = ["severe", "high"].includes(brief.risk_level)
    ? "border-red-500/40 bg-red-500/[0.04]"
    : ["medium", "warning"].includes(brief.risk_level)
      ? "border-amber-500/40 bg-amber-500/[0.04]"
      : "border-cyan-500/30 bg-cyan-500/[0.03]";
  return (
    <section aria-label={`${card.name} AI 决策摘要`} className={cn("grid gap-3 rounded-md border p-3", riskTone)}>
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <strong className="text-sm">{brief.headline}</strong>
            <span className="rounded-full border px-2 py-0.5 text-[10px]">风险 {brief.risk_level}</span>
            <span className="rounded-full border px-2 py-0.5 text-[10px] text-muted-foreground">置信度 {brief.confidence}</span>
          </div>
          <p className="mt-1 text-xs leading-5 text-muted-foreground">{brief.summary}</p>
        </div>
        <span className="text-[10px] text-muted-foreground">{brief.data_status === "verified" ? "证据已验证" : brief.data_status === "partial" ? "部分证据待补" : "数据受阻"}</span>
      </div>
      <ol className="grid gap-1 text-xs leading-5">
        {brief.why_now.slice(0, 3).map((item, index) => <li key={`${index}-${item}`}>{index + 1}. {item}</li>)}
      </ol>
      <div className="grid gap-2 rounded border bg-background/70 p-2.5 text-[11px] leading-5 sm:grid-cols-2">
        <p><strong>下一确认：</strong>{brief.next_confirmation}</p>
        <p><strong>当前判断失效：</strong>{brief.invalidation}</p>
        <p><strong>持仓影响：</strong>{risk.estimated_impact_pct != null ? `${(risk.estimated_impact_pct * 100).toFixed(2)}%` : "样本或持仓数据不足"}{risk.estimated_impact_amount != null ? ` / 约 ¥${risk.estimated_impact_amount.toFixed(2)}` : ""}</p>
        <p><strong>概率：</strong>{risk.risk_probability != null ? `${(risk.risk_probability * 100).toFixed(1)}%` : "不输出伪精确概率"}</p>
      </div>
      <div className="flex flex-wrap gap-1.5" aria-label="AI 决策选择">
        {card.available_choices.slice(0, 3).map((choice) => (
          <button
            key={choice.choice_id}
            type="button"
            disabled={busy === `decision:${card.decision_id}`}
            onClick={() => onChoice(choice)}
            title={choice.description}
            className={cn(
              "rounded-md border px-2.5 py-1.5 text-xs disabled:opacity-50",
              choice.recommended
                ? "border-cyan-500/50 bg-cyan-500/10 font-medium text-cyan-800 dark:text-cyan-200"
                : "bg-background hover:bg-muted",
            )}
          >
            {choice.recommended ? "AI 推荐 · " : ""}{choice.label}
          </button>
        ))}
      </div>
      <div className="flex flex-wrap gap-1.5 text-[10px] text-muted-foreground">
        {[...(card.level_ladder.support || []), ...(card.level_ladder.resistance || [])].slice(0, 5).map((level, index) => (
          <span key={String(level.candidate_id || `${level.role}-${index}`)} className="rounded border bg-background/70 px-2 py-1">
            {level.role || "结构"} {level.lower != null && level.upper != null ? `${level.lower}–${level.upper}` : "待计算"} · {level.score ?? "--"}分
          </span>
        ))}
      </div>
      {draft ? (
        <div className="flex flex-wrap items-center justify-between gap-2 rounded border bg-background/80 p-2 text-xs">
          <span>
            本地条件单草稿 · {draft.side === "buy" ? "买入" : "卖出"} · {draft.status}
            {draft.quantity != null ? ` · ${draft.quantity} 股` : " · 数量待风险设置"}
          </span>
          <span className="flex gap-1.5">
            {draft.status === "draft" ? <button type="button" onClick={() => onValidateDraft(draft.draft_id)} className="rounded border px-2 py-1 hover:bg-muted">校验草稿</button> : null}
            {!['cancelled', 'expired', 'stale'].includes(draft.status) ? <button type="button" onClick={() => onCancelDraft(draft.draft_id)} className="rounded border px-2 py-1 text-muted-foreground hover:bg-muted">撤销草稿</button> : null}
          </span>
          <span className="w-full text-[10px] text-muted-foreground">只保存在本应用，真实订单提交被禁止；触发不保证成交。</span>
        </div>
      ) : null}
    </section>
  );
}

function monitorAutopilotRunDetail(run: MonitorAutopilotRun): string {
  const detail = String(run.detail_error || run.validation_errors?.[0] || run.error || "");
  if (detail.includes("autopilot_recovery_circuit_open")) {
    return "相同输入已连续受阻，自动修复已熔断；等待新报告、持仓或证据变化后再尝试。";
  }
  if (detail.includes("structural_report_refresh_retry_exhausted")) {
    return "结构化研究的一次自动修复额度已耗尽，已停止继续调用模型。";
  }
  if (detail.includes("already above its invalidation level") || detail.includes("already below its invalidation level")) {
    return "当前价已经越过方案失效位，本次没有启用；等待新证据后重新计算。";
  }
  if (detail.includes("mapped source conditions must have an executable condition")) {
    return "报告条件没有完整映射成可执行观察条件，本次没有启用。";
  }
  if (detail.includes("metric is not allowed")) {
    return "AI 返回了不在白名单内的条件字段，本次没有启用。";
  }
  if (detail.includes("automatic_research_daily_limit")) {
    return "该标的今天已完成一次深度研究，等待新交易日或人工重试。";
  }
  if (run.blocked_reasons?.includes("report_revision_conflict")) {
    return "检测到多份报告修订冲突，当前计划保持不变。";
  }
  if (run.status === "blocked") return "数据或语义门禁未通过，当前计划保持不变。";
  if (run.status === "failed") return "本次自动运行失败，当前计划保持不变。";
  return "";
}

function monitorSymbolName(symbol: string, holdingNames: ReadonlyMap<string, string>): string {
  const normalizedSymbol = symbol.trim().toUpperCase();
  const code = normalizedSymbol.split(".")[0];
  const name = holdingNames.get(normalizedSymbol) || holdingNames.get(code) || "";
  return name || symbol;
}

function monitorSymbolDisplayName(symbol: string, holdingNames: ReadonlyMap<string, string>): string {
  const normalizedSymbol = symbol.trim().toUpperCase();
  const code = normalizedSymbol.split(".")[0];
  const name = monitorSymbolName(symbol, holdingNames);
  return name === symbol ? symbol : `${name}（${code}）`;
}

function isAutopilotEligibleSymbol(symbol: string): boolean {
  return /\.(?:SH|SZ|BJ)$/.test(symbol.trim().toUpperCase());
}

type PriceTarget = {
  id: string;
  value: number;
  direction: "up" | "down";
  intent: MonitorTargetIntent;
  level: number;
  rule: MonitorRule;
};

type PriceTargetBoundary = {
  value: number;
  source: "target" | "current";
  target: PriceTarget | null;
};

type PriceTargetWindow = {
  targets: PriceTarget[];
  left: PriceTargetBoundary;
  right: PriceTargetBoundary;
  span: number;
  currentPosition: number;
  openPosition: number | null;
  rangeState: "inside" | "above-last-target" | "below-first-target" | "single-point";
  boostDirection: "up" | "down" | null;
  crossedTargets: PriceTarget[];
  nextTarget: PriceTarget | null;
};

function finitePriceTarget(value: unknown): number | null {
  const number = Number(value);
  return Number.isFinite(number) && number > 0 ? number : null;
}

function positionInPriceDomain(
  price: number | null,
  minimum: number,
  maximum: number,
): number | null {
  if (price == null || !Number.isFinite(price) || maximum <= minimum) return null;
  return Math.min(100, Math.max(0, ((price - minimum) / (maximum - minimum)) * 100));
}

function inferredTargetIntent(rule: MonitorRule, direction: "up" | "down"): MonitorTargetIntent {
  if (rule.target_intent && TARGET_INTENT_LABELS[rule.target_intent]) return rule.target_intent;
  return direction === "up" ? "breakout" : "watch";
}

function collectPriceTargets(
  plan: MonitorPlan | null | undefined,
): PriceTarget[] {
  if (!plan) return [];
  const targets: Array<PriceTarget & { explicitLevel: boolean }> = [];
  for (const rule of plan.market_rules) {
    if (!rule.enabled) continue;
    if (rule.kind === "price_cross_above" || rule.kind === "price_cross_below") {
      const value = finitePriceTarget(rule.parameters.threshold);
      if (value == null) continue;
      const direction = rule.kind === "price_cross_above" ? "up" : "down";
      targets.push({
        id: rule.client_rule_id,
        value,
        direction,
        intent: inferredTargetIntent(rule, direction),
        level: rule.target_level || 0,
        rule,
        explicitLevel: Boolean(rule.target_level),
      });
    } else if (rule.kind === "price_zone_enter" || rule.kind === "price_zone_exit") {
      const lower = finitePriceTarget(rule.parameters.lower);
      const upper = finitePriceTarget(rule.parameters.upper);
      if (lower != null) {
        targets.push({
          id: `${rule.client_rule_id}:lower`,
          value: lower,
          direction: "down",
          intent: inferredTargetIntent(rule, "down"),
          level: rule.target_level || 0,
          rule,
          explicitLevel: Boolean(rule.target_level),
        });
      }
      if (upper != null) {
        targets.push({
          id: `${rule.client_rule_id}:upper`,
          value: upper,
          direction: "up",
          intent: inferredTargetIntent(rule, "up"),
          level: rule.target_level || 0,
          rule,
          explicitLevel: Boolean(rule.target_level),
        });
      }
    }
  }
  for (const direction of ["up", "down"] as const) {
    const directional = targets
      .filter((target) => target.direction === direction)
      .sort((left, right) => direction === "up" ? left.value - right.value : right.value - left.value);
    directional.forEach((target, index) => {
      if (!target.explicitLevel) target.level = index + 1;
    });
  }
  return targets
    .map((target) => ({
      id: target.id,
      value: target.value,
      direction: target.direction,
      intent: target.intent,
      level: target.level,
      rule: target.rule,
    }))
    .sort((left, right) => left.value - right.value);
}

function nearestPriceTarget(
  plan: MonitorPlan | null | undefined,
  currentPrice: number | null,
): PriceTarget | null {
  const targets = collectPriceTargets(plan);
  if (!targets.length) return null;
  if (currentPrice == null) return targets[0] || null;
  return targets.reduce((nearest, target) => (
    Math.abs(target.value - currentPrice) < Math.abs(nearest.value - currentPrice) ? target : nearest
  ));
}

function targetDistancePercentage(target: PriceTarget | null, currentPrice: number | null): number | null {
  if (!target || currentPrice == null || currentPrice === 0) return null;
  return ((target.value - currentPrice) / currentPrice) * 100;
}

function formatTargetDistancePercentage(value: number | null): string {
  if (value == null || !Number.isFinite(value)) return "距现价 --";
  const prefix = value > 0 ? "+" : "";
  return `距现价 ${prefix}${value.toFixed(2)}%`;
}

function compactTargetLabel(target: PriceTarget): string {
  return `L${target.level} ${TARGET_INTENT_LABELS[target.intent].replace("点", "")}`;
}

const TARGET_INTENT_TONE: Record<MonitorTargetIntent, {
  badge: string;
  line: string;
  dot: string;
}> = {
  buy_point: {
    badge: "border-cyan-500/40 bg-cyan-500/10 text-cyan-700 dark:text-cyan-300",
    line: "bg-cyan-500/70",
    dot: "bg-cyan-400",
  },
  add_position: {
    badge: "border-blue-500/40 bg-blue-500/10 text-blue-700 dark:text-blue-300",
    line: "bg-blue-500/70",
    dot: "bg-blue-400",
  },
  take_profit: {
    badge: "border-red-500/40 bg-red-500/10 text-red-700 dark:text-red-300",
    line: "bg-red-500/70",
    dot: "bg-red-400",
  },
  stop_loss: {
    badge: "border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-300",
    line: "bg-amber-500/70",
    dot: "bg-amber-400",
  },
  watch: {
    badge: "border-slate-500/40 bg-slate-500/10 text-slate-700 dark:text-slate-300",
    line: "bg-slate-500/70",
    dot: "bg-slate-400",
  },
  breakout: {
    badge: "border-violet-500/40 bg-violet-500/10 text-violet-700 dark:text-violet-300",
    line: "bg-violet-500/70",
    dot: "bg-violet-400",
  },
};

type MonitorLampState = {
  tone: "healthy" | "waiting" | "error";
  label: string;
};

function monitorLampState({
  profile,
  status,
  connected,
  marketScheduleSupported,
}: {
  profile: MonitorProfile;
  status: PortfolioMonitoringStatus | null;
  connected: boolean;
  marketScheduleSupported: boolean;
}): MonitorLampState {
  if (!connected) return { tone: "error", label: "监控连接异常" };
  if (profile.blocked_reasons.length > 0) return { tone: "error", label: "监控数据异常" };
  const healthy = Boolean(
    profile.status === "active"
    && status?.enabled_by_config
    && status.runtime.running
    && status.runtime.leader
    && status.effective_mode !== "off"
    && marketScheduleSupported
  );
  if (healthy) return { tone: "healthy", label: "监控运行正常" };
  return { tone: "waiting", label: "监控等待中" };
}

function derivePriceTargetWindow(
  plan: MonitorPlan | null | undefined,
  currentPrice: number | null,
  sessionOpen: number | null,
  previousPrice: number | null,
): PriceTargetWindow | null {
  if (currentPrice == null) return null;
  const targets = collectPriceTargets(plan);
  if (!targets.length) return null;
  const epsilon = Math.max(Math.abs(currentPrice) * 1e-8, 1e-8);
  const referencePrice = sessionOpen ?? previousPrice;
  const movement = referencePrice == null
    ? null
    : currentPrice > referencePrice + epsilon
      ? "up"
      : currentPrice < referencePrice - epsilon ? "down" : null;
  const below = targets.filter((target) => target.value < currentPrice - epsilon);
  const above = targets.filter((target) => target.value > currentPrice + epsilon);
  const exact = targets.filter((target) => Math.abs(target.value - currentPrice) <= epsilon);
  let leftTarget = below[below.length - 1] || null;
  let rightTarget = above[0] || null;
  if (exact.length) {
    if (movement === "down") rightTarget = exact[0];
    else leftTarget = exact[exact.length - 1] || null;
  }
  const left: PriceTargetBoundary = leftTarget
    ? { value: leftTarget.value, source: "target", target: leftTarget }
    : { value: currentPrice, source: "current", target: null };
  const right: PriceTargetBoundary = rightTarget
    ? { value: rightTarget.value, source: "target", target: rightTarget }
    : { value: currentPrice, source: "current", target: null };
  const span = Math.max(0, right.value - left.value);
  const currentPosition = span > 0
    ? Math.min(100, Math.max(0, ((currentPrice - left.value) / span) * 100))
    : 50;
  const rawOpenPosition = sessionOpen != null && span > 0
    ? ((sessionOpen - left.value) / span) * 100
    : null;
  const openPosition = rawOpenPosition == null
    ? null
    : Math.min(100, Math.max(0, rawOpenPosition));
  const crossedTargets = movement === "up"
    ? targets.filter((target) => target.direction === "up" && referencePrice != null && target.value > referencePrice + epsilon && target.value <= currentPrice + epsilon)
    : movement === "down"
      ? targets
        .filter((target) => target.direction === "down" && referencePrice != null && target.value < referencePrice - epsilon && target.value >= currentPrice - epsilon)
        .slice()
        .reverse()
      : [];
  const nextTarget = movement === "up"
    ? above.find((target) => target.direction === "up") || null
    : movement === "down"
      ? below.slice().reverse().find((target) => target.direction === "down") || null
      : null;
  const rangeState = span === 0
    ? "single-point"
    : right.source === "current"
      ? "above-last-target"
      : left.source === "current" ? "below-first-target" : "inside";
  return {
    targets,
    left,
    right,
    span,
    currentPosition,
    openPosition,
    rangeState,
    boostDirection: crossedTargets.length ? movement : null,
    crossedTargets,
    nextTarget,
  };
}

function percentageFromOpen(price: number | null, sessionOpen: number | null): number | null {
  if (price == null || sessionOpen == null || !Number.isFinite(price) || !Number.isFinite(sessionOpen) || sessionOpen === 0) {
    return null;
  }
  return ((price - sessionOpen) / sessionOpen) * 100;
}

function formatOpenPercentage(value: number | null): string {
  if (value == null || !Number.isFinite(value)) return "较开盘 --";
  const prefix = value > 0 ? "+" : "";
  return `较开盘 ${prefix}${value.toFixed(2)}%`;
}

function openMoveClass(value: number | null): string {
  if (value == null || Math.abs(value) < 0.005) return "text-muted-foreground";
  return value > 0
    ? "text-red-600 dark:text-red-400"
    : "text-emerald-600 dark:text-emerald-400";
}

function targetLabel(target: PriceTarget | null): string {
  if (!target) return "现价边界";
  return `L${target.level} · ${TARGET_INTENT_LABELS[target.intent]}`;
}

function ruleSummary(rule: MonitorRule): string {
  const value = ruleValue(rule);
  const semantic = rule.kind.startsWith("price_")
    ? `${TARGET_INTENT_LABELS[inferredTargetIntent(rule, rule.kind === "price_cross_above" ? "up" : "down")]} L${rule.target_level || 1} · `
    : "";
  if (rule.kind === "price_zone_enter" || rule.kind === "price_zone_exit") {
    const lower = rule.parameters.lower;
    const upper = rule.parameters.upper;
    return `${semantic}${RULE_LABELS[rule.kind] || rule.kind} ${lower ?? "-"}–${upper ?? "-"}`;
  }
  const suffix = rule.kind.includes("pct_change") ? "%" : "";
  return `${semantic}${RULE_LABELS[rule.kind] || rule.kind}${value == null ? "" : ` ${value}${suffix}`}`;
}

const SESSION_LABELS: Record<string, string> = {
  preopen: "盘前",
  morning: "上午交易",
  lunch: "午间休市",
  afternoon: "下午交易",
  closed: "已收盘",
  unknown: "未知",
};

function ruleValue(rule: MonitorRule): number | undefined {
  return rule.parameters.threshold
    ?? rule.parameters.threshold_pct
    ?? rule.parameters.ratio
    ?? rule.parameters.lower;
}

type MonitorPlanNumericDraft = {
  nearTriggerDistanceBps: string;
  priceVolumeContractionRatio: string;
  priceVolumeExpansionRatio: string;
  rulePrimaryValues: Record<string, string>;
  ruleConfirmationCounts: Record<string, string>;
  ruleCooldownMinutes: Record<string, string>;
};

function createMonitorPlanNumericDraft(plan: MonitorPlan): MonitorPlanNumericDraft {
  return {
    nearTriggerDistanceBps: String(plan.near_trigger_distance_bps),
    priceVolumeContractionRatio: plan.price_volume_policy
      ? String(plan.price_volume_policy.contraction_ratio)
      : "",
    priceVolumeExpansionRatio: plan.price_volume_policy
      ? String(plan.price_volume_policy.expansion_ratio)
      : "",
    rulePrimaryValues: Object.fromEntries(plan.market_rules.map((rule) => [
      rule.client_rule_id,
      ruleValue(rule) == null ? "" : String(ruleValue(rule)),
    ])),
    ruleConfirmationCounts: Object.fromEntries(plan.market_rules.map((rule) => [
      rule.client_rule_id,
      String(rule.parameters.confirmation_count),
    ])),
    ruleCooldownMinutes: Object.fromEntries(plan.market_rules.map((rule) => [
      rule.client_rule_id,
      String(rule.parameters.cooldown_minutes),
    ])),
  };
}

function monitorPlanFormSignature(plan: MonitorPlan, numericDraft: MonitorPlanNumericDraft): string {
  return JSON.stringify({ plan, numericDraft });
}

function setRulePrimaryValue(rule: MonitorRule, value: number): MonitorRule {
  const parameters = { ...rule.parameters };
  if (rule.kind.startsWith("price_cross")) parameters.threshold = value;
  else if (rule.kind.startsWith("intraday_pct")) parameters.threshold_pct = value;
  else if (rule.kind === "volume_ratio_above") parameters.ratio = value;
  else parameters.lower = value;
  return { ...rule, parameters };
}

function materializeAndValidateMonitorPlan(
  sourcePlan: MonitorPlan,
  numericDraft: MonitorPlanNumericDraft,
): { plan: MonitorPlan | null; errors: string[] } {
  const plan = structuredClone(sourcePlan);
  const errors: string[] = [];
  const parseNumber = (
    raw: string,
    label: string,
    options: { min?: number; max?: number; integer?: boolean; positive?: boolean } = {},
  ): number | null => {
    if (!raw.trim()) {
      errors.push(`${label}不能为空`);
      return null;
    }
    const value = Number(raw);
    if (!Number.isFinite(value)) {
      errors.push(`${label}必须是有效数字`);
      return null;
    }
    if (options.integer && !Number.isInteger(value)) errors.push(`${label}必须是整数`);
    if (options.positive && value <= 0) errors.push(`${label}必须大于 0`);
    if (options.min != null && value < options.min) errors.push(`${label}不能小于 ${options.min}`);
    if (options.max != null && value > options.max) errors.push(`${label}不能大于 ${options.max}`);
    return value;
  };

  const nearDistance = parseNumber(
    numericDraft.nearTriggerDistanceBps,
    "接近目标距离",
    { min: 10, max: 500, integer: true },
  );
  if (nearDistance != null) plan.near_trigger_distance_bps = nearDistance;

  if (plan.price_volume_policy) {
    const contraction = parseNumber(
      numericDraft.priceVolumeContractionRatio,
      "缩量阈值",
      { min: 0.1, max: 0.99 },
    );
    const expansion = parseNumber(
      numericDraft.priceVolumeExpansionRatio,
      "放量阈值",
      { min: 1.01, max: 10 },
    );
    if (contraction != null) plan.price_volume_policy.contraction_ratio = contraction;
    if (expansion != null) plan.price_volume_policy.expansion_ratio = expansion;
  }

  plan.market_rules = plan.market_rules.map((sourceRule) => {
    let rule = structuredClone(sourceRule);
    const ruleLabel = RULE_LABELS[rule.kind] || rule.kind;
    const primaryRaw = numericDraft.rulePrimaryValues[rule.client_rule_id] ?? "";
    if (rule.enabled) {
      const primary = parseNumber(
        primaryRaw,
        `${ruleLabel}主要阈值`,
        rule.kind.startsWith("price_") || rule.kind === "volume_ratio_above"
          ? { positive: true }
          : { min: -100, max: 100 },
      );
      if (primary != null) rule = setRulePrimaryValue(rule, primary);

      const confirmationCount = parseNumber(
        numericDraft.ruleConfirmationCounts[rule.client_rule_id] ?? "",
        `${ruleLabel}连续确认次数`,
        { min: 1, max: 3, integer: true },
      );
      const cooldownMinutes = parseNumber(
        numericDraft.ruleCooldownMinutes[rule.client_rule_id] ?? "",
        `${ruleLabel}冷却时间`,
        { min: 5, max: 1440, integer: true },
      );
      if (confirmationCount != null) rule.parameters.confirmation_count = confirmationCount;
      if (cooldownMinutes != null) rule.parameters.cooldown_minutes = cooldownMinutes;
    } else if (primaryRaw.trim() && Number.isFinite(Number(primaryRaw))) {
      rule = setRulePrimaryValue(rule, Number(primaryRaw));
    }
    return rule;
  });

  const enabledRules = plan.market_rules.filter((rule) => rule.enabled);
  if (!enabledRules.length) errors.push("至少需要启用一条行情规则");
  for (const [kind, label] of [
    ["price_cross_above", "上涨"],
    ["price_cross_below", "下跌"],
  ] as const) {
    if (enabledRules.filter((rule) => rule.kind === kind && rule.alert_cue === "ymca_v1").length > 1) {
      errors.push(`每份计划最多只能选择一条${label} YMCA 规则`);
    }
  }
  if (plan.market_rules.some((rule) => rule.alert_cue === "ymca_v1"
    && (!rule.enabled || (rule.kind !== "price_cross_above" && rule.kind !== "price_cross_below")))) {
    errors.push("YMCA 音效只能绑定到已启用的向上或向下突破规则");
  }
  for (const rule of enabledRules) {
    const ruleLabel = RULE_LABELS[rule.kind] || rule.kind;
    if (rule.kind === "price_zone_enter" || rule.kind === "price_zone_exit") {
      const lower = rule.parameters.lower;
      const upper = rule.parameters.upper;
      if (!Number.isFinite(lower) || !Number.isFinite(upper) || Number(upper) <= Number(lower)) {
        errors.push(`${ruleLabel}的区间上沿必须高于下沿`);
      }
    }
    if (rule.kind === "volume_ratio_above") {
      const ratio = rule.parameters.ratio;
      const clearRatio = rule.parameters.clear_ratio;
      if (clearRatio != null && ratio != null && clearRatio >= ratio) {
        errors.push(`${ruleLabel}的解除阈值必须低于触发阈值`);
      }
    }
  }
  if (enabledRules.some((rule) => rule.parameters.interval === "1m") && plan.quote_tier !== "active") {
    errors.push("启用 1 分钟 K 线规则时，服务器检查频次必须为每 1 分钟");
  }
  if (enabledRules.some((rule) => rule.parameters.interval === "5m") && plan.quote_tier === "low") {
    errors.push("启用 5 分钟 K 线规则时，服务器检查频次不能为每 15 分钟");
  }

  const orderedTargets = new Map<"up" | "down", Array<{ level: number; value: number }>>([
    ["up", []],
    ["down", []],
  ]);
  for (const rule of enabledRules) {
    const direction = rule.kind === "price_cross_above"
      ? "up"
      : rule.kind === "price_cross_below" ? "down" : null;
    const value = ruleValue(rule);
    if (direction && value != null) {
      orderedTargets.get(direction)?.push({ level: rule.target_level || 1, value });
    }
  }
  for (const [direction, targets] of orderedTargets) {
    const valuesByLevel = new Map<number, number[]>();
    for (const target of targets) {
      const values = valuesByLevel.get(target.level) || [];
      values.push(target.value);
      valuesByLevel.set(target.level, values);
    }
    const levels = [...valuesByLevel.keys()].sort((left, right) => left - right);
    for (let index = 1; index < levels.length; index += 1) {
      const previousLevel = levels[index - 1];
      const currentLevel = levels[index];
      const previousValues = valuesByLevel.get(previousLevel) || [];
      const currentValues = valuesByLevel.get(currentLevel) || [];
      if (direction === "up" && Math.min(...currentValues) <= Math.max(...previousValues)) {
        errors.push(`上行 L${currentLevel} 目标必须全部高于 L${previousLevel}`);
      }
      if (direction === "down" && Math.max(...currentValues) >= Math.min(...previousValues)) {
        errors.push(`下行 L${currentLevel} 目标必须全部低于 L${previousLevel}`);
      }
    }
  }

  const now = Date.now();
  const minimumValidUntil = now + 30 * 24 * 60 * 60 * 1_000;
  const maximumValidUntil = now + 365 * 24 * 60 * 60 * 1_000;
  const validUntil = Date.parse(plan.hard_valid_until);
  if (!Number.isFinite(validUntil)) errors.push("计划有效期不是有效日期");
  else if (!/(?:Z|[+-]\d{2}:\d{2})$/i.test(plan.hard_valid_until)) errors.push("计划有效期必须包含时区");
  else if (validUntil < minimumValidUntil || validUntil > maximumValidUntil) {
    errors.push("计划有效期必须在未来 30 至 365 天之间");
  }
  plan.market_rules.forEach((rule, index) => {
    const ruleLabel = `${RULE_LABELS[rule.kind] || rule.kind}（规则 ${index + 1}）`;
    const ruleValidUntil = Date.parse(rule.valid_until || "");
    if (!Number.isFinite(ruleValidUntil)) {
      errors.push(`${ruleLabel}缺少有效期`);
    } else {
      if (!/(?:Z|[+-]\d{2}:\d{2})$/i.test(rule.valid_until || "")) {
        errors.push(`${ruleLabel}有效期必须包含时区`);
      }
      if (ruleValidUntil < minimumValidUntil || ruleValidUntil > maximumValidUntil) {
        errors.push(`${ruleLabel}有效期必须在未来 30 至 365 天之间`);
      }
      if (Number.isFinite(validUntil) && ruleValidUntil > validUntil) {
        errors.push(`${ruleLabel}有效期不能晚于计划有效期`);
      }
    }
  });

  return { plan: errors.length ? null : plan, errors: [...new Set(errors)] };
}

type MonitorPlanDiffItem = {
  label: string;
  before: string;
  after: string;
};

function stableSerialize(value: unknown): string {
  if (value === undefined) return "undefined";
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(stableSerialize).join(",")}]`;
  const entries = Object.entries(value as Record<string, unknown>)
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([key, entry]) => `${JSON.stringify(key)}:${stableSerialize(entry)}`);
  return `{${entries.join(",")}}`;
}

function diffValue(value: unknown, fallback = "未配置"): string {
  if (value == null || value === "") return fallback;
  if (typeof value === "boolean") return value ? "启用" : "关闭";
  if (typeof value === "object") return stableSerialize(value);
  return String(value);
}

function dataModeLabel(value: MonitorPlan["data_mode"]): string {
  return value === "single_source" ? "单源模式" : "双源校验";
}

function buildMonitorPlanDiff(activePlan: MonitorPlan, pendingPlan: MonitorPlan): MonitorPlanDiffItem[] {
  const changes: MonitorPlanDiffItem[] = [];
  const append = (label: string, before: string, after: string) => {
    if (before !== after) changes.push({ label, before, after });
  };
  append("计划协议", `schema v${activePlan.schema_version}`, `schema v${pendingPlan.schema_version}`);
  append("证券标识", activePlan.symbol, pendingPlan.symbol);
  append("策略说明", activePlan.summary, pendingPlan.summary);
  append("数据模式", dataModeLabel(activePlan.data_mode), dataModeLabel(pendingPlan.data_mode));
  append("常态检查频次", quoteTierLabel(activePlan.quote_tier), quoteTierLabel(pendingPlan.quote_tier));
  append("接近目标频次", quoteTierLabel(activePlan.near_trigger_tier), quoteTierLabel(pendingPlan.near_trigger_tier));
  append(
    "接近目标距离",
    `${activePlan.near_trigger_distance_bps} bps`,
    `${pendingPlan.near_trigger_distance_bps} bps`,
  );
  append("计划有效期", formatDate(activePlan.hard_valid_until), formatDate(pendingPlan.hard_valid_until));
  const priceVolumeFields: Array<[keyof MonitorPriceVolumePolicy, string]> = [
    ["enabled", "量价分析 · 状态"],
    ["interval", "量价分析 · K 线粒度"],
    ["baseline_method", "量价分析 · 基线方法"],
    ["baseline_sessions", "量价分析 · 基线交易日"],
    ["min_samples", "量价分析 · 最少样本"],
    ["contraction_ratio", "量价分析 · 缩量阈值"],
    ["expansion_ratio", "量价分析 · 放量阈值"],
    ["flat_return_bps", "量价分析 · 横盘阈值"],
    ["acceleration_multiplier", "量价分析 · 加速倍数"],
  ];
  for (const [field, label] of priceVolumeFields) {
    append(
      label,
      diffValue(activePlan.price_volume_policy?.[field]),
      diffValue(pendingPlan.price_volume_policy?.[field]),
    );
  }
  append("新闻主题", diffValue(activePlan.news_topics, "无"), diffValue(pendingPlan.news_topics, "无"));
  append(
    "基本面监控",
    diffValue(activePlan.fundamental_monitor),
    diffValue(pendingPlan.fundamental_monitor),
  );
  append("证据备注", diffValue(activePlan.evidence_notes, "无"), diffValue(pendingPlan.evidence_notes, "无"));

  const activeRules = new Map(activePlan.market_rules.map((rule) => [rule.client_rule_id, rule]));
  const pendingRules = new Map(pendingPlan.market_rules.map((rule) => [rule.client_rule_id, rule]));
  const ruleIds = [...new Set([...activeRules.keys(), ...pendingRules.keys()])].sort();
  for (const ruleId of ruleIds) {
    const activeRule = activeRules.get(ruleId);
    const pendingRule = pendingRules.get(ruleId);
    const prefix = `规则 ${ruleId}`;
    if (!activeRule || !pendingRule) {
      append(
        prefix,
        activeRule ? stableSerialize(activeRule) : "不存在",
        pendingRule ? stableSerialize(pendingRule) : "不存在",
      );
      continue;
    }
    append(`${prefix} · 类型`, activeRule.kind, pendingRule.kind);
    append(`${prefix} · 启用状态`, diffValue(activeRule.enabled), diffValue(pendingRule.enabled));
    append(`${prefix} · 严重级别`, activeRule.severity, pendingRule.severity);
    append(
      `${prefix} · 目标类型`,
      activeRule.target_intent ? TARGET_INTENT_LABELS[activeRule.target_intent] : "未标注",
      pendingRule.target_intent ? TARGET_INTENT_LABELS[pendingRule.target_intent] : "未标注",
    );
    append(
      `${prefix} · 目标层级`,
      activeRule.target_level == null ? "未标注" : `L${activeRule.target_level}`,
      pendingRule.target_level == null ? "未标注" : `L${pendingRule.target_level}`,
    );
    append(`${prefix} · 提醒音效`, diffValue(activeRule.alert_cue, "none"), diffValue(pendingRule.alert_cue, "none"));
    const parameterFields: Array<[keyof MonitorRule["parameters"], string]> = [
      ["threshold", "价格阈值"],
      ["lower", "区间下沿"],
      ["upper", "区间上沿"],
      ["threshold_pct", "涨跌幅阈值"],
      ["ratio", "比率阈值"],
      ["clear_ratio", "解除比率"],
      ["baseline_method", "基线方法"],
      ["baseline_sessions", "基线交易日"],
      ["min_samples", "最少样本"],
      ["interval", "K 线粒度"],
      ["adjustment", "复权口径"],
      ["confirmation_count", "连续确认次数"],
      ["cooldown_minutes", "冷却时间"],
      ["clear_hysteresis_bps", "解除回差"],
    ];
    for (const [field, label] of parameterFields) {
      append(
        `${prefix} · ${label}`,
        diffValue(activeRule.parameters[field]),
        diffValue(pendingRule.parameters[field]),
      );
    }
    append(`${prefix} · 有效期`, diffValue(activeRule.valid_until), diffValue(pendingRule.valid_until));
    append(`${prefix} · 规则说明`, diffValue(activeRule.rationale), diffValue(pendingRule.rationale));
    append(
      `${prefix} · 点位依据`,
      diffValue(activeRule.calculation_basis),
      diffValue(pendingRule.calculation_basis),
    );
  }
  return changes;
}

function rulePointBasisText(rule: MonitorRule): string | null {
  const basis = rule.calculation_basis;
  const summary = basis?.summary || rule.rationale?.trim();
  if (!summary) return null;
  if (!basis) {
    return `${summary} 此旧计划版本未保存点位计算快照；在“计划与审核”中选择 AI 重新分析后，可查看精确日期、参考价格和公式。`;
  }
  const details = [summary];
  if (basis?.formula) details.push(`公式：${basis.formula}`);
  const currentValue = ruleValue(rule);
  if (
    basis
    && currentValue != null
    && Math.abs(currentValue - basis.recommended_value) > Math.max(Math.abs(basis.recommended_value) * 1e-8, 1e-8)
  ) {
    details.push(
      `当前阈值已手动调整为 ${formatMonitorPrice(currentValue)}；规则策略原始推荐为 ${formatMonitorPrice(basis.recommended_value)}。`,
    );
  }
  return details.join(" ");
}

function PointBasisTooltip({
  label,
  basis,
  children,
  className,
}: {
  label: string;
  basis: string | null;
  children: ReactNode;
  className?: string;
}) {
  const tooltipId = useId();
  const [position, setPosition] = useState<{
    left: number;
    top: number;
    width: number;
    placement: "top" | "bottom";
  } | null>(null);
  const show = (element: HTMLButtonElement) => {
    const bounds = element.getBoundingClientRect();
    const width = Math.min(320, Math.max(220, window.innerWidth - 24));
    const left = Math.min(
      window.innerWidth - width - 12,
      Math.max(12, bounds.left + bounds.width / 2 - width / 2),
    );
    const placement = bounds.top >= 170 ? "top" : "bottom";
    setPosition({
      left,
      top: placement === "top" ? bounds.top - 8 : bounds.bottom + 8,
      width,
      placement,
    });
  };
  if (!basis) return <>{children}</>;
  return (
    <>
      <button
        type="button"
        aria-label={`${label}，查看点位依据`}
        aria-describedby={position ? tooltipId : undefined}
        className={cn(
          "inline-flex min-w-0 cursor-help items-center gap-1 rounded-sm text-left outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-1",
          className,
        )}
        onMouseEnter={(event) => show(event.currentTarget)}
        onMouseLeave={() => setPosition(null)}
        onFocus={(event) => show(event.currentTarget)}
        onBlur={() => setPosition(null)}
      >
        {children}
        <Info className="h-3 w-3 shrink-0 opacity-65" aria-hidden="true" />
      </button>
      {position ? createPortal((
        <div
          id={tooltipId}
          role="tooltip"
          className="pointer-events-none fixed z-[100] rounded-md border border-border/70 bg-popover/85 px-3 py-2 text-left text-[11px] leading-5 text-popover-foreground shadow-xl backdrop-blur-md"
          style={{
            left: position.left,
            top: position.top,
            width: position.width,
            transform: position.placement === "top" ? "translateY(-100%)" : undefined,
          }}
        >
          <div className="mb-0.5 font-semibold">{label} · 点位依据</div>
          <div className="text-muted-foreground">{basis}</div>
        </div>
      ), document.body) : null}
    </>
  );
}

function PriceVolumeMeaningTooltip({
  label,
  interpretation,
  children,
}: {
  label: string;
  interpretation: NonNullable<MonitorPriceVolumeSnapshot["interpretation"]>;
  children: ReactNode;
}) {
  const tooltipId = useId();
  const [position, setPosition] = useState<{
    left: number;
    top: number;
    width: number;
    placement: "top" | "bottom";
  } | null>(null);
  const show = (element: HTMLButtonElement) => {
    const bounds = element.getBoundingClientRect();
    const width = Math.min(360, Math.max(260, window.innerWidth - 24));
    const left = Math.min(
      window.innerWidth - width - 12,
      Math.max(12, bounds.left + bounds.width / 2 - width / 2),
    );
    const placement = bounds.top >= 250 ? "top" : "bottom";
    setPosition({
      left,
      top: placement === "top" ? bounds.top - 8 : bounds.bottom + 8,
      width,
      placement,
    });
  };
  return (
    <>
      <button
        type="button"
        aria-label={`${label}，查看量价含义`}
        aria-describedby={position ? tooltipId : undefined}
        className="inline-flex cursor-help items-center gap-1 rounded outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-1"
        onMouseEnter={(event) => show(event.currentTarget)}
        onMouseLeave={() => setPosition(null)}
        onFocus={(event) => show(event.currentTarget)}
        onBlur={() => setPosition(null)}
      >
        {children}
        <Info className="h-3 w-3 opacity-65" aria-hidden="true" />
      </button>
      {position ? createPortal((
        <div
          id={tooltipId}
          role="tooltip"
          data-testid="price-volume-meaning-tooltip"
          className="pointer-events-none fixed z-[100] rounded-lg border border-border/70 bg-popover/85 p-3 text-left text-[11px] leading-5 text-popover-foreground shadow-2xl backdrop-blur-md"
          style={{
            left: position.left,
            top: position.top,
            width: position.width,
            transform: position.placement === "top" ? "translateY(-100%)" : undefined,
          }}
        >
          <div className="mb-2 flex items-center justify-between gap-2">
            <span className="font-semibold">{label} · 量价含义</span>
            <span className="rounded border px-1.5 py-0.5 text-[10px] text-muted-foreground">
              置信度 {PRICE_VOLUME_CONFIDENCE_LABELS[interpretation.confidence] || interpretation.confidence}
            </span>
          </div>
          <dl className="grid gap-1.5">
            <div><dt className="inline font-medium">当前偏向：</dt><dd className="inline text-muted-foreground">{PRICE_VOLUME_BIAS_LABELS[interpretation.bias] || interpretation.bias}</dd></div>
            <div><dt className="inline font-medium">代表含义：</dt><dd className="inline text-muted-foreground">{interpretation.meaning}</dd></div>
            <div><dt className="inline font-medium">主要风险：</dt><dd className="inline text-muted-foreground">{interpretation.risk}</dd></div>
            <div><dt className="inline font-medium">下一步确认：</dt><dd className="inline text-muted-foreground">{interpretation.next_confirmation}</dd></div>
          </dl>
          <div className="mt-2 border-t pt-2 text-[10px] text-muted-foreground">
            这是量价分类解释，不会自动改写报告点位或触发交易。
          </div>
        </div>
      ), document.body) : null}
    </>
  );
}

function MonitorRuleEditor({
  rule,
  onChange,
  primaryValue,
  confirmationCountValue,
  cooldownMinutesValue,
  onPrimaryValueChange,
  onConfirmationCountChange,
  onCooldownMinutesChange,
  disabled,
}: {
  rule: MonitorRule;
  onChange: (next: MonitorRule) => void;
  primaryValue: string;
  confirmationCountValue: string;
  cooldownMinutesValue: string;
  onPrimaryValueChange: (value: string) => void;
  onConfirmationCountChange: (value: string) => void;
  onCooldownMinutesChange: (value: string) => void;
  disabled: boolean;
}) {
  const priceTargetRule = rule.kind.startsWith("price_");
  const targetIntent = priceTargetRule
    ? inferredTargetIntent(rule, rule.kind === "price_cross_above" ? "up" : "down")
    : null;
  const pointBasis = priceTargetRule ? rulePointBasisText(rule) : null;
  return (
    <div className="grid gap-3 rounded-md border p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <label className="flex items-center gap-2 text-sm font-medium">
          <input
            type="checkbox"
            checked={rule.enabled}
            disabled={disabled}
            onChange={(event) => onChange({ ...rule, enabled: event.target.checked })}
          />
          {RULE_LABELS[rule.kind] || rule.kind}
        </label>
        <span className="text-xs text-muted-foreground">{rule.severity} · {rule.parameters.interval} 闭合 bar</span>
      </div>
      <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-5">
        <label className="grid gap-1 text-xs text-muted-foreground">
          主要阈值
          <input
            aria-label={`${RULE_LABELS[rule.kind] || rule.kind}主要阈值`}
            type="number"
            step="any"
            value={primaryValue}
            disabled={disabled}
            onChange={(event) => onPrimaryValueChange(event.target.value)}
            className="rounded-md border bg-background px-2 py-1.5 font-mono text-foreground"
          />
        </label>
        {priceTargetRule && targetIntent ? (
          <label className="grid gap-1 text-xs text-muted-foreground">
            目标类型
            <select
              aria-label={`${RULE_LABELS[rule.kind] || rule.kind}目标类型`}
              value={targetIntent}
              disabled={disabled}
              onChange={(event) => onChange({
                ...rule,
                target_intent: event.target.value as MonitorTargetIntent,
              })}
              className="rounded-md border bg-background px-2 py-1.5 text-foreground disabled:opacity-60"
            >
              {Object.entries(TARGET_INTENT_LABELS).map(([intent, label]) => (
                <option key={intent} value={intent}>{label}</option>
              ))}
            </select>
          </label>
        ) : null}
        <label className="grid gap-1 text-xs text-muted-foreground">
          K 线粒度
          <select
            aria-label={`${RULE_LABELS[rule.kind] || rule.kind}K线粒度`}
            value={rule.parameters.interval}
            disabled={disabled}
            onChange={(event) => onChange({
              ...rule,
              parameters: {
                ...rule.parameters,
                interval: event.target.value as "1m" | "5m",
              },
            })}
            className="rounded-md border bg-background px-2 py-1.5 text-foreground disabled:opacity-60"
          >
            <option value="1m">1 分钟闭合 K 线</option>
            <option value="5m">5 分钟闭合 K 线</option>
          </select>
        </label>
        <label className="grid gap-1 text-xs text-muted-foreground">
          连续确认次数
          <input
            type="number"
            min={1}
            max={3}
            value={confirmationCountValue}
            disabled={disabled}
            onChange={(event) => onConfirmationCountChange(event.target.value)}
            className="rounded-md border bg-background px-2 py-1.5 font-mono text-foreground"
          />
        </label>
        <label className="grid gap-1 text-xs text-muted-foreground">
          冷却（分钟）
          <input
            type="number"
            min={5}
            max={1440}
            value={cooldownMinutesValue}
            disabled={disabled}
            onChange={(event) => onCooldownMinutesChange(event.target.value)}
            className="rounded-md border bg-background px-2 py-1.5 font-mono text-foreground"
          />
        </label>
      </div>
      {priceTargetRule ? (
        <div className="text-[11px] font-medium text-muted-foreground">
          目标层级 L{rule.target_level || 1} · {targetIntent ? TARGET_INTENT_LABELS[targetIntent] : "观察点"}
        </div>
      ) : null}
      {pointBasis ? (
        <div
          aria-label={`${RULE_LABELS[rule.kind] || rule.kind} 策略推荐点位依据`}
          className="rounded-md border border-cyan-500/25 bg-cyan-500/5 px-3 py-2 text-xs"
        >
          <div className="flex items-center gap-1.5 font-medium text-foreground">
            <Info className="h-3.5 w-3.5 text-cyan-600 dark:text-cyan-400" aria-hidden="true" />
            策略推荐点位依据
            {rule.calculation_basis?.method_label ? (
              <span className="font-normal text-muted-foreground">· {rule.calculation_basis.method_label}</span>
            ) : null}
          </div>
          <p className="mt-1 leading-5 text-muted-foreground">{pointBasis}</p>
        </div>
      ) : rule.rationale ? <p className="text-xs text-muted-foreground">{rule.rationale}</p> : null}
    </div>
  );
}

function MonitorPriceVolumePolicyEditor({
  policy,
  contractionRatioValue,
  expansionRatioValue,
  disabled,
  onChange,
  onContractionRatioChange,
  onExpansionRatioChange,
}: {
  policy: MonitorPriceVolumePolicy;
  contractionRatioValue: string;
  expansionRatioValue: string;
  disabled: boolean;
  onChange: (next: MonitorPriceVolumePolicy) => void;
  onContractionRatioChange: (value: string) => void;
  onExpansionRatioChange: (value: string) => void;
}) {
  return (
    <div aria-label="量价分析设置" className="grid gap-3 rounded-md border p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2 text-sm font-semibold">
            <Activity className="h-4 w-4 text-cyan-600 dark:text-cyan-400" aria-hidden="true" />
            量价分析
          </div>
          <p className="mt-1 text-xs leading-5 text-muted-foreground">
            使用闭合 5 分钟 K 线和历史同时间桶成交量中位数，只提供目标位二次确认，不改变原价格提醒。
          </p>
        </div>
        <label className="inline-flex items-center gap-2 text-xs font-medium">
          <input
            aria-label="启用量价分析"
            type="checkbox"
            checked={policy.enabled}
            disabled={disabled}
            onChange={(event) => onChange({ ...policy, enabled: event.target.checked })}
          />
          {policy.enabled ? "已启用" : "已关闭"}
        </label>
      </div>
      <div className="grid gap-3 sm:grid-cols-2">
        <label className="grid gap-1 text-xs text-muted-foreground">
          缩量阈值
          <input
            aria-label="量价缩量阈值"
            type="number"
            min={0.1}
            max={0.99}
            step={0.05}
            value={contractionRatioValue}
            disabled={disabled || !policy.enabled}
            onChange={(event) => onContractionRatioChange(event.target.value)}
            className="rounded-md border bg-background px-3 py-2 font-mono text-sm text-foreground disabled:opacity-60"
          />
        </label>
        <label className="grid gap-1 text-xs text-muted-foreground">
          放量阈值
          <input
            aria-label="量价放量阈值"
            type="number"
            min={1.01}
            max={10}
            step={0.05}
            value={expansionRatioValue}
            disabled={disabled || !policy.enabled}
            onChange={(event) => onExpansionRatioChange(event.target.value)}
            className="rounded-md border bg-background px-3 py-2 font-mono text-sm text-foreground disabled:opacity-60"
          />
        </label>
      </div>
      <div className="text-[11px] leading-5 text-muted-foreground">
        固定口径：{policy.interval} · 前 {policy.baseline_sessions} 个有效交易日同时间桶中位数 · 至少 {policy.min_samples} 个样本 · 横盘 ±{policy.flat_return_bps} bps · 加速倍数 {policy.acceleration_multiplier.toFixed(2)}
      </div>
    </div>
  );
}

function MonitorPlanVersionNavigator({
  profile,
  selectedVersion,
  selectedPlan,
  onSelectVersion,
}: {
  profile: MonitorProfile;
  selectedVersion: MonitorPlanVersion | undefined;
  selectedPlan: MonitorPlan | null;
  onSelectVersion: (version: number) => void;
}) {
  const [diffExpanded, setDiffExpanded] = useState(false);
  const versions = [...(profile.plans || [])].sort((left, right) => right.version - left.version);
  const activeVersion = profile.active_plan_version == null
    ? versions.find((item) => item.status === "active")
    : versions.find((item) => item.version === profile.active_plan_version);
  const pendingVersions = versions.filter((item) => item.status === "pending_review");
  const historyVersions = versions.filter((item) => (
    item.version !== activeVersion?.version && item.status !== "pending_review"
  ));
  const diff = selectedVersion?.status === "pending_review" && activeVersion && selectedPlan
    ? buildMonitorPlanDiff(activeVersion.plan, selectedPlan)
    : [];
  const visibleDiff = diffExpanded ? diff : diff.slice(0, 12);
  useEffect(() => {
    setDiffExpanded(false);
  }, [activeVersion?.version, selectedVersion?.version]);
  if (!versions.length) return null;
  const versionButton = (item: MonitorPlanVersion, label: string) => (
    <button
      key={item.version}
      type="button"
      aria-label={`${label} v${item.version}`}
      aria-pressed={selectedVersion?.version === item.version}
      onClick={() => onSelectVersion(item.version)}
      className={cn(
        "rounded-md border px-2.5 py-1.5 text-xs hover:bg-muted",
        selectedVersion?.version === item.version && "border-primary bg-primary/10 text-primary",
      )}
    >
      {label} · v{item.version}
    </button>
  );
  return (
    <div className="grid gap-3 rounded-md border p-4" aria-label="监控计划版本">
      <div className="flex flex-wrap items-center gap-2">
        <span className="mr-1 text-xs font-semibold">计划版本</span>
        {activeVersion ? versionButton(activeVersion, "运行版") : null}
        {pendingVersions.map((item) => versionButton(item, "待审核版"))}
        {historyVersions.length ? (
          <details className="relative">
            <summary className="cursor-pointer rounded-md border px-2.5 py-1.5 text-xs hover:bg-muted">
              历史版本 {historyVersions.length}
            </summary>
            <div className="mt-2 flex max-w-full flex-wrap gap-2 rounded-md border bg-background p-2 shadow-sm">
              {historyVersions.map((item) => versionButton(
                item,
                STATUS_LABELS[item.status] || item.status,
              ))}
            </div>
          </details>
        ) : null}
      </div>
      {selectedVersion?.status === "pending_review" && activeVersion ? (
        <div className="grid gap-2 border-t pt-3" aria-label={`运行版 v${activeVersion.version} 与待审核版 v${selectedVersion.version} 差异`}>
          <div className="flex items-center justify-between gap-2 text-xs">
            <span className="font-semibold">相对运行版的变化</span>
            <span className="text-muted-foreground">{diff.length} 项</span>
          </div>
          {diff.length ? (
            <div className="grid gap-1.5">
              {visibleDiff.map((item) => (
                <div key={`${item.label}:${item.before}:${item.after}`} className="grid gap-1 rounded-md bg-muted/30 px-2.5 py-2 text-xs sm:grid-cols-[minmax(7rem,0.7fr)_minmax(0,1fr)_auto_minmax(0,1fr)] sm:items-center">
                  <span className="font-medium">{item.label}</span>
                  <span className="break-words text-muted-foreground">{item.before}</span>
                  <ArrowRight className="hidden h-3.5 w-3.5 text-muted-foreground sm:block" aria-hidden="true" />
                  <span className="break-words text-foreground">{item.after}</span>
                </div>
              ))}
              {diff.length > 12 ? (
                <button
                  type="button"
                  aria-expanded={diffExpanded}
                  onClick={() => setDiffExpanded((current) => !current)}
                  className="justify-self-start rounded-md border px-2.5 py-1.5 text-xs font-medium hover:bg-muted"
                >
                  {diffExpanded ? "收起差异" : `展开全部 ${diff.length} 项差异`}
                </button>
              ) : null}
            </div>
          ) : <p className="text-xs text-muted-foreground">待审核版与当前运行版没有结构化差异。</p>}
        </div>
      ) : null}
    </div>
  );
}

function MonitorPlanDrawer({
  profile,
  displayName,
  version,
  plan,
  numericDraft,
  formErrors,
  isDirty,
  busy,
  loading,
  onChange,
  onNumericChange,
  onSelectVersion,
  onClose,
  onSave,
  onSaveAndActivate,
  onReanalyze,
  onRecheck,
  onUseSingleSource,
  ymcaAvailability,
}: {
  profile: MonitorProfile;
  displayName: string;
  version: MonitorPlanVersion | undefined;
  plan: MonitorPlan | null;
  numericDraft: MonitorPlanNumericDraft | null;
  formErrors: string[];
  isDirty: boolean;
  busy: boolean;
  loading: boolean;
  onChange: (next: MonitorPlan) => void;
  onNumericChange: (next: MonitorPlanNumericDraft) => void;
  onSelectVersion: (version: number) => void;
  onClose: () => void;
  onSave: () => void;
  onSaveAndActivate: () => void;
  onReanalyze: () => void;
  onRecheck: () => void;
  onUseSingleSource: () => void;
  ymcaAvailability?: MonitorEffectAvailability;
}) {
  const editable = version?.status === "pending_review";
  const requiresOneMinutePolling = Boolean(plan?.market_rules.some(
    (rule) => rule.enabled && rule.parameters.interval === "1m",
  ));
  const upYmcaRuleId = plan?.market_rules.find(
    (rule) => rule.kind === "price_cross_above" && rule.alert_cue === "ymca_v1",
  )?.client_rule_id || "";
  const downYmcaRuleId = plan?.market_rules.find(
    (rule) => rule.kind === "price_cross_below" && rule.alert_cue === "ymca_v1",
  )?.client_rule_id || "";
  const ymcaSchemaSupported = Boolean(plan && plan.schema_version >= 3);
  const upStickerReady = ymcaAvailability?.up_sticker_ready ?? ymcaAvailability?.sticker_ready;
  const downStickerReady = ymcaAvailability?.down_sticker_ready ?? ymcaAvailability?.sticker_ready;
  const audioUnavailable = ymcaAvailability?.audio_ready === false;
  const upAssetsUnavailable = Boolean(upYmcaRuleId && (audioUnavailable || upStickerReady === false));
  const downAssetsUnavailable = Boolean(downYmcaRuleId && (audioUnavailable || downStickerReady === false));
  const selectedYmcaAssetsUnavailable = upAssetsUnavailable || downAssetsUnavailable;
  return createPortal((
    <div className="fixed inset-0 z-50 flex justify-end bg-background/70 backdrop-blur-sm" role="presentation" onMouseDown={onClose}>
      <aside
        role="dialog"
        aria-modal="true"
        aria-label={`${profile.symbol} 监控计划`}
        className="h-full w-full max-w-2xl overflow-y-auto border-l bg-background shadow-2xl"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <div className="sticky top-0 z-10 flex items-center justify-between border-b bg-background/95 px-5 py-4 backdrop-blur">
          <div>
            <div className="text-xs text-muted-foreground">
              计划与审核 · {" "}
              {version && version.schema_version >= 3 && version.model_id === "evidence-policy-v3"
                ? "规则策略生成 · evidence-policy-v3"
                : "旧版规则策略 · 待重新分析升级"}
              {` · v${version?.version || "-"}`}
              {isDirty ? " · 有未保存修改" : ""}
            </div>
            <h3 className="mt-1 text-lg font-semibold">{displayName}</h3>
            <div className="mt-0.5 font-mono text-xs font-medium text-muted-foreground">{profile.symbol}</div>
          </div>
          <button type="button" aria-label="关闭监控计划" onClick={onClose} className="rounded-md border p-2 hover:bg-muted">
            <X className="h-4 w-4" />
          </button>
        </div>
        <div className="grid gap-5 p-5">
          {profile.blocked_reasons.length ? (
            <div className="rounded-md border border-amber-500/40 bg-amber-500/10 p-4 text-sm">
              <div className="font-medium">已自动刷新，但数据仍不足</div>
              <ul className="mt-2 list-disc space-y-1 pl-5 text-muted-foreground">
                {profile.blocked_reasons.map((reason) => <li key={reason}>{blockedReasonLabel(reason)}</li>)}
              </ul>
              {profile.blocked_reasons.includes("quote_not_actionable:single_source") ? (
                <button
                  type="button"
                  disabled={busy}
                  onClick={onUseSingleSource}
                  className="mt-3 inline-flex items-center gap-2 rounded-md border border-amber-500/50 bg-background px-3 py-2 text-xs font-medium text-amber-800 hover:bg-amber-500/10 disabled:opacity-50 dark:text-amber-200"
                >
                  {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <AlertTriangle className="h-3.5 w-3.5" />}
                  同意使用单源模式
                </button>
              ) : null}
            </div>
          ) : null}
          {loading && !plan ? (
            <div className="flex items-center justify-center gap-2 rounded-md border border-dashed p-8 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" />正在加载监控计划…
            </div>
          ) : plan ? (
            <>
              {profile.status !== "closed" ? (
                <div className="grid gap-3 rounded-md border border-cyan-500/30 bg-cyan-500/5 p-4 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center">
                  <div>
                    <div className="flex items-center gap-2 text-sm font-medium"><BrainCircuit className="h-4 w-4 text-cyan-600 dark:text-cyan-300" />AI 重新分析</div>
                    <p className="mt-1 text-xs leading-5 text-muted-foreground">
                      根据最新行情重新生成一份待审核草案；当前运行版会继续监控。手动修改请在待审核版中完成。
                    </p>
                  </div>
                  <button type="button" disabled={busy} onClick={onReanalyze} className="inline-flex items-center justify-center gap-2 rounded-md border border-cyan-500/40 bg-background px-3 py-2 text-xs font-medium text-cyan-700 hover:bg-cyan-500/10 disabled:opacity-50 dark:text-cyan-300">
                    {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}
                    {busy ? "正在重新分析…" : "AI 重新分析"}
                  </button>
                </div>
              ) : null}
              <MonitorPlanVersionNavigator
                profile={profile}
                selectedVersion={version}
                selectedPlan={plan}
                onSelectVersion={onSelectVersion}
              />
              {plan.data_mode === "single_source" ? <SingleSourceWarning /> : null}
              {plan.analysis_ref ? (
                <div className="grid gap-3 rounded-md border border-cyan-500/30 bg-cyan-500/[0.03] p-4">
                  <div className="flex flex-wrap items-start justify-between gap-2">
                    <div>
                      <div className="text-xs text-muted-foreground">来源报告 · 冻结快照</div>
                      <div className="mt-1 text-sm font-medium">{plan.analysis_ref.title}</div>
                    </div>
                    <span className="rounded border px-2 py-1 text-xs">{plan.analysis_ref.quality_status}</span>
                  </div>
                  <div className="grid gap-1 text-xs text-muted-foreground sm:grid-cols-2">
                    <span>报告类型：{plan.analysis_ref.report_type}</span>
                    <span>数据截至：{formatDate(plan.analysis_ref.data_as_of)}</span>
                    <span>修订：v{plan.analysis_ref.revision}</span>
                    <span className="truncate font-mono" title={plan.analysis_ref.body_sha256}>正文哈希：{plan.analysis_ref.body_sha256.slice(0, 16)}…</span>
                  </div>
                  {plan.watch_scenarios?.length ? (
                    <div className="grid gap-2">
                      {plan.watch_scenarios.map((scenario) => (
                        <div key={scenario.scenario_id} className="rounded-md border bg-background p-3 text-xs">
                          <div className="font-medium">{scenario.label}</div>
                          <div className="mt-1 grid gap-1 text-muted-foreground sm:grid-cols-2">
                            <span>触发：{scenario.trigger.kind} {scenario.trigger.threshold ?? `${scenario.trigger.lower}-${scenario.trigger.upper}`}</span>
                            <span>确认：{scenario.trigger.confirmation_count} 根闭合 {scenario.trigger.interval}</span>
                            <span>临界区：{scenario.approach_policy.distance_bps} bps · 1 分钟检查</span>
                            <span>量价：{scenario.volume_confirmation.metric} {scenario.volume_confirmation.comparator} {scenario.volume_confirmation.threshold}</span>
                            <span>观察窗口：{scenario.resolution_policy.max_observation_bars} 根</span>
                            <span>量价仅分类，不屏蔽价格提醒</span>
                          </div>
                        </div>
                      ))}
                    </div>
                  ) : null}
                </div>
              ) : null}
              {profile.watch_episodes?.length ? (
                <div className="grid gap-2 rounded-md border p-4">
                  <div className="text-sm font-medium">关键点位 episode 时间线</div>
                  {profile.watch_episodes.slice(0, 12).map((episode) => (
                    <div key={episode.episode_id} className="grid gap-1 rounded-md bg-muted/40 px-3 py-2 text-xs sm:grid-cols-[auto_minmax(0,1fr)_auto] sm:items-center">
                      <span className="font-mono text-muted-foreground">{episode.session_date}</span>
                      <span>{episode.client_rule_id} · {episode.phase}{episode.outcome ? ` · ${episode.outcome}` : ""}</span>
                      <span className="text-muted-foreground">{episode.volume_verdict || "量价证据不足"}</span>
                    </div>
                  ))}
                </div>
              ) : null}
              <div className="rounded-md border p-4">
                <div className="flex items-center gap-2 text-sm font-medium"><BrainCircuit className="h-4 w-4" />规则策略说明</div>
                <p className="mt-2 text-sm text-muted-foreground">{plan.summary}</p>
                <div className="mt-3 grid gap-2 text-xs text-muted-foreground sm:grid-cols-2">
                  <span>常态频次：{plan.quote_tier === "normal" ? "5 分钟" : plan.quote_tier === "active" ? "1 分钟" : "15 分钟"}</span>
                  <span>接近阈值：{plan.near_trigger_distance_bps} bps</span>
                  <span>计划协议：schema v{plan.schema_version}</span>
                  <span>数据截至：{formatDate(version?.data_as_of)}</span>
                  <span>计划到期：{formatDate(plan.hard_valid_until)}</span>
                </div>
              </div>
              <div className="grid gap-3 rounded-md border p-4 sm:grid-cols-[minmax(0,1fr)_minmax(0,1.4fr)] sm:items-start">
                <div className="grid gap-3">
                  <label className="grid gap-1.5 text-xs text-muted-foreground">
                    服务器检查频次
                    <select
                      aria-label="服务器检查频次"
                      value={plan.quote_tier}
                      disabled={!editable}
                      onChange={(event) => onChange({
                        ...plan,
                        quote_tier: event.target.value as MonitorPlan["quote_tier"],
                      })}
                      className="rounded-md border bg-background px-3 py-2 text-sm font-medium text-foreground disabled:opacity-60"
                    >
                      <option value="active">每 1 分钟（灵敏）</option>
                      <option value="normal" disabled={requiresOneMinutePolling}>每 5 分钟（标准）</option>
                      {plan.quote_tier === "low" ? <option value="low" disabled>每 15 分钟（当前规则不支持）</option> : null}
                    </select>
                  </label>
                  <label className="grid gap-1.5 text-xs text-muted-foreground">
                    接近目标距离（bps）
                    <input
                      aria-label="接近目标距离（bps）"
                      type="number"
                      min={10}
                      max={500}
                      step={10}
                      value={numericDraft?.nearTriggerDistanceBps ?? ""}
                      disabled={!editable}
                      onChange={(event) => numericDraft && onNumericChange({
                        ...numericDraft,
                        nearTriggerDistanceBps: event.target.value,
                      })}
                      className="rounded-md border bg-background px-3 py-2 font-mono text-sm text-foreground disabled:opacity-60"
                    />
                  </label>
                </div>
                <div className="text-xs leading-5 text-muted-foreground">
                  <p>检查频次控制服务器多久取数一次；K 线粒度控制规则使用 1 分钟还是 5 分钟闭合数据。</p>
                  <p className="mt-1">检查频次不能慢于已启用规则的 K 线粒度，且实际执行不低于服务器安全下限。</p>
                  <p className="mt-1">现价距任一目标不超过接近距离时，切换为接近目标频次；价格区间内的距离按 0 计算。</p>
                  {!editable ? <p className="mt-1 text-amber-700 dark:text-amber-300">已生效版本不可直接改写；请在上方选择“AI 重新分析”生成待审核新版本。</p> : null}
                </div>
              </div>
              {plan.price_volume_policy ? (
                <MonitorPriceVolumePolicyEditor
                  policy={plan.price_volume_policy}
                  contractionRatioValue={numericDraft?.priceVolumeContractionRatio ?? ""}
                  expansionRatioValue={numericDraft?.priceVolumeExpansionRatio ?? ""}
                  disabled={!editable}
                  onChange={(priceVolumePolicy) => onChange({
                    ...plan,
                    price_volume_policy: priceVolumePolicy,
                  })}
                  onContractionRatioChange={(value) => numericDraft && onNumericChange({
                    ...numericDraft,
                    priceVolumeContractionRatio: value,
                  })}
                  onExpansionRatioChange={(value) => numericDraft && onNumericChange({
                    ...numericDraft,
                    priceVolumeExpansionRatio: value,
                  })}
                />
              ) : (
                <div className="rounded-md border border-dashed p-4 text-xs leading-5 text-muted-foreground">
                  当前为兼容的旧版价格计划，尚未配置量价策略。选择上方“AI 重新分析”生成支持量价的新版（schema v2+）后即可启用和编辑量价分析。
                </div>
              )}
              <div className="grid gap-2 rounded-md border border-fuchsia-500/25 bg-fuchsia-500/5 p-4">
                <div className="flex items-center gap-2 text-sm font-medium">
                  <Volume2 className="h-4 w-4 text-fuchsia-600 dark:text-fuchsia-400" aria-hidden="true" />
                  YMCA 突破联动
                </div>
                <div className="grid gap-3 sm:grid-cols-2">
                  <label className="grid gap-1.5 text-xs text-muted-foreground">
                    上涨 YMCA 规则
                    <select
                      aria-label="上涨 YMCA 规则"
                      value={upYmcaRuleId}
                      disabled={!editable || !ymcaSchemaSupported}
                      onChange={(event) => onChange({
                        ...plan,
                        market_rules: plan.market_rules.map((rule) => rule.kind === "price_cross_above"
                          ? {
                            ...rule,
                            alert_cue: event.target.value === rule.client_rule_id ? "ymca_v1" : "none",
                          }
                          : rule),
                      })}
                      className="rounded-md border bg-background px-3 py-2 text-sm text-foreground disabled:opacity-60"
                    >
                      <option value="">不上涨联动</option>
                      {plan.market_rules
                        .filter((rule) => rule.enabled && rule.kind === "price_cross_above")
                        .map((rule) => (
                          <option key={rule.client_rule_id} value={rule.client_rule_id}>
                            {rule.parameters.threshold == null
                              ? rule.client_rule_id
                              : `向上突破 ${rule.parameters.threshold}（${rule.client_rule_id}）`}
                          </option>
                        ))}
                    </select>
                  </label>
                  <label className="grid gap-1.5 text-xs text-muted-foreground">
                    下跌 YMCA 规则
                    <select
                      aria-label="下跌 YMCA 规则"
                      value={downYmcaRuleId}
                      disabled={!editable || !ymcaSchemaSupported}
                      onChange={(event) => onChange({
                        ...plan,
                        market_rules: plan.market_rules.map((rule) => rule.kind === "price_cross_below"
                          ? {
                            ...rule,
                            alert_cue: event.target.value === rule.client_rule_id ? "ymca_v1" : "none",
                          }
                          : rule),
                      })}
                      className="rounded-md border bg-background px-3 py-2 text-sm text-foreground disabled:opacity-60"
                    >
                      <option value="">不下跌联动</option>
                      {plan.market_rules
                        .filter((rule) => rule.enabled && rule.kind === "price_cross_below")
                        .map((rule) => (
                          <option key={rule.client_rule_id} value={rule.client_rule_id}>
                            {rule.parameters.threshold == null
                              ? rule.client_rule_id
                              : `向下跌破 ${rule.parameters.threshold}（${rule.client_rule_id}）`}
                          </option>
                        ))}
                    </select>
                  </label>
                </div>
                <p className="text-[11px] leading-5 text-muted-foreground">
                  {ymcaSchemaSupported
                    ? "上涨和下跌方向可各选择一条已启用的突破规则，两种方向播放同一段 YMCA 音频；切换规则只会清除同方向的旧选择。"
                    : "当前计划协议早于 schema v3，不能保存 YMCA 设置；请在上方选择“AI 重新分析”生成 v3 草案。"}
                </p>
                {selectedYmcaAssetsUnavailable ? (
                  <p role="alert" className="text-xs text-amber-700 dark:text-amber-300">
                    {audioUnavailable
                      ? "服务器尚未准备授权音频；可保存草案，但素材就绪前不能启用该计划。"
                      : `${[
                        upAssetsUnavailable ? "上涨" : "",
                        downAssetsUnavailable ? "下跌" : "",
                      ].filter(Boolean).join("、")}飞书表情包尚未就绪；可保存草案，但相应素材就绪前不能启用该计划。`}
                  </p>
                ) : null}
              </div>
              <div className="grid gap-3">
                <div className="flex items-center justify-between">
                  <h4 className="text-sm font-semibold">行情规则</h4>
                  <span className="text-xs text-muted-foreground">仅使用白名单规则</span>
                </div>
                {plan.market_rules.map((rule, index) => (
                  <MonitorRuleEditor
                    key={rule.client_rule_id}
                    rule={rule}
                    disabled={!editable}
                    primaryValue={numericDraft?.rulePrimaryValues[rule.client_rule_id] ?? ""}
                    confirmationCountValue={numericDraft?.ruleConfirmationCounts[rule.client_rule_id] ?? ""}
                    cooldownMinutesValue={numericDraft?.ruleCooldownMinutes[rule.client_rule_id] ?? ""}
                    onPrimaryValueChange={(value) => numericDraft && onNumericChange({
                      ...numericDraft,
                      rulePrimaryValues: { ...numericDraft.rulePrimaryValues, [rule.client_rule_id]: value },
                    })}
                    onConfirmationCountChange={(value) => numericDraft && onNumericChange({
                      ...numericDraft,
                      ruleConfirmationCounts: { ...numericDraft.ruleConfirmationCounts, [rule.client_rule_id]: value },
                    })}
                    onCooldownMinutesChange={(value) => numericDraft && onNumericChange({
                      ...numericDraft,
                      ruleCooldownMinutes: { ...numericDraft.ruleCooldownMinutes, [rule.client_rule_id]: value },
                    })}
                    onChange={(nextRule) => {
                      const normalizedRule = nextRule.enabled
                        && (nextRule.kind === "price_cross_above" || nextRule.kind === "price_cross_below")
                        ? nextRule
                        : { ...nextRule, alert_cue: "none" as const };
                      onChange({
                        ...plan,
                        quote_tier: normalizedRule.enabled && normalizedRule.parameters.interval === "1m"
                          ? "active"
                          : plan.quote_tier,
                        market_rules: plan.market_rules.map((item, itemIndex) => itemIndex === index ? normalizedRule : item),
                      });
                    }}
                  />
                ))}
              </div>
              <div className="rounded-md border p-4 text-xs text-muted-foreground">
                <div className="flex items-center gap-2 font-medium text-foreground"><ShieldCheck className="h-4 w-4" />能力边界</div>
                <p className="mt-2">价格规则可用。新闻语义与基本面评分在数据契约和校准完成前保持关闭，不会伪造结果。系统只提醒，不执行交易。</p>
              </div>
              {formErrors.length ? (
                <div role="alert" className="rounded-md border border-red-500/40 bg-red-500/5 p-3 text-xs text-red-700 dark:text-red-300">
                  <div className="font-semibold">请先修正以下计划设置</div>
                  <ul className="mt-1.5 list-disc space-y-1 pl-5">
                    {formErrors.map((error) => <li key={error}>{error}</li>)}
                  </ul>
                </div>
              ) : null}
              {profile.status === "closed" ? (
                <div className="grid gap-3 border-t pt-4 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center">
                  <p className="text-xs leading-5 text-muted-foreground">
                    该监控已关闭。系统会先自动刷新并校验数据源；数据可用后会在当前抽屉生成新草案，再由你确认启用。
                  </p>
                  <button type="button" disabled={busy} onClick={onRecheck} className="inline-flex items-center justify-center gap-2 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground disabled:opacity-50">
                    {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
                    重新检测数据源
                  </button>
                </div>
              ) : version?.status === "pending_review" ? (
                <div className="flex flex-wrap justify-end gap-2 border-t pt-4">
                  <button type="button" disabled={busy || !isDirty} onClick={onSave} className="rounded-md border px-4 py-2 text-sm font-medium hover:bg-muted disabled:opacity-50">
                    {isDirty ? "保存修改" : "已保存"}
                  </button>
                  <button
                    type="button"
                    disabled={busy || selectedYmcaAssetsUnavailable}
                    onClick={onSaveAndActivate}
                    title={selectedYmcaAssetsUnavailable ? "YMCA 音频和所选方向的飞书表情素材就绪后才能启用" : undefined}
                    className="inline-flex items-center gap-2 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground disabled:opacity-50"
                  >
                    {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />}
                    保存并启用
                  </button>
                </div>
              ) : null}
            </>
          ) : (
            <div className="grid justify-items-center gap-3 rounded-md border border-dashed p-6 text-center text-sm text-muted-foreground">
              <p>当前尚无可查看的监控计划。</p>
              <button
                type="button"
                disabled={busy}
                onClick={profile.status === "closed" ? onRecheck : onReanalyze}
                className="inline-flex items-center gap-2 rounded-md border border-cyan-500/40 bg-background px-3 py-2 text-xs font-medium text-cyan-700 hover:bg-cyan-500/10 disabled:opacity-50 dark:text-cyan-300"
              >
                {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <BrainCircuit className="h-3.5 w-3.5" />}
                {busy ? "正在处理…" : profile.status === "closed" ? "重新检测并打开" : "AI 重新分析并生成草案"}
              </button>
            </div>
          )}
        </div>
      </aside>
    </div>
  ), document.body);
}

function MonitorHeartbeat({
  profile,
  status,
  nowMs,
  marketScheduleSupported,
  connected,
}: {
  profile: MonitorProfile;
  status: PortfolioMonitoringStatus | null;
  nowMs: number;
  marketScheduleSupported: boolean;
  connected: boolean;
}) {
  const lastCheckMs = profile.last_quote_check_at ? new Date(profile.last_quote_check_at).getTime() : Number.NaN;
  const checkAgeMs = nowMs - lastCheckMs;
  const runtimeActive = Boolean(
    connected
    && status?.enabled_by_config
    && status.runtime.running
    && status.runtime.leader
    && status.effective_mode !== "off"
    && marketScheduleSupported
  );
  const pulsing = profile.status === "active"
    && runtimeActive
    && Number.isFinite(checkAgeMs)
    && checkAgeMs >= 0
    && checkAgeMs < 8_000;
  const blocked = profile.blocked_reasons.length > 0;
  const tradingPreopen = status?.runtime.calendar?.is_trading_day === true
    && status.runtime.calendar.session === "preopen";
  let headline = "等待启用";
  if (profile.status === "paused") headline = "监控已暂停";
  else if (profile.status === "closed") headline = "监控已关闭";
  else if (profile.status === "drafting") headline = "等待可用数据";
  else if (profile.status === "pending_review") headline = "等待审核计划";
  else if (profile.status === "expired") headline = "计划已到期";
  else if (!connected) headline = "页面与监控服务断联";
  else if (!status?.enabled_by_config || status.effective_mode === "off") headline = "当前未实际监控 · 服务未启动";
  else if (!status.runtime.running) headline = "当前未实际监控 · 服务未运行";
  else if (!marketScheduleSupported) headline = "当前未实际监控 · 该市场调度待接入";
  else if (!status.runtime.leader) headline = "备用实例 · 等待接管";
  else if (tradingPreopen) headline = "盘前准备 · 9:35 首轮检查";
  else if (status.runtime.calendar?.open === false) headline = "休市等待 · 下个交易时段继续";
  else if (pulsing) headline = `刚刚完成检查 · ${blocked ? "数据受阻" : "数据正常"}`;
  else if (profile.last_quote_check_at) headline = `持续监控中 · 上次 ${formatRelativeTime(profile.last_quote_check_at, nowMs)}`;
  else headline = "持续监控中 · 等待首次检查";

  let scheduleText = "启用后开始检查";
  if (profile.status === "active") {
    if (!connected) scheduleText = "下次检查：等待页面重新连接";
    else if (!status?.enabled_by_config || status.effective_mode === "off") scheduleText = "下次检查：等待监控服务启动";
    else if (!status.runtime.running) scheduleText = "下次检查：等待监控服务运行";
    else if (!marketScheduleSupported) scheduleText = "下次检查：等待该市场交易日历接入";
    else if (!status.runtime.leader) scheduleText = "下次检查：等待当前实例接管";
    else if (tradingPreopen) scheduleText = "下次检查：09:35（09:00 发送盘前提示）";
    else if (status.runtime.calendar?.open === false) scheduleText = "下次检查：下个交易时段重新排程";
    else {
      const schedule = formatScheduleTime(profile.next_quote_run_at, nowMs);
      scheduleText = schedule.startsWith("已延迟") ? `检查排队中：${schedule}` : `下次检查：${schedule}`;
    }
  } else if (profile.status === "paused") scheduleText = "恢复后重新排程";
  else if (profile.status === "closed") scheduleText = "已停止后续检查";
  else if (profile.status === "expired") scheduleText = "重新分析后才能继续";

  const tone = profile.status === "active" && runtimeActive
    ? blocked ? "text-amber-700 dark:text-amber-300" : "text-emerald-700 dark:text-emerald-300"
    : "text-muted-foreground";
  return (
    <div
      aria-label={`${profile.symbol} 监控心跳`}
      aria-live="polite"
      data-check-pulse={pulsing ? "true" : "false"}
      className={cn("flex items-center gap-2 rounded-md border bg-muted/20 px-2.5 py-2 text-xs", tone)}
    >
      <span className="relative flex h-4 w-4 shrink-0 items-center justify-center" aria-hidden="true">
        {pulsing ? <span className={cn("absolute inset-0 rounded-full opacity-60 motion-reduce:animate-none", blocked ? "animate-ping bg-amber-400" : "animate-ping bg-emerald-400")} /> : null}
        <Activity className={cn("relative h-3.5 w-3.5", pulsing && "animate-bounce motion-reduce:animate-none")} />
      </span>
      <span className="min-w-0">
        <span className="block font-medium">{headline}</span>
        <span className="mt-0.5 block text-[11px] text-muted-foreground">
          最近检查：{formatRelativeTime(profile.last_quote_check_at, nowMs)} · {scheduleText}
        </span>
      </span>
    </div>
  );
}

function MonitorStatusLamp({ state, pulse = false }: { state: MonitorLampState; pulse?: boolean }) {
  const colorClass = state.tone === "healthy"
    ? "bg-emerald-400 shadow-[0_0_10px_rgba(52,211,153,0.75)]"
    : state.tone === "error"
      ? "bg-red-500 shadow-[0_0_10px_rgba(239,68,68,0.65)]"
      : "bg-slate-500";
  return (
    <span
      role="img"
      aria-label={state.label}
      title={state.label}
      data-monitor-lamp={state.tone}
      className="relative inline-flex h-4 w-4 shrink-0 items-center justify-center"
    >
      {pulse && state.tone === "healthy" ? (
        <span className="absolute h-3 w-3 animate-ping rounded-full bg-emerald-400/50 motion-reduce:animate-none" aria-hidden="true" />
      ) : null}
      <span className={cn("relative h-2.5 w-2.5 rounded-full ring-2 ring-background", colorClass)} aria-hidden="true" />
    </span>
  );
}

function MonitorTargetGlyph({
  target,
  currentPrice,
}: {
  target: PriceTarget | null;
  currentPrice: number | null;
}) {
  if (!target) return <span className="text-xs text-muted-foreground">--</span>;
  const tone = TARGET_INTENT_TONE[target.intent];
  const distance = targetDistancePercentage(target, currentPrice);
  const targetIsHigher = distance == null ? target.direction === "up" : distance >= 0;
  return (
    <span
      className="grid min-w-0 gap-1"
      data-target-side={targetIsHigher ? "right" : "left"}
      aria-label={`${compactTargetLabel(target)}，目标价 ${formatMonitorPrice(target.value)}${distance == null ? "" : `，距离 ${Math.abs(distance).toFixed(2)}%`}`}
    >
      <span className={cn(
        "flex min-w-0 items-center gap-1.5",
        targetIsHigher ? "justify-start" : "flex-row-reverse justify-end",
      )}>
        <span className="relative h-2.5 w-8 shrink-0" aria-hidden="true">
          <span className="absolute inset-x-0 top-1 h-px bg-muted-foreground/30" />
          <span className={cn(
            "absolute top-1 h-px w-1/2",
            targetIsHigher ? "left-1/2" : "left-0",
            tone.line,
          )} />
          <span className="absolute left-1/2 top-0 h-2 w-px bg-foreground/70" />
          <span className={cn(
            "absolute top-0.5 h-1.5 w-1.5 rounded-full ring-1 ring-background",
            targetIsHigher ? "right-0" : "left-0",
            tone.dot,
          )} />
        </span>
        <span className={cn("truncate rounded border px-1 py-0.5 text-[10px] font-semibold", tone.badge)}>
          {compactTargetLabel(target)}
        </span>
      </span>
      <span className="flex items-baseline gap-1 font-mono text-[11px]">
        <strong>{formatMonitorPrice(target.value)}</strong>
        {distance != null ? (
          <span className="text-[9px] text-muted-foreground">{distance > 0 ? "↑" : distance < 0 ? "↓" : "="}{Math.abs(distance).toFixed(2)}%</span>
        ) : null}
      </span>
    </span>
  );
}

function MonitorCountdownRing({
  profile,
  version,
  nowMs,
  active,
  compact = false,
  showCaption = true,
  statusState,
}: {
  profile: MonitorProfile;
  version?: MonitorPlanVersion | null;
  nowMs: number;
  active: boolean;
  compact?: boolean;
  showCaption?: boolean;
  statusState?: MonitorLampState;
}) {
  const nextRunMs = profile.next_quote_run_at
    ? new Date(profile.next_quote_run_at).getTime()
    : Number.NaN;
  const totalSeconds = version?.plan ? quoteTierSeconds(version.plan.quote_tier) : null;
  const available = active && totalSeconds != null && Number.isFinite(nextRunMs);
  const remainingSeconds = available
    ? Math.max(0, Math.ceil((nextRunMs - nowMs) / 1000))
    : null;
  const remainingRatio = remainingSeconds == null || totalSeconds == null
    ? 0
    : Math.min(1, remainingSeconds / totalSeconds);
  const radius = 20;
  const circumference = 2 * Math.PI * radius;
  const countdownLabel = remainingSeconds == null
    ? "--"
    : remainingSeconds <= 0
      ? "0s"
      : remainingSeconds < 60
        ? `${remainingSeconds}s`
        : `${Math.floor(remainingSeconds / 60)}:${String(remainingSeconds % 60).padStart(2, "0")}`;
  if (statusState?.tone === "error") {
    return (
      <span
        role="img"
        aria-label={`${profile.symbol} ${statusState.label}`}
        title={statusState.label}
        data-monitor-lamp="error"
        className={cn(
          "grid shrink-0 place-items-center rounded-full border border-red-500/35 bg-red-500/[0.08]",
          compact ? "h-11 w-11" : "h-14 w-14",
        )}
      >
        <span
          className="h-3 w-3 rounded-full bg-red-500 shadow-[0_0_12px_rgba(239,68,68,0.8)]"
          aria-hidden="true"
        />
      </span>
    );
  }
  const countdownAriaLabel = statusState
    ? `${profile.symbol} ${statusState.label}，下次检查倒计时 ${countdownLabel}`
    : `${profile.symbol} 下次检查倒计时`;
  return (
    <div
      role={statusState ? "img" : undefined}
      aria-label={countdownAriaLabel}
      title={remainingSeconds == null ? "暂未排定检查" : `下次检查 ${countdownLabel}`}
      data-monitor-lamp={statusState?.tone}
      className={cn("grid shrink-0 justify-items-center", showCaption && "gap-1")}
    >
      <div className={cn("relative grid place-items-center", compact ? "h-11 w-11" : "h-14 w-14")}>
        <svg className={cn("-rotate-90", compact ? "h-11 w-11" : "h-14 w-14")} viewBox="0 0 48 48" aria-hidden="true">
          <circle
            cx="24"
            cy="24"
            r={radius}
            fill="none"
            className={cn(
              statusState?.tone === "healthy"
                ? "stroke-emerald-500/55"
                : statusState?.tone === "waiting" ? "stroke-slate-500/45" : "stroke-muted",
            )}
            strokeWidth="4"
          />
          <circle
            cx="24"
            cy="24"
            r={radius}
            fill="none"
            className={cn(
              "transition-[stroke-dashoffset] duration-500 motion-reduce:transition-none",
              statusState?.tone === "healthy"
                ? "stroke-emerald-400"
                : statusState?.tone === "waiting"
                  ? "stroke-slate-500"
                  : available ? "stroke-cyan-500" : "stroke-muted-foreground/35",
            )}
            strokeLinecap="round"
            strokeWidth="4"
            strokeDasharray={circumference}
            strokeDashoffset={circumference * (1 - remainingRatio)}
          />
        </svg>
        <span className={cn("absolute font-semibold tabular-nums", compact ? "text-[10px]" : "text-[11px]")}>{countdownLabel}</span>
      </div>
      {showCaption ? <span className="text-[10px] text-muted-foreground">检查倒计时</span> : null}
    </div>
  );
}

function MonitorCarouselRow({
  profile,
  displayName,
  code,
  marketLabel,
  selected,
  lampState,
  nowMs,
  countdownActive,
  onSelect,
}: {
  profile: MonitorProfile;
  displayName: string;
  code: string;
  marketLabel: string;
  selected: boolean;
  lampState: MonitorLampState;
  nowMs: number;
  countdownActive: boolean;
  onSelect: () => void;
}) {
  const currentPrice = finitePriceTarget(profile.last_quote?.price);
  const sessionOpen = finitePriceTarget(profile.last_quote?.session_open);
  const todayChange = percentageFromOpen(currentPrice, sessionOpen);
  const nearestTarget = nearestPriceTarget(profile.display_plan?.plan, currentPrice);
  const TodayIcon = todayChange == null || Math.abs(todayChange) < 0.005
    ? ArrowRight
    : todayChange > 0 ? ArrowUpRight : ArrowDownRight;
  const todayTone = openMoveClass(todayChange);
  return (
    <button
      type="button"
      onClick={onSelect}
      aria-current={selected ? "true" : undefined}
      aria-label={`${displayName} ${code}，现价 ${formatMonitorPrice(currentPrice)}，较开盘 ${todayChange == null ? "--" : `${todayChange > 0 ? "+" : ""}${todayChange.toFixed(2)}%`}，${lampState.label}`}
      className={cn(
        "grid min-w-[17rem] grid-cols-[minmax(0,1fr)_auto_auto] grid-rows-2 items-center gap-x-3 gap-y-1 rounded-md border px-3 py-2 text-left transition-colors",
        "lg:min-w-0 lg:grid-cols-[minmax(5.25rem,1fr)_minmax(3.7rem,.7fr)_minmax(4.2rem,.75fr)_minmax(6.5rem,1.1fr)_2.75rem] lg:grid-rows-1 lg:gap-x-2",
        selected
          ? "border-cyan-500/70 bg-cyan-500/[0.07] shadow-[inset_3px_0_0_rgba(6,182,212,0.85)]"
          : "border-border/80 bg-background/55 hover:border-cyan-500/35 hover:bg-muted/35",
      )}
    >
      <span className="min-w-0 self-start lg:self-center">
        <span className="block truncate text-xs font-semibold">{displayName}</span>
        <span className="mt-0.5 flex items-center gap-1 font-mono text-[10px] text-muted-foreground">
          <span>{code}</span>
          {marketLabel ? <span>{marketLabel}</span> : null}
        </span>
      </span>
      <span className="col-start-2 row-start-1 justify-self-end font-mono text-xs font-semibold lg:col-start-2 lg:row-start-1 lg:justify-self-start">
        {formatMonitorPrice(currentPrice)}
      </span>
      <span className={cn(
        "col-start-2 row-start-2 inline-flex items-center justify-self-end gap-0.5 text-[10px] font-semibold lg:col-start-3 lg:row-start-1 lg:justify-self-start",
        todayTone,
      )}>
        <TodayIcon className="h-3 w-3" aria-hidden="true" />
        {todayChange == null ? "--" : `${todayChange > 0 ? "+" : ""}${todayChange.toFixed(2)}%`}
      </span>
      <span className="col-start-1 row-start-2 min-w-0 lg:col-start-4 lg:row-start-1">
        <MonitorTargetGlyph target={nearestTarget} currentPrice={currentPrice} />
      </span>
      <span className="col-start-3 row-span-2 row-start-1 justify-self-center lg:col-start-5 lg:row-span-1 lg:row-start-1">
        <MonitorCountdownRing
          profile={profile}
          version={profile.display_plan}
          nowMs={nowMs}
          active={countdownActive}
          compact
          showCaption={false}
          statusState={lampState}
        />
      </span>
    </button>
  );
}

function pendingAutopilotStatus(
  run: MonitorAutopilotRun | null,
  card: MonitorTargetMonitoringCard | null = null,
): {
  label: string;
  detail: string;
  tone: string;
  working: boolean;
} {
  if (card?.profile_status === "blocked") {
    return {
      label: "建档受阻",
      detail: card.blockers.map((blocker) => blockedReasonLabel(blocker.code)).join("；") || "数据门禁未通过，价格观察档案尚未激活。",
      tone: "border-amber-500/40 bg-amber-500/10 text-amber-800 dark:text-amber-200",
      working: false,
    };
  }
  if (!run) {
    return {
      label: "等待建档",
      detail: "已进入 AI 自主监控范围，等待系统创建首个监控计划。",
      tone: "border-cyan-500/40 bg-cyan-500/10 text-cyan-700 dark:text-cyan-300",
      working: true,
    };
  }
  if (run.status === "queued" || run.status === "running") {
    return {
      label: run.status === "queued" ? "等待研究" : "正在建档",
      detail: "系统正在补齐研究证据、生成监控点位并执行启用门禁。",
      tone: "border-cyan-500/40 bg-cyan-500/10 text-cyan-700 dark:text-cyan-300",
      working: true,
    };
  }
  if (run.status === "completed") {
    return {
      label: "正在核对",
      detail: "任务记录已结束但监控档案尚未生成；系统会自动对账并恢复未完成步骤。",
      tone: "border-amber-500/40 bg-amber-500/10 text-amber-800 dark:text-amber-200",
      working: true,
    };
  }
  if (run.status === "blocked") {
    return {
      label: "建档受阻",
      detail: monitorAutopilotRunDetail(run) || "数据或语义门禁未通过，尚未启用监控。",
      tone: "border-amber-500/40 bg-amber-500/10 text-amber-800 dark:text-amber-200",
      working: false,
    };
  }
  if (run.status === "failed") {
    return {
      label: "建档失败",
      detail: monitorAutopilotRunDetail(run) || "自动建档失败，错误已保留供排查。",
      tone: "border-red-500/40 bg-red-500/10 text-red-700 dark:text-red-300",
      working: false,
    };
  }
  return {
    label: "等待重建",
    detail: "当前任务已取消；标的仍在自主范围内，后续有效触发会重新建档。",
    tone: "border-slate-500/40 bg-slate-500/10 text-slate-700 dark:text-slate-300",
    working: false,
  };
}

function MonitorPendingCarouselRow({
  symbol,
  displayName,
  selected,
  run,
  card,
  onSelect,
}: {
  symbol: string;
  displayName: string;
  selected: boolean;
  run: MonitorAutopilotRun | null;
  card: MonitorTargetMonitoringCard | null;
  onSelect: () => void;
}) {
  const [code, marketLabel = ""] = symbol.trim().toUpperCase().split(".");
  const pending = pendingAutopilotStatus(run, card);
  const stageLabel = card?.build_state?.stage_label || run?.build_state?.stage_label || pending.label;
  return (
    <button
      type="button"
      onClick={onSelect}
      aria-current={selected ? "true" : undefined}
      aria-label={`${displayName} ${code}，${pending.label}`}
      className={cn(
        "grid min-w-[17rem] grid-cols-[minmax(0,1fr)_auto_auto] grid-rows-2 items-center gap-x-3 gap-y-1 rounded-md border px-3 py-2 text-left transition-colors",
        "lg:min-w-0 lg:grid-cols-[minmax(5.25rem,1fr)_minmax(3.7rem,.7fr)_minmax(4.2rem,.75fr)_minmax(6.5rem,1.1fr)_2.75rem] lg:grid-rows-1 lg:gap-x-2",
        selected
          ? "border-cyan-500/70 bg-cyan-500/[0.07] shadow-[inset_3px_0_0_rgba(6,182,212,0.85)]"
          : "border-border/80 bg-background/55 hover:border-cyan-500/35 hover:bg-muted/35",
      )}
    >
      <span className="min-w-0 self-start lg:self-center">
        <span className="block truncate text-xs font-semibold">{displayName}</span>
        <span className="mt-0.5 flex items-center gap-1 font-mono text-[10px] text-muted-foreground">
          <span>{code}</span>
          {marketLabel ? <span>{marketLabel}</span> : null}
        </span>
      </span>
      <span className="col-start-2 row-start-1 justify-self-end font-mono text-xs text-muted-foreground lg:col-start-2 lg:justify-self-start">--</span>
      <span className="col-start-2 row-start-2 justify-self-end text-[10px] text-muted-foreground lg:col-start-3 lg:row-start-1 lg:justify-self-start">--</span>
      <span className="col-start-1 row-start-2 truncate text-[10px] font-medium text-muted-foreground lg:col-start-4 lg:row-start-1">
        {stageLabel}
      </span>
      <span className="col-start-3 row-span-2 row-start-1 grid h-9 w-9 place-items-center justify-self-center rounded-full border border-cyan-500/30 bg-cyan-500/5 lg:col-start-5 lg:row-span-1">
        {pending.working
          ? <Loader2 className="h-4 w-4 animate-spin text-cyan-600" aria-hidden="true" />
          : <AlertTriangle className="h-4 w-4 text-amber-600" aria-hidden="true" />}
      </span>
    </button>
  );
}

function MonitorPendingTargetCard({
  symbol,
  displayName,
  run,
  card,
  onShowUsage,
}: {
  symbol: string;
  displayName: string;
  run: MonitorAutopilotRun | null;
  card: MonitorTargetMonitoringCard | null;
  onShowUsage: (jobId: string) => void;
}) {
  const [code, marketLabel = ""] = symbol.trim().toUpperCase().split(".");
  const pending = pendingAutopilotStatus(run, card);
  const blockedReasons = card?.blockers.map((blocker) => blockedReasonLabel(blocker.code))
    || run?.blocked_reasons?.map(blockedReasonLabel)
    || [];
  const buildState = card?.build_state || run?.build_state;
  const progressPercent = Math.max(0, Math.min(100, buildState?.progress_percent ?? (
    run?.status === "running" ? 50 : run?.status === "queued" ? 10 : 100
  )));
  const buildSteps = [
    { threshold: 5, label: "排队" },
    { threshold: 15, label: "行情刷新" },
    { threshold: 30, label: "连续性" },
    { threshold: 50, label: "结构计算" },
    { threshold: 65, label: "AI情景" },
    { threshold: 75, label: "行动剧本" },
    { threshold: 85, label: "规则映射" },
    { threshold: 95, label: "门禁验证" },
    { threshold: 100, label: "完成" },
  ];
  const selfRepair = card?.self_repair || buildState?.self_repair;
  const continuityStatus = String(card?.continuity?.status || "未检查");
  const volumeStatus = String(card?.volume_gate?.status || "待取证");
  const levelEntries = card
    ? (Array.isArray(card.level_summary) ? card.level_summary : Object.values(card.level_summary || {}))
    : [];
  return (
    <article
      aria-label={`${displayName} ${code} 自动监控准备中`}
      className="grid min-w-0 content-start gap-4 overflow-hidden rounded-md border border-dashed border-cyan-500/40 bg-cyan-500/[0.025] p-4"
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="truncate text-sm font-semibold">{displayName}</div>
          <div className="mt-0.5 flex items-center gap-1.5 text-xs text-muted-foreground">
            <span className="font-mono font-medium text-foreground">{code}</span>
            {marketLabel ? <span>{marketLabel}</span> : null}
          </div>
        </div>
        <span className={cn("inline-flex shrink-0 items-center gap-1.5 rounded-full border px-2 py-1 text-[10px] font-medium", pending.tone)}>
          {pending.working ? <Loader2 className="h-3 w-3 animate-spin" aria-hidden="true" /> : <AlertTriangle className="h-3 w-3" aria-hidden="true" />}
          {pending.label}
        </span>
      </div>
      {card?.decision_brief ? (
        <div className="rounded-md border bg-background/80 p-3 text-xs">
          <div className="flex flex-wrap items-center gap-2">
            <strong>{card.decision_brief.headline}</strong>
            <span className="rounded-full border px-2 py-0.5 text-[10px]">风险 {card.decision_brief.risk_level}</span>
          </div>
          <p className="mt-1 leading-5 text-muted-foreground">{card.decision_brief.summary}</p>
          <p className="mt-1 leading-5"><strong>下一步：</strong>{card.decision_brief.next_confirmation}</p>
        </div>
      ) : null}
      <div className="rounded-md border bg-background/70 p-3 text-xs leading-5 text-muted-foreground">
        <p className="font-medium text-foreground">已纳入 AI 自主监控范围</p>
        <p className="mt-1">{pending.detail}</p>
        <p className="mt-2">监控计划通过证据与点位门禁后会自动替换此卡片；在此之前不会假装已经开始行情监控。</p>
      </div>
      <div className="grid gap-2" aria-label={`${displayName} 监控档案建立进度`}>
        <div className="flex items-center justify-between gap-3 text-xs">
          <span className="font-medium">{buildState?.stage_label || pending.label}</span>
          <span className="font-mono tabular-nums text-muted-foreground">{progressPercent}%</span>
        </div>
        <div
          role="progressbar"
          aria-label="监控档案建立进度"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={progressPercent}
          className="h-1.5 overflow-hidden rounded-full bg-muted"
        >
          <div
            className={cn(
              "h-full rounded-full transition-[width]",
              buildState?.status === "failed" ? "bg-red-500" : buildState?.status === "blocked" ? "bg-amber-500" : "bg-cyan-500",
            )}
            style={{ width: `${progressPercent}%` }}
          />
        </div>
        <div className="grid grid-cols-4 gap-1 text-center text-[10px] text-muted-foreground sm:grid-cols-9">
          {buildSteps.map((step) => (
            <span
              key={step.label}
              className={cn(
                "rounded border px-1 py-1",
                progressPercent >= step.threshold && "border-cyan-500/35 bg-cyan-500/[0.06] text-foreground",
              )}
            >
              {step.label}
            </span>
          ))}
        </div>
        {buildState?.attempt ? (
          <p className="text-[11px] text-muted-foreground">规划尝试 {buildState.attempt} 次 · 最近更新 {formatDate(buildState.updated_at)}</p>
        ) : null}
      </div>
      {blockedReasons.length ? (
        <p className="text-xs text-amber-800 dark:text-amber-200">受阻原因：{blockedReasons.join("；")}</p>
      ) : null}
      {card ? (
        <div className="grid gap-2 rounded-md border bg-background/70 p-3 text-xs sm:grid-cols-3">
          <span><strong>连续性：</strong>{continuityStatus}</span>
          <span><strong>量能：</strong>{volumeStatus}</span>
          <span><strong>候选：</strong>{levelEntries.length ? levelEntries.map((level) => String(level.score ?? "--")).join(" / ") : "尚无合格点位"}</span>
        </div>
      ) : null}
      {selfRepair ? (
        <div className={cn(
          "rounded-md border px-3 py-2 text-xs",
          selfRepair.circuit_open
            ? "border-amber-500/40 bg-amber-500/5 text-amber-800 dark:text-amber-200"
            : "border-slate-500/30 bg-slate-500/[0.04] text-muted-foreground",
        )}>
          {selfRepair.circuit_open ? (
            <><strong>已停止自动重试：</strong>相同输入多次受阻或一次修复额度已耗尽；后续轮询不会继续调用模型。新报告、持仓或证据变化后才会重新尝试。</>
          ) : (
            <><strong>自动修复：</strong>先刷新行情并校验连续性与折算因子，再由多周期量价结构引擎重算候选；同一输入最多两次模型校验，超限后熔断停止消耗 Token。</>
          )}
        </div>
      ) : null}
      <div className="flex flex-wrap items-center justify-between gap-2 text-[11px] text-muted-foreground">
        <span>{run ? `${AUTOPILOT_TRIGGER_LABELS[run.trigger_type] || run.trigger_type} · ${formatDate(run.created_at)}` : "等待首次任务"}</span>
        <span>只监控，不自动交易</span>
      </div>
      {run?.planner_job_id ? (
        <button
          type="button"
          onClick={() => onShowUsage(run.planner_job_id || "")}
          className="w-fit rounded-md border px-2.5 py-1.5 text-xs hover:bg-muted"
        >
          查看任务用量
        </button>
      ) : null}
    </article>
  );
}

function MonitorPriceVolumeSnapshotCard({
  symbol,
  liveSnapshot,
  historicalSnapshot,
  backfill,
  enabled,
  marketSupported,
}: {
  symbol: string;
  liveSnapshot?: MonitorPriceVolumeSnapshot | null;
  historicalSnapshot?: MonitorPriceVolumeSnapshot | null;
  backfill?: NonNullable<MonitorProfile["last_quote"]>["price_volume_backfill"];
  enabled: boolean;
  marketSupported: boolean;
}) {
  if (!liveSnapshot && !historicalSnapshot && !enabled) return null;
  if (!marketSupported && enabled) {
    return (
      <div
        aria-label={`${symbol} 量价分析`}
        data-price-volume-status="unsupported-market"
        className="rounded-md border border-dashed px-3 py-2 text-xs text-muted-foreground"
      >
        该市场量价监测暂未支持。
      </div>
    );
  }
  const liveReady = liveSnapshot?.status === "ready";
  const historicalReady = !liveReady && historicalSnapshot?.status === "ready";
  const snapshot = liveReady
    ? liveSnapshot
    : historicalReady
      ? historicalSnapshot
      : liveSnapshot;
  const backfillRunning = backfill?.status === "queued" || backfill?.status === "running";
  if (!snapshot && backfillRunning) {
    return (
      <div
        aria-label={`${symbol} 量价分析`}
        data-price-volume-status="backfilling"
        className="flex items-center gap-2 rounded-md border border-cyan-500/30 bg-cyan-500/5 px-3 py-2 text-xs text-cyan-700 dark:text-cyan-300"
      >
        <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
        正在补齐历史量价基线
      </div>
    );
  }
  if (!snapshot) {
    return (
      <div
        aria-label={`${symbol} 量价分析`}
        data-price-volume-status="waiting"
        className="rounded-md border border-dashed px-3 py-2 text-xs text-muted-foreground"
      >
        量价分析已启用，等待首个合格的闭合 5 分钟 K 线。
      </div>
    );
  }

  const ready = snapshot.status === "ready";
  const threeBarReturn = formatSignedBps(snapshot.three_bar_return_bps);
  const ratio = snapshot.volume_ratio == null || !Number.isFinite(snapshot.volume_ratio)
    ? "--"
    : `${snapshot.volume_ratio.toFixed(2)}×`;
  const closeLocation = snapshot.close_location == null || !Number.isFinite(snapshot.close_location)
    ? "--"
    : `${Math.round(Math.min(1, Math.max(0, snapshot.close_location)) * 100)}%`;
  const reasonText = priceVolumeReasonText(snapshot.reason_codes || []);
  const regimeLabel = priceVolumeRegimeLabel(snapshot);
  const regimeTone = snapshot.accelerated_decline
    ? "border-red-500/40 bg-red-500/10 text-red-700 dark:text-red-300"
    : snapshot.regime === "bearish_expansion"
      ? "border-amber-500/40 bg-amber-500/10 text-amber-800 dark:text-amber-200"
      : "border-cyan-500/30 bg-cyan-500/10 text-cyan-700 dark:text-cyan-300";

  return (
    <div
      aria-label={`${symbol} 量价分析`}
      data-price-volume-status={snapshot.status}
      data-analysis-scope={historicalReady ? "historical" : "live"}
      data-accelerated-decline={snapshot.accelerated_decline ? "true" : "false"}
      className="grid gap-2 rounded-md border bg-background/70 p-2.5"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-1.5 text-xs font-semibold">
          <Activity className="h-3.5 w-3.5 text-cyan-600 dark:text-cyan-400" aria-hidden="true" />
          {historicalReady ? "历史量价参考" : "当前实时量价"}
        </div>
        {snapshot.interpretation ? (
          <PriceVolumeMeaningTooltip label={regimeLabel} interpretation={snapshot.interpretation}>
            <span className={cn("rounded border px-1.5 py-0.5 text-[10px] font-medium", regimeTone)}>
              {regimeLabel}
            </span>
          </PriceVolumeMeaningTooltip>
        ) : (
          <span className={cn("rounded border px-1.5 py-0.5 text-[10px] font-medium", regimeTone)}>
            {regimeLabel}
          </span>
        )}
      </div>
      {ready ? (
        <>
          <div className="grid grid-cols-2 gap-x-3 gap-y-2 text-[11px] sm:grid-cols-4">
            <div>
              <div className="text-muted-foreground">量价态势</div>
              <div className="mt-0.5 font-medium">{regimeLabel}</div>
            </div>
            <div>
              <div className="text-muted-foreground">量比</div>
              <div className="mt-0.5 font-mono font-medium">
                {ratio}{snapshot.volume_state ? ` · ${VOLUME_STATE_LABELS[snapshot.volume_state] || snapshot.volume_state}` : ""}
              </div>
            </div>
            <div>
              <div className="text-muted-foreground">三根收益</div>
              <div className="mt-0.5 font-mono font-medium">{threeBarReturn} bps</div>
            </div>
            <div>
              <div className="text-muted-foreground">数据质量</div>
              <div className="mt-0.5 font-medium">可用 · {snapshot.baseline_samples} 个同期样本</div>
            </div>
          </div>
          <div className="text-[10px] text-muted-foreground">
            最新 5 分钟 {formatSignedBps(snapshot.latest_return_bps)} bps · 收盘位于本根振幅 {closeLocation}
          </div>
          {snapshot.volume_quality === "single_source" ? (
            <div className="rounded border border-amber-500/30 bg-amber-500/5 px-2 py-1 text-[10px] text-amber-700 dark:text-amber-300">
              单一来源量价参考 · 已按计划中的单源授权展示
            </div>
          ) : null}
        </>
      ) : backfillRunning ? (
        <div className="flex items-center gap-1.5 text-xs text-cyan-700 dark:text-cyan-300">
          <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
          正在补齐历史量价基线
        </div>
      ) : (
        <div className="text-xs leading-5 text-muted-foreground">
          <span className="font-medium text-foreground">
            {snapshot.status === "disabled" ? "量价分析未启用" : "数据质量：量价证据不足"}
          </span>
          {snapshot.baseline_samples > 0 ? ` · 当前 ${snapshot.baseline_samples} 个同期样本` : ""}
          {reasonText ? ` · ${reasonText}` : ""}
        </div>
      )}
      {historicalReady ? (
        <div role="status" className="rounded border border-amber-500/30 bg-amber-500/5 px-2 py-1.5 text-[11px] leading-5 text-amber-800 dark:text-amber-200">
          历史量价参考 · 截至 {formatDate(snapshot.data_as_of)}。实时量价暂不可用
          {liveSnapshot ? `：${priceVolumeReasonText(liveSnapshot.reason_codes || [])}` : ""}；该结论不参与实时触发。
        </div>
      ) : null}
      {snapshot.accelerated_decline ? (
        <div role="status" className="flex items-center gap-1.5 rounded border border-red-500/40 bg-red-500/10 px-2 py-1.5 text-xs font-semibold text-red-700 dark:text-red-300">
          <AlertTriangle className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
          放量加速下跌，不宜补仓
        </div>
      ) : null}
    </div>
  );
}

function MonitorPriceSnapshot({
  profile,
  version,
  nowMs,
  countdownActive,
  effectsActive,
}: {
  profile: MonitorProfile;
  version?: MonitorPlanVersion | null;
  nowMs: number;
  countdownActive: boolean;
  effectsActive: boolean;
}) {
  const rawPrice = profile.last_quote?.price;
  const currentPrice = typeof rawPrice === "number" && Number.isFinite(rawPrice) ? rawPrice : null;
  const rawPreviousPrice = profile.last_quote?.previous_price;
  const previousPrice = typeof rawPreviousPrice === "number" && Number.isFinite(rawPreviousPrice)
    ? rawPreviousPrice
    : null;
  const rawSessionOpen = profile.last_quote?.session_open;
  const sessionOpen = typeof rawSessionOpen === "number" && Number.isFinite(rawSessionOpen)
    ? rawSessionOpen
    : null;
  const currentVsOpen = percentageFromOpen(currentPrice, sessionOpen);
  const trend = currentVsOpen == null
    ? "unknown"
    : Math.abs(currentVsOpen) < 0.005 ? "flat" : currentVsOpen > 0 ? "up" : "down";
  const TrendIcon = trend === "up" ? ArrowUpRight : trend === "down" ? ArrowDownRight : ArrowRight;
  const trendLabel = trend === "up"
    ? `较开盘 +${Math.abs(currentVsOpen || 0).toFixed(2)}%`
    : trend === "down"
      ? `较开盘 -${Math.abs(currentVsOpen || 0).toFixed(2)}%`
      : trend === "flat" ? "较开盘持平" : "日内走势待形成";
  const trendAriaLabel = trend === "up"
    ? "趋势上涨"
    : trend === "down" ? "趋势下跌" : trend === "flat" ? "趋势持平" : "暂无趋势";
  const trendClass = trend === "up"
    ? "text-red-600 dark:text-red-400"
    : trend === "down"
      ? "text-emerald-600 dark:text-emerald-400"
      : "text-muted-foreground";
  const reportedSessionHigh = finitePriceTarget(profile.last_quote?.session_high);
  const reportedSessionLow = finitePriceTarget(profile.last_quote?.session_low);
  const sessionHigh = reportedSessionHigh == null
    ? null
    : Math.max(reportedSessionHigh, currentPrice ?? reportedSessionHigh, sessionOpen ?? reportedSessionHigh);
  const sessionLow = reportedSessionLow == null
    ? null
    : Math.min(reportedSessionLow, currentPrice ?? reportedSessionLow, sessionOpen ?? reportedSessionLow);
  const targetWindow = derivePriceTargetWindow(
    version?.plan,
    currentPrice,
    sessionOpen,
    previousPrice,
  );
  const displayMinimum = targetWindow
    ? Math.min(targetWindow.left.value, sessionLow ?? targetWindow.left.value)
    : 0;
  const displayMaximum = targetWindow
    ? Math.max(targetWindow.right.value, sessionHigh ?? targetWindow.right.value)
    : 0;
  const displaySpan = Math.max(0, displayMaximum - displayMinimum);
  const currentPosition = positionInPriceDomain(currentPrice, displayMinimum, displayMaximum) ?? 50;
  const openPosition = positionInPriceDomain(sessionOpen, displayMinimum, displayMaximum);
  const leftTargetPosition = positionInPriceDomain(
    targetWindow?.left.value ?? null,
    displayMinimum,
    displayMaximum,
  );
  const rightTargetPosition = positionInPriceDomain(
    targetWindow?.right.value ?? null,
    displayMinimum,
    displayMaximum,
  );
  const sessionLowPosition = positionInPriceDomain(sessionLow, displayMinimum, displayMaximum);
  const sessionHighPosition = positionInPriceDomain(sessionHigh, displayMinimum, displayMaximum);
  const leftVsOpen = percentageFromOpen(targetWindow?.left.value ?? null, sessionOpen);
  const rightVsCurrent = targetDistancePercentage(targetWindow?.right.target ?? null, currentPrice);
  const currentMarkerAlignment = currentPosition <= 12
    ? "translate-x-0"
    : currentPosition >= 88 ? "-translate-x-full" : "-translate-x-1/2";
  const boostDirection = effectsActive ? targetWindow?.boostDirection || null : null;
  const crossedTargets = targetWindow?.crossedTargets || [];
  const lastCrossedTarget = crossedTargets[crossedTargets.length - 1] || null;
  const focusTarget = lastCrossedTarget || targetWindow?.right.target || targetWindow?.left.target || null;
  const focusTargetDistance = targetDistancePercentage(focusTarget, currentPrice);
  const boostMessage = boostDirection && lastCrossedTarget
    ? `${boostDirection === "up" ? "今日上行，已突破" : "今日下行，已跌破"} L${lastCrossedTarget.level}`
    : null;
  return (
    <div
      aria-label={`${profile.symbol} 价格监控概览`}
      data-boost-direction={boostDirection || "none"}
      className={cn(
        "relative isolate overflow-hidden rounded-md border bg-gradient-to-br from-background to-muted/20",
        boostDirection === "up" && "border-red-500/50",
        boostDirection === "down" && "border-emerald-500/50",
      )}
    >
      {boostDirection ? (
        <span
          className={cn(
            "monitor-boost-ambient pointer-events-none absolute inset-0 opacity-60",
            boostDirection === "up" ? "bg-red-500/5" : "bg-emerald-500/5",
          )}
          aria-hidden="true"
        />
      ) : null}
      <div className="relative z-[1] grid gap-3 p-3">
        <div className="grid gap-3 sm:grid-cols-[minmax(0,1fr)_minmax(8.5rem,auto)_auto] sm:items-start">
          <div className="min-w-0">
            <div className="text-[11px] text-muted-foreground">最新监测价格</div>
            <div className="mt-0.5 font-mono text-xl font-semibold tracking-tight">
              {formatMonitorPrice(currentPrice)}
            </div>
            <div className={cn("mt-0.5 flex items-center gap-1 text-[11px] font-medium", trendClass)}>
              <TrendIcon className="h-3.5 w-3.5" aria-hidden="true" />
              <span>{trendLabel}</span>
            </div>
            <div className="mt-1 truncate text-[10px] text-muted-foreground">
              {profile.last_quote
                ? `${profile.last_quote.interval || "行情"} · ${formatDate(profile.last_quote.data_as_of || profile.last_quote.observed_at)}`
                : "尚未取得可用监测价格"}
            </div>
          </div>
          <div className="grid min-w-0 justify-items-start gap-1 sm:justify-items-end sm:text-right">
            {focusTarget ? (
              <>
                <PointBasisTooltip
                  label={targetLabel(focusTarget)}
                  basis={rulePointBasisText(focusTarget.rule)}
                >
                  <span className="text-[10px] text-muted-foreground">{targetLabel(focusTarget)}</span>
                </PointBasisTooltip>
                <strong className="font-mono text-base font-semibold text-foreground">
                  {formatMonitorPrice(focusTarget.value)}
                </strong>
                <span className={cn(
                  "text-[10px] font-medium",
                  focusTarget.direction === "down"
                    ? "text-emerald-600 dark:text-emerald-400"
                    : "text-red-600 dark:text-red-400",
                )}>
                  {formatTargetDistancePercentage(focusTargetDistance)}
                </span>
              </>
            ) : null}
            {boostMessage ? (
              <div
                role="status"
                className={cn(
                  "mt-1 inline-flex max-w-full items-center gap-1.5 whitespace-nowrap rounded-md border px-2 py-1 text-[10px] font-medium leading-4 shadow-sm",
                  boostDirection === "up"
                    ? "border-red-500/40 bg-red-500/10 text-red-700 dark:text-red-300"
                    : "border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300",
                )}
              >
                <Zap className="h-3 w-3 shrink-0" aria-hidden="true" />
                <span>{boostMessage}</span>
              </div>
            ) : null}
          </div>
          <div className="justify-self-end">
            <MonitorCountdownRing
              profile={profile}
              version={version}
              nowMs={nowMs}
              active={countdownActive}
              compact
              showCaption={false}
            />
          </div>
        </div>

        <MonitorPriceVolumeSnapshotCard
          symbol={profile.symbol}
          liveSnapshot={profile.last_quote?.price_volume}
          historicalSnapshot={profile.last_quote?.historical_price_volume}
          backfill={profile.last_quote?.price_volume_backfill}
          enabled={Boolean(version?.plan.price_volume_policy?.enabled)}
          marketSupported={["SH", "SZ", "BJ"].includes(profile.market.toUpperCase())}
        />

        {currentPrice != null && targetWindow ? (
          <div
            aria-label={`${profile.symbol} 价格目标区间`}
            data-current-range-state={targetWindow.rangeState}
            data-current-position={currentPosition.toFixed(2)}
            data-open-position={openPosition?.toFixed(2) || "unavailable"}
            data-left-target-position={leftTargetPosition?.toFixed(2) || "unavailable"}
            data-right-target-position={rightTargetPosition?.toFixed(2) || "unavailable"}
            data-session-low-position={sessionLowPosition?.toFixed(2) || "unavailable"}
            data-session-high-position={sessionHighPosition?.toFixed(2) || "unavailable"}
            data-left-boundary-source={targetWindow.left.source}
            data-right-boundary-source={targetWindow.right.source}
            data-crossed-target-count={targetWindow.crossedTargets.length}
            className="grid gap-2 border-t pt-3"
          >
            <div className="grid grid-cols-[1fr_auto_1fr] items-start gap-2 text-[10px]">
              <div className="grid justify-items-start gap-0.5">
                <span className="text-muted-foreground">左侧目标</span>
                <PointBasisTooltip
                  label={targetLabel(targetWindow.left.target)}
                  basis={targetWindow.left.target ? rulePointBasisText(targetWindow.left.target.rule) : null}
                >
                  <span className={cn(
                    "rounded border px-1.5 py-0.5 font-medium",
                    targetWindow.left.target?.direction === "up"
                      ? "border-red-500/30 bg-red-500/10 text-red-700 dark:text-red-300"
                      : "border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300",
                  )}>{targetLabel(targetWindow.left.target)}</span>
                </PointBasisTooltip>
                <strong className="font-mono text-xs text-foreground">{formatMonitorPrice(targetWindow.left.value)}</strong>
                <span className={cn("font-medium", openMoveClass(leftVsOpen))}>{formatOpenPercentage(leftVsOpen)}</span>
              </div>
              <div className="grid justify-items-center gap-0.5 text-center">
                <span className="text-muted-foreground">今日开盘基准</span>
                <strong className="font-mono text-xs text-foreground">{formatMonitorPrice(sessionOpen)}</strong>
                <span className="text-muted-foreground">{sessionOpen == null ? "待获取" : "0.00%"}</span>
              </div>
              <div className="grid justify-items-end gap-0.5 text-right">
                <span className="text-muted-foreground">右侧目标</span>
                <PointBasisTooltip
                  label={targetLabel(targetWindow.right.target)}
                  basis={targetWindow.right.target ? rulePointBasisText(targetWindow.right.target.rule) : null}
                >
                  <span className={cn(
                    "rounded border px-1.5 py-0.5 font-medium",
                    targetWindow.right.target?.direction === "down"
                      ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300"
                      : "border-red-500/30 bg-red-500/10 text-red-700 dark:text-red-300",
                  )}>{targetLabel(targetWindow.right.target)}</span>
                </PointBasisTooltip>
                <strong className="font-mono text-xs text-foreground">{formatMonitorPrice(targetWindow.right.value)}</strong>
                <span className={cn(
                  "font-medium",
                  targetWindow.right.target?.direction === "down"
                    ? "text-emerald-600 dark:text-emerald-400"
                    : "text-red-600 dark:text-red-400",
                )}>
                  {targetWindow.right.source === "current"
                    ? "现价"
                    : formatTargetDistancePercentage(rightVsCurrent)}
                </span>
              </div>
            </div>

            {displaySpan > 0 ? (
              <div className="relative h-28">
                <div className="absolute left-0 right-0 top-12 h-1.5 rounded-full bg-muted" />
                {openPosition != null ? (
                  <>
                    <div
                      className="absolute left-0 top-12 h-1.5 rounded-l-full bg-emerald-500/70"
                      style={{ width: `${openPosition}%` }}
                      aria-hidden="true"
                    />
                    <div
                      className="absolute right-0 top-12 h-1.5 rounded-r-full bg-red-500/70"
                      style={{ width: `${100 - openPosition}%` }}
                      aria-hidden="true"
                    />
                    <span
                      className="absolute top-10 h-5 w-0.5 -translate-x-1/2 bg-muted-foreground"
                      style={{ left: `${openPosition}%` }}
                      title={`今日开盘 ${formatMonitorPrice(sessionOpen)}`}
                      aria-hidden="true"
                    />
                  </>
                ) : null}
                {boostDirection ? (
                  <span
                    className={cn(
                      "monitor-target-boost-sweep pointer-events-none absolute top-[45px] z-[2] h-3 w-16 rounded-full blur-[1px]",
                      boostDirection === "up" ? "bg-red-300/80" : "bg-emerald-300/80",
                    )}
                    data-direction={boostDirection}
                    aria-hidden="true"
                  />
                ) : null}
                {leftTargetPosition != null ? (
                  <span
                    className={cn(
                      "absolute top-10 h-5 w-1 -translate-x-1/2 rounded-full",
                      targetWindow.left.target?.direction === "up" ? "bg-red-600" : "bg-emerald-600",
                    )}
                    style={{ left: `${leftTargetPosition}%` }}
                    aria-hidden="true"
                  />
                ) : null}
                {rightTargetPosition != null ? (
                  <span
                    className={cn(
                      "absolute top-10 h-5 w-1 -translate-x-1/2 rounded-full",
                      targetWindow.right.target?.direction === "down" ? "bg-emerald-600" : "bg-red-600",
                    )}
                    style={{ left: `${rightTargetPosition}%` }}
                    aria-hidden="true"
                  />
                ) : null}
                {sessionLowPosition != null ? (
                  <span
                    aria-label={`当日低点 ${formatMonitorPrice(sessionLow)}`}
                    className="absolute top-9 z-[5] h-7 w-px -translate-x-1/2 bg-zinc-950 shadow-[0_0_0_1px_rgba(255,255,255,0.6)] dark:bg-zinc-100 dark:shadow-[0_0_0_1px_rgba(0,0,0,0.6)]"
                    style={{ left: `${sessionLowPosition}%` }}
                  >
                    <span className={cn(
                      "absolute top-7 whitespace-nowrap rounded bg-background/95 px-1 py-0.5 text-[9px] font-medium text-foreground shadow-sm",
                      sessionLowPosition <= 12
                        ? "left-0"
                        : sessionLowPosition >= 88 ? "right-0" : "left-1/2 -translate-x-1/2",
                    )}>
                      当日低 {formatMonitorPrice(sessionLow)}
                    </span>
                  </span>
                ) : null}
                {sessionHighPosition != null ? (
                  <span
                    aria-label={`当日高点 ${formatMonitorPrice(sessionHigh)}`}
                    className="absolute top-9 z-[5] h-7 w-px -translate-x-1/2 bg-zinc-950 shadow-[0_0_0_1px_rgba(255,255,255,0.6)] dark:bg-zinc-100 dark:shadow-[0_0_0_1px_rgba(0,0,0,0.6)]"
                    style={{ left: `${sessionHighPosition}%` }}
                  >
                    <span className={cn(
                      "absolute top-12 whitespace-nowrap rounded bg-background/95 px-1 py-0.5 text-[9px] font-medium text-foreground shadow-sm",
                      sessionHighPosition <= 12
                        ? "left-0"
                        : sessionHighPosition >= 88 ? "right-0" : "left-1/2 -translate-x-1/2",
                    )}>
                      当日高 {formatMonitorPrice(sessionHigh)}
                    </span>
                  </span>
                ) : null}
                <span
                  aria-label={`最新监测价 ${formatMonitorPrice(currentPrice)}，${formatOpenPercentage(currentVsOpen)}，${trendAriaLabel}`}
                  className={cn("absolute top-0 z-10 inline-grid whitespace-nowrap rounded-md border bg-background px-2 py-1 font-mono text-[10px] font-semibold shadow-sm", currentMarkerAlignment)}
                  style={{ left: `${currentPosition}%` }}
                >
                  <span className="inline-flex items-center">
                    现价 {formatMonitorPrice(currentPrice)}
                    <TrendIcon className={cn("ml-0.5 h-3 w-3", trendClass)} aria-hidden="true" />
                  </span>
                  <span className={cn("text-[9px]", openMoveClass(currentVsOpen))}>{formatOpenPercentage(currentVsOpen)}</span>
                </span>
                <span
                  className="absolute top-9 h-6 w-0.5 -translate-x-1/2 bg-foreground"
                  style={{ left: `${currentPosition}%` }}
                  aria-hidden="true"
                />
              </div>
            ) : (
              <div className="rounded-md border border-dashed px-3 py-2 text-[10px] text-muted-foreground">
                现价与唯一目标重合，等待下一层目标后展开价格轴。
              </div>
            )}
            <div className="text-[10px] text-muted-foreground">
              当前左右眼：<span className="font-medium text-foreground">{targetLabel(targetWindow.left.target)}</span>
              <span className="px-1">↔</span>
              <span className="font-medium text-foreground">{targetLabel(targetWindow.right.target)}</span>
              {targetWindow.rangeState === "above-last-target" ? "；暂无更高目标，现价作为右侧。" : null}
              {targetWindow.rangeState === "below-first-target" ? "；暂无更低目标，现价作为左侧。" : null}
            </div>
          </div>
        ) : (
          <div className="border-t pt-2 text-[11px] text-muted-foreground">
            {currentPrice == null
              ? "取得首个合格监测价格后显示动态目标区间。"
              : "当前计划尚无可用价格目标；请在计划与审核中重新分析，并标注止盈、止损、观察或加仓点。"}
          </div>
        )}

      </div>
    </div>
  );
}

function MonitorPlanSummary({
  version,
  target,
  targetId,
}: {
  version?: MonitorPlanVersion | null;
  target?: MonitorDeliveryTarget;
  targetId?: string | null;
}) {
  if (!version?.plan) {
    return (
      <div className="rounded-md border border-dashed bg-muted/10 px-3 py-2 text-xs text-muted-foreground">
        尚无可展示的生效计划细节。
      </div>
    );
  }
  const enabledRules = version.plan.market_rules.filter((rule) => rule.enabled);
  const priceTargetCount = collectPriceTargets(version.plan).length;
  const intervals = Array.from(new Set(enabledRules.map((rule) => rule.parameters.interval))).sort();
  const scenarios = version.plan.watch_scenarios || [];
  const actionReadyCount = scenarios.filter((scenario) => scenario.automation_status === "action_ready").length;
  const watchOnlyCount = scenarios.filter((scenario) => scenario.automation_status !== "action_ready").length;
  const unsupportedConditionCount = scenarios.reduce(
    (total, scenario) => total + (scenario.source_conditions || []).filter((condition) => condition.coverage_status !== "mapped").length,
    0,
  );
  const autonomousReportApproval = version.plan.automation_policy?.activation_mode === "autonomous";
  const reportApprovalText = autonomousReportApproval
    ? version.status === "active"
      ? "AI 已完成证据门禁判断并自动启用 shadow 监测；所有进退仍由人工决定。"
      : "AI 已取得自主判断权限；计划只有通过数据门禁后才能自动换版。"
    : "该计划仍须人工确认后启用。";
  const deliveryLabel = target
    ? `飞书${target.chat_type === "group" ? "群聊" : "私聊"}已绑定`
    : targetId ? "飞书目标待确认" : "飞书目标未绑定";
  return (
    <div className="grid gap-2 rounded-md border bg-muted/10 p-3 text-xs" aria-label={`监控计划 v${version.version} 摘要`}>
      {version.plan.data_mode === "single_source" ? <SingleSourceWarning /> : null}
      {version.plan.source_horizon ? (
        <div className="rounded border border-blue-500/20 bg-blue-500/5 px-2 py-2 text-[11px]">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
            <span className="font-medium">来源：{version.plan.source_horizon === "weekly" ? "正式周报" : version.plan.source_horizon === "structural" ? "正式结构报告" : "正式日报"}</span>
            <span>{version.plan.analysis_ref?.title || version.plan.source_report_id}</span>
            {version.plan.source_period?.label ? <span>{version.plan.source_period.label}</span> : null}
            {version.plan.review_due_at ? <span>复核于 {formatDate(version.plan.review_due_at)}</span> : null}
          </div>
          <div className="mt-1 text-muted-foreground">
            action_ready {actionReadyCount} · watch_only {watchOnlyCount} · 未自动映射条件 {unsupportedConditionCount}；{reportApprovalText}
          </div>
        </div>
      ) : null}
      {priceTargetCount > 0 && priceTargetCount < 4 ? (
        <div className="rounded border border-amber-500/30 bg-amber-500/5 px-2 py-1.5 text-[11px] text-amber-800 dark:text-amber-200">
          当前计划只有 {priceTargetCount} 个价格目标；在“计划与审核”中选择 AI 重新分析，可生成带语义的四级目标阶梯，旧计划会持续运行到你确认新版本。
        </div>
      ) : null}
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        <div>
          <div className="text-muted-foreground">服务器检查</div>
          <div className="mt-0.5 font-medium">{quoteTierLabel(version.plan.quote_tier)}</div>
        </div>
        <div>
          <div className="text-muted-foreground">规则数据</div>
          <div className="mt-0.5 font-medium">{intervals.length ? `${intervals.join(" / ")} K线` : "尚无启用规则"}</div>
        </div>
        <div>
          <div className="text-muted-foreground">当前版本</div>
          <div className="mt-0.5 font-medium">v{version.version} · {enabledRules.length} 条规则</div>
        </div>
        <div>
          <div className="text-muted-foreground">量价分析</div>
          <div className="mt-0.5 font-medium">
            {version.plan.price_volume_policy?.enabled
              ? `${version.plan.price_volume_policy.interval} · 已启用`
              : version.plan.schema_version >= 2 ? "已关闭" : "v1 未配置"}
          </div>
        </div>
      </div>
      {enabledRules.length ? (
        <div
          className="grid overflow-hidden rounded-md border text-muted-foreground sm:grid-cols-2"
          data-monitor-rule-grid="compact"
        >
          {enabledRules.slice(0, 4).map((rule, index) => {
            const summary = ruleSummary(rule);
            return (
              <div
                key={rule.client_rule_id}
                className={cn(
                  "grid min-w-0 gap-0.5 bg-background/30 px-2 py-1.5",
                  index > 0 && "border-t",
                  index === 1 && "sm:border-l sm:border-t-0",
                  index === 2 && "sm:border-t",
                  index === 3 && "sm:border-l sm:border-t",
                )}
              >
                <PointBasisTooltip
                  label={summary}
                  basis={rule.kind.startsWith("price_") ? rulePointBasisText(rule) : null}
                  className="min-w-0 text-foreground"
                >
                  <span className="leading-4" title={summary}>{summary}</span>
                </PointBasisTooltip>
                <span className="font-mono text-[10px]">连续 {rule.parameters.confirmation_count} 次</span>
              </div>
            );
          })}
          {enabledRules.length > 4 ? (
            <div className="border-t px-2 py-1.5 text-[10px] sm:col-span-2">
              另有 {enabledRules.length - 4} 条规则，请在“计划与审核”中展开。
            </div>
          ) : null}
        </div>
      ) : null}
      <div className="flex flex-wrap items-center justify-between gap-1 border-t pt-2 text-[11px] text-muted-foreground">
        <span>数据截至：{formatDate(version.data_as_of)}</span>
        <span>{deliveryLabel}</span>
      </div>
    </div>
  );
}

function MonitorSoundControls() {
  const {
    availability,
    disableSound,
    enableAndTest,
    enabled,
    playbackStatus,
    setVolume,
    streamStatus,
    testSound,
    volume,
  } = usePortfolioMonitorEffects();
  const loading = playbackStatus === "loading";
  const statusLabel = playbackStatus === "blocked"
    ? "自动播放被阻止"
    : playbackStatus === "unavailable" || availability?.audio_ready === false
      ? "音频素材未就绪"
      : enabled
        ? "声音已启用"
        : "声音已关闭";
  const assetLabel = availability == null
    ? "素材状态待确认"
    : availability.available
      ? "网页音频与上涨/下跌飞书表情已就绪"
      : availability.audio_ready
        ? `网页音频就绪；${[
          (availability.up_sticker_ready ?? availability.sticker_ready) === false ? "上涨" : "",
          (availability.down_sticker_ready ?? availability.sticker_ready) === false ? "下跌" : "",
        ].filter(Boolean).join("、") || "部分"}飞书表情未就绪`
        : "YMCA 音频未就绪";
  return (
    <div
      role="group"
      aria-label="YMCA 突破提醒音"
      data-playback-status={playbackStatus}
      data-stream-status={streamStatus}
      className="flex flex-wrap items-center gap-2 rounded-md border bg-background px-2.5 py-1.5"
    >
      <span className="inline-flex items-center gap-1 text-xs font-medium" aria-live="polite">
        {enabled ? <Volume2 className="h-3.5 w-3.5 text-fuchsia-600" aria-hidden="true" /> : <VolumeX className="h-3.5 w-3.5 text-muted-foreground" aria-hidden="true" />}
        {statusLabel}
      </span>
      {enabled ? (
        <>
          <button
            type="button"
            disabled={loading}
            onClick={() => void testSound()}
            className="rounded border px-2 py-1 text-[11px] hover:bg-muted disabled:opacity-50"
          >
            试听 YMCA 提醒音
          </button>
          <button
            type="button"
            onClick={disableSound}
            className="rounded border px-2 py-1 text-[11px] hover:bg-muted"
          >
            静音 YMCA 提醒音
          </button>
        </>
      ) : (
        <button
          type="button"
          disabled={loading || availability?.audio_ready === false}
          onClick={() => void enableAndTest()}
          className="rounded border px-2 py-1 text-[11px] hover:bg-muted disabled:opacity-50"
        >
          {loading ? "正在加载音频…" : "启用并试听 YMCA 提醒音"}
        </button>
      )}
      <label className="inline-flex items-center gap-1 text-[11px] text-muted-foreground" title={assetLabel}>
        音量
        <input
          aria-label="YMCA 提醒音量"
          type="range"
          min={0}
          max={100}
          step={5}
          value={Math.round(volume * 100)}
          onChange={(event) => setVolume(Number(event.target.value) / 100)}
          className="w-20 accent-fuchsia-600"
        />
        <span className="w-7 text-right tabular-nums">{Math.round(volume * 100)}%</span>
      </label>
      <span className="text-[10px] text-muted-foreground">{assetLabel}</span>
      <span className="sr-only">事件流{streamStatus}</span>
    </div>
  );
}

function MonitorEventRow({
  event,
  displayName,
  onAcknowledge,
}: {
  event: MonitorEvent;
  displayName: string;
  onAcknowledge: (event: MonitorEvent) => void;
}) {
  const price = event.facts.last_price;
  const assessment = event.facts.target_assessment;
  const delivery = event.deliveries?.[0];
  const displayTitle = event.title.startsWith(event.symbol)
    ? `${displayName}${event.title.slice(event.symbol.length)}`
    : event.title;
  return (
    <div className="grid gap-2 border-t px-4 py-3 first:border-t-0 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center">
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-xs font-medium">{displayName}</span>
          <span className={cn("rounded border px-1.5 py-0.5 text-[10px]", event.severity === "critical" ? "border-red-500/40 text-red-600" : "border-amber-500/40 text-amber-700")}>{event.severity}</span>
          {delivery?.status === "shadow_suppressed" ? (
            <span className="rounded border border-blue-500/40 bg-blue-500/10 px-1.5 py-0.5 text-[10px] text-blue-700 dark:text-blue-300">影子命中 · 未发送</span>
          ) : null}
          {delivery?.status === "delivery_uncertain" ? (
            <span className="rounded border border-red-500/40 bg-red-500/10 px-1.5 py-0.5 text-[10px] text-red-700 dark:text-red-300">投递结果未知</span>
          ) : null}
          {event.facts.quality_status === "single_source" ? <SingleSourceWarning compact /> : null}
          <span className="text-xs text-muted-foreground">{formatDate(event.first_seen_at)}</span>
        </div>
        <div className="mt-1 text-sm font-medium">{displayTitle}</div>
        <p className="mt-1 text-xs text-muted-foreground">{event.summary}{typeof price === "number" ? ` 最新价 ${price}` : ""}</p>
        {event.episode_id ? (
          <div className="mt-2 flex flex-wrap gap-1.5 text-[10px] text-muted-foreground">
            <span className="rounded border px-1.5 py-0.5">episode · {event.phase || "observing"}</span>
            {event.outcome ? <span className="rounded border px-1.5 py-0.5">{event.outcome}</span> : null}
            <span className="rounded border px-1.5 py-0.5">{event.volume_verdict || "insufficient_evidence"}</span>
          </div>
        ) : null}
        {assessment ? (
          <div
            aria-label={`${displayName} 目标位量价确认`}
            data-target-assessment={assessment.decision}
            className="mt-2 grid gap-1.5 rounded-md border bg-muted/10 px-2.5 py-2 text-xs"
          >
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="font-medium">
                L{assessment.target_level} · {TARGET_INTENT_LABELS[assessment.target_intent] || assessment.target_intent}
              </span>
              <span className="rounded border px-1.5 py-0.5 text-[10px] text-muted-foreground">
                {assessment.phase === "reached"
                  ? "已到目标"
                  : `接近目标 · ${Math.round(assessment.distance_bps)} bps`}
              </span>
              <span className={cn("rounded border px-1.5 py-0.5 text-[10px] font-semibold", targetAssessmentTone(assessment.decision))}>
                {TARGET_ASSESSMENT_LABELS[assessment.decision]}
              </span>
            </div>
            <p className="leading-5 text-muted-foreground">{assessment.message}</p>
          </div>
        ) : null}
      </div>
      {event.status !== "resolved" ? (
        <button type="button" onClick={() => onAcknowledge(event)} className="rounded-md border px-3 py-1.5 text-xs hover:bg-muted">确认收到</button>
      ) : <span className="text-xs text-muted-foreground">已确认</span>}
    </div>
  );
}

export default function PortfolioMonitorPanel({
  holdings,
  selectedSymbols,
  manualSymbols = selectedSymbols,
  autonomousSymbols = selectedSymbols,
  selectionRevision = 0,
  selectionHydrationPending = false,
  onHydrateSelection,
}: {
  holdings: PortfolioHolding[];
  selectedSymbols: Set<string>;
  manualSymbols?: Set<string>;
  autonomousSymbols?: Set<string>;
  selectionRevision?: number;
  selectionHydrationPending?: boolean;
  onHydrateSelection?: (selection: { manual: string[]; autonomous: string[] }) => void;
}) {
  const [profiles, setProfiles] = useState<MonitorProfile[]>([]);
  const [events, setEvents] = useState<MonitorEvent[]>([]);
  const [targets, setTargets] = useState<MonitorDeliveryTarget[]>([]);
  const [status, setStatus] = useState<PortfolioMonitoringStatus | null>(null);
  const [autopilot, setAutopilot] = useState<MonitorAutopilotConfig | null>(null);
  const [autopilotSelectionSyncState, setAutopilotSelectionSyncState] = useState<"idle" | "syncing" | "synced" | "error">("idle");
  const [autopilotSelectionSyncCycle, setAutopilotSelectionSyncCycle] = useState(0);
  const [autopilotRuns, setAutopilotRuns] = useState<MonitorAutopilotRun[]>([]);
  const [monitoringTargetCards, setMonitoringTargetCards] = useState<MonitorTargetMonitoringCard[]>([]);
  const [showAllAutopilotRuns, setShowAllAutopilotRuns] = useState(false);
  const [showAllEvents, setShowAllEvents] = useState(false);
  const [recommendations, setRecommendations] = useState<MonitorRecommendation[]>([]);
  const [reportCandidates, setReportCandidates] = useState<Record<string, MonitorReportCandidate[]>>({});
  const [selectedReportRefs, setSelectedReportRefs] = useState<Record<string, string>>({});
  const [plannerJob, setPlannerJob] = useState<MonitorPlannerJob | null>(null);
  const [usageJobId, setUsageJobId] = useState<string | null>(null);
  const [selectedTarget, setSelectedTarget] = useState("");
  const [bindingAttempt, setBindingAttempt] = useState<MonitorDeliveryBindingAttempt | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [openingProfileId, setOpeningProfileId] = useState<string | null>(null);
  const [drawerProfile, setDrawerProfile] = useState<MonitorProfile | null>(null);
  const [drawerPlanVersion, setDrawerPlanVersion] = useState<number | null>(null);
  const [draftPlan, setDraftPlan] = useState<MonitorPlan | null>(null);
  const [draftNumericPlan, setDraftNumericPlan] = useState<MonitorPlanNumericDraft | null>(null);
  const [draftBaselineSignature, setDraftBaselineSignature] = useState<string | null>(null);
  const [planFormErrors, setPlanFormErrors] = useState<string[]>([]);
  const [expanded, setExpanded] = useState(true);
  const [showClosedProfiles, setShowClosedProfiles] = useState(false);
  const [selectedProfileId, setSelectedProfileId] = useState<string | null>(null);
  const [carouselPaused, setCarouselPaused] = useState(false);
  const [heartbeatNow, setHeartbeatNow] = useState(() => Date.now());
  const [syncFailureCount, setSyncFailureCount] = useState(0);
  const [lastSuccessfulSyncAt, setLastSuccessfulSyncAt] = useState<number | null>(null);
  const [connectionRecoveredAt, setConnectionRecoveredAt] = useState<number | null>(null);
  const syncFailureCountRef = useRef(0);
  const mountedRef = useRef(true);
  const syncGenerationRef = useRef(0);
  const openPlanRequestRef = useRef(0);
  const autopilotSelectionSyncInFlightRef = useRef(false);
  const failedAutopilotSelectionSyncRef = useRef<string | null>(null);
  const autopilotSelectionHydrationRequestedRef = useRef(false);
  const handledSelectionRevisionRef = useRef(selectionRevision);
  const { liveEvents, resetVersion, syncAvailability } = usePortfolioMonitorEffects();
  const ymcaAvailability = status?.effects?.ymca_v1;
  const displayEvents = useMemo(() => {
    const merged = new Map<string, MonitorEvent>();
    for (const event of liveEvents) merged.set(event.event_id, event);
    for (const event of events) merged.set(event.event_id, event);
    return [...merged.values()]
      .sort((left, right) => Date.parse(right.first_seen_at) - Date.parse(left.first_seen_at))
      .slice(0, 100);
  }, [events, liveEvents]);
  const renderedEvents = showAllEvents
    ? displayEvents
    : displayEvents.slice(0, RECENT_EVENT_PREVIEW_LIMIT);
  const effectiveMode = status?.effective_mode || status?.runtime.mode || "off";
  const runtimeLabel = effectiveMode === "deliver"
    ? (status?.runtime.running ? (status.runtime.leader ? "真实提醒" : "真实提醒 · 备用") : "真实提醒未运行")
    : effectiveMode === "shadow"
      ? (status?.runtime.running ? (status.runtime.leader ? "影子运行" : "影子模式 · 备用") : "影子模式未运行")
      : "监控服务未启动";
  const runtimeTone = effectiveMode === "deliver" && status?.runtime.running && status.runtime.leader
    ? statusClass("active")
    : effectiveMode === "shadow"
      ? statusClass("paused")
      : statusClass("closed");
  const monitorDisconnected = syncFailureCount >= 2;
  const runtimeDisplayTone = monitorDisconnected
    ? "border-red-500/40 bg-red-500/10 text-red-700 dark:text-red-300"
    : runtimeTone;
  const connectionRecentlyRecovered = !monitorDisconnected && connectionRecoveredAt != null;
  const drawerPlanDirty = Boolean(
    draftPlan
    && draftNumericPlan
    && draftBaselineSignature
    && monitorPlanFormSignature(draftPlan, draftNumericPlan) !== draftBaselineSignature,
  );
  const markSyncSuccess = useCallback(() => {
    const now = Date.now();
    if (syncFailureCountRef.current >= 2) setConnectionRecoveredAt(now);
    syncFailureCountRef.current = 0;
    setSyncFailureCount(0);
    setLastSuccessfulSyncAt(now);
  }, []);

  const markSyncFailure = useCallback(() => {
    syncFailureCountRef.current += 1;
    setSyncFailureCount(syncFailureCountRef.current);
  }, []);

  const beginSync = useCallback(() => {
    syncGenerationRef.current += 1;
    return syncGenerationRef.current;
  }, []);

  const isCurrentSync = useCallback((generation: number) => (
    mountedRef.current && generation === syncGenerationRef.current
  ), []);
  const clearUsageJobRequest = useCallback(() => setUsageJobId(null), []);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      syncGenerationRef.current += 1;
      openPlanRequestRef.current += 1;
    };
  }, []);

  const load = useCallback(async () => {
    const generation = beginSync();
    try {
      const [
        profilesResult,
        eventsResult,
        targetsResult,
        statusResult,
        autopilotResult,
        runsResult,
        targetCardsResult,
        recommendationsResult,
      ] = await Promise.all([
        api.listPortfolioMonitors(),
        api.listPortfolioMonitorEvents(20),
        api.listPortfolioMonitorDeliveryTargets(),
        api.getPortfolioMonitoringStatus(),
        api.getPortfolioMonitoringAutopilot(),
        api.listPortfolioMonitoringAutopilotRuns(20),
        api.listPortfolioMonitoringTargets(),
        api.listPortfolioMonitorRecommendations(20),
      ]);
      if (!isCurrentSync(generation)) return;
      setProfiles((current) => mergeMonitorProfileSummaries(current, profilesResult.profiles));
      setEvents(eventsResult.events);
      setTargets(targetsResult.targets);
      setStatus(statusResult);
      setAutopilot((current) => (
        current && current.revision > autopilotResult.revision ? current : autopilotResult
      ));
      setAutopilotRuns(runsResult.runs);
      setMonitoringTargetCards(targetCardsResult.targets);
      setRecommendations(recommendationsResult.recommendations);
      markSyncSuccess();
      setSelectedTarget((current) => current || autopilotResult.delivery_target_id || "");
    } catch (error) {
      if (!isCurrentSync(generation)) return;
      markSyncFailure();
      toast.error(error instanceof Error ? error.message : "加载 AI 监控中心失败");
    }
  }, [beginSync, isCurrentSync, markSyncFailure, markSyncSuccess]);

  useEffect(() => { void load(); }, [load]);

  useEffect(() => {
    syncAvailability(ymcaAvailability);
  }, [
    syncAvailability,
    ymcaAvailability?.audio_ready,
    ymcaAvailability?.sticker_ready,
    ymcaAvailability?.up_sticker_ready,
    ymcaAvailability?.down_sticker_ready,
    ymcaAvailability?.available,
  ]);

  useEffect(() => {
    if (!resetVersion) return;
    let cancelled = false;
    void api.listPortfolioMonitorEvents(20).then((result) => {
      if (!cancelled) setEvents(result.events);
    }).catch(() => {
      // The regular panel refresh remains available if a cursor reset coincides with an API outage.
    });
    return () => { cancelled = true; };
  }, [resetVersion]);

  const refreshLiveState = useCallback(async () => {
    const generation = beginSync();
    try {
      const [profilesResult, statusResult, autopilotResult, runsResult, targetCardsResult, recommendationsResult] = await Promise.all([
        api.listPortfolioMonitors(),
        api.getPortfolioMonitoringStatus(),
        api.getPortfolioMonitoringAutopilot(),
        api.listPortfolioMonitoringAutopilotRuns(20),
        api.listPortfolioMonitoringTargets(),
        api.listPortfolioMonitorRecommendations(20),
      ]);
      if (!isCurrentSync(generation)) return;
      setProfiles((current) => mergeMonitorProfileSummaries(current, profilesResult.profiles));
      setStatus(statusResult);
      setAutopilot((current) => (
        current && current.revision > autopilotResult.revision ? current : autopilotResult
      ));
      setAutopilotRuns(runsResult.runs);
      setMonitoringTargetCards(targetCardsResult.targets);
      setRecommendations(recommendationsResult.recommendations);
      markSyncSuccess();
    } catch {
      if (!isCurrentSync(generation)) return;
      markSyncFailure();
    }
  }, [beginSync, isCurrentSync, markSyncFailure, markSyncSuccess]);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const poll = async () => {
      await refreshLiveState();
      if (!cancelled) timer = setTimeout(poll, 5_000);
    };
    timer = setTimeout(poll, 5_000);
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [refreshLiveState]);

  useEffect(() => {
    if (!expanded) return;
    const timer = setInterval(() => setHeartbeatNow(Date.now()), 1_000);
    return () => clearInterval(timer);
  }, [expanded]);

  useEffect(() => {
    if (connectionRecoveredAt == null) return;
    const timer = setTimeout(() => setConnectionRecoveredAt(null), 10_000);
    return () => clearTimeout(timer);
  }, [connectionRecoveredAt]);

  useEffect(() => {
    const bindingId = bindingAttempt?.binding_id;
    if (!bindingId || bindingAttempt.status !== "pending") return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const poll = async () => {
      try {
        const result = await api.getPortfolioMonitorDeliveryBindingCode(bindingId);
        if (cancelled) return;
        setBindingAttempt((current) => ({
          ...current,
          ...result,
          code: result.code || current?.code,
          command: result.command || current?.command,
        }));
        if (result.status === "claimed" && result.target) {
          setSelectedTarget(result.target.target_id);
          toast.success("飞书提醒目标绑定成功。");
          await load();
          return;
        }
        if (result.status === "expired") return;
      } catch {
        // Keep the one-time code visible; a later poll or manual check can recover.
      }
      if (!cancelled) timer = setTimeout(poll, 2_000);
    };
    timer = setTimeout(poll, 1_500);
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [bindingAttempt?.binding_id, bindingAttempt?.status, load]);

  const selectedNames = useMemo(() => holdings
    .filter((holding) => selectedSymbols.has(String(holding.symbol || holding.code || "")))
    .map((holding) => holding.name || holding.symbol || holding.code)
    .join("、"), [holdings, selectedSymbols]);

  const selectedManualSymbols = useMemo(
    () => [...new Set(
      Array.from(manualSymbols, (symbol) => String(symbol).trim().toUpperCase()).filter(Boolean),
    )].sort(),
    [manualSymbols],
  );

  const selectedAutonomousSymbols = useMemo(
    () => [...new Set(
      Array.from(autonomousSymbols, (symbol) => String(symbol).trim().toUpperCase()).filter(Boolean),
    )].sort(),
    [autonomousSymbols],
  );

  const configuredAutopilotSymbols = useMemo(
    () => [...new Set(
      (autopilot?.selected_symbols || [])
        .map((symbol) => String(symbol).trim().toUpperCase())
        .filter(Boolean),
    )].sort(),
    [autopilot?.selected_symbols],
  );

  const selectedAutopilotSymbols = useMemo(
    () => selectedAutonomousSymbols.filter(isAutopilotEligibleSymbol),
    [selectedAutonomousSymbols],
  );
  const unsupportedAutopilotSymbols = useMemo(
    () => selectedAutonomousSymbols.filter((symbol) => !isAutopilotEligibleSymbol(symbol)),
    [selectedAutonomousSymbols],
  );

  const visibleAutopilotRuns = useMemo(() => {
    const scope = new Set(
      configuredAutopilotSymbols.length ? configuredAutopilotSymbols : selectedAutopilotSymbols,
    );
    return autopilotRuns.filter((run) => scope.has(run.symbol.trim().toUpperCase()));
  }, [autopilotRuns, configuredAutopilotSymbols, selectedAutopilotSymbols]);
  const displayedAutopilotRuns = showAllAutopilotRuns
    ? visibleAutopilotRuns
    : visibleAutopilotRuns.slice(0, AUTOPILOT_RUN_PREVIEW_LIMIT);

  const selectedAutopilotFingerprint = selectedAutopilotSymbols.join("|");
  const configuredAutopilotFingerprint = configuredAutopilotSymbols.join("|");
  const autopilotOperational = Boolean(autopilot?.enabled && configuredAutopilotSymbols.length);

  const selectedAutopilotNames = useMemo(() => {
    const selected = new Set(selectedAutopilotSymbols);
    return holdings
      .filter((holding) => selected.has(String(holding.symbol || holding.code || "").trim().toUpperCase()))
      .map((holding) => String(holding.name || holding.code || holding.symbol || "").trim())
      .filter(Boolean)
      .join("、");
  }, [holdings, selectedAutopilotSymbols]);

  useEffect(() => {
    if (!selectionHydrationPending) {
      autopilotSelectionHydrationRequestedRef.current = false;
      return;
    }
    if (!autopilot || autopilotSelectionHydrationRequestedRef.current || !onHydrateSelection) return;
    autopilotSelectionHydrationRequestedRef.current = true;
    const autonomous = new Set(configuredAutopilotSymbols);
    const manual = [...new Set(profiles
      .filter((profile) => profile.status !== "closed")
      .map((profile) => profile.symbol.trim().toUpperCase())
      .filter((symbol) => symbol && !autonomous.has(symbol)))].sort();
    onHydrateSelection({ manual, autonomous: configuredAutopilotSymbols });
  }, [autopilot, configuredAutopilotSymbols, onHydrateSelection, profiles, selectionHydrationPending]);

  useEffect(() => {
    if (selectionHydrationPending) return;
    if (!autopilot?.enabled) {
      handledSelectionRevisionRef.current = selectionRevision;
      failedAutopilotSelectionSyncRef.current = null;
      if (autopilotSelectionSyncState !== "idle") setAutopilotSelectionSyncState("idle");
      return;
    }
    // Polling may discover an enabled server config in another open tab. Only a
    // holding-matrix change made in this mounted page is allowed to write the
    // global scope; an old tab's initial localStorage snapshot is read-only.
    if (handledSelectionRevisionRef.current === selectionRevision) return;
    const shouldRemainEnabled = selectedAutopilotSymbols.length > 0;
    if (
      selectedAutopilotFingerprint === configuredAutopilotFingerprint
      && autopilot.enabled === shouldRemainEnabled
    ) {
      handledSelectionRevisionRef.current = selectionRevision;
      failedAutopilotSelectionSyncRef.current = null;
      return;
    }

    const pendingSelectionRevision = selectionRevision;
    const syncKey = `${pendingSelectionRevision}:${autopilot.enabled}:${configuredAutopilotFingerprint}->${shouldRemainEnabled}:${selectedAutopilotFingerprint}`;
    if (
      autopilotSelectionSyncInFlightRef.current
      || failedAutopilotSelectionSyncRef.current === syncKey
    ) return;

    autopilotSelectionSyncInFlightRef.current = true;
    setAutopilotSelectionSyncState("syncing");
    void api.configurePortfolioMonitoringAutopilot({
      enabled: shouldRemainEnabled,
      selected_symbols: selectedAutopilotSymbols,
      change_source: "holding_selection",
      daily_close_enabled: autopilot.daily_close_enabled,
      delivery_target_id: selectedTarget || null,
      runtime_mode: autopilot.runtime_mode,
    }).then((next) => {
      if (!mountedRef.current) return;
      setAutopilot((current) => (
        current && current.revision > next.revision ? current : next
      ));
      handledSelectionRevisionRef.current = pendingSelectionRevision;
      failedAutopilotSelectionSyncRef.current = null;
      setAutopilotSelectionSyncState("synced");
      if (!selectedAutopilotSymbols.length) {
        toast.success(selectedAutonomousSymbols.length
          ? "所选标的暂不支持自主监控，自主监控已自动关闭。"
          : "持仓矩阵已清空 AI 自主等级，自主监控已自动关闭。");
      }
    }).catch((error) => {
      if (!mountedRef.current) return;
      failedAutopilotSelectionSyncRef.current = syncKey;
      setAutopilotSelectionSyncState("error");
      toast.error(error instanceof Error ? error.message : "自主监控范围同步失败");
    }).finally(() => {
      autopilotSelectionSyncInFlightRef.current = false;
      if (mountedRef.current) setAutopilotSelectionSyncCycle((value) => value + 1);
    });
  }, [
    autopilot?.daily_close_enabled,
    autopilot?.delivery_target_id,
    autopilot?.enabled,
    autopilot?.revision,
    autopilot?.runtime_mode,
    autopilotSelectionSyncCycle,
    autopilotSelectionSyncState,
    configuredAutopilotFingerprint,
    selectedAutopilotFingerprint,
    selectedAutopilotSymbols,
    selectedAutonomousSymbols,
    selectedTarget,
    selectionHydrationPending,
    selectionRevision,
  ]);

  useEffect(() => {
    if (!selectedManualSymbols.length) {
      setReportCandidates({});
      setSelectedReportRefs({});
      return;
    }
    let cancelled = false;
    void Promise.all(
      selectedManualSymbols.map(async (symbol) => {
        const result = await api.listPortfolioMonitorReportCandidates(symbol);
        return [symbol, result.candidates] as const;
      }),
    ).then((entries) => {
      if (cancelled) return;
      const nextCandidates = Object.fromEntries(entries);
      setReportCandidates(nextCandidates);
      setSelectedReportRefs((current) => {
        const next: Record<string, string> = {};
        for (const symbol of selectedManualSymbols) {
          const candidates = nextCandidates[symbol] || [];
          const retained = candidates.find((candidate) => candidate.report_ref === current[symbol]);
          const preferred = retained
            || candidates.find((candidate) => !candidate.stale && candidate.quality_status === "ready")
            || candidates[0];
          if (preferred) next[symbol] = preferred.report_ref;
        }
        return next;
      });
    }).catch(() => {
      if (!cancelled) setReportCandidates({});
    });
    return () => { cancelled = true; };
  }, [selectedManualSymbols]);

  const holdingNameByIdentifier = useMemo(() => {
    const names = new Map<string, string>();
    for (const holding of holdings) {
      const name = String(holding.name || "").trim();
      const symbol = String(holding.symbol || "").trim().toUpperCase();
      const code = String(holding.code || symbol.split(".")[0] || "").trim().toUpperCase();
      if (!name) continue;
      if (symbol) names.set(symbol, name);
      if (code) names.set(code, name);
    }
    return names;
  }, [holdings]);

  const deliveryTargetById = useMemo(
    () => new Map(targets.map((target) => [target.target_id, target])),
    [targets],
  );

  const openProfiles = useMemo(
    () => profiles.filter((profile) => profile.status !== "closed"),
    [profiles],
  );
  const closedProfileCount = profiles.length - openProfiles.length;
  const visibleProfiles = showClosedProfiles ? profiles : openProfiles;
  const visibleCarouselTargets = useMemo<MonitorCarouselTarget[]>(() => {
    const knownProfileSymbols = new Set(
      openProfiles.map((profile) => profile.symbol.trim().toUpperCase()),
    );
    const targetCardBySymbol = new Map(
      monitoringTargetCards.map((card) => [card.symbol.trim().toUpperCase(), card]),
    );
    const latestRunBySymbol = new Map<string, MonitorAutopilotRun>();
    for (const run of autopilotRuns) {
      const symbol = run.symbol.trim().toUpperCase();
      if (symbol && !latestRunBySymbol.has(symbol)) latestRunBySymbol.set(symbol, run);
    }
    const targetCardSymbols = monitoringTargetCards
      .filter((card) => card.selected)
      .map((card) => card.symbol.trim().toUpperCase());
    const pendingSymbols = autopilot?.enabled
      ? [...new Set([...configuredAutopilotSymbols, ...targetCardSymbols])]
        .filter((symbol) => !knownProfileSymbols.has(symbol))
      : [];
    return [
      ...visibleProfiles.map((profile) => ({
        key: profile.profile_id,
        kind: "profile" as const,
        symbol: profile.symbol.trim().toUpperCase(),
        profile,
        card: targetCardBySymbol.get(profile.symbol.trim().toUpperCase()) || null,
      })),
      ...pendingSymbols.map((symbol) => ({
        key: `autopilot:${symbol}`,
        kind: "pending_autopilot" as const,
        symbol,
        run: latestRunBySymbol.get(symbol) || null,
        card: targetCardBySymbol.get(symbol) || null,
      })),
    ];
  }, [autopilot?.enabled, autopilotRuns, configuredAutopilotSymbols, monitoringTargetCards, openProfiles, visibleProfiles]);
  const selectedCarouselTarget = visibleCarouselTargets.find(
    (target) => target.key === selectedProfileId,
  ) || visibleCarouselTargets[0] || null;
  const selectedProfile = selectedCarouselTarget?.kind === "profile"
    ? selectedCarouselTarget.profile
    : null;
  const selectedTargetCard = selectedCarouselTarget?.card || null;
  const selectedPendingTarget = selectedCarouselTarget?.kind === "pending_autopilot"
    ? selectedCarouselTarget
    : null;
  const selectedProfileIndex = selectedCarouselTarget
    ? visibleCarouselTargets.findIndex((target) => target.key === selectedCarouselTarget.key)
    : -1;
  const visibleProfileIds = visibleCarouselTargets.map((target) => target.key).join("|");

  useEffect(() => {
    if (carouselPaused || !visibleProfileIds.includes("|")) return undefined;
    const profileIds = visibleProfileIds.split("|").filter(Boolean);
    const timer = window.setInterval(() => {
      setSelectedProfileId((current) => {
        const currentIndex = Math.max(0, profileIds.indexOf(current || ""));
        return profileIds[(currentIndex + 1) % profileIds.length] || null;
      });
    }, 9_000);
    return () => window.clearInterval(timer);
  }, [carouselPaused, visibleProfileIds]);

  const selectAdjacentProfile = (offset: number) => {
    if (!visibleCarouselTargets.length) return;
    const currentIndex = selectedProfileIndex < 0 ? 0 : selectedProfileIndex;
    const nextIndex = (
      currentIndex + offset + visibleCarouselTargets.length
    ) % visibleCarouselTargets.length;
    setSelectedProfileId(visibleCarouselTargets[nextIndex]?.key || null);
  };

  const showPlan = (detail: MonitorProfile, preferredVersion?: number | null) => {
    const version = selectPlanVersion(detail, preferredVersion);
    const nextPlan = version?.plan ? structuredClone(version.plan) : null;
    const nextNumericPlan = nextPlan ? createMonitorPlanNumericDraft(nextPlan) : null;
    setProfiles((current) => {
      const index = current.findIndex((profile) => profile.profile_id === detail.profile_id);
      if (index < 0) return [...current, detail];
      if (current[index] === detail) return current;
      const next = [...current];
      next[index] = detail;
      return next;
    });
    setDrawerProfile(detail);
    setDrawerPlanVersion(version?.version ?? null);
    setDraftPlan(nextPlan);
    setDraftNumericPlan(nextNumericPlan);
    setDraftBaselineSignature(nextPlan && nextNumericPlan
      ? monitorPlanFormSignature(nextPlan, nextNumericPlan)
      : null);
    setPlanFormErrors([]);
  };

  useEffect(() => {
    if (!drawerProfile) return;
    const freshProfile = profiles.find((profile) => profile.profile_id === drawerProfile.profile_id);
    if (!freshProfile || freshProfile === drawerProfile) return;
    const freshVersion = selectPlanVersion(freshProfile, drawerPlanVersion);
    const selectedVersionStillAvailable = drawerPlanVersion != null
      && freshVersion?.version === drawerPlanVersion;
    setDrawerProfile(freshProfile);
    if (selectedVersionStillAvailable && drawerPlanDirty) return;
    const nextPlan = freshVersion?.plan ? structuredClone(freshVersion.plan) : null;
    const nextNumericPlan = nextPlan ? createMonitorPlanNumericDraft(nextPlan) : null;
    setDrawerPlanVersion(freshVersion?.version ?? null);
    setDraftPlan(nextPlan);
    setDraftNumericPlan(nextNumericPlan);
    setDraftBaselineSignature(nextPlan && nextNumericPlan
      ? monitorPlanFormSignature(nextPlan, nextNumericPlan)
      : null);
    setPlanFormErrors([]);
  }, [drawerPlanDirty, drawerPlanVersion, drawerProfile, profiles]);

  const closePlanDrawer = (force = false) => {
    if (!force && drawerPlanDirty && !window.confirm("监控计划还有未保存修改，确认放弃并关闭？")) return;
    openPlanRequestRef.current += 1;
    setOpeningProfileId(null);
    setDrawerProfile(null);
    setDrawerPlanVersion(null);
    setDraftPlan(null);
    setDraftNumericPlan(null);
    setDraftBaselineSignature(null);
    setPlanFormErrors([]);
  };

  const selectDrawerPlanVersion = (versionNumber: number) => {
    if (!drawerProfile || versionNumber === drawerPlanVersion) return;
    if (drawerPlanDirty && !window.confirm("切换版本会放弃当前未保存修改，是否继续？")) return;
    const version = drawerProfile.plans?.find((item) => item.version === versionNumber);
    if (!version) return;
    const nextPlan = structuredClone(version.plan);
    const nextNumericPlan = createMonitorPlanNumericDraft(nextPlan);
    setDrawerPlanVersion(version.version);
    setDraftPlan(nextPlan);
    setDraftNumericPlan(nextNumericPlan);
    setDraftBaselineSignature(monitorPlanFormSignature(nextPlan, nextNumericPlan));
    setPlanFormErrors([]);
  };

  const updateDraftPlan = (nextPlan: MonitorPlan) => {
    setDraftPlan(nextPlan);
    setPlanFormErrors([]);
  };

  const updateDraftNumericPlan = (nextDraft: MonitorPlanNumericDraft) => {
    setDraftNumericPlan(nextDraft);
    setPlanFormErrors([]);
  };

  const toggleRuntime = async () => {
    if (
      status?.enabled_by_config
      && status.runtime.running
      && !window.confirm(`停止监控服务？当前有 ${status.active_profiles} 个已启用标的会暂停自动检查；计划和历史记录会保留。`)
    ) return;
    setBusy("runtime-control");
    try {
      let nextStatus: PortfolioMonitoringStatus;
      if (status?.enabled_by_config && status.runtime.running) {
        nextStatus = await api.configurePortfolioMonitoringRuntime(false);
        toast.success("监控服务已停止，计划和历史记录均已保留。");
      } else if (status?.enabled_by_config) {
        nextStatus = await api.startPortfolioMonitoringRuntime();
        toast.success("监控服务已重新启动。");
      } else {
        nextStatus = await api.configurePortfolioMonitoringRuntime(true, "shadow");
        toast.success("影子监控已启动：会真实检查和记录，但暂不发送飞书。");
      }
      setStatus(nextStatus);
      setHeartbeatNow(Date.now());
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "监控服务切换失败");
    } finally {
      setBusy(null);
    }
  };

  const toggleAutopilot = async () => {
    const nextEnabled = !autopilotOperational;
    if (nextEnabled && !selectedAutopilotSymbols.length) {
      toast.error("请先在持仓矩阵把至少一个 A 股或场内 ETF 切换为紫色 AI 自主等级。");
      return;
    }
    if (!nextEnabled && !window.confirm("关闭自主监控后，现有计划仍会保留，但 Agent 不再自动研究或换版。确定关闭吗？")) {
      return;
    }
    setBusy("autopilot-control");
    try {
      if (nextEnabled && !status?.enabled_by_config) {
        setStatus(await api.configurePortfolioMonitoringRuntime(true, "shadow"));
      }
      const next = await api.configurePortfolioMonitoringAutopilot({
        enabled: nextEnabled,
        selected_symbols: selectedAutopilotSymbols,
        change_source: "user_toggle",
        daily_close_enabled: true,
        delivery_target_id: selectedTarget || null,
        runtime_mode: "shadow",
      });
      setAutopilot(next);
      failedAutopilotSelectionSyncRef.current = null;
      setAutopilotSelectionSyncState("idle");
      toast.success(
        nextEnabled
          ? `自主监控已开启：Agent 只会分析已选的 ${next.selected_symbols.length} 只标的；所有买卖仍由你决定。`
          : "自主监控已关闭，已有计划和历史记录均已保留。",
      );
      await load();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "自主监控切换失败");
    } finally {
      setBusy(null);
    }
  };

  const acknowledgeRecommendation = async (
    recommendation: MonitorRecommendation,
    feedback: MonitorRecommendationFeedback,
  ) => {
    try {
      const updated = await api.acknowledgePortfolioMonitorRecommendation(
        recommendation.recommendation_id,
        feedback,
      );
      setRecommendations((current) => current.map((item) => (
        item.recommendation_id === updated.recommendation_id ? updated : item
      )));
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "记录反馈失败");
    }
  };

  const configureDecisionRiskPreference = async (card: MonitorTargetMonitoringCard) => {
    const existing = card.risk_preference;
    const holding = holdings.find((item) => String(item.symbol || item.code || "").toUpperCase().startsWith(card.symbol.split(".")[0]));
    const riskText = window.prompt("单标的最大风险金额（元，必填；不会自动下单）", existing?.max_risk_amount != null ? String(existing.max_risk_amount) : "");
    if (riskText == null) return false;
    const maxRiskAmount = Number(riskText);
    if (!Number.isFinite(maxRiskAmount) || maxRiskAmount <= 0) {
      toast.error("请输入大于 0 的最大风险金额。");
      return false;
    }
    const addText = window.prompt("单次最大加仓金额（元，可留空）", existing?.max_add_amount != null ? String(existing.max_add_amount) : "");
    if (addText == null) return false;
    const positionText = window.prompt("单标的最大总仓位金额（元，可留空）", existing?.max_position_amount != null ? String(existing.max_position_amount) : "");
    if (positionText == null) return false;
    const sellableText = window.prompt("当前可卖数量（股，可留空；卖出草稿必填）", existing?.sellable_quantity != null ? String(existing.sellable_quantity) : String(holding?.quantity ?? ""));
    if (sellableText == null) return false;
    const reduceText = window.prompt("结构失效时计划减仓比例（例如 0.3 表示30%，可留空）", existing?.default_reduce_fraction != null ? String(existing.default_reduce_fraction) : "");
    if (reduceText == null) return false;
    const holdingPeriodText = window.prompt("持有周期（short_term / swing / long_term）", existing?.holding_period || "");
    if (holdingPeriodText == null) return false;
    const holdingPeriod = holdingPeriodText.trim();
    if (!["short_term", "swing", "long_term"].includes(holdingPeriod)) {
      toast.error("持有周期必须是 short_term、swing 或 long_term。");
      return false;
    }
    const minimumRewardRiskText = window.prompt("最低收益风险比（例如 2）", existing?.minimum_reward_risk != null ? String(existing.minimum_reward_risk) : "");
    if (minimumRewardRiskText == null) return false;
    const confirmationText = window.prompt("确认周期，用逗号分隔（例如 5m,1d）", (existing?.confirmation_intervals || []).join(","));
    if (confirmationText == null) return false;
    const optionalNumber = (value: string) => value.trim() ? Number(value) : null;
    const maxAddAmount = optionalNumber(addText);
    const maxPositionAmount = optionalNumber(positionText);
    const sellableQuantity = optionalNumber(sellableText);
    const defaultReduceFraction = optionalNumber(reduceText);
    const minimumRewardRisk = optionalNumber(minimumRewardRiskText);
    const confirmationIntervals = confirmationText.split(",").map((value) => value.trim()).filter(Boolean);
    if ([maxAddAmount, maxPositionAmount, sellableQuantity, defaultReduceFraction, minimumRewardRisk].some((value) => value != null && !Number.isFinite(value))) {
      toast.error("风险设置包含无效数字。");
      return false;
    }
    if (minimumRewardRisk == null || confirmationIntervals.length === 0) {
      toast.error("请明确最低收益风险比和至少一个确认周期。");
      return false;
    }
    if (!confirmationIntervals.every((value): value is "5m" | "30m" | "1d" => ["5m", "30m", "1d"].includes(value))) {
      toast.error("确认周期只支持 5m、30m 和 1d。");
      return false;
    }
    await api.setPortfolioMonitoringRiskPreference(card.symbol, {
      holding_period: holdingPeriod as "short_term" | "swing" | "long_term",
      max_risk_amount: maxRiskAmount,
      max_add_amount: maxAddAmount,
      max_position_amount: maxPositionAmount,
      minimum_reward_risk: minimumRewardRisk,
      confirmation_intervals: confirmationIntervals,
      draft_valid_minutes: existing?.draft_valid_minutes || 30,
      condition_order_permission: "local_draft",
      sellable_quantity: sellableQuantity,
      intraday_added_quantity: existing?.intraday_added_quantity ?? 0,
      default_reduce_fraction: defaultReduceFraction,
    });
    toast.success("风险设置已保存；旧草稿已自动标记失效。");
    return true;
  };

  const chooseMonitoringDecision = async (
    card: MonitorTargetMonitoringCard,
    choice: MonitorDecisionChoice,
  ) => {
    setBusy(`decision:${card.decision_id}`);
    try {
      const idempotencyKey = typeof crypto !== "undefined" && "randomUUID" in crypto
        ? crypto.randomUUID()
        : `${card.decision_id}-${choice.choice_id}-${Date.now()}`;
      await api.choosePortfolioMonitoringDecision(card.decision_id, choice.choice_id, {
        decision_id: card.decision_id,
        choice_id: choice.choice_id,
        decision_revision: card.decision_revision,
        evidence_fingerprint: card.evidence_fingerprint,
        idempotency_key: idempotencyKey,
      });
      if (choice.choice_id === "adjust_risk_preferences") {
        const configured = await configureDecisionRiskPreference(card);
        if (!configured) return;
        await load();
        return;
      }
      if (choice.eligible_draft_type) {
        const draft = await api.createPortfolioMonitoringConditionDraft(card.decision_id, {
          choice_id: choice.choice_id,
          decision_revision: card.decision_revision,
          evidence_fingerprint: card.evidence_fingerprint,
        });
        toast.success(
          draft.status === "draft"
            ? "本地条件单草稿已生成；真实订单提交仍被禁止。"
            : "已保存方向预案，但风险设置或约束不足，未生成数量。",
        );
      } else {
        toast.success("选择已记录；相同证据不会反复要求确认。");
      }
      await load();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "记录决策失败");
    } finally {
      setBusy(null);
    }
  };

  const validateConditionDraft = async (draftId: string) => {
    try {
      await api.validatePortfolioMonitoringConditionDraft(draftId);
      toast.success("草稿已通过当前证据和约束校验；仍不会提交真实订单。");
      await load();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "草稿校验失败");
    }
  };

  const cancelConditionDraft = async (draftId: string) => {
    try {
      await api.cancelPortfolioMonitoringConditionDraft(draftId);
      toast.success("本地草稿已撤销，历史记录仍保留。");
      await load();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "撤销草稿失败");
    }
  };

  const openPlan = async (profile: MonitorProfile) => {
    const requestId = openPlanRequestRef.current + 1;
    openPlanRequestRef.current = requestId;
    setDrawerProfile(profile);
    setDrawerPlanVersion(null);
    setDraftPlan(null);
    setDraftNumericPlan(null);
    setDraftBaselineSignature(null);
    setPlanFormErrors([]);
    setOpeningProfileId(profile.profile_id);
    try {
      const detail = await api.getPortfolioMonitor(profile.profile_id);
      if (!mountedRef.current || requestId !== openPlanRequestRef.current) return;
      showPlan(detail);
    } catch (error) {
      if (!mountedRef.current || requestId !== openPlanRequestRef.current) return;
      closePlanDrawer();
      toast.error(error instanceof Error ? error.message : "加载监控计划失败");
    } finally {
      if (mountedRef.current && requestId === openPlanRequestRef.current) {
        setOpeningProfileId(null);
      }
    }
  };

  const createBindingCode = async () => {
    setBusy("create-binding");
    try {
      setBindingAttempt(await api.createPortfolioMonitorDeliveryBindingCode());
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "生成飞书绑定验证码失败");
    } finally {
      setBusy(null);
    }
  };

  const checkBindingStatus = async () => {
    if (!bindingAttempt) return;
    setBusy("check-binding");
    try {
      const result = await api.getPortfolioMonitorDeliveryBindingCode(bindingAttempt.binding_id);
      setBindingAttempt((current) => ({
        ...current,
        ...result,
        code: result.code || current?.code,
        command: result.command || current?.command,
      }));
      if (result.status === "claimed" && result.target) {
        setSelectedTarget(result.target.target_id);
        toast.success("飞书提醒目标绑定成功。");
        await load();
      } else if (result.status === "expired") {
        toast.error("验证码已过期，请重新生成。");
      } else {
        toast.info("尚未收到验证码，请在飞书发送后再检查。");
      }
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "检查飞书绑定状态失败");
    } finally {
      setBusy(null);
    }
  };

  const copyBindingCommand = async () => {
    if (!bindingAttempt?.command) return;
    try {
      await navigator.clipboard.writeText(bindingAttempt.command);
      toast.success("绑定口令已复制。");
    } catch {
      toast.error("无法自动复制，请手动复制验证码。");
    }
  };

  const createDrafts = async () => {
    if (!selectedManualSymbols.length) return;
    setBusy("create");
    try {
      let job = await api.createPortfolioMonitorPlannerJob({
        symbols: selectedManualSymbols,
        report_refs: selectedReportRefs,
        research_policy: "if_needed",
        delivery_target_id: selectedTarget || undefined,
        force_fresh: true,
      });
      setPlannerJob(job);
      while (!["ready", "blocked", "failed", "cancelled"].includes(job.status)) {
        await new Promise((resolve) => window.setTimeout(resolve, 800));
        job = await api.getPortfolioMonitorPlannerJob(job.job_id);
        setPlannerJob(job);
      }
      const batch = { items: job.items };
      const blocked = batch.items.filter((item) => item.status !== "ready");
      const singleSourceBlocked = blocked.some((item) => item.blocked_reasons.includes("quote_not_actionable:single_source"));
      toast[blocked.length ? "warning" : "success"](
        blocked.length
          ? `已自动刷新数据源；${batch.items.length - blocked.length} 只草案就绪，${blocked.length} 只数据仍不足${singleSourceBlocked ? "，可逐只同意使用单源模式" : ""}。`
          : "监控草案已生成，请逐只审核后启用。",
      );
      await load();
      const firstReady = batch.items.find((item) => item.status === "ready" && item.profile_id);
      const firstVisible = firstReady || batch.items.find((item) => item.profile_id);
      if (firstVisible?.profile_id) {
        const detail = await api.getPortfolioMonitor(firstVisible.profile_id);
        showPlan(detail, firstVisible.plan_version);
      }
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "生成监控草案失败");
    } finally {
      setBusy(null);
    }
  };

  const actOnProfile = async (profile: MonitorProfile, action: "pause" | "resume" | "close" | "reanalyze") => {
    const displayName = monitorSymbolDisplayName(profile.symbol, holdingNameByIdentifier);
    if (action === "close" && !window.confirm(`关闭 ${displayName} 的监控？历史事件会保留。`)) return;
    setBusy(`${action}:${profile.profile_id}`);
    try {
      if (action === "reanalyze") {
        const batch = await api.reanalyzePortfolioMonitor(profile.profile_id);
        const item = batch.items.find((candidate) => candidate.profile_id === profile.profile_id)
          || batch.items[0];
        await load();
        if (item?.status === "ready" && item.profile_id) {
          const detail = await api.getPortfolioMonitor(item.profile_id);
          showPlan(detail, item.plan_version);
          toast.success("AI 已重新分析并生成待审核草案；可在计划与审核中手动修改，旧计划继续运行到你确认新版本。 ");
        } else if (item?.status === "blocked") {
          if (item.profile_id) showPlan(await api.getPortfolioMonitor(item.profile_id));
          toast.warning(`已刷新数据源，但当前数据仍不足：${item.blocked_reasons.map(blockedReasonLabel).join("；") || "暂无可用数据"}`);
        } else {
          toast.error(item?.error || "重新分析失败");
        }
        return;
      }
      if (action === "pause") await api.pausePortfolioMonitor(profile.profile_id);
      if (action === "resume") await api.resumePortfolioMonitor(profile.profile_id);
      if (action === "close") {
        const closedProfile = await api.closePortfolioMonitor(profile.profile_id);
        setProfiles((current) => current.map((candidate) => (
          candidate.profile_id === closedProfile.profile_id ? { ...candidate, ...closedProfile } : candidate
        )));
        setShowClosedProfiles(false);
        if (drawerProfile?.profile_id === profile.profile_id) closePlanDrawer(true);
        toast.success(`${displayName} 已关闭监控；卡片已移除，历史记录继续保留。`);
      } else {
        toast.success("监控状态已更新。");
      }
      await load();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "监控操作失败");
    } finally {
      setBusy(null);
    }
  };

  const reopenProfile = async (profile: MonitorProfile, askConfirmation = true) => {
    const targetId = selectedTarget || profile.delivery_target_id || "";
    if (!targetId) {
      toast.error("请先选择飞书提醒目标。");
      return;
    }
    const displayName = monitorSymbolDisplayName(profile.symbol, holdingNameByIdentifier);
    if (askConfirmation && !window.confirm(`重新检测 ${displayName} 的当前数据并生成新草案？系统会先自动刷新数据源，也不会直接启用监控。`)) return;
    setBusy(`reopen:${profile.profile_id}`);
    try {
      const batch = await api.reopenPortfolioMonitor(profile.profile_id, targetId);
      const item = batch.items.find((candidate) => candidate.profile_id === profile.profile_id)
        || batch.items[0];
      await load();
      if (item?.status === "ready" && item.profile_id) {
        const detail = await api.getPortfolioMonitor(item.profile_id);
        showPlan(detail, item.plan_version);
        toast.success("当前数据校验通过，新草案已生成；请审核后再启用。");
        return;
      }
      if (item?.status === "blocked") {
        if (item.profile_id) showPlan(await api.getPortfolioMonitor(item.profile_id));
        const reasons = item.blocked_reasons.length ? `：${item.blocked_reasons.map(blockedReasonLabel).join("；")}` : "";
        toast.warning(`已刷新数据源，但当前数据仍不足${reasons}`);
        return;
      }
      toast.error(item?.error || "重新检测失败，请稍后再试。");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "重新打开监控失败");
    } finally {
      setBusy(null);
    }
  };

  const useSingleSource = async (profile: MonitorProfile) => {
    const targetId = selectedTarget || profile.delivery_target_id || "";
    if (profile.status === "closed" && !targetId) {
      toast.error("请先选择飞书提醒目标。");
      return;
    }
    const displayName = monitorSymbolDisplayName(profile.symbol, holdingNameByIdentifier);
    if (!window.confirm(`确认让 ${displayName} 进入单源模式？单一来源可能不准确，系统会持续显示警告，并在双源恢复后优先使用已校验数据。`)) return;
    setBusy(`single-source:${profile.profile_id}`);
    try {
      const batch = profile.status === "closed"
        ? await api.reopenPortfolioMonitor(profile.profile_id, targetId, true)
        : await api.reanalyzePortfolioMonitor(profile.profile_id, true);
      const item = batch.items.find((candidate) => candidate.profile_id === profile.profile_id)
        || batch.items[0];
      await load();
      if (item?.status === "ready" && item.profile_id) {
        const detail = await api.getPortfolioMonitor(item.profile_id);
        showPlan(detail, item.plan_version);
        toast.warning("单源监控草案已生成。启用后会持续标注“此数据为单源，可能不准确”。");
        return;
      }
      toast.error(item?.error || `单源模式仍无法取得可用数据：${item?.blocked_reasons.map(blockedReasonLabel).join("；") || "数据不足"}`);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "进入单源模式失败");
    } finally {
      setBusy(null);
    }
  };

  const savePlan = async () => {
    const version = drawerProfile
      ? selectPlanVersion(drawerProfile, drawerPlanVersion)
      : undefined;
    if (!drawerProfile || !version || !draftPlan || !draftNumericPlan) return;
    const validation = materializeAndValidateMonitorPlan(draftPlan, draftNumericPlan);
    if (!validation.plan) {
      setPlanFormErrors(validation.errors);
      toast.error("计划设置未通过校验，请先修正后再保存。");
      return;
    }
    setBusy("save-plan");
    try {
      await api.updatePortfolioMonitorPlan(
        drawerProfile.profile_id,
        version.version,
        validation.plan,
        drawerProfile.profile_revision,
      );
      const detail = await api.getPortfolioMonitor(drawerProfile.profile_id);
      showPlan(detail, version.version);
      toast.success("监控计划已保存。 ");
      await load();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "保存监控计划失败");
    } finally {
      setBusy(null);
    }
  };

  const saveAndActivatePlan = async () => {
    const version = drawerProfile
      ? selectPlanVersion(drawerProfile, drawerPlanVersion)
      : undefined;
    if (!drawerProfile || !version || !draftPlan || !draftNumericPlan) return;
    if (drawerProfile.status === "closed") {
      await reopenProfile(drawerProfile, false);
      return;
    }
    const validation = materializeAndValidateMonitorPlan(draftPlan, draftNumericPlan);
    if (!validation.plan) {
      setPlanFormErrors(validation.errors);
      toast.error("计划设置未通过校验，未启用旧值。");
      return;
    }
    const singleSourceNotice = validation.plan.data_mode === "single_source"
      ? " 当前为单源数据，可能不准确。"
      : "";
    const displayName = monitorSymbolDisplayName(drawerProfile.symbol, holdingNameByIdentifier);
    if (!window.confirm(`保存当前修改并启用 ${displayName} 的监控？${singleSourceNotice}系统只发送提醒，不会执行交易。`)) return;
    setBusy("save-and-activate-plan");
    try {
      await api.saveAndActivatePortfolioMonitorPlan(
        drawerProfile.profile_id,
        version.version,
        validation.plan,
        drawerProfile.profile_revision,
      );
      toast.success(`${displayName} 已保存当前计划并开始常态监控。`);
      closePlanDrawer(true);
      await load();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "启用监控失败");
    } finally {
      setBusy(null);
    }
  };

  const acknowledge = async (event: MonitorEvent) => {
    try {
      await api.acknowledgePortfolioMonitorEvent(event.event_id);
      setEvents((current) => current.some((item) => item.event_id === event.event_id)
        ? current.map((item) => item.event_id === event.event_id ? { ...item, status: "resolved" } : item)
        : [{ ...event, status: "resolved" }, ...current]);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "确认事件失败");
    }
  };

  return (
    <section id="ai-monitor" className="overflow-hidden rounded-md border" aria-label="AI 持仓监控中心">
      <div className="flex flex-col gap-4 bg-muted/20 p-4 lg:flex-row lg:items-center lg:justify-between">
        <div className="flex items-start gap-3">
          <div className="rounded-md border bg-background p-2"><BellRing className="h-5 w-5" /></div>
          <div>
            <div className="flex flex-wrap items-center gap-2">
              <h2 className="font-semibold">AI 持仓监控中心</h2>
              <span className={cn("rounded border px-2 py-0.5 text-[11px]", runtimeDisplayTone)}>
                <span className="inline-flex items-center gap-1.5">
                  {!monitorDisconnected && status?.runtime.current_tick_started_at ? <Loader2 aria-label="监控检查进行中" className="h-3 w-3 animate-spin" /> : null}
                  {monitorDisconnected ? "页面与服务断联" : runtimeLabel}
                </span>
              </span>
            </div>
            <p className="mt-1 text-xs text-muted-foreground">对指定持仓生成可审核规则，按合适频次读取已校核行情；命中后通过飞书提醒，不执行交易。</p>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <MonitorUsagePanel
            running={Boolean(
              status?.runtime.running
              || (plannerJob && !["ready", "blocked", "failed", "cancelled"].includes(plannerJob.status))
            )}
            requestedJobId={usageJobId}
            onRequestedJobHandled={clearUsageJobRequest}
          />
          <MonitorSoundControls />
          <span className="text-xs text-muted-foreground">
            已设 {selectedSymbols.size} 只 · 普通 {selectedManualSymbols.length} · AI 自主 {selectedAutonomousSymbols.length}
            {selectedNames ? `：${selectedNames}` : ""}
          </span>
          <button
            type="button"
            disabled={busy === "runtime-control" || monitorDisconnected}
            onClick={() => void toggleRuntime()}
            title={monitorDisconnected ? "页面重新连接后才能切换监控服务" : undefined}
            className={cn(
              "inline-flex items-center gap-2 rounded-md px-3 py-2 text-sm font-medium disabled:opacity-50",
              status?.enabled_by_config && status.runtime.running
                ? "border text-foreground hover:bg-muted"
                : "bg-emerald-600 text-white hover:bg-emerald-700",
            )}
          >
            {busy === "runtime-control" ? <Loader2 className="h-4 w-4 animate-spin" /> : <Activity className="h-4 w-4" />}
            {status?.enabled_by_config && status.runtime.running
              ? "停止监控服务"
              : status?.enabled_by_config ? "启动监控服务" : "启动影子监控"}
          </button>
          <button
            type="button"
            disabled={!selectedManualSymbols.length || busy === "create"}
            onClick={() => void createDrafts()}
            className="inline-flex items-center gap-2 rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground disabled:opacity-50"
          >
            {busy === "create" ? <Loader2 className="h-4 w-4 animate-spin" /> : <BrainCircuit className="h-4 w-4" />}
            {busy === "create"
              ? "正在检查并刷新数据源…"
              : autopilotOperational ? "手动生成覆盖草案" : "生成监控草案"}
          </button>
          <button type="button" onClick={() => setExpanded((value) => !value)} className="rounded-md border px-3 py-2 text-sm hover:bg-muted">
            {expanded ? "收起" : "展开"}
          </button>
        </div>
      </div>

      {monitorDisconnected ? (
        <div role="alert" className="flex items-start gap-2 border-t border-red-500/30 bg-red-500/5 px-4 py-3 text-xs text-red-700 dark:text-red-300">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
          <span>
            页面与监控服务断联，已停止倒计时和运行中动效；最近成功同步：{lastSuccessfulSyncAt ? formatDate(new Date(lastSuccessfulSyncAt).toISOString()) : "尚无记录"}。系统会自动重试连接。
          </span>
        </div>
      ) : connectionRecentlyRecovered ? (
        <div role="status" className="flex items-start gap-2 border-t border-emerald-500/30 bg-emerald-500/5 px-4 py-3 text-xs text-emerald-700 dark:text-emerald-300">
          <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0" />
          已重新连接监控服务 · {lastSuccessfulSyncAt ? formatDate(new Date(lastSuccessfulSyncAt).toISOString()) : "刚刚"}
        </div>
      ) : null}

      {!status?.enabled_by_config ? (
        <div className="flex items-start gap-2 border-t border-amber-500/30 bg-amber-500/5 px-4 py-3 text-xs text-amber-800 dark:text-amber-200">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
          监控服务当前没有启动：计划会被保存，但系统不会自动检查行情，也不会发送飞书提醒。点击右上角“启动影子监控”即可开启，不需要修改配置文件。
        </div>
      ) : null}

      {status?.enabled_by_config && effectiveMode === "shadow" ? (
        <div className="flex items-start gap-2 border-t border-blue-500/30 bg-blue-500/5 px-4 py-3 text-xs text-blue-800 dark:text-blue-200">
          <Eye className="mt-0.5 h-4 w-4 shrink-0" />
          当前为影子模式：沪深京标的会真实取数、评估并记录“本应发送”的事件，但飞书外发被强制抑制；其他市场的计划会保留并等待对应交易日历接入。
        </div>
      ) : null}

      {status?.runtime.mode_valid === false ? (
        <div className="flex items-start gap-2 border-t border-amber-500/30 bg-amber-500/5 px-4 py-3 text-xs text-amber-800 dark:text-amber-200">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
          运行模式配置无效，系统已自动降级为 shadow；请检查 VIBE_TRADING_MONITORING_MODE。
        </div>
      ) : null}

      {expanded ? (
        <div className="grid min-w-0 grid-cols-[minmax(0,1fr)] gap-5 border-t p-4">
          <div className="grid gap-4 rounded-lg border border-emerald-500/30 bg-emerald-500/[0.04] p-4">
            <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
              <div>
                <div className="flex flex-wrap items-center gap-2">
                  <h3 className="text-sm font-semibold">自主监控</h3>
                  <span className={cn(
                    "rounded-full border px-2 py-0.5 text-[11px]",
                    autopilotOperational
                      ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300"
                      : "border-muted text-muted-foreground",
                  )}>
                    {autopilotOperational
                      ? "Agent 正在自动维护"
                      : autopilot?.enabled ? "未选择标的 · 当前不运行" : "尚未开启"}
                  </span>
                  <span className="rounded-full border px-2 py-0.5 text-[11px] text-muted-foreground">
                    只建议 · 不下单
                  </span>
                </div>
                <p className="mt-1 max-w-3xl text-xs leading-5 text-muted-foreground">
                  自主监控只跟随持仓矩阵中紫色的「AI 自主」等级。开启后，Agent 会自动判断周报条件和版本差异；通过闭合日线、量能、来源及有效期门禁后自动换版并进入 shadow 监控。
                </p>
                <p className="mt-1 max-w-3xl text-xs leading-5 text-muted-foreground">
                  与下方共用同一套报告、计划和监控运行版：这里只扩大提醒判断权，不接入交易；买入、减仓和退出仍全部由你决定。
                </p>
              </div>
              <button
                type="button"
                role="switch"
                aria-checked={autopilotOperational}
                disabled={busy === "autopilot-control" || autopilotSelectionSyncState === "syncing" || monitorDisconnected || (!autopilotOperational && !selectedAutopilotSymbols.length)}
                onClick={() => void toggleAutopilot()}
                className={cn(
                  "inline-flex min-w-32 items-center justify-center gap-2 rounded-md px-3 py-2 text-sm font-medium disabled:opacity-50",
                  autopilotOperational
                    ? "border bg-background text-foreground hover:bg-muted"
                    : "bg-emerald-600 text-white hover:bg-emerald-700",
                )}
              >
                {busy === "autopilot-control" ? <Loader2 className="h-4 w-4 animate-spin" /> : <Zap className="h-4 w-4" />}
                {autopilotOperational ? "关闭自主监控" : "开启自主监控"}
              </button>
            </div>

            <div className="grid gap-3 rounded-md border bg-background/70 p-3" aria-label="自主监控标的范围">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div>
                  <div className="text-xs font-medium">跟随持仓矩阵的 AI 自主等级</div>
                  <p className="mt-1 text-[11px] text-muted-foreground">
                    当前已设普通监控 {selectedManualSymbols.length} 只、AI 自主 {selectedAutonomousSymbols.length} 只 / 共 {holdings.length} 只；自主服务可纳入 {selectedAutopilotSymbols.length} 只
                    {selectedAutopilotNames ? `：${selectedAutopilotNames}` : "。请先在持仓矩阵勾选需要监控的标的。"}
                  </p>
                </div>
                <div className="flex flex-wrap items-center justify-end gap-2">
                  <a
                    href="#portfolio-holdings"
                    className="inline-flex items-center gap-1 rounded-md border px-2.5 py-1 text-[11px] font-medium text-foreground hover:bg-muted"
                  >
                    去持仓矩阵选择
                    <ArrowRight aria-hidden="true" className="h-3 w-3" />
                  </a>
                  <span className={cn(
                    "inline-flex items-center gap-1.5 rounded-full border px-2 py-1 text-[11px]",
                    autopilotSelectionSyncState === "error"
                      ? "border-red-500/40 text-red-700 dark:text-red-300"
                      : autopilotSelectionSyncState === "syncing"
                        ? "border-blue-500/40 text-blue-700 dark:text-blue-300"
                        : "border-muted text-muted-foreground",
                  )}>
                    {autopilotSelectionSyncState === "syncing" ? <Loader2 className="h-3 w-3 animate-spin" /> : null}
                    {autopilotSelectionSyncState === "syncing"
                      ? "正在同步范围"
                      : autopilotSelectionSyncState === "error"
                        ? "范围同步失败"
                        : autopilot?.enabled
                          ? `已同步 ${configuredAutopilotSymbols.length} 只`
                          : "自主监控未开启"}
                  </span>
                </div>
              </div>
              <p className="text-[11px] leading-5 text-muted-foreground">
                在本页下方「持仓矩阵」第一列连续点击同一个雷达：灰色未监控 → 蓝色普通监控 → 紫色 AI 自主。停止操作 5 秒后才应用最终等级。
              </p>
              {autopilotSelectionSyncState === "error" ? (
                <div className="flex flex-wrap items-center justify-between gap-2 rounded border border-red-500/30 bg-red-500/5 px-2.5 py-2 text-[11px] text-red-700 dark:text-red-300" role="alert">
                  <span>持仓矩阵的最新选择尚未写入自主监控，Agent 会继续使用上一次成功保存的范围。</span>
                  <button
                    type="button"
                    onClick={() => {
                      failedAutopilotSelectionSyncRef.current = null;
                      setAutopilotSelectionSyncCycle((value) => value + 1);
                    }}
                    className="rounded border border-current px-2 py-1 hover:bg-red-500/10"
                  >
                    重试同步
                  </button>
                </div>
              ) : null}
              {unsupportedAutopilotSymbols.length ? (
                <div
                  aria-label="自主监控暂不支持的标的"
                  className="rounded border border-amber-500/30 bg-amber-500/5 px-2.5 py-2 text-[11px] leading-5 text-amber-800 dark:text-amber-200"
                >
                  暂不支持、未纳入自主监控：{unsupportedAutopilotSymbols
                    .map((symbol) => monitorSymbolDisplayName(symbol, holdingNameByIdentifier))
                    .join("、")}。这些标的仍保留在持仓矩阵选择中，也可继续使用下方手工报告入口。
                </div>
              ) : null}
            </div>

            <div className="grid gap-3 lg:grid-cols-2">
              <div className="rounded-md border bg-background/70 p-3">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <span className="text-xs font-medium">最近自动运行</span>
                  <div className="flex flex-wrap items-center justify-end gap-2 text-[11px]">
                    <span className="text-muted-foreground">
                      范围内记录 {visibleAutopilotRuns.length} 条 · 已显示 {displayedAutopilotRuns.length} 条
                    </span>
                    {visibleAutopilotRuns.length > AUTOPILOT_RUN_PREVIEW_LIMIT ? (
                      <button
                        type="button"
                        aria-expanded={showAllAutopilotRuns}
                        onClick={() => setShowAllAutopilotRuns((current) => !current)}
                        className="rounded border px-2 py-1 font-medium text-foreground hover:bg-muted"
                      >
                        {showAllAutopilotRuns
                          ? `收起，仅看最新 ${AUTOPILOT_RUN_PREVIEW_LIMIT} 条`
                          : `查看全部 ${visibleAutopilotRuns.length} 条`}
                      </button>
                    ) : null}
                  </div>
                </div>
                <div className="mt-2 grid gap-1.5" aria-label="最近自动运行列表">
                  {displayedAutopilotRuns.map((run) => {
                    const display = monitorAutopilotRunDisplay(run, holdingNameByIdentifier);
                    const detail = monitorAutopilotRunDetail(run);
                    return (
                      <div key={run.trigger_id} className="grid gap-1 rounded bg-muted/40 px-2.5 py-2 text-xs">
                        <div className="flex items-center justify-between gap-3">
                          <span aria-label={`${display.name ? `${display.name}（${display.code}）` : display.code} · ${run.trigger_type}`}>
                            <strong>{display.name || display.code}</strong>
                            {display.name ? <span className="font-mono">（{display.code}）</span> : null}
                            {" · "}{AUTOPILOT_TRIGGER_LABELS[run.trigger_type] || run.trigger_type}
                          </span>
                          <span className="inline-flex items-center gap-2 text-muted-foreground">
                            {AUTOPILOT_RUN_STATUS_LABELS[run.status] || run.status} · {formatDate(run.created_at)}
                            {run.planner_job_id ? (
                              <button
                                type="button"
                                onClick={() => setUsageJobId(run.planner_job_id || null)}
                                className="rounded border px-2 py-1 text-[10px] text-foreground hover:bg-muted"
                              >
                                用量
                              </button>
                            ) : null}
                          </span>
                        </div>
                        {detail ? <p className="text-[11px] leading-4 text-amber-700 dark:text-amber-300">{detail}</p> : null}
                      </div>
                    );
                  })}
                  {!visibleAutopilotRuns.length ? (
                    <div className="rounded border border-dashed p-3 text-center text-xs text-muted-foreground">
                      开启后会自动发现持仓和报告变化。
                    </div>
                  ) : null}
                </div>
              </div>

              <div className="rounded-md border bg-background/70 p-3">
                <div className="flex items-center justify-between gap-2">
                  <span className="text-xs font-medium">最新人工决策建议</span>
                  <span className="text-[11px] text-muted-foreground">不会修改持仓</span>
                </div>
                <div className="mt-2 grid gap-2">
                  {recommendations.slice(0, 3).map((recommendation) => {
                    const displayName = monitorSymbolDisplayName(recommendation.symbol, holdingNameByIdentifier);
                    return (
                      <div key={recommendation.recommendation_id} className="grid gap-2 rounded border bg-background p-2.5 text-xs">
                        <div className="flex flex-wrap items-center justify-between gap-2">
                          <span>
                            <strong>{displayName}</strong>
                            {" · "}{recommendation.status === "ready"
                              ? recommendation.action
                              : recommendation.status === "evidence_pending"
                                ? "证据待补"
                                : recommendation.status === "cancelled"
                                  ? "已取消"
                                  : recommendation.status === "expired" ? "已过期" : recommendation.status}
                            {recommendation.constrained_quantity != null ? ` ${recommendation.constrained_quantity} 份` : ""}
                          </span>
                          <span className="text-muted-foreground">有效至 {formatDate(recommendation.valid_until)}</span>
                        </div>
                        {recommendation.feedback_status === "pending"
                          && !["cancelled", "expired"].includes(recommendation.status) ? (
                          <div className="flex flex-wrap gap-1.5">
                            <button type="button" onClick={() => void acknowledgeRecommendation(recommendation, "handled")} className="rounded border px-2 py-1 hover:bg-muted">我已处理</button>
                            <button type="button" onClick={() => void acknowledgeRecommendation(recommendation, "continue_observing")} className="rounded border px-2 py-1 hover:bg-muted">继续观察</button>
                            <button type="button" onClick={() => void acknowledgeRecommendation(recommendation, "ignored")} className="rounded border px-2 py-1 text-muted-foreground hover:bg-muted">忽略本次</button>
                          </div>
                        ) : recommendation.status === "cancelled" ? (
                          <span className="text-muted-foreground">该标的已移出自主监控，本次建议已取消。</span>
                        ) : recommendation.status === "expired" ? (
                          <span className="text-muted-foreground">本次建议已过期。</span>
                        ) : (
                          <span className="text-muted-foreground">反馈：{recommendation.feedback_status}</span>
                        )}
                      </div>
                    );
                  })}
                  {!recommendations.length ? (
                    <div className="rounded border border-dashed p-3 text-center text-xs text-muted-foreground">
                      情景确认后，动作、数量、有效期和失效条件会显示在这里。
                    </div>
                  ) : null}
                </div>
              </div>
            </div>
          </div>

          {selectedManualSymbols.length ? (
            <div className="grid gap-3 rounded-md border border-cyan-500/30 bg-cyan-500/[0.03] p-3">
              <div>
                <div className="text-sm font-semibold">普通监控设置</div>
                <p className="mt-1 text-xs text-muted-foreground">
                  蓝色普通监控标的在这里选择报告并生成草案；人工确认启用后持续监控，AI 不会自动改写其运行版。
                </p>
              </div>
              <div className="grid gap-2 lg:grid-cols-2">
                {selectedManualSymbols.map((symbol) => {
                  const candidates = reportCandidates[symbol] || [];
                  return (
                    <label key={symbol} className="grid gap-1 text-xs text-muted-foreground">
                      <span className="font-medium text-foreground">{monitorSymbolDisplayName(symbol, holdingNameByIdentifier)}</span>
                      <select
                        value={selectedReportRefs[symbol] || ""}
                        onChange={(event) => setSelectedReportRefs((current) => ({
                          ...current,
                          [symbol]: event.target.value,
                        }))}
                        className="rounded-md border bg-background px-3 py-2 text-sm text-foreground"
                      >
                        <option value="">未找到有效报告 · 自动发起监控深研</option>
                        {candidates.map((candidate) => (
                          <option key={candidate.report_ref} value={candidate.report_ref}>
                            {candidate.title} · {candidate.data_as_of.slice(0, 10)}
                            {candidate.stale ? " · 已过期" : ""}
                            {candidate.quality_status !== "ready" ? " · 数据受限" : ""}
                          </option>
                        ))}
                      </select>
                    </label>
                  );
                })}
              </div>
            </div>
          ) : null}

          {plannerJob ? (
            <div className="grid gap-2 rounded-md border p-3" aria-live="polite">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="text-sm font-medium">监控深研与规划 · {plannerJob.status}</div>
                <div className="flex items-center gap-2">
                  <button
                    type="button"
                    onClick={() => setUsageJobId(plannerJob.job_id)}
                    className="rounded-md border px-2.5 py-1.5 text-xs hover:bg-muted"
                  >
                    查看用量
                  </button>
                  {!["ready", "blocked", "failed", "cancelled"].includes(plannerJob.status) ? (
                    <button
                      type="button"
                      onClick={() => void api.cancelPortfolioMonitorPlannerJob(plannerJob.job_id).then(setPlannerJob)}
                      className="rounded-md border px-2.5 py-1.5 text-xs hover:bg-muted"
                    >
                      取消任务
                    </button>
                  ) : null}
                </div>
              </div>
              <div className="grid gap-1 text-xs text-muted-foreground">
                {plannerJob.items.map((item) => (
                  <div key={item.symbol} className="flex flex-wrap items-center justify-between gap-2 rounded bg-muted/40 px-2 py-1.5">
                    <span className="font-medium text-foreground">{monitorSymbolDisplayName(item.symbol, holdingNameByIdentifier)}</span>
                    <span>{item.status}{item.error ? ` · ${item.error}` : ""}</span>
                  </div>
                ))}
              </div>
            </div>
          ) : null}
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4" role="region" aria-label="监控运行健康">
            <div className="rounded-md border p-3">
              <div className="flex items-center gap-2 text-xs text-muted-foreground"><Activity className="h-3.5 w-3.5" />交易时段调度</div>
              <div className="mt-2 text-sm font-medium">P95 {formatMilliseconds(status?.runtime_health?.schedule_lag_ms?.p95)}</div>
              <p className="mt-1 text-xs text-muted-foreground">24 小时 {status?.runtime_health?.tick_count || 0} 轮 · 错误 {status?.runtime_health?.error_tick_count || 0}</p>
              <p className="mt-1 text-[11px] text-muted-foreground">
                休市积压 {status?.runtime_health?.closed_session_backlog?.due_profile_ticks || 0} profile-ticks
                {status?.runtime_health?.closed_session_backlog?.lag_ms?.p95 != null
                  ? ` · P95 ${formatMilliseconds(status.runtime_health.closed_session_backlog.lag_ms.p95)}`
                  : ""}
              </p>
            </div>
            <div className="rounded-md border p-3">
              <div className="flex items-center gap-2 text-xs text-muted-foreground"><ShieldCheck className="h-3.5 w-3.5" />交易日历</div>
              <div className="mt-2 text-sm font-medium">{SESSION_LABELS[status?.runtime.calendar?.session || "unknown"] || status?.runtime.calendar?.session || "未知"}</div>
              <p className="mt-1 truncate text-xs text-muted-foreground" title={status?.runtime.calendar?.mode}>{status?.runtime.calendar?.mode || "尚未校核"}</p>
            </div>
            <div className="rounded-md border p-3">
              <div className="flex items-center gap-2 text-xs text-muted-foreground"><Eye className="h-3.5 w-3.5" />影子命中</div>
              <div className="mt-2 text-sm font-medium">{status?.shadow_suppressed_deliveries || 0} 条</div>
              <p className="mt-1 text-xs text-muted-foreground">数据阻断 {status?.blocked_profiles || 0} 个 profile</p>
            </div>
            <div className="rounded-md border p-3">
              <div className="flex items-center gap-2 text-xs text-muted-foreground"><Database className="h-3.5 w-3.5" />监控存储</div>
              <div className="mt-2 text-sm font-medium">{formatBytes(status?.database_size_bytes)}</div>
              <p className="mt-1 text-xs text-muted-foreground">上限 {formatBytes(status?.database_max_bytes)} · 最近备份 {formatDate(status?.maintenance?.finished_at)}</p>
            </div>
          </div>

          {status?.runtime.last_error ? (
            <div className="rounded-md border border-red-500/40 bg-red-500/5 px-3 py-2 text-xs text-red-700 dark:text-red-300">
              最近运行错误：{status.runtime.last_error}
            </div>
          ) : null}

          {(status?.database_utilization || 0) >= 0.8 ? (
            <div className="rounded-md border border-amber-500/40 bg-amber-500/5 px-3 py-2 text-xs text-amber-800 dark:text-amber-200">
              监控数据库已使用配置上限的 {Math.round((status?.database_utilization || 0) * 100)}%，请先执行备份和保留清理，再扩大监控范围。
            </div>
          ) : null}

          {status?.maintenance?.status === "failed" ? (
            <div className="rounded-md border border-red-500/40 bg-red-500/5 px-3 py-2 text-xs text-red-700 dark:text-red-300">
              最近维护失败：{status.maintenance.error || "未返回错误详情"}
            </div>
          ) : null}

          <div className="grid gap-3 rounded-md border p-3">
            <div className="grid gap-3 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-end">
              <div className="grid gap-1 text-xs text-muted-foreground">
                <label htmlFor="portfolio-monitor-feishu-target">飞书提醒目标</label>
                <select
                  id="portfolio-monitor-feishu-target"
                  aria-describedby="portfolio-monitor-feishu-target-help"
                  value={selectedTarget}
                  onChange={(event) => setSelectedTarget(event.target.value)}
                  className="rounded-md border bg-background px-3 py-2 text-sm text-foreground"
                >
                  <option value="">跟随全局设置</option>
                  {targets.filter((target) => target.status === "active").map((target) => (
                    <option key={target.target_id} value={target.target_id}>{target.chat_type === "group" ? "群聊" : "私聊"} · {target.chat_id}</option>
                  ))}
                </select>
                <span id="portfolio-monitor-feishu-target-help" className="text-[11px] text-muted-foreground">选择“跟随全局设置”时，发送时使用设置页的默认目标；已有监控的独立目标不会被覆盖。</span>
              </div>
              <button
                type="button"
                disabled={busy === "create-binding"}
                onClick={() => void createBindingCode()}
                className="inline-flex items-center justify-center gap-2 rounded-md border px-3 py-2 text-sm hover:bg-muted disabled:opacity-50"
              >
                {busy === "create-binding" ? <Loader2 className="h-4 w-4 animate-spin" /> : <KeyRound className="h-4 w-4" />}
                {bindingAttempt ? "重新生成验证码" : "生成飞书绑定验证码"}
              </button>
            </div>

            {bindingAttempt ? (
              <div
                className={cn(
                  "grid gap-3 rounded-md border p-3",
                  bindingAttempt.status === "claimed" && "border-emerald-500/40 bg-emerald-500/5",
                  bindingAttempt.status === "expired" && "border-amber-500/40 bg-amber-500/5",
                )}
                aria-live="polite"
              >
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <div>
                    <div className="text-xs text-muted-foreground">一次性验证码 · 10 分钟内有效</div>
                    <div className="mt-1 font-mono text-xl font-semibold tracking-[0.18em]">
                      {bindingAttempt.code || "已验证"}
                    </div>
                  </div>
                  {bindingAttempt.status === "pending" ? (
                    <button type="button" onClick={() => void copyBindingCommand()} className="inline-flex items-center gap-2 rounded-md border bg-background px-3 py-2 text-xs hover:bg-muted">
                      <Copy className="h-3.5 w-3.5" />复制绑定口令
                    </button>
                  ) : null}
                </div>

                {bindingAttempt.status === "pending" ? (
                  <div className="grid gap-2 text-xs text-muted-foreground">
                    <p><strong className="text-foreground">私聊：</strong>向飞书机器人发送 <code className="rounded bg-muted px-1.5 py-0.5 text-foreground">{bindingAttempt.command}</code></p>
                    <p><strong className="text-foreground">群聊：</strong>发送 <code className="rounded bg-muted px-1.5 py-0.5 text-foreground">@机器人 {bindingAttempt.command}</code>，该群会成为提醒目标。</p>
                    <div>
                      <button type="button" disabled={busy === "check-binding"} onClick={() => void checkBindingStatus()} className="inline-flex items-center gap-2 rounded-md border bg-background px-3 py-2 text-xs hover:bg-muted disabled:opacity-50">
                        {busy === "check-binding" ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <MessageSquare className="h-3.5 w-3.5" />}
                        我已发送，立即检查
                      </button>
                    </div>
                  </div>
                ) : bindingAttempt.status === "claimed" ? (
                  <div className="flex items-center gap-2 text-sm text-emerald-700 dark:text-emerald-300">
                    <CheckCircle2 className="h-4 w-4" />绑定成功，已自动选中{bindingAttempt.target?.chat_type === "group" ? "该群聊" : "该私聊"}。
                  </div>
                ) : (
                  <div className="text-sm text-amber-800 dark:text-amber-200">验证码已过期，请重新生成后发送。</div>
                )}
              </div>
            ) : (
              <p className="text-xs text-muted-foreground">不需要查找 open_id 或 chat_id；在目标群聊或私聊中发送一次性验证码即可完成绑定。</p>
            )}
          </div>

          <div className="grid w-full min-w-0 max-w-full gap-3 overflow-hidden">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="flex items-center gap-2">
                <h3 className="text-sm font-semibold">监控标的</h3>
                <span className="rounded-full border bg-muted/30 px-2 py-0.5 text-[10px] font-medium text-muted-foreground">
                  {visibleCarouselTargets.length}
                </span>
              </div>
              <div className="flex items-center gap-1.5">
                {closedProfileCount ? (
                  <button
                    type="button"
                    aria-pressed={showClosedProfiles}
                    onClick={() => setShowClosedProfiles((value) => !value)}
                    className="mr-1 text-xs text-muted-foreground hover:text-foreground"
                  >
                    {showClosedProfiles ? "隐藏已关闭" : `查看已关闭 ${closedProfileCount}`}
                  </button>
                ) : null}
                {visibleCarouselTargets.length > 1 ? (
                  <>
                    <button
                      type="button"
                      aria-label="上一个监控标的"
                      onClick={() => selectAdjacentProfile(-1)}
                      className="grid h-7 w-7 place-items-center rounded-md border text-muted-foreground hover:bg-muted hover:text-foreground"
                    >
                      <ChevronLeft className="h-3.5 w-3.5" />
                    </button>
                    <span className="min-w-10 text-center font-mono text-[10px] tabular-nums text-muted-foreground">
                      {selectedProfileIndex + 1} / {visibleCarouselTargets.length}
                    </span>
                    <button
                      type="button"
                      aria-label="下一个监控标的"
                      onClick={() => selectAdjacentProfile(1)}
                      className="grid h-7 w-7 place-items-center rounded-md border text-muted-foreground hover:bg-muted hover:text-foreground"
                    >
                      <ChevronRight className="h-3.5 w-3.5" />
                    </button>
                  </>
                ) : null}
                <button
                  type="button"
                  aria-label="刷新"
                  title="刷新"
                  onClick={() => void load()}
                  className="grid h-7 w-7 place-items-center rounded-md border text-muted-foreground hover:bg-muted hover:text-foreground"
                >
                  <RefreshCw className="h-3.5 w-3.5" />
                </button>
              </div>
            </div>
            {visibleCarouselTargets.length && selectedCarouselTarget ? (
              <div
                className="grid w-full min-w-0 max-w-full gap-2 lg:h-[56rem] lg:grid-cols-2"
                onMouseEnter={() => setCarouselPaused(true)}
                onMouseLeave={() => setCarouselPaused(false)}
                onFocusCapture={() => setCarouselPaused(true)}
                onBlurCapture={(event) => {
                  if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setCarouselPaused(false);
                }}
              >
                <div className="grid min-w-0 grid-rows-[auto_minmax(0,1fr)] overflow-hidden rounded-md border bg-muted/[0.08]">
                  <div className="hidden grid-cols-[minmax(5.25rem,1fr)_minmax(3.7rem,.7fr)_minmax(4.2rem,.75fr)_minmax(6.5rem,1.1fr)_2.75rem] gap-x-2 border-b px-3 py-2 text-[9px] font-medium uppercase tracking-[0.08em] text-muted-foreground lg:grid">
                    <span>标的</span>
                    <span>现价</span>
                    <span>今日</span>
                    <span>最近点位</span>
                    <span className="sr-only">状态与下次检查</span>
                  </div>
                  <div className="flex gap-2 overflow-x-auto p-2 lg:grid lg:content-start lg:overflow-x-hidden lg:overflow-y-auto">
                    {visibleCarouselTargets.map((target) => {
                      if (target.kind === "pending_autopilot") {
                        return (
                          <MonitorPendingCarouselRow
                            key={target.key}
                            symbol={target.symbol}
                            displayName={monitorSymbolName(target.symbol, holdingNameByIdentifier)}
                            selected={target.key === selectedCarouselTarget.key}
                            run={target.run}
                            card={target.card}
                            onSelect={() => setSelectedProfileId(target.key)}
                          />
                        );
                      }
                      const { profile } = target;
                      const normalizedSymbol = profile.symbol.trim().toUpperCase();
                      const [code, symbolMarket] = normalizedSymbol.split(".");
                      const marketLabel = symbolMarket || profile.market || "";
                      const marketScheduleSupported = ["SH", "SZ", "BJ"].includes(marketLabel.toUpperCase());
                      const lampState = monitorLampState({
                        profile,
                        status,
                        connected: !monitorDisconnected,
                        marketScheduleSupported,
                      });
                      const countdownActive = Boolean(
                        lampState.tone === "healthy"
                        && status?.runtime.calendar?.open !== false
                      );
                      return (
                        <MonitorCarouselRow
                          key={profile.profile_id}
                          profile={profile}
                          displayName={monitorProfileDisplayName(profile, holdingNameByIdentifier)}
                          code={code}
                          marketLabel={marketLabel}
                          selected={target.key === selectedCarouselTarget.key}
                          lampState={lampState}
                          nowMs={heartbeatNow}
                          countdownActive={countdownActive}
                          onSelect={() => setSelectedProfileId(profile.profile_id)}
                        />
                      );
                    })}
                  </div>
                </div>
                {selectedProfile ? (() => {
                  const profile = selectedProfile;
                  const normalizedSymbol = profile.symbol.trim().toUpperCase();
                  const [code, symbolMarket] = normalizedSymbol.split(".");
                  const displayName = monitorProfileDisplayName(profile, holdingNameByIdentifier);
                  const marketLabel = symbolMarket || profile.market || "";
                  const marketScheduleSupported = ["SH", "SZ", "BJ"].includes(marketLabel.toUpperCase());
                  const lampState = monitorLampState({
                    profile,
                    status,
                    connected: !monitorDisconnected,
                    marketScheduleSupported,
                  });
                  const monitorActuallyRunning = lampState.tone === "healthy";
                  const profileStatusLabel = profile.status === "active" && !monitorActuallyRunning
                    ? "计划已启用"
                    : STATUS_LABELS[profile.status] || profile.status;
                  const cardTargetWindow = derivePriceTargetWindow(
                    profile.display_plan?.plan,
                    finitePriceTarget(profile.last_quote?.price),
                    finitePriceTarget(profile.last_quote?.session_open),
                    finitePriceTarget(profile.last_quote?.previous_price),
                  );
                  const cardBoostDirection = monitorDisconnected ? null : cardTargetWindow?.boostDirection || null;
                  const pendingPlanCount = profile.plans?.filter((item) => item.status === "pending_review").length || 0;
                  return (
                    <article
                      aria-label={`${displayName} ${code} 监控标的`}
                      data-boost-direction={cardBoostDirection || "none"}
                      className={cn(
                        "monitor-boost-shell relative isolate grid max-h-[56rem] min-w-0 content-start gap-3 overflow-x-hidden overflow-y-auto rounded-md border p-3",
                        cardBoostDirection === "up" && "monitor-boost-card-up border-red-500/40 bg-red-500/[0.02]",
                        cardBoostDirection === "down" && "monitor-boost-card-down border-emerald-500/40 bg-emerald-500/[0.02]",
                      )}
                    >
                      {cardBoostDirection ? <MonitorBoostParticles direction={cardBoostDirection} /> : null}
                      <div className="relative z-[1] flex items-center justify-between gap-3">
                        <div className="min-w-0">
                          <div className="truncate text-sm font-semibold">{displayName}</div>
                          <div className="mt-0.5 flex items-center gap-1.5 text-xs text-muted-foreground">
                            <span className="font-mono font-medium text-foreground">{code}</span>
                            {marketLabel ? <span>{marketLabel}</span> : null}
                          </div>
                        </div>
                        <div className="flex shrink-0 items-center gap-2">
                          {profile.display_plan?.plan.data_mode === "single_source" ? <SingleSourceWarning compact /> : null}
                          <MonitorStatusLamp state={lampState} pulse />
                          <span className="sr-only">{profileStatusLabel}</span>
                        </div>
                      </div>
                      {selectedTargetCard?.decision_brief ? (
                        <MonitorDecisionCard
                          card={selectedTargetCard}
                          busy={busy}
                          onChoice={(choice) => void chooseMonitoringDecision(selectedTargetCard, choice)}
                          onValidateDraft={(draftId) => void validateConditionDraft(draftId)}
                          onCancelDraft={(draftId) => void cancelConditionDraft(draftId)}
                        />
                      ) : null}
                      <div className="sr-only">
                        <MonitorHeartbeat
                          profile={profile}
                          status={status}
                          nowMs={heartbeatNow}
                          marketScheduleSupported={marketScheduleSupported}
                          connected={!monitorDisconnected}
                        />
                      </div>
                      <MonitorPriceSnapshot
                        profile={profile}
                        version={profile.display_plan}
                        nowMs={heartbeatNow}
                        countdownActive={Boolean(
                          monitorActuallyRunning
                          && status?.runtime.calendar?.open !== false
                          && profile.status === "active"
                        )}
                        effectsActive={!monitorDisconnected}
                      />
                      <MonitorPlanSummary
                        version={profile.display_plan}
                        target={profile.delivery_target_id ? deliveryTargetById.get(profile.delivery_target_id) : undefined}
                        targetId={profile.delivery_target_id}
                      />
                      {profile.display_plan && (
                        profile.display_plan.schema_version < 3
                        || profile.display_plan.model_id !== "evidence-policy-v3"
                      ) ? (
                        <p className="text-xs text-amber-700 dark:text-amber-300">旧版规则策略 · 待重新分析升级</p>
                      ) : null}
                      {pendingPlanCount ? (
                        <p className="text-xs text-cyan-700 dark:text-cyan-300">有 {pendingPlanCount} 个待审核版本；运行版会持续生效，直到保存并启用新版本。</p>
                      ) : null}
                      {profile.blocked_reasons.length ? <p className="text-xs text-amber-700 dark:text-amber-300">已自动刷新：{profile.blocked_reasons.map(blockedReasonLabel).join("；")}</p> : null}
                      {profile.input_outdated ? <p className="text-xs text-amber-700 dark:text-amber-300">持仓数量或成本已变化，建议重新分析；现有行情规则不会输出旧仓位影响。</p> : null}
                      <div className="flex flex-wrap items-center gap-2 self-end">
                        <button type="button" disabled={openingProfileId !== null} onClick={() => void openPlan(profile)} className="inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1.5 text-xs hover:bg-muted disabled:opacity-50">
                          {openingProfileId === profile.profile_id ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Settings2 className="h-3.5 w-3.5" />}计划与审核
                        </button>
                        {profile.status === "active" ? <button type="button" onClick={() => void actOnProfile(profile, "pause")} className="inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1.5 text-xs hover:bg-muted"><Pause className="h-3.5 w-3.5" />暂停</button> : null}
                        {profile.status === "paused" ? <button type="button" onClick={() => void actOnProfile(profile, "resume")} className="inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1.5 text-xs hover:bg-muted"><Play className="h-3.5 w-3.5" />恢复</button> : null}
                        {profile.status === "closed" ? <button type="button" disabled={busy === `reopen:${profile.profile_id}`} onClick={() => void reopenProfile(profile)} className="inline-flex items-center gap-1.5 rounded-md border border-cyan-500/40 px-2.5 py-1.5 text-xs text-cyan-700 hover:bg-cyan-500/10 disabled:opacity-50 dark:text-cyan-300">{busy === `reopen:${profile.profile_id}` ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}{busy === `reopen:${profile.profile_id}` ? "正在刷新数据源…" : "重新检测并打开"}</button> : null}
                        {profile.blocked_reasons.includes("quote_not_actionable:single_source") ? (
                          <button
                            type="button"
                            disabled={busy === `single-source:${profile.profile_id}`}
                            onClick={() => void useSingleSource(profile)}
                            className="inline-flex items-center gap-1.5 rounded-md border border-amber-500/50 px-2.5 py-1.5 text-xs text-amber-800 hover:bg-amber-500/10 disabled:opacity-50 dark:text-amber-200"
                          >
                            {busy === `single-source:${profile.profile_id}` ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <AlertTriangle className="h-3.5 w-3.5" />}
                            {busy === `single-source:${profile.profile_id}` ? "正在刷新数据源…" : "同意使用单源模式"}
                          </button>
                        ) : null}
                        {profile.status === "drafting"
                          && profile.blocked_reasons.length > 0
                          && !profile.blocked_reasons.includes("quote_not_actionable:single_source") ? (
                          <button
                            type="button"
                            disabled={busy === `reanalyze:${profile.profile_id}`}
                            onClick={() => void actOnProfile(profile, "reanalyze")}
                            className="inline-flex items-center gap-1.5 rounded-md border border-cyan-500/40 px-2.5 py-1.5 text-xs text-cyan-700 hover:bg-cyan-500/10 disabled:opacity-50 dark:text-cyan-300"
                          >
                            {busy === `reanalyze:${profile.profile_id}` ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}
                            {busy === `reanalyze:${profile.profile_id}` ? "正在重新获取…" : "重新获取数据"}
                          </button>
                        ) : null}
                        {profile.status !== "closed" ? (
                          <button
                            type="button"
                            disabled={busy === `close:${profile.profile_id}`}
                            onClick={() => void actOnProfile(profile, "close")}
                            className="inline-flex items-center gap-1.5 rounded-md border border-red-500/40 px-2.5 py-1.5 text-xs text-red-700 hover:bg-red-500/10 disabled:opacity-50 dark:text-red-300"
                          >
                            {busy === `close:${profile.profile_id}` ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <X className="h-3.5 w-3.5" />}
                            {busy === `close:${profile.profile_id}` ? "关闭中…" : "关闭监控"}
                          </button>
                        ) : null}
                      </div>
                    </article>
                  );
                })() : selectedPendingTarget ? (
                  <MonitorPendingTargetCard
                    symbol={selectedPendingTarget.symbol}
                    displayName={monitorSymbolName(
                      selectedPendingTarget.symbol,
                      holdingNameByIdentifier,
                    )}
                    run={selectedPendingTarget.run}
                    card={selectedPendingTarget.card}
                    onShowUsage={setUsageJobId}
                  />
                ) : null}
              </div>
            ) : (
              <div className="rounded-md border border-dashed p-6 text-center text-sm text-muted-foreground">
                {closedProfileCount
                  ? "当前没有正在监控的标的。已关闭卡片已收起，历史事件仍在下方保留。"
                  : "在下方持仓表点亮雷达按钮，然后生成第一份监控草案。"}
              </div>
            )}
          </div>

          <div className="grid gap-2">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <h3 className="text-sm font-semibold">最近事件</h3>
              <div className="flex flex-wrap items-center justify-end gap-2 text-xs">
                <span className="inline-flex items-center gap-1 text-muted-foreground"><Clock3 className="h-3.5 w-3.5" />已记录 {Math.max(status?.events || 0, displayEvents.length)} 条</span>
                {displayEvents.length > RECENT_EVENT_PREVIEW_LIMIT ? (
                  <button
                    type="button"
                    aria-expanded={showAllEvents}
                    onClick={() => setShowAllEvents((current) => !current)}
                    className="rounded border px-2 py-1 font-medium text-foreground hover:bg-muted"
                  >
                    {showAllEvents
                      ? `收起，仅看最新 ${RECENT_EVENT_PREVIEW_LIMIT} 条`
                      : `展开全部 ${displayEvents.length} 条`}
                  </button>
                ) : null}
              </div>
            </div>
            <div className="overflow-hidden rounded-md border" aria-label="最近事件列表">
              {renderedEvents.length ? renderedEvents.map((event) => (
                <MonitorEventRow
                  key={event.event_id}
                  event={event}
                  displayName={monitorSymbolDisplayName(event.symbol, holdingNameByIdentifier)}
                  onAcknowledge={(item) => void acknowledge(item)}
                />
              )) : (
                <div className="flex items-center justify-center gap-2 p-6 text-sm text-muted-foreground"><CheckCircle2 className="h-4 w-4" />暂无触发事件</div>
              )}
            </div>
          </div>
        </div>
      ) : null}

      <div className="sr-only" aria-live="polite">{busy ? (busy === "create" || busy.startsWith("reanalyze:") || busy.startsWith("reopen:") || busy.startsWith("single-source:") ? "正在刷新数据源" : "监控操作处理中") : "监控操作已就绪"}</div>
      {drawerProfile ? (
        <MonitorPlanDrawer
          profile={drawerProfile}
          displayName={monitorProfileDisplayName(drawerProfile, holdingNameByIdentifier)}
          version={selectPlanVersion(drawerProfile, drawerPlanVersion)}
          plan={draftPlan}
          numericDraft={draftNumericPlan}
          formErrors={planFormErrors}
          isDirty={drawerPlanDirty}
          busy={Boolean(busy)}
          loading={openingProfileId === drawerProfile.profile_id}
          onChange={updateDraftPlan}
          onNumericChange={updateDraftNumericPlan}
          onSelectVersion={selectDrawerPlanVersion}
          onClose={() => closePlanDrawer()}
          onSave={() => void savePlan()}
          onSaveAndActivate={() => void saveAndActivatePlan()}
          onReanalyze={() => {
            if (drawerPlanDirty && !window.confirm("当前有未保存修改。AI 重新分析会生成新草案，确认放弃这些修改？")) return;
            void actOnProfile(drawerProfile, "reanalyze");
          }}
          onRecheck={() => void reopenProfile(drawerProfile, false)}
          onUseSingleSource={() => void useSingleSource(drawerProfile)}
          ymcaAvailability={ymcaAvailability}
        />
      ) : null}
    </section>
  );
}
