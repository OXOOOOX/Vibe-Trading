import { useTranslation } from "react-i18next";
import type { LucideIcon } from "lucide-react";
import {
  Activity,
  ArrowUpRight,
  BookOpen,
  Bot,
  CalendarRange,
  CheckCircle2,
  Globe,
  Landmark,
  NotebookPen,
  PieChart,
  Search,
  Sparkles,
  Target,
  TrendingUp,
  UserCircle2,
  Users,
} from "lucide-react";
import { useId } from "react";

interface Example {
  title: string;
  desc: string;
  prompt: string;
}

interface Category {
  label: string;
  icon: React.ReactNode;
  color: string;
  examples: Example[];
}

export type WelcomeMode = "deepReport" | "researchGoal";

type QuickActionBehavior =
  | { kind: "prompt"; promptKey: string }
  | { kind: "draft"; promptKey: string }
  | { kind: "mode"; mode: WelcomeMode };

interface QuickAction {
  key: string;
  icon: LucideIcon;
  tone: string;
  iconTone: string;
  behavior: QuickActionBehavior;
}

const QUICK_ACTIONS: QuickAction[] = [
  {
    key: "portfolioAnalysis",
    icon: PieChart,
    tone: "border-orange-500/30 hover:border-orange-500/60 hover:bg-orange-500/[0.04]",
    iconTone: "bg-orange-500/10 text-orange-600 dark:text-orange-300",
    behavior: { kind: "prompt", promptKey: "welcome.quickStart.actions.portfolioAnalysis.prompt" },
  },
  {
    key: "stockAnalysis",
    icon: Search,
    tone: "border-blue-500/30 hover:border-blue-500/60 hover:bg-blue-500/[0.04]",
    iconTone: "bg-blue-500/10 text-blue-600 dark:text-blue-300",
    behavior: { kind: "draft", promptKey: "welcome.quickStart.actions.stockAnalysis.prompt" },
  },
  {
    key: "deepReport",
    icon: BookOpen,
    tone: "border-cyan-500/30 hover:border-cyan-500/60 hover:bg-cyan-500/[0.04]",
    iconTone: "bg-cyan-500/10 text-cyan-600 dark:text-cyan-300",
    behavior: { kind: "mode", mode: "deepReport" },
  },
  {
    key: "weeklyReport",
    icon: CalendarRange,
    tone: "border-indigo-500/30 hover:border-indigo-500/60 hover:bg-indigo-500/[0.04]",
    iconTone: "bg-indigo-500/10 text-indigo-600 dark:text-indigo-300",
    behavior: { kind: "draft", promptKey: "welcome.quickStart.actions.weeklyReport.prompt" },
  },
  {
    key: "marketReview",
    icon: Activity,
    tone: "border-emerald-500/30 hover:border-emerald-500/60 hover:bg-emerald-500/[0.04]",
    iconTone: "bg-emerald-500/10 text-emerald-600 dark:text-emerald-300",
    behavior: { kind: "prompt", promptKey: "welcome.quickStart.actions.marketReview.prompt" },
  },
  {
    key: "allocationPlan",
    icon: TrendingUp,
    tone: "border-violet-500/30 hover:border-violet-500/60 hover:bg-violet-500/[0.04]",
    iconTone: "bg-violet-500/10 text-violet-600 dark:text-violet-300",
    behavior: { kind: "prompt", promptKey: "welcome.quickStart.actions.allocationPlan.prompt" },
  },
  {
    key: "researchGoal",
    icon: Target,
    tone: "border-amber-500/30 hover:border-amber-500/60 hover:bg-amber-500/[0.04]",
    iconTone: "bg-amber-500/10 text-amber-600 dark:text-amber-300",
    behavior: { kind: "mode", mode: "researchGoal" },
  },
];

interface QuickActionCardProps {
  actionKey: string;
  icon: LucideIcon;
  tone: string;
  iconTone: string;
  title: string;
  description: string;
  eyebrow: string;
  details: string[];
  footer: string;
  cta: string;
  onClick: () => void;
}

