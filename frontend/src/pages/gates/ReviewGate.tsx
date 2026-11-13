import { useState } from 'react';

import type { InterruptView } from '../../api/client';
import { SEVERITY_LABELS } from '../../api/labels';
import styles from './gates.module.css';
import type { IssueDisposition, ReviewDecision } from './types';

export function ReviewGate({
  interrupt,
  busy,
  onChange,
}: {
  interrupt: InterruptView;
  busy: boolean;
  onChange: (decision: ReviewDecision | null, comment: string, dispositions: IssueDisposition[]) => void;
}) {
  const [decision, setDecision] = useState<ReviewDecision | null>(null);
  const [comment, setComment] = useState('');
  const [dispositions, setDispositions] = useState<Record<string, IssueDisposition>>({});
  const review = interrupt.review;
  const allowed = new Set(review?.allowed_decisions ?? ['approve', 'request_changes', 'reject']);
  const incomplete = review?.checks_complete !== true || Boolean(review?.issues.some(issue => issue.hard));

  function choose(next: ReviewDecision) {
    setDecision(next);
    onChange(next, comment, Object.values(dispositions));
  }

  function updateDisposition(issueId: string, selected: boolean, reason: string) {
    const next = { ...dispositions };
    if (selected) next[issueId] = { issue_id: issueId, decision: 'false_positive', reason };
    else delete next[issueId];
    setDispositions(next);
    onChange(decision, comment, Object.values(next));
  }

  return (
    <section className={styles.card} aria-label="审核决定">
      <header className={styles.head}>
        <span className={styles.flag}>人工审核</span>
      </header>
      <div className={styles.body}>
        {review?.summary && <p>{review.summary}</p>}
        {incomplete && <p role="status">当前仍有完整性问题或未完成检查，请要求修改或驳回，暂不能通过。</p>}
        {review?.errors?.map((error, index) => <p role="alert" key={index}>{error}</p>)}
        {review && review.issues.length > 0 && (
          <ul>
            {review.issues.map((issue, index) => (
              <li key={issue.issue_id || `${issue.location}-${issue.reason}`}>
                {SEVERITY_LABELS[issue.severity] ?? issue.severity} · {issue.location} · {issue.reason}
                {!issue.hard && issue.issue_id && ['major', 'critical'].includes(issue.severity) && (
                  <fieldset disabled={busy} className={styles.disposition}>
                    <legend>问题 {index + 1} 的处置</legend>
                    <label>误报处置
                      <select name={`disposition-${issue.issue_id}`} value={dispositions[issue.issue_id] ? 'false_positive' : ''}
                        onChange={event => updateDisposition(issue.issue_id, event.target.value === 'false_positive', dispositions[issue.issue_id]?.reason ?? '')}>
                        <option value="">尚未裁决</option>
                        <option value="false_positive">经核查属于误报</option>
                      </select>
                    </label>
                    <label>处置理由（判定误报时必填）
                      <textarea className={styles.textarea} rows={2} value={dispositions[issue.issue_id]?.reason ?? ''}
                        disabled={!dispositions[issue.issue_id]} maxLength={4000}
                        onChange={event => updateDisposition(issue.issue_id, true, event.target.value)} />
                    </label>
                  </fieldset>
                )}
              </li>
            ))}
          </ul>
        )}
        <div className={styles.actions}>
          {allowed.has('approve') && (
            <button
              type="button"
              className={decision === 'approve' ? 'btn btn--primary' : 'btn'}
              disabled={busy || incomplete}
              onClick={() => choose('approve')}
            >
              通过
            </button>
          )}
          {allowed.has('request_changes') && (
            <button
              type="button"
              className={decision === 'request_changes' ? 'btn btn--primary' : 'btn'}
              disabled={busy}
              onClick={() => choose('request_changes')}
            >
              要求修改
            </button>
          )}
          {allowed.has('reject') && (
            <button
              type="button"
              className={decision === 'reject' ? 'btn btn--danger' : 'btn'}
              disabled={busy}
              onClick={() => choose('reject')}
            >
              驳回
            </button>
          )}
        </div>
        {decision && decision !== 'approve' && (
          <textarea
            className={styles.textarea}
            rows={3}
            value={comment}
            placeholder="审核意见（必填）"
            aria-label="审核意见"
            onChange={(event) => {
              setComment(event.target.value);
              onChange(decision, event.target.value, Object.values(dispositions));
            }}
          />
        )}
      </div>
    </section>
  );
}
