import { EmptyState } from '../components/EmptyState';
import styles from './ManagementPage.module.css';

export function MemoryPage() {
  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <p className={styles.eyebrow}>个人研究空间 / 长期记忆</p>
        <h1>研究记忆</h1>
        <p className={styles.lede}>
          记住资料中的知识，也记住研究过程中奏效与失效的方法。长期记忆尚未接入。
        </p>
      </header>

      <div className={styles.summaryGrid}>
        <article className={styles.summaryCard}>
          <span className={styles.summaryLabel}>研报知识</span>
          <strong className={styles.summaryValue}>0</strong>
          <p>带来源、日期与原文定位的摘要和观点。</p>
        </article>
        <article className={styles.summaryCard}>
          <span className={styles.summaryLabel}>任务经验</span>
          <strong className={styles.summaryValue}>0</strong>
          <p>记录成功做法、失败教训与适用边界。</p>
        </article>
      </div>

      <div className={styles.emptyWrap}>
        <EmptyState
          symbol="◇"
          title="还没有可召回的记忆"
          description="阶段一只提供空状态：不读取数据库，也不会生成演示记忆。"
          hint="后续将区分研报知识与任务经验；未经来源和依据检查的候选不会进入召回。"
        />
      </div>
    </div>
  );
}