function QuickActionCard({
  actionKey,
  icon: Icon,
  tone,
  iconTone,
  title,
  description,
  eyebrow,
  details,
  footer,
  cta,
  onClick,
}: QuickActionCardProps) {
  const hintId = useId();

  return (
    <div className="group relative z-0 min-w-0 hover:z-50 focus-within:z-50" data-quick-action={actionKey}>
      <button
        type="button"
        onClick={onClick}
        aria-describedby={hintId}
        className={`flex h-full w-full items-start gap-3 rounded-2xl border bg-card/40 px-4 py-3.5 text-left shadow-sm transition duration-150 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40 ${tone}`}
      >
        <span className={`mt-0.5 rounded-xl p-2 ${iconTone}`}>
          <Icon className="h-4 w-4" />
        </span>
        <span className="min-w-0 flex-1">
          <span className="flex items-center justify-between gap-2">
            <span className="text-sm font-semibold text-foreground">{title}</span>
            <ArrowUpRight className="h-3.5 w-3.5 shrink-0 text-muted-foreground transition-transform group-hover:-translate-y-0.5 group-hover:translate-x-0.5" />
          </span>
          <span className="mt-1 block text-xs leading-5 text-muted-foreground">{description}</span>
          <span className="mt-2 block text-[11px] font-medium text-foreground/70">{cta}</span>
        </span>
      </button>

      <div
        id={hintId}
        role="tooltip"
        className="pointer-events-none invisible absolute left-1/2 top-[calc(100%+0.6rem)] z-[70] w-[min(22rem,calc(100vw-3rem))] -translate-x-1/2 translate-y-1 rounded-2xl border border-border/80 bg-background p-4 text-foreground opacity-0 shadow-2xl ring-1 ring-black/10 transition duration-150 group-hover:visible group-hover:translate-y-0 group-hover:opacity-100 group-focus-within:visible group-focus-within:translate-y-0 group-focus-within:opacity-100"
      >
        <span aria-hidden="true" className="absolute -top-1.5 left-1/2 h-3 w-3 -translate-x-1/2 rotate-45 border-l border-t border-border/80 bg-background" />
        <div className="flex items-center gap-2">
          <span className={`rounded-lg p-1.5 ${iconTone}`}>
            <Icon className="h-4 w-4" />
          </span>
          <div>
            <div className="text-sm font-semibold">{title}</div>
            <div className="mt-0.5 text-[11px] font-medium text-muted-foreground">{eyebrow}</div>
          </div>
        </div>
        <ul className="mt-3 space-y-2">
          {details.map((detail) => (
            <li key={detail} className="flex gap-2 text-xs leading-5">
              <CheckCircle2 className="mt-0.5 h-3.5 w-3.5 shrink-0 text-emerald-600 dark:text-emerald-400" />
              <span>{detail}</span>
            </li>
          ))}
        </ul>
        <div className="mt-3 rounded-xl border bg-muted/70 px-3 py-2 text-[11px] leading-5 text-muted-foreground">
          {footer}
        </div>
      </div>
    </div>
  );
}

interface ExampleActionCardProps {
  exampleKey: string;
  icon: React.ReactNode;
  tone: string;
  accentClass: string;
  title: string;
  description: string;
  eyebrow: string;
  details: string[];
  footer: string;
  cta: string;
  onClick: () => void;
}

