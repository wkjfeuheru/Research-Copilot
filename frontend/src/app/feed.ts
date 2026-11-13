/** User-visible progress text from the current authoritative snapshot. */
import type { WorkspaceResponse } from "../api/client";
import { ROLE_LABELS, SESSION_STATUS_LABELS, TOOL_LABELS } from "../api/labels";

export function describeWorkspace(data: WorkspaceResponse): string {
  if (data.read_only) return "历史会话只读";
  if (data.waiting_for === "clarification") return "等待补充研究信息";
  if (data.runtime.last_error) {
    return `运行失败：${data.runtime.last_error}`;
  }
  if (data.report.status === "cancelled") {
    return "会话已取消";
  }
  if (data.signed || data.workflow.phase === "complete") {
    return "报告已签署";
  }
  if (data.workflow.phase === "blocked") {
    return data.workflow.reason || "工作流已停止";
  }
  const interrupt = data.interrupts[0];
  if (interrupt?.kind === "reviewer_deep_review") {
    return "等待审核决定";
  }
  if (interrupt?.tool) {
    return `等待${TOOL_LABELS[interrupt.tool] ?? interrupt.tool}`;
  }
  if (data.runtime.busy) {
    const role = data.workflow.role ? ROLE_LABELS[data.workflow.role] : "";
    return role ? `正在${role}` : "正在运行";
  }
  return SESSION_STATUS_LABELS[data.report.status] ?? data.report.status;
}
