import type { AnalysisView } from '../../api/client';
import { ConfirmationChoices } from './ConfirmationChoices';
import styles from './gates.module.css';
import type { ToolDecision } from './types';

export function AnalysisGate({
  analysis,
  choice,
  busy,
  onChoose,
}: {
  analysis: AnalysisView | null;
  choice: ToolDecision | null;
  busy: boolean;
  onChoose: (decision: ToolDecision) => void;
}) {
  return (
    <section className={styles.card} aria-label="分析结果确认">
      <header className={styles.head}>
        <span className={styles.flag}>
          分析结果{analysis?.version ? ` · v${analysis.version}` : ''}
        </span>
      </header>
      <div className={styles.body}>
        {analysis ? (
          <>
            {analysis.summary && <p>{analysis.summary}</p>}
            {analysis.conclusions.length > 0 && (
              <ul>
                {analysis.conclusions.map((item) => (
                  <li key={item}>{item}</li>
                ))}
              </ul>
            )}
            {analysis.limitations.length > 0 && (
              <p className={styles.hint}>限制：{analysis.limitations.join('；')}</p>
            )}
            {analysis.gaps.length > 0 && (
              <p className={styles.hint}>缺口：{analysis.gaps.join('；')}</p>
            )}
          </>
        ) : (
          <p className={styles.hint}>当前分析产物还不能读取。仍可按中断里的版本批准或拒绝。</p>
        )}
        <ConfirmationChoices choice={choice} busy={busy} onChoose={onChoose} />
      </div>
    </section>
  );
}
