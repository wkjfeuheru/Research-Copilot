import type { PlanView } from '../../api/client';
import { REPORT_TYPE_LABELS, ROLE_LABELS } from '../../api/labels';
import { ConversationMarkdown } from '../../components/ConversationMarkdown';
import { ConfirmationChoices } from './ConfirmationChoices';
import styles from './gates.module.css';
import type { ToolDecision } from './types';

const ROLE_ORDER = ['searcher', 'analysist', 'writer', 'reviewer'] as const;

/** 把计划数据拼成 Markdown 文档，复用对话的排版渲染。 */
function planToMarkdown(plan: PlanView): string {
  const typeLabel = plan.report_type
    ? (REPORT_TYPE_LABELS[plan.report_type] ?? plan.report_type)
    : '—';
  const lines = [
    `- **报告类型**：${typeLabel}`,
    `- **研究目标**：${plan.objective}`,
    '',
  ];
  for (const role of ROLE_ORDER) {
    const items = plan.tasks.filter((task) => task.role === role);
    if (items.length === 0) continue;
    lines.push(`## ${ROLE_LABELS[role] ?? role}`);
    for (const task of items) {
      lines.push(`1. **${task.task_id}** ${task.objective}`);
    }
    lines.push('');
  }
  return lines.join('\n');
}

export function PlanGate({
  plan,
  choice,
  busy,
  onChoose,
}: {
  plan: PlanView | null;
  choice: ToolDecision | null;
  busy: boolean;
  onChoose: (decision: ToolDecision) => void;
}) {
  return (
    <section className={styles.card} aria-label="研究计划确认">
      <header className={styles.head}>
        <span className={styles.flag}>研究计划 · {plan ? `v${plan.version}` : '当前版本'}</span>
      </header>
      <div className={styles.body}>
        {plan ? (
          <ConversationMarkdown text={planToMarkdown(plan)} reportId="" />
        ) : (
          <p className={styles.hint}>当前计划产物还不能读取。仍可按中断里的版本批准或拒绝。</p>
        )}
        <ConfirmationChoices choice={choice} busy={busy} onChoose={onChoose} />
      </div>
    </section>
  );
}
