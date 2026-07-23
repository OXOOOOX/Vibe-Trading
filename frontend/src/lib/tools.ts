/**
 * Single source of truth for tool name → user-facing label.
 */
export const TOOL_LABELS: Record<string, string> = {
  load_skill: "加载研究方法",
  write_file: "生成文件",
  edit_file: "更新文件",
  read_file: "读取文件",
  run_backtest: "运行策略回测",
  bash: "执行数据处理",
  web_search: "检索公开资料",
  read_url: "阅读网页资料",
  read_research_document: "读取研究原文片段",
  query_research_knowledge: "查询历史研究知识",
  read_webpage: "阅读网页资料",
  read_document: "阅读研究资料",
  search_symbol: "确认股票代码",
  get_data_context: "整理市场与公司数据",
  analyze_financial_snapshot: "核对三张财务报表",
  record_report_evidence: "登记研究依据",
  financial_rigor: "执行财务与估值计算",
  report_workspace: "整理报告章节",
  weekly_report: "创建正式周报",
  trading_connections: "查看交易账户连接",
  trading_select_connection: "选择交易账户",
  trading_check: "检查交易账户连接",
  trading_account: "读取账户概况",
  trading_positions: "读取当前持仓",
  trading_orders: "读取委托记录",
  trading_quote: "读取最新行情",
  trading_history: "读取历史交易",
  compact: "整理对话上下文",
  create_task: "创建研究任务",
  update_task: "更新研究任务",
  spawn_subagent: "分配并行研究任务",
};

export function localizeToolName(tool: string, fallback?: string): string {
  if (tool in TOOL_LABELS) {
    return TOOL_LABELS[tool];
  }
  if (fallback !== undefined) {
    return fallback;
  }
  return "执行研究步骤";
}

const TOOL_STAGE_LABELS: Record<string, string> = {
  queued: "等待开始",
  fetching: "获取资料",
  searching: "检索资料",
  reading: "阅读资料",
  normalizing: "统一数据口径",
  calculating: "核对与计算",
  validating: "校验结果",
  compiling: "整理报告",
  writing: "撰写内容",
  completed: "已完成",
};

export function localizeToolStage(stage: string): string {
  const normalized = stage.trim().toLowerCase().replace(/[\s-]+/g, "_");
  return TOOL_STAGE_LABELS[normalized] || "处理中";
}

export function localizeToolProgressMessage(message: string, tool: string): string {
  const value = message.trim();
  if (!value) return "";
  if (/^(?:GET|POST|PUT|PATCH|DELETE)\s+https?:\/\//i.test(value) || /^https?:\/\//i.test(value)) {
    return tool === "web_search" ? "正在检索相关公开资料" : "正在读取所需资料";
  }
  if (/^[A-Za-z][A-Za-z0-9 _./:\-]{8,}$/.test(value)) {
    return "正在处理当前研究步骤";
  }
  return value;
}
