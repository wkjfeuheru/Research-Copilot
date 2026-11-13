import type { ReviewView } from '../../api/client';
import { SEVERITY_LABELS } from '../../api/labels';
import { ConfirmationChoices } from './ConfirmationChoices';
import styles from './gates.module.css';
import type { ToolDecision } from './types';

export function SignoffGate({
  review,
  signoffReady,
  choice,
  busy,
  onChoose,
}: {
  review: ReviewView | null;
  signoffReady: boolean;
  choice: ToolDecision | null;
  busy: boolean;
  onChoose: (decision: ToolDecision) => void;
}) {
  return (
    <section className={styles.card} aria-label="报告签署">
      <header className={styles.head}>
        <span className={styles.flag}>{signoffReady ? '可以签署' : '签署条件未满足'}</span>
      </header>
      <div className={styles.body}>
        <p className={styles.hint}>
          {review?.checks_complete ? '审核检查完整。' : '审核尚未完整完成。'}
          {review?.outcome ? ` 结论：${review.outcome}。` : ''}
        </p>
        {review && review.issues.length > 0 && (
          <ul>
            {review.issues.map((issue) => (
              <li key={`${issue.location}-${issue.reason}`}>
                {SEVERITY_LABELS[issue.severity] ?? issue.severity} · {issue.location} · {issue.reason}
              </li>
            ))}
          </ul>
        )}
        <ConfirmationChoices choice={choice} busy={busy} allowApprove={signoffReady} onChoose={onChoose} />
      </div>
    </section>
  );
}
