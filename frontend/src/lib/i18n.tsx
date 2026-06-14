import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

export type Language = "zh-CN" | "en";

const zh = {
  home: "首页", agent: "研究助手", alphaZoo: "Alpha 因子库", settings: "设置", correlation: "相关性矩阵",
  sessions: "研究会话", newChat: "新建对话", noSessions: "暂无会话", confirm: "确认", cancel: "取消",
  rename: "重命名", delete: "删除", light: "浅色", dark: "深色", expand: "展开侧边栏", collapse: "收起侧边栏",
  loading: "加载中...", configured: "已配置", saving: "保存中...", savedTo: "保存位置", off: "关闭",
  homeTitle: "AI 驱动的量化策略研究平台",
  homeSubtitle: "用自然语言描述交易策略，研究助手会生成代码、执行回测并持续优化，全程实时呈现。",
  startResearch: "开始研究", aiAgent: "AI 研究助手", aiAgentDesc: "使用 ReAct 推理，通过自然语言生成和完善策略",
  backtest: "内置回测引擎", backtestDesc: "覆盖 A 股、美股、港股和加密资产的 7 类数据源",
  streaming: "实时执行过程", streamingDesc: "实时查看助手思考、调用工具和迭代研究",
  replay: "策略复盘", replayDesc: "分析交易流水并建立影子账户，提取规则、回测并归因收益差异",
  settingsSubtitle: "配置本地项目使用的模型凭据和市场数据源令牌。", unavailable: "设置暂不可用",
  localAccess: "本地 API 访问", localAccessDesc: "远程或私有部署时，可在当前浏览器中保存服务端 API 密钥。本机访问无需填写。",
  serverApiKey: "服务端 API 密钥", browserOnly: "仅保存在当前浏览器中；留空可清除。", saveLocalKey: "保存本地密钥",
  localKeySaved: "本地 API 密钥已保存", llmSettings: "大模型设置",
  llmDesc: "选择研究助手使用的模型，配置将保存到项目本地的 agent/.env 文件。", connection: "模型连接",
  provider: "服务商", providerHint: "切换服务商后，会自动填入推荐模型和接口地址。", model: "模型",
  useDefaults: "使用推荐配置", modelHint: "请填写服务商要求的准确模型 ID。", baseUrl: "接口地址", apiKey: "API 密钥",
  keepKey: "留空将保留当前密钥", noKey: "此服务商不需要 API 密钥。", oauth: "此服务商使用 OAuth，请运行：{command}",
  clearApiKey: "清除已保存的 API 密钥", generation: "生成参数", temperature: "温度", timeout: "超时时间（秒）",
  maxRetries: "最大重试次数", reasoning: "推理强度", reasoningHint: "推理强度越高，回答通常更全面，但速度更慢；选择关闭可获得最快响应。",
  saveSettings: "保存模型设置", settingsSaved: "模型设置已保存", dataSettings: "数据源设置",
  dataDesc: "配置回测和研究助手使用的可选市场数据凭据。", tushareToken: "Tushare 令牌",
  keepToken: "留空将保留当前令牌", tushareHint: "用于 A 股、期货、基金和宏观数据。未配置时，项目会尽可能回退到 AKShare。",
  clearTushare: "清除已保存的 Tushare 令牌", saveData: "保存数据源设置", dataSaved: "数据源设置已保存",
  loaderAvailable: "数据加载器可用", noLoader: "项目未提供加载器", packageInstalled: "Python 包已安装", packageNotInstalled: "Python 包未安装",
  assetCodes: "资产代码", assetHint: "使用英文逗号分隔代码，例如 BTC-USDT,ETH-USDT,AAPL,SPY",
  windowDays: "时间窗口（天）", method: "计算方法", compute: "计算相关性",
  strategyComparison: "策略对比", baseline: "基准策略", compare: "对比策略", select: "-- 请选择 --",
  equityDrawdown: "权益曲线与回撤", metric: "指标", delta: "差异", compareEmpty: "请选择两次运行，对比它们的核心指标。",
} as const;

