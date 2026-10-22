export type HealthCheckStatus = 'ok' | 'not_configured' | 'not_checked';

export interface HealthCheck {
  status: HealthCheckStatus;
  detail: string;
}

export interface HealthChecks {
  api: HealthCheck;
  database: HealthCheck;
  model: HealthCheck;
  workflow: HealthCheck;
}

export interface HealthResponse {
  status: 'ok';
  service: string;
  version: string;
  environment: string;
  checked_at: string;
  message: string;
  checks: HealthChecks;
}

const CHECK_STATUSES: ReadonlySet<string> = new Set([
  'ok',
  'not_configured',
  'not_checked',
]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}

function isHealthCheck(value: unknown): value is HealthCheck {
  return (
    isRecord(value) &&
    typeof value.status === 'string' &&
    CHECK_STATUSES.has(value.status) &&
    typeof value.detail === 'string'
  );
}

function isHealthResponse(value: unknown): value is HealthResponse {
  if (!isRecord(value) || !isRecord(value.checks)) {
    return false;
  }

  return (
    value.status === 'ok' &&
    typeof value.service === 'string' &&
    typeof value.version === 'string' &&
    typeof value.environment === 'string' &&
    typeof value.checked_at === 'string' &&
    typeof value.message === 'string' &&
    isHealthCheck(value.checks.api) &&
    isHealthCheck(value.checks.database) &&
    isHealthCheck(value.checks.model) &&
    isHealthCheck(value.checks.workflow)
  );
}

export async function fetchHealth(signal?: AbortSignal): Promise<HealthResponse> {
  const response = await fetch('/api/health', {
    signal,
    headers: { Accept: 'application/json' },
  });

  if (!response.ok) {
    throw new Error(`健康检查失败（HTTP ${response.status}）`);
  }

  const payload: unknown = await response.json();
  if (!isHealthResponse(payload)) {
    throw new Error('健康检查响应不符合预期结构');
  }

  return payload;
}