function ExampleActionCard({
  exampleKey,
  icon,
  tone,
  accentClass,
  title,
  description,
  eyebrow,
  details,
  footer,
  cta,
  onClick,
}: ExampleActionCardProps) {
  const hintId = useId();

  return (
    <div className="group relative z-0 min-w-0 hover:z-50 focus-within:z-50" data-example-action={exampleKey}>
      <button
        type="button"
        onClick={onClick}
        aria-describedby={hintId}
        className={`flex w-full items-start gap-3 rounded-2xl border bg-card/30 px-3.5 py-3 text-left shadow-sm transition duration-150 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40 ${tone}`}
      >
        <span className={`mt-0.5 rounded-xl bg-muted/70 p-2 ${accentClass}`}>
          {icon}
        </span>
        <span className="min-w-0 flex-1">
          <span className="flex items-start justify-between gap-2">
            <span className="text-sm font-semibold leading-snug text-foreground">{title}</span>
            <ArrowUpRight className="mt-0.5 h-3.5 w-3.5 shrink-0 text-muted-foreground transition-transform group-hover:-translate-y-0.5 group-hover:translate-x-0.5" />
          </span>
          <span className="mt-1 block text-xs leading-5 text-muted-foreground">{description}</span>
          <span className="mt-2 block text-[11px] font-medium text-foreground/70">{cta}</span>
        </span>
      </button>

      <div
        id={hintId}
        role="tooltip"
        className="pointer-events-none invisible absolute left-1/2 top-[calc(100%+0.6rem)] z-[70] w-[min(22rem,calc(100vw-3rem))] -translate-x-1/2 translate-y-1 rounded-2xl border border-border/80 bg-background p-4 text-foreground opacity-0 shadow-2xl ring-1 ring-black/10 transition duration-150 group-hover:visible group-hover:translate-y-0 group-hover:opacity-100 group-focus-within:visible group-focus-within:translate-y-0 group-focus-within:opacity-100"
      >
        <span aria-hidden="true" className="absolute -top-1.5 left-1/2 h-3 w-3 -translate-x-1/2 rotate-45 border-l border-t border-border/80 bg-background" />
        <div className="flex items-center gap-2">
          <span className={`rounded-lg bg-muted/70 p-1.5 ${accentClass}`}>
            {icon}
          </span>
          <div>
            <div className="text-sm font-semibold">{title}</div>
            <div className="mt-0.5 text-[11px] font-medium text-muted-foreground">{eyebrow}</div>
          </div>
        </div>
        <p className="mt-3 text-xs leading-5 text-muted-foreground">{description}</p>
        <ul className="mt-3 space-y-2">
          {details.map((detail) => (
            <li key={detail} className="flex gap-2 text-xs leading-5">
              <CheckCircle2 className="mt-0.5 h-3.5 w-3.5 shrink-0 text-emerald-600 dark:text-emerald-400" />
              <span>{detail}</span>
            </li>
          ))}
        </ul>
        <div className="mt-3 rounded-xl border bg-muted/70 px-3 py-2 text-[11px] leading-5 text-muted-foreground">
          {footer}
        </div>
      </div>
    </div>
  );
}

