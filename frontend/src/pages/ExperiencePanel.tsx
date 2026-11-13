import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';

import {
  fetchReportExperiences,
  setExperienceEnabled,
  type ExperienceView,
  type ReportExperienceResponse,
} from '../api/client';
import {
  EXPERIENCE_STAGE_LABELS,
  PROBLEM_TYPE_LABELS,
  RETROSPECTIVE_STATUS_LABELS,
  ROLE_LABELS,
} from '../api/labels';
import { EmptyState } from '../components/EmptyState';
import styles from './ExperiencePanel.module.css';
import { RetrospectiveRecovery } from './RetrospectiveRecovery';

function shortId(value: string): string {
  return value.length > 12 ? `${value.slice(0, 8)}…${value.slice(-4)}` : value;
}

function ExperienceCard({
  experience,
  showUsage,
  busy,
  onToggle,
}: {
  experience: ExperienceView;
  showUsage: boolean;
  busy: boolean;
  onToggle: (experience: ExperienceView) => void;
}) {
  return (
    <article className={styles.card} aria-label="经验条目">
      <div className={styles.tags}>
        <span className={styles.tag}>{ROLE_LABELS[experience.role] ?? experience.role}</span>
        <span className={styles.tag}>
          {EXPERIENCE_STAGE_LABELS[experience.stage] ?? experience.stage}
        </span>
        <span className={styles.tag}>
          {PROBLEM_TYPE_LABELS[experience.problem_type] ?? experience.problem_type}
        </span>
        {showUsage && (
          <span className={experience.usage?.adopted ? styles.adopted : styles.recalled}>
            {experience.usage?.adopted ? '已采用' : '仅收到'}
          </span>
        )}
        {!experience.enabled && <span className={styles.disabled}>已停用</span>}
      </div>
      {!showUsage && experience.usage_summary && (
        <p className={styles.meta}>
          被 {experience.usage_summary.recalled} 次收到 ·{' '}
          {experience.usage_summary.adopted} 次采用
        </p>
      )}
      <p className={styles.text}>
        <strong>适用条件</strong>
        {experience.applies_when}
      </p>
      <p className={styles.text}>
        <strong>建议做法</strong>
        {experience.recommendation}
      </p>
      <p className={styles.source}>
        来源：
        <Link to={`/reports/${experience.source_report_id}`}>
          报告 {shortId(experience.source_report_id)}
        </Link>
        {' '}· v{experience.source_report_version}
        {experience.created_at && ` · ${experience.created_at.slice(0, 10)}`}
      </p>
      {experience.evidence_refs.length > 0 && (
        <details className={styles.refs}>
          <summary>过程依据 · {experience.evidence_refs.length}</summary>
          <ul>
            {experience.evidence_refs.map((ref) => (
              <li key={ref}>
                <code>{ref}</code>
              </li>
            ))}
          </ul>
        </details>
      )}
      <div className={styles.actions}>
        <button
          type="button"
          className={styles.toggle}
          disabled={busy}
          onClick={() => onToggle(experience)}
        >
          {experience.enabled ? '停用此经验' : '重新启用'}
        </button>
      </div>
    </article>
  );
}

export function ExperiencePanel({ reportId }: { reportId: string }) {
  const [data, setData] = useState<ReportExperienceResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    fetchReportExperiences(reportId, controller.signal)
      .then((response) => {
        setData(response);
        setError(null);
      })
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) {
          setError(reason instanceof Error ? reason.message : '经验复盘加载失败');
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [reportId, reloadKey]);

  useEffect(() => {
    if (!data) return;
    const active = data.jobs.some(
      (job) => job.status === 'pending' || job.status === 'running',
    );
    if (!active) return;
    const timer = window.setTimeout(
      () => setReloadKey((value) => value + 1),
      3000,
    );
    return () => window.clearTimeout(timer);
  }, [data]);

  async function toggle(experience: ExperienceView) {
    setBusyId(experience.experience_id);
    setActionError(null);
    try {
      await setExperienceEnabled(experience.experience_id, !experience.enabled);
      setReloadKey((value) => value + 1);
    } catch (reason) {
      setActionError(reason instanceof Error ? reason.message : '启用状态更新失败');
    } finally {
      setBusyId(null);
    }
  }

  if (error) {
    return (
      <EmptyState
        title="经验复盘加载失败"
        description={error}
        tone="error"
        action={
          <button
            type="button"
            className="btn"
            onClick={() => setReloadKey((value) => value + 1)}
          >
            重试
          </button>
        }
      />
    );
  }
  if (loading && !data) return <p className={styles.intro}>正在读取经验复盘…</p>;
  if (!data) return null;

  const received = data.received;
  const produced = data.produced;

  return (
    <div className={styles.panel} aria-label="经验复盘">
      <p className={styles.intro}>
        报告签署后登记复盘任务。经验只作为后续同类任务的带来源参考，不会自动修改研究结论；停用后立即停止召回。
      </p>
      {actionError && (
        <p className={styles.error} role="alert">
          {actionError}
        </p>
      )}

      <section className={styles.section}>
        <h3>复盘记录</h3>
        {data.jobs.length === 0 ? (
          <p className={styles.empty}>
            尚未开始。完整交付并签署后会登记复盘；暂停、取消和最终失败不会触发。
          </p>
        ) : (
          <ol className={styles.jobs}>
            {data.jobs.map((job) => (
              <li key={job.id}>
                <span className={styles.jobVersion}>v{job.report_version}</span>
                <span>{RETROSPECTIVE_STATUS_LABELS[job.status] ?? job.status}</span>
                <span className={styles.meta}>
                  尝试 {job.attempts}/{job.max_attempts} · 经验 {job.experience_count} 条
                  {job.completed_at ? ` · ${job.completed_at.slice(0, 19).replace('T', ' ')}` : ''}
                </span>
                {job.last_error && <span className={styles.error}>{job.last_error}</span>}
                <RetrospectiveRecovery key={`${job.id}:${job.recovery_revision ?? 0}:${job.status}`} reportId={reportId} job={job} onChanged={() => setReloadKey(value => value + 1)} />
              </li>
            ))}
          </ol>
        )}
      </section>

      <section className={styles.section}>
        <h3>本次研究收到的经验 · {received.length}</h3>
        {received.length === 0 ? (
          <p className={styles.empty}>当前没有收到可复用的历史经验。</p>
        ) : (
          received.map((experience) => (
            <ExperienceCard
              key={`${experience.experience_id}-${experience.usage?.dispatch_id ?? ''}`}
              experience={experience}
              showUsage
              busy={busyId === experience.experience_id}
              onToggle={toggle}
            />
          ))
        )}
      </section>

      <section className={styles.section}>
        <h3>本报告产出的经验 · {produced.length}</h3>
        {produced.length === 0 ? (
          <p className={styles.empty}>本报告尚未产出经验。</p>
        ) : (
          produced.map((experience) => (
            <ExperienceCard
              key={experience.experience_id}
              experience={experience}
              showUsage={false}
              busy={busyId === experience.experience_id}
              onToggle={toggle}
            />
          ))
        )}
      </section>
    </div>
  );
}
