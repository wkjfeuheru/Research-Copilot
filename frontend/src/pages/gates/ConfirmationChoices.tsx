import styles from './gates.module.css';
import type { ToolDecision } from './types';

export function ConfirmationChoices({
  choice,
  busy,
  onChoose,
  allowApprove = true,
}: {
  choice: ToolDecision | null;
  busy: boolean;
  onChoose: (decision: ToolDecision) => void;
  allowApprove?: boolean;
}) {
  return (
    <div className={styles.actions}>
      <button
        type="button"
        className={choice === 'approve' ? 'btn btn--primary' : 'btn'}
        disabled={busy || !allowApprove}
        onClick={() => onChoose('approve')}
      >
        批准
      </button>
      <button
        type="button"
        className={choice === 'reject' ? 'btn btn--danger' : 'btn'}
        disabled={busy}
        onClick={() => onChoose('reject')}
      >
        拒绝
      </button>
      <p className={styles.hint}>拒绝会停止当前关口，不会自动修改研究范围。</p>
    </div>
  );
}
