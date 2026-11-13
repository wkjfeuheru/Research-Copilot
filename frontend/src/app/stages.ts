/** 阶段模型：把 工作流阶段收敛成五段进度。 */

import type { WorkspaceResponse } from "../api/client";
import {
  STAGE_LABELS,
  STAGE_RUNNING_LABELS,
  STAGE_WAITING_LABELS,
  TOOL_LABELS,
} from "../api/labels";

export type StageKey = "plan" | "collect" | "analyze" | "write" | "review";

export interface StageDef {
  key: StageKey;
  index: number;
  label: string;
}

export const STAGES: StageDef[] = [
  { key: "plan", index: 1, label: STAGE_LABELS.plan },
  { key: "collect", index: 2, label: STAGE_LABELS.collect },
  { key: "analyze", index: 3, label: STAGE_LABELS.analyze },
  { key: "write", index: 4, label: STAGE_LABELS.write },
  { key: "review", index: 5, label: STAGE_LABELS.review },
];

export type StageState = "done" | "active" | "waiting" | "pending";

export interface StageView extends StageDef {
  state: StageState;
  message: string | null;
}

const ROLE_STAGE: Record<string, StageKey> = {
  searcher: "collect",
  analysist: "analyze",
  writer: "write",
  reviewer: "review",
};

const ACTION_STAGE: Record<string, StageKey> = {
  confirm_plan: "plan",
  propose_plan: "plan",
  propose_rework: "review",
  confirm_analysis: "analyze",
  confirm_rework: "review",
  signoff_report: "review",
  task: "collect",
};

export function stageForWorkspace(data: WorkspaceResponse): StageKey | null {
  if (data.signed || data.workflow.phase === "complete") return "review";
  const action = data.workflow.next_action;
  const role = data.workflow.role;
  if (role && ROLE_STAGE[role]) return ROLE_STAGE[role];
  if (action && ACTION_STAGE[action]) return ACTION_STAGE[action];
  const interrupt = data.interrupts.find((item) => item.tool)?.tool;
  if (interrupt && ACTION_STAGE[interrupt]) return ACTION_STAGE[interrupt];
  if (data.workflow.phase === "plan") return "plan";
  if (data.workflow.phase === "blocked") {
    if (data.report_document) return "review";
    if (data.analysis?.approved) return "write";
    if (data.analysis) return "analyze";
    if (data.plan?.approved) return "collect";
    return "plan";
  }
  return data.report.status === "created" ? "plan" : null;
}

export function resolveStages(data: WorkspaceResponse): StageView[] {
  if (data.read_only) return STAGES.map(stage => ({ ...stage, state: "pending", message: "历史运行进度不可用" }));
  if (data.workflow_version === "products-v2") {
    return STAGES.map(stage => {
      const tasks = (data.tasks ?? []).filter(task => ROLE_STAGE[task.role] === stage.key);
      const last = tasks.at(-1);
      const state: StageState = data.signed ? "done"
        : stage.key === "plan" ? (data.report_scope ? "done" : "active")
        : last?.status === "in_progress" ? (data.runtime.busy ? "active" : "waiting")
        : last && ["completed", "passed"].includes(last.status) ? "done"
        : last ? "waiting" : "pending";
      return { ...stage, state, message: data.read_only ? "历史会话只读" : null };
    });
  }
  if (data.signed || data.workflow.phase === "complete") {
    return STAGES.map((stage) => ({ ...stage, state: "done", message: null }));
  }
  const currentKey = stageForWorkspace(data) ?? "plan";
  const currentIndex =
    STAGES.find((stage) => stage.key === currentKey)?.index ?? 1;
  const waiting = data.interrupts[0];
  const waitingMessage = waiting
    ? `等待${TOOL_LABELS[waiting.tool ?? ""] ?? (waiting.kind === "reviewer_deep_review" ? "审核决定" : "决定")}`
    : undefined;
  return STAGES.map((stage) => {
    if (stage.index < currentIndex) {
      return { ...stage, state: "done", message: null };
    }
    if (stage.index > currentIndex) {
      return { ...stage, state: "pending", message: null };
    }
    if (data.report.status === "cancelled") {
      return { ...stage, state: "waiting", message: "已取消" };
    }
    if (data.workflow.phase === "blocked") {
      return {
        ...stage,
        state: "waiting",
        message: data.workflow.reason || "已停止",
      };
    }
    if (data.runtime.busy) {
      return {
        ...stage,
        state: "active",
        message: STAGE_RUNNING_LABELS[stage.key],
      };
    }
    return {
      ...stage,
      state: "waiting",
      message: waitingMessage ?? STAGE_WAITING_LABELS[stage.key],
    };
  });
}