const CATEGORIES: Category[] = [
  {
    label: "Multi-Market Backtest",
    icon: <TrendingUp className="h-4 w-4" />,
    color: "text-red-400 border-red-500/30 hover:border-red-500/60 hover:bg-red-500/5",
    examples: [
      {
        title: "Cross-Market Portfolio",
        desc: "A-shares + crypto + US equities with risk-parity optimizer",
        prompt: "Backtest a risk-parity portfolio of 000001.SZ, BTC-USDT, and AAPL for full-year 2024, compare against equal-weight baseline",
      },
      {
        title: "BTC 5-Min MACD Strategy",
        desc: "Minute-level crypto backtest with real-time OKX data",
        prompt: "Backtest BTC-USDT 5-minute MACD strategy, fast=12 slow=26 signal=9, last 30 days",
      },
      {
        title: "US Tech Max Diversification",
        desc: "Portfolio optimizer across FAANG+ via yfinance",
        prompt: "Backtest AAPL, MSFT, GOOGL, AMZN, NVDA with max_diversification portfolio optimizer, full-year 2024",
      },
    ],
  },
  {
    label: "Research & Analysis",
    icon: <Sparkles className="h-4 w-4" />,
    color: "text-amber-400 border-amber-500/30 hover:border-amber-500/60 hover:bg-amber-500/5",
    examples: [
      {
        title: "Multi-Factor Alpha Model",
        desc: "IC-weighted factor synthesis across 300 stocks",
        prompt: "Build a multi-factor alpha model using momentum, reversal, volatility, and turnover on CSI 300 constituents with IC-weighted factor synthesis, backtest 2023-2024",
      },
      {
        title: "Options Greeks Analysis",
        desc: "Black-Scholes pricing with Delta/Gamma/Theta/Vega",
        prompt: "Calculate option Greeks using Black-Scholes: spot=100, strike=105, risk-free rate=3%, vol=25%, expiry=90 days, analyze Delta/Gamma/Theta/Vega",
      },
    ],
  },
  {
    label: "Swarm Teams",
    icon: <Users className="h-4 w-4" />,
    color: "text-violet-400 border-violet-500/30 hover:border-violet-500/60 hover:bg-violet-500/5",
    examples: [
      {
        title: "Investment Committee Review",
        desc: "Multi-agent debate: long vs short, risk review, PM decision",
        prompt: "[Swarm Team Mode] Use the investment_committee preset to evaluate whether to go long or short on NVDA given current market conditions",
      },
      {
        title: "Quant Strategy Desk",
        desc: "Screening → factor research → backtest → risk audit pipeline",
        prompt: "[Swarm Team Mode] Use the quant_strategy_desk preset to find and backtest the best momentum strategy on CSI 300 constituents",
      },
    ],
  },
  {
    label: "Document & Web Research",
    icon: <Globe className="h-4 w-4" />,
    color: "text-blue-400 border-blue-500/30 hover:border-blue-500/60 hover:bg-blue-500/5",
    examples: [
      {
        title: "Analyze an Earnings Report PDF",
        desc: "Upload a PDF and ask questions about the financials",
        prompt: "Summarize the key financial metrics, risks, and outlook from the uploaded earnings report",
      },
      {
        title: "Web Research: Macro Outlook",
        desc: "Read live web sources for macro analysis",
        prompt: "Read the latest Fed meeting minutes and summarize the key takeaways for equity and crypto markets",
      },
    ],
  },
  {
    label: "Trade Journal",
    icon: <NotebookPen className="h-4 w-4" />,
    color: "text-orange-400 border-orange-500/30 hover:border-orange-500/60 hover:bg-orange-500/5",
    examples: [
      {
        title: "Analyze My Broker Export",
        desc: "Parse 同花顺/东财/富途/generic CSV — holding days, win rate, PnL ratio, hourly distribution",
        prompt: "Analyze the trade journal I just uploaded — full profile with holding stats, win rate, top symbols, and hourly distribution",
      },
      {
        title: "Diagnose My Behavior Biases",
        desc: "Disposition effect, overtrading, chasing momentum, anchoring — severity + numeric evidence",
        prompt: "Run the 4 behavior diagnostics on my trade journal (disposition, overtrading, chasing, anchoring) and tell me which bias hurts my PnL most",
      },
    ],
  },
  {
    label: "Trading Connectors",
    icon: <Landmark className="h-4 w-4" />,
    color: "text-cyan-400 border-cyan-500/30 hover:border-cyan-500/60 hover:bg-cyan-500/5",
    examples: [
      {
        title: "Check Selected Connector",
        desc: "List connector profiles and verify the selected one",
        prompt: "List my trading connector profiles, show which one is selected, then check that selected connector. If it is not ready, tell me exactly what setup step is missing. Do not place or modify orders.",
      },
      {
        title: "Analyze Connector Portfolio",
        desc: "Read account summary and positions from the selected connector",
        prompt: "Use the selected trading connector profile to summarize my account, positions, concentration, cash, and portfolio risk. Do not place or modify orders.",
      },
      {
        title: "Quote & Trend",
        desc: "Fetch a quote plus recent daily bars through the selected connector",
        prompt: "Use the selected trading connector to fetch an AAPL quote and 30 daily bars, then summarize the current quote versus the recent trend. Keep it read-only.",
      },
    ],
  },
  {
    label: "Shadow Account",
    icon: <UserCircle2 className="h-4 w-4" />,
    color: "text-emerald-400 border-emerald-500/30 hover:border-emerald-500/60 hover:bg-emerald-500/5",
    examples: [
      {
        title: "Train My Shadow from Journal",
        desc: "Extract your strategy rules from a broker CSV and persist a Shadow profile",
        prompt: "Train my shadow account from the trading journal I just uploaded — show the extracted rules and confirm they look like my behavior",
      },
      {
        title: "How Much Am I Leaving on the Table?",
        desc: "Backtest your shadow strategy and attribute delta vs. your actual PnL",
        prompt: "Run a shadow backtest for the last 90 days on the US market and break down where my PnL diverged from the shadow (rule violations, early exits, missed signals)",
      },
      {
        title: "Generate Shadow Report",
        desc: "8-section HTML/PDF — equity curve, per-market Sharpe, attribution waterfall",
        prompt: "Render the shadow report and give me the URL — lead with the you-vs-shadow delta",
      },
    ],
  },
];

