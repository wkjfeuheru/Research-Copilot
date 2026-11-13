import type { ReworkView } from '../../api/client';
import { ROLE_LABELS } from '../../api/labels';
import { ConfirmationChoices } from './ConfirmationChoices';
import styles from './gates.module.css';
import type { ToolDecision } from './types';

export function ReworkGate({
  rework,
  choice,
  busy,
  onChoose,
}: {
  rework: ReworkView | null;
  choice: ToolDecision | null;
  busy: boolean;
  onChoose: (decision: ToolDecision) => void;
}) {
  return (
    <section className={styles.card} aria-label="返工方案确认">
      <header className={styles.head}>
        <span className={styles.flag}>
          返工方案{rework?.version ? ` · v${rework.version}` : ''}
        </span>
      </header>
      <div className={styles.body}>
        {rework && rework.tasks.length > 0 ? (
          rework.tasks.map((task) => (
            <p key={`${task.role}-${task.objective}`}>
              <code>{ROLE_LABELS[task.role] ?? task.role}</code> {task.objective}
            </p>
          ))
        ) : (
          <p className={styles.hint}>当前返工产物还不能读取。仍可按中断里的版本批准或拒绝。</p>
        )}
        <ConfirmationChoices choice={choice} busy={busy} onChoose={onChoose} />
      </div>
    </section>
  );
}
