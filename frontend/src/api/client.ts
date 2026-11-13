/** 工作台 API。决定原样交给会话校验，不另做一套批准语义。 */

export interface ReportSummary {
  read_only?: boolean;
  id: string;
  status: string;
  topic: string | null;
  report_type: string | null;
  report_skill: string | null;
  current_version: number;
  user_request: string;
  created_at: string | null;
  updated_at: string | null;
}

export interface ReportsResponse {
  reports: ReportSummary[];
  active_report_id: string | null;
}

export interface ActionSpec {
  dispatch_call_id?: string;
  key: string;
  label: string;
  description: string;
  kind: "primary" | "cost" | "danger";
  requires_confirmation: boolean;
}

export interface DeleteReportResponse {
  deleted: boolean;
  report_id: string;
  cleanup: {
    checkpoints: boolean;
    workspace: boolean;
    errors: string[];
  };
}

export interface ArtifactView {
  artifact_id: string;
  version: string;
  content_hash: string;
}

export interface PlanTaskView {
  task_id: string;
  role: string;
  objective: string;
}

export interface PlanView {
  report_type: string | null;
  objective: string;
  version: string;
  content_hash: string;
  approved: boolean;
  tasks: PlanTaskView[];
}

export interface AnalysisView {
  version: string | null;
  content_hash: string | null;
  approved: boolean;
  summary: string;
  conclusions: string[];
  assumptions: string[];
  limitations: string[];
  gaps: string[];
}

export interface ReviewIssueView {
  issue_id: string;
  hard?: boolean;
  category: string;
  severity: string;
  location: string;
  quote: string;
  reason: string;
  suggestion: string;
}

export interface ReviewView {
  outcome: string | null;
  checks_complete: boolean;
  pass_confidence: number | null;
  issues: ReviewIssueView[];
}

export interface ReworkView {
  phase: string | null;
  version: string | null;
  tasks: { role: string; objective: string; issue_ids: string[] }[];
}

export interface RetrospectiveView {
  status: string;
  version: string | null;
  attempts: number;
  experience_count: number;
  last_error: string | null;
  completed_at: string | null;
  produced_count: number;
  job_count: number;
}

export interface RetrospectiveJobView {
  id: string;
  report_id: string;
  report_version: string;
  status: string;
  attempts: number;
  max_attempts: number;
  available_at: string | null;
  locked_until: string | null;
  last_error: string | null;
  experience_count: number;
  usage: Record<string, unknown>;
  recovery_revision?: number;
  created_at: string | null;
  updated_at: string | null;
  completed_at: string | null;
}

export interface ExperienceUsageView {
  dispatch_id: string;
  role: string;
  stage: string;
  recalled_at: string | null;
  adopted: boolean;
  adopted_at: string | null;
}

export interface ExperienceView {
  experience_id: string;
  role: string;
  stage: string;
  problem_type: string;
  applies_when: string;
  recommendation: string;
  evidence_refs: string[];
  source_report_id: string;
  source_report_version: string;
  enabled: boolean;
  disabled_at: string | null;
  disabled_by: string | null;
  created_at: string | null;
  usage?: ExperienceUsageView;
  usage_summary?: { recalled: number; adopted: number };
}

export interface ReportExperienceResponse {
  report_id: string;
  jobs: RetrospectiveJobView[];
  received: ExperienceView[];
  produced: ExperienceView[];
}

export interface BudgetView {
  calls: number;
  call_limit: number | null;
  known_actual_tokens: number;
  unknown_reserved_tokens: number;
  remaining_tokens: number;
  reserved_tokens: number;
  cost_complete: boolean;
  stage_remaining: Record<string, { calls: number; tokens: number }>;
  soft_warning: boolean;
  usage_verified: boolean;
  estimated_cost: string | null;
  stages: Record<
    string,
    {
      calls: number;
      known_actual_tokens: number;
      unknown_reserved_tokens: number;
    }
  >;
}