const CAPABILITY_CHIPS = [
  "Finance Skills Library",
  "Swarm Agent Teams",
  "Auto-Discovered Tools",
  "3 Markets: A-Share · Crypto · HK/US",
  "Trading Connector Profiles",
  "Minute to Daily Timeframes",
  "4 Portfolio Optimizers",
  "15+ Risk Metrics",
  "Options & Derivatives",
  "PDF & Web Research",
  "Factor Analysis & ML",
  "Trade Journal Analyzer",
  "Shadow Account Backtest",
  "Persistent Memory",
  "Session Search",
];

const CATEGORY_KEYS: Record<string, string> = {
  "Multi-Market Backtest": "multiMarketBacktest",
  "Research & Analysis": "researchAnalysis",
  "Swarm Teams": "swarmTeams",
  "Document & Web Research": "docWebResearch",
  "Trade Journal": "tradeJournal",
  "Trading Connectors": "tradingConnectors",
  "Shadow Account": "shadowAccount",
};

const EXAMPLE_KEYS: Record<string, string> = {
  "Cross-Market Portfolio": "crossMarketPortfolio",
  "BTC 5-Min MACD Strategy": "btcMacd",
  "US Tech Max Diversification": "usTechMaxDiv",
  "Multi-Factor Alpha Model": "multiFactorAlpha",
  "Options Greeks Analysis": "optionsGreeks",
  "Investment Committee Review": "investmentCommittee",
  "Quant Strategy Desk": "quantStrategyDesk",
  "Analyze an Earnings Report PDF": "earningsReport",
  "Web Research: Macro Outlook": "macroResearch",
  "Analyze My Broker Export": "analyzeBrokerExport",
  "Diagnose My Behavior Biases": "diagnoseBehavior",
  "Check Selected Connector": "checkConnector",
  "Analyze Connector Portfolio": "analyzePortfolio",
  "Quote & Trend": "quoteTrend",
  "Train My Shadow from Journal": "trainShadow",
  "How Much Am I Leaving on the Table?": "shadowDelta",
  "Generate Shadow Report": "shadowReport",
};

const CAPABILITY_KEYS: Record<string, string> = {
  "Finance Skills Library": "financeSkills",
  "Swarm Agent Teams": "swarmTeams",
  "Auto-Discovered Tools": "autoTools",
  "3 Markets: A-Share · Crypto · HK/US": "markets",
  "Trading Connector Profiles": "connectors",
  "Minute to Daily Timeframes": "timeframes",
  "4 Portfolio Optimizers": "optimizers",
  "15+ Risk Metrics": "riskMetrics",
  "Options & Derivatives": "options",
  "PDF & Web Research": "pdfWeb",
  "Factor Analysis & ML": "factorML",
  "Trade Journal Analyzer": "journalAnalyzer",
  "Shadow Account Backtest": "shadowBacktest",
  "Persistent Memory": "memory",
  "Session Search": "sessionSearch",
};

interface Props {
  onExample: (s: string) => void;
  onDraft?: (s: string) => void;
  onModeSelect?: (mode: WelcomeMode) => void;
}

