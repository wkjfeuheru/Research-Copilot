import type { WorkspaceResponse } from '../api/client';
import {
  RETROSPECTIVE_STATUS_LABELS,
  ROLE_LABELS,
  SEVERITY_LABELS,
} from '../api/labels';
import styles from './OverviewPanel.module.css';

const ROLE_ORDER = ['searcher', 'analysist', 'writer', 'reviewer'] as const;

export function OverviewPanel({ data }: { data: WorkspaceResponse }) {
  const plan = data.plan;
  const analysis = data.analysis;
  const review = data.review;

  return (
    <div className={styles.overview}>
      {data.budget && (
        <section className={styles.panel} aria-label="预算与审核">
          <h2>运行预算与审核</h2>
          <p>
            模型调用 {data.budget.calls} / {data.budget.call_limit ?? '未设置'} · 实际用量{' '}
            {data.budget.known_actual_tokens.toLocaleString()} tokens · 剩余额度{' '}
            {data.budget.remaining_tokens.toLocaleString()} tokens · 待结算预留{' '}
            {data.budget.reserved_tokens.toLocaleString()} tokens
          </p>
          {!data.budget.usage_verified && (
            <p role="status">
              用量尚未核实：未知占额 {data.budget.unknown_reserved_tokens.toLocaleString()} tokens；不能据此确认成本验收通过。
            </p>
          )}
          {data.budget.soft_warning && <p role="status">预算已接近或达到上限。</p>}
          <p>
            费用估算：{data.budget.estimated_cost ?? '未配置价格，未估算'}
            {data.budget.estimated_cost && !data.budget.cost_complete && '（部分请求未估算）'}
          </p>
          <dl className={styles.stats}>
            {Object.entries(data.budget.stages).map(([role, usage]) => (
              <div key={role}>
                <dt>{ROLE_LABELS[role] ?? role}</dt>
                <dd>
                  {usage.calls} 次 · {usage.known_actual_tokens.toLocaleString()} tokens
                  {data.budget?.stage_remaining[role] && (
                    <small>
                      {' '}
                      · 剩余 {data.budget.stage_remaining[role].calls} 次 /{' '}
                      {data.budget.stage_remaining[role].tokens.toLocaleString()} tokens
                    </small>
                  )}
                </dd>
              </div>
            ))}
          </dl>
          <p>
            {review?.checks_complete ? '审核检查完整' : '审核尚未完整完成'} · {data.signed ? '已签署' : '未签署'}
            {data.signoff_ready ? ' · 可以签署' : ''}
          </p>
        </section>
      )}

      <dl className={styles.stats}>
        <div>
          <dt>证据</dt>
          <dd>{data.evidence_count}</dd>
        </div>
        <div>
          <dt>分析缺口</dt>
          <dd>{analysis?.gaps.length ?? 0}</dd>
        </div>
        <div>
          <dt>审核问题</dt>
          <dd>{review?.issues.length ?? 0}</dd>
        </div>
        <div>
          <dt>返工</dt>
          <dd>{data.rework ? data.rework.phase ?? '进行中' : '—'}</dd>
        </div>
        <div>
          <dt>经验复盘</dt>
          <dd>
            {RETROSPECTIVE_STATUS_LABELS[data.retrospective?.status ?? 'not_scheduled'] ??
              data.retrospective?.status ??
              '尚未开始'}
          </dd>
        </div>
      </dl>

      {plan && (
        <section className={styles.panel}>
          <header className={styles.panelHead}>
            <h2>研究计划</h2>
            <span className="meta">
              v{plan.version}
              {plan.approved ? ' · 已批准' : ' · 未批准'}
            </span>
          </header>
          <p>{plan.objective}</p>
          <div className={styles.taskGroups}>
            {ROLE_ORDER.map((role) => {
              const items = plan.tasks.filter((task) => task.role === role);
              return (
                <div key={role} className={styles.taskGroup}>
                  <h3>
                    {ROLE_LABELS[role] ?? role}
                    <span>{items.length}</span>
                  </h3>
                  {items.map((task) => (
                    <div key={task.task_id} className={styles.taskRow}>
                      <code>{task.task_id}</code>
                      <span>{task.objective}</span>
                    </div>
                  ))}
                </div>
              );
            })}
          </div>
        </section>
      )}

      {analysis && (analysis.summary || analysis.conclusions.length > 0) && (
        <section className={styles.panel}>
          <h2>分析结果{analysis.version ? ` · v${analysis.version}` : ''}</h2>
          {analysis.summary && <p className={styles.preWrap}>{analysis.summary}</p>}
          {analysis.conclusions.length > 0 && (
            <ul className={styles.list}>
              {analysis.conclusions.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          )}
          {analysis.assumptions.length > 0 && (
            <p className={styles.limit}>假设：{analysis.assumptions.join('；')}</p>
          )}
          {analysis.limitations.length > 0 && (
            <p className={styles.limit}>限制：{analysis.limitations.join('；')}</p>
          )}
        </section>
      )}

      {review && review.issues.length > 0 && (
        <section className={styles.panel}>
          <h2>审核问题</h2>
          <ul className={styles.list}>
            {review.issues.map((issue) => (
              <li key={`${issue.location}-${issue.reason}`}>
                {SEVERITY_LABELS[issue.severity] ?? issue.severity} · {issue.location} · {issue.reason}
              </li>
            ))}
          </ul>
        </section>
      )}

      {data.rework && data.rework.tasks.length > 0 && (
        <section className={styles.panel}>
          <h2>返工</h2>
          {data.rework.tasks.map((task) => (
            <p key={`${task.role}-${task.objective}`}>
              <code>{ROLE_LABELS[task.role] ?? task.role}</code> {task.objective}
            </p>
          ))}
        </section>
      )}
    </div>
  );
}