export interface ToolActionView {
  name: string | null;
  artifact: ArtifactView | null;
}

export interface InterruptView {
  id: string;
  kind: "tool" | "reviewer_deep_review" | "clarification";
  question?: string;
  tool: string | null;
  artifact: ArtifactView | null;
  actions: ToolActionView[];
  review: {
    review_session_id?: string | null;
    snapshot_id?: string | null;
    checks_complete?: boolean | null;
    allowed_decisions: string[];
    summary: string;
    issues: ReviewIssueView[];
    errors: string[];
  } | null;
}

export interface WorkflowView {
  version: string | null;
  phase: string | null;
  next_action: string | null;
  role: string | null;
  reason: string | null;
}

export interface WorkspaceResponse {
  workflow_version?: string | null;
  read_only?: boolean;
  report_scope?: { objective: string; report_type: string; as_of: string } | null;
  todos?: { content: string; status: "pending" | "in_progress" | "completed" }[];
  tasks?: { issue_ids?: string[]; task_id: string; dispatch_id: string; role: string; objective: string; status: string }[];
  waiting_for?: "clarification" | "reviewer_deep_review" | "signoff" | "recovery" | null;
  dispatch_count?: number;
  dispatch_remaining?: number;
  messages?: {
    id: string;
    role: "user" | "assistant";
    content: string;
    kind: string;
    created_at: string;
  }[];
  activity?: { id: string; role: string; event: string }[];
  report: {
    id: string;
    user_request: string;
    status: string;
    created_at: string | null;
  };
  runtime: {
    busy: boolean;
    queue_state?: "pending" | "claimed" | "running" | "succeeded" | "failed" | "cancelled" | "recovery_required" | null;
    queue_job_id?: string | null;
    recoverable: boolean;
    last_error: string | null;
    error_detail?: string | null;
  };
  workflow: WorkflowView;
  budget: BudgetView | null;
  signoff_ready: boolean;
  signed: boolean;
  plan: PlanView | null;
  analysis: AnalysisView | null;
  review: ReviewView | null;
  rework: ReworkView | null;
  retrospective: RetrospectiveView | null;
  evidence_count: number;
  report_document: { version: string; frozen: boolean } | null;
  interrupts: InterruptView[];
  actions: ActionSpec[];
}

export interface EvidenceListItem {
  evidence_id: string;
  task_id: string;
  source: string;
  title: string;
  summary: string;
  content_kind: string;
  content_hash: string;
  collected_at: string | null;
  published_at: string | null;
  collection: string;
  page: string | number | null;
  heading_path: string;
  broker: string;
  report_type: string;
}

export interface EvidencePage {
  total: number;
  offset: number;
  limit: number;
  next_offset: number | null;
  records: EvidenceListItem[];
}

export interface EvidenceDetailResponse {
  evidence: EvidenceListItem;
  text: string;
  offset: number;
  limit: number;
  total_length: number;
  truncated: boolean;
  next_offset: number | null;
}

export interface ReportTextResponse {
  version: string | null;
  is_frozen: boolean;
  file_path: string | null;
  content: string;
  content_hash: string;
  manifest_hash: string | null;
}

export interface ReportChartResponse {
  report_id: string;
  report_version: string;
  chart_id: string;
  engine: "echarts" | "matplotlib";
  title: string;
  alt: string;
  content_hash: string;
  static_url: string;
  option_url: string | null;
  theme_version: string;
}

export function chartUrl(
  reportId: string,
  version: string,
  chartId: string,
): string {
  return `/api/reports/${encodeURIComponent(reportId)}/versions/${encodeURIComponent(version)}/charts/${encodeURIComponent(chartId)}`;
}

export function fetchReportChart(
  reportId: string,
  version: string,
  chartId: string,
  signal: AbortSignal,
): Promise<ReportChartResponse> {
  return request<ReportChartResponse>(chartUrl(reportId, version, chartId), {
    signal,
  });
}