export function WelcomeScreen({ onExample, onDraft, onModeSelect }: Props) {
  const { t } = useTranslation();

  const runQuickAction = (behavior: QuickActionBehavior) => {
    if (behavior.kind === "mode") {
      onModeSelect?.(behavior.mode);
      return;
    }
    const prompt = t(behavior.promptKey);
    if (behavior.kind === "draft") {
      onDraft?.(prompt);
      return;
    }
    onExample(prompt);
  };

  return (
    <div className="flex flex-col items-center justify-center min-h-[60vh] space-y-8 text-center">
      {/* Header */}
      <div className="space-y-3">
        <div className="h-16 w-16 mx-auto rounded-2xl bg-gradient-to-br from-primary/80 to-info/80 flex items-center justify-center shadow-lg">
          <Bot className="h-8 w-8 text-white" />
        </div>
        <div>
          <h2 className="text-2xl font-bold tracking-tight">{t('welcome.title')}</h2>
          <p className="text-xs text-muted-foreground mt-1 max-w-sm mx-auto leading-relaxed">
            {t('welcome.subtitle')}
          </p>
          <p className="text-sm text-muted-foreground mt-2 max-w-md leading-relaxed mx-auto">
            {t('welcome.describePrompt')}
          </p>
        </div>
      </div>

      {/* High-frequency research entry points */}
      <section className="w-full max-w-3xl space-y-3 text-left" aria-labelledby="quick-start-title">
        <div className="flex items-end justify-between gap-4 px-1">
          <div>
            <h3 id="quick-start-title" className="text-sm font-semibold text-foreground">
              {t("welcome.quickStart.title")}
            </h3>
            <p className="mt-1 text-xs leading-5 text-muted-foreground">
              {t("welcome.quickStart.subtitle")}
            </p>
          </div>
          <span className="hidden shrink-0 text-[11px] text-muted-foreground sm:inline">
            {t("welcome.quickStart.hoverHint")}
          </span>
        </div>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          {QUICK_ACTIONS.map((action) => {
            const keyBase = `welcome.quickStart.actions.${action.key}`;
            return (
              <QuickActionCard
                key={action.key}
                actionKey={action.key}
                icon={action.icon}
                tone={action.tone}
                iconTone={action.iconTone}
                title={t(`${keyBase}.title`)}
                description={t(`${keyBase}.description`)}
                eyebrow={t(`${keyBase}.eyebrow`)}
                details={[
                  t(`${keyBase}.detail1`),
                  t(`${keyBase}.detail2`),
                  t(`${keyBase}.detail3`),
                ]}
                footer={t(`${keyBase}.footer`)}
                cta={t(`${keyBase}.cta`)}
                onClick={() => runQuickAction(action.behavior)}
              />
            );
          })}
        </div>
      </section>

      {/* Capability chips */}
      <div className="flex flex-wrap justify-center gap-2 max-w-lg">
        {CAPABILITY_CHIPS.map((chip) => (
          <span
            key={chip}
            className="px-2.5 py-1 text-xs rounded-full border border-border/60 text-muted-foreground bg-muted/30"
          >
            {t(`welcome.capabilities.${CAPABILITY_KEYS[chip]}`)}
          </span>
        ))}
      </div>

      {/* Example categories grid */}
      <section className="w-full max-w-3xl space-y-4 text-left" aria-labelledby="example-actions-title">
        <div className="flex items-end justify-between gap-4 px-1">
          <div>
            <h3 id="example-actions-title" className="text-sm font-semibold text-foreground">
              {t("welcome.tryExample")}
            </h3>
            <p className="mt-1 text-xs leading-5 text-muted-foreground">
              {t("welcome.exampleHints.subtitle")}
            </p>
          </div>
          <span className="hidden shrink-0 text-[11px] text-muted-foreground sm:inline">
            {t("welcome.exampleHints.hoverHint")}
          </span>
        </div>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          {CATEGORIES.map((cat) => {
            const categoryKey = CATEGORY_KEYS[cat.label];
            const accentClass = cat.color.split(" ").filter((className) => className.startsWith("text-")).join(" ");
            const hintBase = `welcome.exampleHints.categories.${categoryKey}`;
            return (
              <div key={cat.label} className="space-y-2.5">
                <div className={`flex items-center gap-1.5 px-1 text-xs font-medium ${accentClass}`}>
                  {cat.icon}
                  <span>{t(`welcome.categories.${categoryKey}`)}</span>
                </div>
                <div className="space-y-2">
                  {cat.examples.map((ex) => {
                    const exampleKey = EXAMPLE_KEYS[ex.title];
                    return (
                      <ExampleActionCard
                        key={ex.title}
                        exampleKey={exampleKey}
                        icon={cat.icon}
                        tone={cat.color}
                        accentClass={accentClass}
                        title={t(`welcome.examples.${exampleKey}`)}
                        description={t(`welcome.examples.${exampleKey}Desc`)}
                        eyebrow={t(`${hintBase}.eyebrow`)}
                        details={[
                          t(`${hintBase}.detail1`),
                          t(`${hintBase}.detail2`),
                          t(`${hintBase}.detail3`),
                        ]}
                        footer={t(`${hintBase}.footer`)}
                        cta={t("welcome.exampleHints.cta")}
                        onClick={() => onExample(ex.prompt)}
                      />
                    );
                  })}
                </div>
              </div>
            );
          })}
        </div>
      </section>
    </div>
  );
}
