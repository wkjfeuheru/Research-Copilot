import { useEffect, useRef, useState } from 'react';
import { fetchRunRecovery, recoverRunRequest, releaseRun, type RequestRecoveryDecision, type RunRecoveryView, type RunReleaseDecision } from '../api/client';
import styles from './RetrospectiveRecovery.module.css';

interface Fields { requestId: string; usage: string; note: string; ownerStopped: boolean; acknowledge: boolean }
function initial(view: RunRecoveryView): Fields {
  return { requestId: view.pending_requests[0] ?? view.requests.find(r => !r.released)?.id ?? '', usage: '', note: '', ownerStopped: false, acknowledge: false };
}
function valid(fields: Fields) { return fields.ownerStopped && fields.note.trim().length > 0 && fields.note.trim().length <= 2000; }
export function requestRecoveryPayload(view: RunRecoveryView, fields: Fields, action: RequestRecoveryDecision['action']): RequestRecoveryDecision | null {
  const row = view.requests.find(r => r.id === fields.requestId);
  if (!valid(fields) || !row || row.released) return null;
  const body = { action, expected_revision: view.budget_revision, owner_stopped: true, note: fields.note.trim() };
  if (action === 'authorize_retry') return !row.pending && row.operation_id && !row.retry_authorization && fields.acknowledge ? { ...body, acknowledge_duplicate_charge: true } : null;
  if (action === 'resolve_pending') return row.pending ? body : null;
  const usage = Number(fields.usage);
  return !row.pending && !row.reconciled && fields.usage.trim() && Number.isSafeInteger(usage) && usage >= 0 ? { ...body, usage } : null;
}
export function releasePayload(view: RunRecoveryView, fields: Fields): RunReleaseDecision | null {
  return valid(fields) && view.can_release && view.pending_requests.length === 0 && view.command?.claim_token
    ? { claim_token: view.command.claim_token, owner_stopped: true, note: fields.note.trim() } : null;
}