export class ApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { Accept: "application/json", ...init?.headers },
  });
  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (typeof body.detail === "string") {
        detail = body.detail;
      }
    } catch {
      // 非 JSON 错误体；保留 HTTP 状态码消息。
    }
    throw new ApiError(response.status, detail);
  }
  return (await response.json()) as T;
}

export function fetchReports(signal?: AbortSignal): Promise<ReportsResponse> {
  return request<ReportsResponse>("/api/reports", { signal });
}

export interface ServiceHealth {
  status: "ok" | "degraded";
  message: string;
  sources?: {
    ready: boolean;
    available_domains: number;
    total_domains: number;
    checked_at: string | null;
    detail: string;
  };
}

export function fetchServiceHealth(signal?: AbortSignal): Promise<ServiceHealth> {
  return request<ServiceHealth>("/api/health", { signal });
}

export async function createReport(
  userRequest: string,
  origin: string,
  storage: Storage = window.sessionStorage,
): Promise<{ report: ReportSummary }> {
  // Keep the retry identity across a reload without storing the research question.
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(userRequest));
  const fingerprint = Array.from(new Uint8Array(digest), (n) => n.toString(16).padStart(2, "0")).join("");
  const slot = `rc-create:${origin}`;
  const saved = storage.getItem(slot)?.split(":");
  const requestKey = saved?.[0] === fingerprint ? saved[1] : crypto.randomUUID();
  const intent = `${fingerprint}:${requestKey}`;
  storage.setItem(slot, intent);
  const response = await request<{ report: ReportSummary }>("/api/reports", {
    method: "POST",
    headers: { "Content-Type": "application/json", "Idempotency-Key": requestKey },
    body: JSON.stringify({ user_request: userRequest }),
  });
  if (storage.getItem(slot) === intent) storage.removeItem(slot);
  return response;
}

export function fetchStatus(
  reportId: string,
  signal?: AbortSignal,
): Promise<WorkspaceResponse> {
  return request<WorkspaceResponse>(
    `/api/reports/${encodeURIComponent(reportId)}`,
    { signal },
  );
}

export function deleteReport(reportId: string): Promise<DeleteReportResponse> {
  return request<DeleteReportResponse>(
    `/api/reports/${encodeURIComponent(reportId)}`,
    {
      method: "DELETE",
    },
  );
}

