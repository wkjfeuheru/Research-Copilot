import { EmptyState } from '../components/EmptyState';
import styles from './ManagementPage.module.css';

export function SkillsPage() {
  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <p className={styles.eyebrow}>个人研究空间 / 技能</p>
        <h1>技能</h1>
        <p className={styles.lede}>
          选择研究方法，让系统按问题补充所需能力。技能目录尚未接入。
        </p>
      </header>

      <div className={styles.summaryGrid}>
        <article className={styles.summaryCard}>
          <span className={styles.summaryLabel}>可用技能</span>
          <strong className={styles.summaryValue}>0</strong>
          <p>当前没有可加载的指令、模板或可信脚本。</p>
        </article>
        <article className={styles.summaryCard}>
          <span className={styles.summaryLabel}>可执行脚本</span>
          <strong className={styles.summaryValue}>0</strong>
          <p>可信脚本与隔离执行器尚未实现。</p>
        </article>
      </div>

      <div className={styles.emptyWrap}>
        <EmptyState
          symbol="⌘"
          title="还没有可用技能"
          description="阶段一只提供空状态：不加载技能目录，也不会执行脚本。"
          hint="后续支持显式触发与自动发现；技能不能绕过风险检查和执行边界。"
        />
      </div>
    </div>
  );
}
