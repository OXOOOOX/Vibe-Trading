import { Bot, TrendingUp, Globe, Sparkles, Users, UserCircle2, NotebookPen, Landmark } from "lucide-react";

import { useI18n } from "@/lib/i18n";

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

interface Props {
  onExample: (s: string) => void;
}

export function WelcomeScreen({ onExample }: Props) {
  const { language } = useI18n();
  const categoryLabels: Record<string, string> = {
    "Multi-Market Backtest": "多市场回测",
    "Research & Analysis": "研究与分析",
    "Swarm Teams": "多智能体团队",
    "Document & Web Research": "文档与网络研究",
    "Trade Journal": "交易日志",
    "Trading Connectors": "交易连接器",
    "Shadow Account": "影子账户",
  };
  const capabilityLabels: Record<string, string> = {
    "Finance Skills Library": "金融技能库",
    "Swarm Agent Teams": "多智能体研究团队",
    "Auto-Discovered Tools": "自动发现工具",
    "3 Markets: A-Share · Crypto · HK/US": "三大市场：A 股 · 加密资产 · 港美股",
    "Trading Connector Profiles": "交易连接器配置",
    "Minute to Daily Timeframes": "分钟级至日线周期",
    "4 Portfolio Optimizers": "4 类投资组合优化器",
    "15+ Risk Metrics": "15+ 风险指标",
    "Options & Derivatives": "期权与衍生品",
    "PDF & Web Research": "PDF 与网络研究",
    "Factor Analysis & ML": "因子分析与机器学习",
    "Trade Journal Analyzer": "交易日志分析",
    "Shadow Account Backtest": "影子账户回测",
    "Persistent Memory": "持久化记忆",
    "Session Search": "会话搜索",
  };
  const exampleLabels: Record<string, { title: string; desc: string }> = {
    "Cross-Market Portfolio": { title: "跨市场投资组合", desc: "使用风险平价优化器配置 A 股、加密资产和美股" },
    "BTC 5-Min MACD Strategy": { title: "BTC 五分钟 MACD 策略", desc: "使用 OKX 实时数据进行分钟级加密资产回测" },
    "US Tech Max Diversification": { title: "美股科技股最大分散化", desc: "通过 yfinance 对大型科技股组合进行优化" },
    "Multi-Factor Alpha Model": { title: "多因子 Alpha 模型", desc: "在 300 只股票上进行 IC 加权因子合成" },
    "Options Greeks Analysis": { title: "期权希腊值分析", desc: "使用 Black-Scholes 模型计算 Delta、Gamma、Theta 与 Vega" },
    "Investment Committee Review": { title: "投资委员会评审", desc: "多智能体辩论：多空观点、风险审查与投资经理决策" },
    "Quant Strategy Desk": { title: "量化策略研究团队", desc: "筛选、因子研究、回测与风险审计的完整流程" },
    "Analyze an Earnings Report PDF": { title: "分析财报 PDF", desc: "上传 PDF 并针对财务数据进行提问" },
    "Web Research: Macro Outlook": { title: "网络研究：宏观展望", desc: "读取实时网络来源并开展宏观分析" },
    "Analyze My Broker Export": { title: "分析我的券商交易流水", desc: "解析同花顺、东财、富途或通用 CSV，统计持仓天数、胜率、盈亏比与时段分布" },
    "Diagnose My Behavior Biases": { title: "诊断我的交易行为偏差", desc: "分析处置效应、过度交易、追涨和锚定，并提供量化证据" },
    "Check Selected Connector": { title: "检查当前交易连接器", desc: "列出连接器配置，并验证当前选中的连接器" },
    "Analyze Connector Portfolio": { title: "分析连接器账户组合", desc: "读取当前连接器的账户摘要与持仓" },
    "Quote & Trend": { title: "行情与趋势", desc: "通过当前连接器获取报价和近期日线数据" },
    "Train My Shadow from Journal": { title: "根据交易日志训练影子账户", desc: "从券商 CSV 提取交易规则，并保存影子策略配置" },
    "How Much Am I Leaving on the Table?": { title: "我错过了多少收益？", desc: "回测影子策略，并归因其与实际收益之间的差异" },
    "Generate Shadow Report": { title: "生成影子账户报告", desc: "生成包含权益曲线、分市场夏普率和归因瀑布图的 HTML/PDF 报告" },
  };
  return (
    <div className="flex flex-col items-center justify-center min-h-[60vh] space-y-8 text-center">
      {/* Header */}
      <div className="space-y-3">
        <div className="h-16 w-16 mx-auto rounded-2xl bg-gradient-to-br from-primary/80 to-info/80 flex items-center justify-center shadow-lg">
          <Bot className="h-8 w-8 text-white" />
        </div>
        <div>
          <h2 className="text-2xl font-bold tracking-tight">Vibe-Trading</h2>
          <p className="text-xs text-muted-foreground mt-1 max-w-sm mx-auto leading-relaxed">
            {language === "zh-CN" ? "与你的专业金融智能体团队一起开展交易研究" : "vibe trading with your professional financial agent team"}
          </p>
          <p className="text-sm text-muted-foreground mt-2 max-w-md leading-relaxed mx-auto">
            {language === "zh-CN" ? "描述你的交易策略，开始一项新的研究。" : "Describe a trading strategy to get started."}
          </p>
        </div>
      </div>

      {/* Capability chips */}
      <div className="flex flex-wrap justify-center gap-2 max-w-lg">
        {CAPABILITY_CHIPS.map((chip) => (
          <span
            key={chip}
            className="px-2.5 py-1 text-xs rounded-full border border-border/60 text-muted-foreground bg-muted/30"
          >
            {language === "zh-CN" ? capabilityLabels[chip] ?? chip : chip}
          </span>
        ))}
      </div>

      {/* Example categories grid */}
      <div className="w-full max-w-2xl text-left space-y-4">
        <p className="text-xs text-muted-foreground px-1">{language === "zh-CN" ? "试试这些示例：" : "Try an example:"}</p>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
          {CATEGORIES.map((cat) => (
            <div key={cat.label} className="space-y-2">
              <div className={`flex items-center gap-1.5 text-xs font-medium px-1 ${cat.color.split(" ").filter(c => c.startsWith("text-")).join(" ")}`}>
                {cat.icon}
                <span>{language === "zh-CN" ? categoryLabels[cat.label] ?? cat.label : cat.label}</span>
              </div>
              <div className="space-y-1.5">
                {cat.examples.map((ex) => (
                  <button
                    key={ex.title}
                    onClick={() => onExample(ex.prompt)}
                    className={`block w-full text-left px-3 py-2.5 rounded-xl border transition-colors ${cat.color}`}
                  >
                    <span className="text-sm font-medium text-foreground leading-snug">
                      {language === "zh-CN" ? exampleLabels[ex.title]?.title ?? ex.title : ex.title}
                    </span>
                    <span className="block text-xs text-muted-foreground mt-0.5 leading-snug">
                      {language === "zh-CN" ? exampleLabels[ex.title]?.desc ?? ex.desc : ex.desc}
                    </span>
                  </button>
                ))}
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