export function submitDecisions(
  reportId: string,
  decisions: Record<string, unknown>,
): Promise<{ action: string; state: string }> {
  return request(`/api/reports/${encodeURIComponent(reportId)}/decisions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ decisions }),
  });
}

export function correctReport(reportId: string, dispatchId: string) {
  return request(`/api/reports/${encodeURIComponent(reportId)}/correct`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ dispatch_call_id: dispatchId }),
  });
}

export function resumeReport(
  reportId: string,
): Promise<{ action: string; state: string }> {
  return request(`/api/reports/${encodeURIComponent(reportId)}/resume`, {
    method: "POST",
  });
}

export function sendMessage(
  reportId: string,
  content: string,
  id: string,
): Promise<{ action: string; state: string }> {
  return request(`/api/reports/${encodeURIComponent(reportId)}/messages`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content, id }),
  });
}

export function cancelReport(
  reportId: string,
): Promise<{ action: string; state: string }> {
  return request(`/api/reports/${encodeURIComponent(reportId)}/cancel`, {
    method: "POST",
  });
}

export function fetchEvidence(
  reportId: string,
  params: { taskId?: string; offset?: number; limit?: number } = {},
  signal?: AbortSignal,
): Promise<EvidencePage> {
  const query = new URLSearchParams();
  if (params.taskId) query.set("task_id", params.taskId);
  if (params.offset !== undefined) query.set("offset", String(params.offset));
  if (params.limit !== undefined) query.set("limit", String(params.limit));
  return request<EvidencePage>(
    `/api/reports/${encodeURIComponent(reportId)}/evidence?${query.toString()}`,
    { signal },
  );
}

export function fetchEvidenceDetail(
  reportId: string,
  evidenceId: string,
  signal?: AbortSignal,
): Promise<EvidenceDetailResponse> {
  return request<EvidenceDetailResponse>(
    `/api/reports/${encodeURIComponent(reportId)}/evidence/${encodeURIComponent(evidenceId)}`,
    { signal },
  );
}

export function fetchReportText(
  reportId: string,
  version?: string,
  signal?: AbortSignal,
): Promise<ReportTextResponse> {
  const suffix = version ? `?version=${encodeURIComponent(version)}` : "";
  return request<ReportTextResponse>(
    `/api/reports/${encodeURIComponent(reportId)}/report${suffix}`,
    { signal },
  );
}

export function fetchReportExperiences(
  reportId: string,
  signal?: AbortSignal,
): Promise<ReportExperienceResponse> {
  return request<ReportExperienceResponse>(
    `/api/reports/${encodeURIComponent(reportId)}/experiences`,
    { signal },
  );
}

export interface RetrospectiveRecoveryRequest {
  action: 'reconcile' | 'authorize_retry';
  operation_id: string;
  expected_revision: number;
  note: string;
  attempt_id?: string;
  input_tokens?: number;
  output_tokens?: number;
  acknowledge_duplicate_charge?: boolean;
}

export interface RunRecoveryRequestView {
  id: string; pending: boolean; sent: boolean | null; stage: string | null;
  usage: number | null; reserved: number; released: boolean | null;
  operation_id: string | null; reconciled: boolean | null; usage_provisional: boolean;
  recovery: { note: string } | null; reconcile_details: { note: string } | null;
  retry_authorization: { note: string; consumed_by?: string } | null;
}
export interface RunReleaseReceipt { job_id: string; note: string; created_at: string; unresolved_requests: string[] }
export interface RunRecoveryView {
  command: { id: string; status: string; claim_token: string | null; worker_id: string | null; locked_until: string | null } | null;
  pending_requests: string[]; unresolved_requests: string[]; budget_revision: number;
  budget: BudgetView & { unknown_requests: number; charged_tokens: number };
  can_release: boolean; receipt: RunReleaseReceipt | null; requests: RunRecoveryRequestView[];
}
export interface RequestRecoveryDecision {
  action: 'resolve_pending' | 'reconcile' | 'authorize_retry'; expected_revision: number;
  owner_stopped: boolean; note: string; usage?: number; acknowledge_duplicate_charge?: boolean;
}
export interface RunReleaseDecision { claim_token: string; owner_stopped: boolean; note: string }
export function fetchRunRecovery(reportId: string, signal?: AbortSignal): Promise<RunRecoveryView> {
  return request(`/api/reports/${encodeURIComponent(reportId)}/run-recovery`, { signal });
}
export function recoverRunRequest(reportId: string, requestId: string, body: RequestRecoveryDecision) {
  return request(`/api/reports/${encodeURIComponent(reportId)}/requests/${encodeURIComponent(requestId)}/recovery`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  });
}
export function releaseRun(reportId: string, jobId: string, body: RunReleaseDecision): Promise<RunReleaseReceipt> {
  return request(`/api/reports/${encodeURIComponent(reportId)}/runs/${encodeURIComponent(jobId)}/release`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  });
}

export function recoverRetrospective(reportId: string, jobId: string, body: RetrospectiveRecoveryRequest): Promise<{ job: RetrospectiveJobView }> {
  return request(`/api/reports/${encodeURIComponent(reportId)}/retrospectives/${encodeURIComponent(jobId)}/recovery`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  });
}

export function setExperienceEnabled(
  experienceId: string,
  enabled: boolean,
): Promise<{ experience: ExperienceView }> {
  return request(`/api/experiences/${encodeURIComponent(experienceId)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled }),
  });
}