export function RunRecoveryForm({ reportId, view, onChanged }: { reportId: string; view: RunRecoveryView; onChanged: () => void | Promise<void> }) {
  const [fields, setFields] = useState<Fields>(() => initial(view));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const submitting = useRef(false);
  useEffect(() => { setFields(old => ({ ...initial(view), requestId: view.requests.some(r => r.id === old.requestId && !r.released) ? old.requestId : initial(view).requestId })); }, [view.budget_revision, view.command?.id, view.command?.claim_token, view.command?.status]);
  function change<K extends keyof Fields>(key: K, value: Fields[K]) { setFields(old => ({ ...old, [key]: value })); }
  async function submit(action: RequestRecoveryDecision['action'] | 'release') {
    if (submitting.current) return;
    const body = action === 'release' ? releasePayload(view, fields) : requestRecoveryPayload(view, fields, action);
    if (!body) return;
    submitting.current = true; setBusy(true); setError(null); setNotice(null);
    try {
      if (action === 'release') await releaseRun(reportId, view.command!.id, body as RunReleaseDecision);
      else await recoverRunRequest(reportId, fields.requestId, body as RequestRecoveryDecision);
      setNotice(action === 'release' ? '执行名额已释放。研究尚未恢复，请核查账本后另行决定是否继续。' : '决定已保存。研究尚未恢复。');
    } catch (reason) { setError(reason instanceof Error ? reason.message : '恢复决定未能保存'); }
    finally {
      try { await onChanged(); } catch { setError('未能读取最新记录，请刷新后再操作'); }
      setBusy(false); submitting.current = false;
    }
  }
  const selected = view.requests.find(r => r.id === fields.requestId);
  return <section className={styles.panel} aria-label="运行恢复操作" aria-busy={busy}>
    <p>请求结果与执行名额分别处理。以下操作不会启动研究；再次收费的授权须单独确认。</p>
    <p role="status">{view.budget.usage_verified ? '用量已核实' : '用量仍未知或待核实'} · 待登记请求 {view.pending_requests.length} 项</p>
    {error && <p role="alert" className={styles.error}>{error}</p>}
    {notice && <p role="status">{notice}</p>}
    {view.command && <details><summary>原执行身份</summary><p className={styles.meta}>执行者：{view.command.worker_id ?? '未领取'}<br />任务：{view.command.id}<br />租约期限：{view.command.locked_until ?? '已结束'}</p></details>}
    <fieldset className={styles.form} disabled={busy}>
      <legend>停机核查</legend>
      <label className={styles.check}><input type="checkbox" checked={fields.ownerStopped} onChange={e => change('ownerStopped', e.target.checked)} />我已核查旧执行者及相关外部调用、子进程或沙盒已停止；租约过期本身不代表执行已停止</label>
      <label>核查依据<textarea value={fields.note} maxLength={2000} onChange={e => change('note', e.target.value)} placeholder="填写停机核查、供应商回执或账单依据；重试时说明理由" /></label>
      {view.requests.some(r => !r.released) && <>
        <label>请求记录<select value={fields.requestId} onChange={e => setFields(old => ({ ...old, requestId: e.target.value, usage: '', acknowledge: false }))}>
          {view.requests.filter(r => !r.released).map(r => <option key={r.id} value={r.id}>{r.stage ?? '研究'} · {r.id.slice(0, 8)} · {r.pending ? '待登记' : r.usage == null ? '用量未知' : `${r.usage} token`}</option>)}
        </select></label>
        {selected && <p className={styles.meta}>请求：{selected.id}<br />{selected.sent ? '已记录发送；未收到结果不代表未执行。' : '账本未记录发送。'} {selected.pending ? '' : `核实用量：${selected.usage ?? '未知'}`}</p>}
        {selected?.pending ? <button className="btn" type="button" disabled={!requestRecoveryPayload(view, fields, 'resolve_pending')} onClick={() => void submit('resolve_pending')}>{selected.sent ? '登记为已发送、用量未知' : '登记未发送并释放预留'}</button> : <>
          <label>核实总 token<input type="number" min="0" step="1" value={fields.usage} onChange={e => change('usage', e.target.value)} /></label>
          <button className="btn" type="button" disabled={!requestRecoveryPayload(view, fields, 'reconcile')} onClick={() => void submit('reconcile')}>保存核实用量</button>
          {!selected?.retry_authorization && selected?.operation_id && <div className={styles.retry}>
            <label className={styles.check}><input type="checkbox" checked={fields.acknowledge} onChange={e => change('acknowledge', e.target.checked)} />我确认允许此请求再尝试一次，理解旧请求可能已执行、可能重复收费</label>
            <button className="btn" type="button" disabled={!requestRecoveryPayload(view, fields, 'authorize_retry')} onClick={() => void submit('authorize_retry')}>记录一次性重试授权</button>
          </div>}
        </>}
      </>}
      <div className={styles.retry}>
        <p>{view.pending_requests.length ? '先逐项登记待处理请求，再释放名额。' : view.can_release ? '释放后其他任务可使用名额；本任务仍需单独决定是否继续。' : '当前没有符合释放条件的失联执行。'}</p>
        <button className="btn" type="button" disabled={!releasePayload(view, fields)} onClick={() => void submit('release')}>释放执行名额</button>
      </div>
    </fieldset>
    <details><summary>人工处理记录</summary>
      {view.receipt && <p>名额释放 · {view.receipt.created_at} · {view.receipt.note}</p>}
      {view.requests.map(row => <div key={row.id}>{row.recovery && <p>请求 {row.id.slice(0, 8)} 登记 · {row.recovery.note}</p>}{row.reconcile_details && <p>用量核实 · {row.reconcile_details.note}</p>}{row.retry_authorization && <p>一次性授权{row.retry_authorization.consumed_by ? '已使用' : '未使用'} · {row.retry_authorization.note}</p>}</div>)}
    </details>
  </section>;
}

export function RunRecoveryPanel({ reportId, onChanged }: { reportId: string; onChanged: () => void | Promise<void> }) {
  const [view, setView] = useState<RunRecoveryView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const sequence = useRef(0);
  async function load(signal?: AbortSignal) {
    const current = ++sequence.current;
    try { const next = await fetchRunRecovery(reportId, signal); if (current === sequence.current) { setView(next); setError(null); } }
    catch (reason) { if (current === sequence.current && !signal?.aborted) { setError(reason instanceof Error ? reason.message : '无法读取恢复记录'); throw reason; } }
  }
  useEffect(() => { const controller = new AbortController(); void load(controller.signal).catch(() => {}); return () => { ++sequence.current; controller.abort(); }; }, [reportId]);
  return <div>
    {error && <p role="alert">{error}</p>}
    {!view && !error && <p role="status">正在读取恢复记录…</p>}
    <button type="button" className="btn" onClick={() => void load().catch(() => {})}>刷新恢复记录</button>
    {view && !error && <RunRecoveryForm reportId={reportId} view={view} onChanged={async () => { await load(); await onChanged(); }} />}
  </div>;
}