const en: Record<keyof typeof zh, string> = {
  home: "Home", agent: "Agent", alphaZoo: "Alpha Zoo", settings: "Settings", correlation: "Correlation Matrix",
  sessions: "Sessions", newChat: "New Chat", noSessions: "No sessions yet", confirm: "Confirm", cancel: "Cancel",
  rename: "Rename", delete: "Delete", light: "Light", dark: "Dark", expand: "Expand sidebar", collapse: "Collapse sidebar",
  loading: "Loading...", configured: "Configured", saving: "Saving...", savedTo: "Saved to", off: "Off",
  homeTitle: "AI-Powered Quant Strategy Research", homeSubtitle: "Describe a trading strategy in natural language. The agent generates code, runs backtests, and optimizes, all in real time.",
  startResearch: "Start Research", aiAgent: "AI Agent", aiAgentDesc: "Natural language strategy generation with ReAct reasoning",
  backtest: "Built-in Backtest", backtestDesc: "7 data sources across A-shares, US/HK and crypto", streaming: "Real-time Streaming",
  streamingDesc: "Watch the agent think, call tools, and iterate", replay: "Strategy Replay",
  replayDesc: "Analyze trade journals and build a Shadow Account to extract rules, backtest, and attribute PnL delta",
  settingsSubtitle: "Configure model credentials and market data source tokens for this local project.", unavailable: "Settings are unavailable",
  localAccess: "Local API access", localAccessDesc: "For remote or private deployments, save the server API key in this browser. Localhost use can stay blank.",
  serverApiKey: "Server API key", browserOnly: "Stored only in this browser. Leave blank to clear it.", saveLocalKey: "Save local key",
  localKeySaved: "Local API key saved", llmSettings: "LLM Settings", llmDesc: "Choose the model used by the agent and save it to the project-local agent/.env file.",
  connection: "Connection", provider: "Provider", providerHint: "Changing providers updates the recommended model and endpoint.", model: "Model",
  useDefaults: "Use provider defaults", modelHint: "Use the exact model ID required by your provider.", baseUrl: "Base URL", apiKey: "API key",
  keepKey: "Leave blank to keep the current key", noKey: "This provider does not require an API key.", oauth: "This provider uses OAuth. Run: {command}",
  clearApiKey: "Clear saved API key", generation: "Generation", temperature: "Temperature", timeout: "Timeout seconds", maxRetries: "Max retries",
  reasoning: "Reasoning effort", reasoningHint: "Higher effort is more thorough but slower; leave it off for the fastest replies.",
  saveSettings: "Save settings", settingsSaved: "LLM settings saved", dataSettings: "Data Source Settings",
  dataDesc: "Configure optional market data credentials used by backtests and research agents.", tushareToken: "Tushare token",
  keepToken: "Leave blank to keep the current token", tushareHint: "Used for China A-share, futures, fund, and macro data. If unset, the project falls back to AKShare where available.",
  clearTushare: "Clear saved Tushare token", saveData: "Save data source settings", dataSaved: "Data source settings saved",
  loaderAvailable: "Loader available", noLoader: "No project loader", packageInstalled: "Python package installed", packageNotInstalled: "Python package not installed",
  assetCodes: "Asset codes", assetHint: "Comma-separated ticker symbols, e.g. BTC-USDT,ETH-USDT,AAPL,SPY", windowDays: "Window (days)",
  method: "Method", compute: "Compute", strategyComparison: "Strategy Comparison", baseline: "Baseline", compare: "Compare", select: "-- Select --",
  equityDrawdown: "Equity & Drawdown", metric: "Metric", delta: "Delta", compareEmpty: "Select two runs to compare their metrics.",
};

type Key = keyof typeof zh;
type Context = { language: Language; toggleLanguage: () => void; t: (key: Key, values?: Record<string, string | number>) => string };
const I18nContext = createContext<Context | null>(null);

export function I18nProvider({ children }: { children: ReactNode }) {
  const [language, setLanguage] = useState<Language>(() => localStorage.getItem("vibe-trading-language") === "en" ? "en" : "zh-CN");
  useEffect(() => {
    localStorage.setItem("vibe-trading-language", language);
    document.documentElement.lang = language;
  }, [language]);
  const value = useMemo<Context>(() => ({
    language,
    toggleLanguage: () => setLanguage((current) => current === "zh-CN" ? "en" : "zh-CN"),
    t: (key, values) => {
      let text = (language === "zh-CN" ? zh[key] : en[key]) as string;
      for (const [name, value] of Object.entries(values ?? {})) text = text.split(`{${name}}`).join(String(value));
      return text;
    },
  }), [language]);
  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

export function useI18n() {
  const value = useContext(I18nContext);
  return value ?? {
    language: "en",
    toggleLanguage: () => {},
    t: (key: Key, values?: Record<string, string | number>) => {
      let text = en[key];
      for (const [name, replacement] of Object.entries(values ?? {})) text = text.split(`{${name}}`).join(String(replacement));
      return text;
    },
  };
}
