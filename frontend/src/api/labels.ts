/** 对话与确认界面的显示文案。 */

export const SESSION_STATUS_LABELS: Record<string, string> = {
  created: "已创建",
  read_only: "历史只读",
  running: "运行中",
  paused: "等待决定",
  awaiting_input: "等待补充",
  idle: "空闲",
  failed: "失败",
  cancelled: "已取消",
};

export const ROLE_LABELS: Record<string, string> = {
  supervisor: "协调",
  searcher: "采集",
  analysist: "分析",
  writer: "写作",
  reviewer: "审核",
};

export const REPORT_TYPE_LABELS: Record<string, string> = {
  company_report: "公司报告",
  industry_report: "行业报告",
  strategy_report: "策略报告",
  flash_comment: "快评",
};

export const TOOL_LABELS: Record<string, string> = {
  confirm_plan: "确认研究计划",
  confirm_analysis: "确认分析结果",
  confirm_rework: "确认返工方案",
  signoff_report: "签署报告",
};

export const STAGE_LABELS: Record<string, string> = {
  plan: "规划",
  collect: "采集",
  analyze: "分析",
  write: "写作",
  review: "审核",
};

export const STAGE_RUNNING_LABELS: Record<string, string> = {
  plan: "正在规划任务",
  collect: "正在获取数据",
  analyze: "正在分析数据",
  write: "正在撰写报告",
  review: "正在审核报告",
};

export const STAGE_WAITING_LABELS: Record<string, string> = {
  plan: "等待确认计划",
  collect: "等待开始采集",
  analyze: "等待确认分析",
  write: "等待开始写作",
  review: "等待签核",
};

export const SOURCE_LABELS: Record<string, string> = {
  financekb: "FinanceKB",
  financekb_market: "FinanceKB 行情",
  financekb_fundamentals: "FinanceKB 财务",
  financekb_macro: "FinanceKB 宏观",
  financekb_news: "FinanceKB 资讯",
  financekb_knowledge: "FinanceKB 研报/政策",
  // Historical sources remain labelled so old reports stay readable.
  cnequity: "CNEquity",
  tickdb: "TickDB",
  tavily: "Tavily",
  deepseek_search: "DeepSeek 搜索",
  qc: "质量",
  compliance: "合规",
  human: "人工",
};

export const EVIDENCE_KIND_LABELS: Record<string, string> = {
  financial_data: "结构化数据",
  search_snippet: "检索片段",
  original_text: "原文",
  attachment_original: "附件",
  // Historical evidence keeps its original kind label.
  web: "网页",
};

export const SEVERITY_LABELS: Record<string, string> = {
  critical: "严重",
  major: "重要",
  minor: "次要",
  high: "高",
  medium: "中",
  low: "低",
};

export const EXPERIENCE_STAGE_LABELS: Record<string, string> = {
  collect: "采集阶段",
  analysis: "分析阶段",
  writing: "写作阶段",
};

export const PROBLEM_TYPE_LABELS: Record<string, string> = {
  source_quality: "来源与证据质量",
  numeric_accuracy: "数字、单位与期间",
  logic_consistency: "逻辑与一致性",
  structure_format: "结构与格式",
  disclosure_compliance: "披露与合规",
  scope_completeness: "范围与完整性",
  valuation_method: "估值方法与假设",
  chart_presentation: "图表呈现",
  writing_quality: "语言与表达",
};

export const RETROSPECTIVE_STATUS_LABELS: Record<string, string> = {
  pending: "等待复盘",
  running: "正在复盘",
  completed: "复盘完成",
  failed: "复盘失败",
  not_scheduled: "尚未开始",
};

export function clockTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const match = /T(\d{2}:\d{2}:\d{2})/.exec(iso);
  return match ? match[1] : iso;
}
