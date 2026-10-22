import { useEffect, useState } from 'react';

import {
  fetchHealth,
  type HealthCheckStatus,
  type HealthResponse,
} from '../api/health';
import styles from './ServiceStatus.module.css';

type LoadState =
  | { kind: 'loading' }
  | { kind: 'ready'; health: HealthResponse }
  | { kind: 'error'; message: string };

const STATUS_LABELS: Record<HealthCheckStatus, string> = {
  ok: '可用',
  not_configured: '未配置',
  not_checked: '未验证',
};

const CHECK_LABELS = {
  database: '数据库',
  model: '模型',
  workflow: '研究链路',
} as const;

function describeError(error: unknown): string {
  return error instanceof Error ? error.message : '未知错误';
}

export function ServiceStatus() {
  const [attempt, setAttempt] = useState(0);
  const [state, setState] = useState<LoadState>({ kind: 'loading' });

  useEffect(() => {
    let active = true;
    const controller = new AbortController();
    setState({ kind: 'loading' });

    fetchHealth(controller.signal)
      .then((health) => {
        if (active) {
          setState({ kind: 'ready', health });
        }
      })
      .catch((error: unknown) => {
        if (!active || controller.signal.aborted) {
          return;
        }
        setState({ kind: 'error', message: describeError(error) });
      });

    return () => {
      active = false;
      controller.abort();
    };
  }, [attempt]);

  return (
    <section className={styles.panel} aria-labelledby="service-status-title">
      <div className={styles.headingRow}>
        <h3 id="service-status-title" className={styles.heading}>
          服务状态
        </h3>
        <button
          type="button"
          className={styles.retry}
          onClick={() => setAttempt((value) => value + 1)}
          disabled={state.kind === 'loading'}
        >
          重新检查
        </button>
      </div>

      {state.kind === 'loading' ? (
        <p className={styles.body} role="status">
          正在检查后端…
        </p>
      ) : null}

      {state.kind === 'error' ? (
        <div role="status">
          <p className={styles.error}>未连接后端：{state.message}</p>
          <p className={styles.body}>
            请确认 API 已在 8000 端口启动；开发代理配置见 README。
          </p>
        </div>
      ) : null}

      {state.kind === 'ready' ? (
        <div>
          <p className={styles.ready} role="status">
            <span className={styles.dot} aria-hidden="true" />
            API 进程可用
            <span className={styles.version}>v{state.health.version}</span>
          </p>
          <p className={styles.body}>
            仅表示后端进程可响应；数据库、模型与研究链路未接入。
          </p>
          <dl className={styles.checks}>
            {(['database', 'model', 'workflow'] as const).map((key) => {
              const check = state.health.checks[key];
              return (
                <div key={key} className={styles.checkRow}>
                  <dt>{CHECK_LABELS[key]}</dt>
                  <dd
                    className={
                      check.status === 'ok' ? styles.ok : styles.pending
                    }
                  >
                    {STATUS_LABELS[check.status]}
                  </dd>
                </div>
              );
            })}
          </dl>
        </div>
      ) : null}
    </section>
  );
}
