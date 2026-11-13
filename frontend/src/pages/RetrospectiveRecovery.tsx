import { useRef, useState } from 'react';
import { recoverRetrospective, type RetrospectiveJobView, type RetrospectiveRecoveryRequest } from '../api/client';
import styles from './RetrospectiveRecovery.module.css';

interface Fields { attemptId: string; input: string; output: string; note: string; acknowledge: boolean }
interface Attempt { attempt_id: string; attempt_number: number; usage: Record<string, unknown> }

function attemptsFor(job: RetrospectiveJobView): Attempt[] {
  if (!Array.isArray(job.usage.attempts)) return [];
  return job.usage.attempts.filter((value): value is Attempt => Boolean(value && typeof value.attempt_id === 'string' && typeof value.attempt_number === 'number' && value.usage && typeof value.usage === 'object'));
}

export function recoveryPayload(job: RetrospectiveJobView, fields: Fields, action: RetrospectiveRecoveryRequest['action'], operationId: string): RetrospectiveRecoveryRequest | null {
  if (!['failed', 'completed'].includes(job.status) || !fields.note.trim() || fields.note.trim().length > 2000) return null;
  const common = { action, operation_id: operationId, expected_revision: job.recovery_revision ?? 0, note: fields.note.trim() };
  if (action === 'authorize_retry') {
    return job.status === 'failed' && fields.acknowledge ? { ...common, acknowledge_duplicate_charge: true } : null;
  }
  if (!attemptsFor(job).some(attempt => attempt.attempt_id === fields.attemptId) || !fields.input.trim() || !fields.output.trim()) return null;
  const input = Number(fields.input), output = Number(fields.output);
  if (![input, output, input + output].every(value => Number.isSafeInteger(value) && value >= 0)) return null;
  return { ...common, attempt_id: fields.attemptId, input_tokens: input, output_tokens: output };
}

export function RetrospectiveRecovery({ reportId, job, onChanged }: { reportId: string; job: RetrospectiveJobView; onChanged: () => void }) {
  const attempts = attemptsFor(job);
  const [fields, setFields] = useState<Fields>({ attemptId: attempts[0]?.attempt_id ?? '', input: '', output: '', note: '', acknowledge: false });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const pending = useRef<{ key: string; body: RetrospectiveRecoveryRequest } | null>(null);
  function change<K extends keyof Fields>(key: K, value: Fields[K]) { setFields(old => ({ ...old, [key]: value })); }
  async function submit(action: RetrospectiveRecoveryRequest['action']) {
    if (busy) return;
    const body = recoveryPayload(job, fields, action, '');
    if (!body) return;
    const key = JSON.stringify(body);
    if (pending.current?.key !== key) pending.current = { key, body: { ...body, operation_id: crypto.randomUUID() } };
    setBusy(true); setError(null);
    try {
      await recoverRetrospective(reportId, job.id, pending.current.body);
      onChanged();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '恢复决定未能提交');
    } finally { setBusy(false); }
  }
  if (!['failed', 'completed'].includes(job.status)) return null;
  const selected = attempts.find(attempt => attempt.attempt_id === fields.attemptId);
  const history = Array.isArray(job.usage.recovery_audit) ? job.usage.recovery_audit : [];
  return <details className={styles.panel}>
    <summary>核实用量与恢复</summary>
    <p>核实只更新复盘账本。需要重试时另行授权，已签署报告保持可读。</p>
    {error && <p role="alert" className={styles.error}>{error}</p>}
    <fieldset disabled={busy} className={styles.form}>
      <legend>核实用量</legend>
      {attempts.length > 0 ? <>
        <label>待核实的尝试<select value={fields.attemptId} onChange={e => { change('attemptId', e.target.value); change('input', ''); change('output', ''); }}>
          {attempts.map(attempt => <option key={attempt.attempt_id} value={attempt.attempt_id}>第 {attempt.attempt_number} 次 · {attempt.attempt_id.slice(0, 8)}</option>)}
        </select></label>
        <p className={styles.meta}>当前记录：输入 {String(selected?.usage.input_tokens ?? '未知')} · 输出 {String(selected?.usage.output_tokens ?? '未知')}{selected?.usage.provider_request_id ? ` · 供应商请求 ${String(selected.usage.provider_request_id)}` : ''}</p>
        <div className={styles.tokens}>
          <label>核实输入 token<input type="number" min="0" step="1" value={fields.input} onChange={e => change('input', e.target.value)} /></label>
          <label>核实输出 token<input type="number" min="0" step="1" value={fields.output} onChange={e => change('output', e.target.value)} /></label>
        </div>
      </> : <p>此历史任务没有可逐次核实的用量记录。</p>}
      <label>核实依据或重试理由<textarea maxLength={2000} value={fields.note} onChange={e => change('note', e.target.value)} placeholder="填写供应商回执或账单核对依据；授权重试时说明理由" /></label>
      <button type="button" className="btn" disabled={!recoveryPayload(job, fields, 'reconcile', '')} onClick={() => void submit('reconcile')}>保存核实用量</button>
      {job.status === 'failed' && <div className={styles.retry}>
        <label className={styles.check}><input type="checkbox" checked={fields.acknowledge} onChange={e => change('acknowledge', e.target.checked)} />我确认再尝试一次，理解旧请求可能已执行、可能重复收费</label>
        <button type="button" className="btn" disabled={!recoveryPayload(job, fields, 'authorize_retry', '')} onClick={() => void submit('authorize_retry')}>授权再尝试一次</button>
      </div>}
    </fieldset>
    <button type="button" className="btn" disabled={busy} onClick={onChanged}>刷新复盘记录</button>
    {history.length > 0 && <details><summary>人工处理记录 · {history.length}</summary><ul>{history.map((event, index) => <li key={index}>{String(event.at ?? '')} · {event.action === 'reconcile' ? '核实用量' : '授权重试'} · {String(event.note ?? '')}</li>)}</ul></details>}
  </details>;
}
