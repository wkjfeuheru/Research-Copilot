import { formatBytes } from '../app/format';
import type { LocalAttachment } from '../app/types';
import { ServiceStatus } from './ServiceStatus';
import styles from './ResearchContext.module.css';

interface ResearchContextProps {
  attachments: readonly LocalAttachment[];
  selectedSkillNames: readonly string[];
}

export function ResearchContext({
  attachments,
  selectedSkillNames,
}: ResearchContextProps) {
  return (
    <div className={styles.context}>
      <h2 className={styles.heading}>研究上下文</h2>
      <p className={styles.lede}>资料与方法，与问题一起出发。</p>

      <section className={styles.block} aria-labelledby="context-files">
        <div className={styles.blockHeader}>
          <h3 id="context-files">本次资料</h3>
          <span className={styles.count}>{attachments.length}</span>
        </div>
        {attachments.length === 0 ? (
          <p className={styles.note}>
            添加研报、调研笔记或数据文件；本阶段仅记录文件名，不上传、不解析。
          </p>
        ) : (
          <ul className={styles.fileList}>
            {attachments.map((attachment) => (
              <li key={attachment.id}>
                <span>{attachment.name}</span>
                <small>{formatBytes(attachment.size)} · 仅本地</small>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className={styles.block} aria-labelledby="context-skills">
        <h3 id="context-skills">研究方法</h3>
        {selectedSkillNames.length === 0 ? (
          <p className={styles.note}>
            尚未选择技能；占位技能不会加载或执行。
          </p>
        ) : (
          <ul className={styles.skillList}>
            {selectedSkillNames.map((name) => (
              <li key={name}>{name}</li>
            ))}
          </ul>
        )}
      </section>

      <section className={styles.block} aria-labelledby="context-flow">
        <h3 id="context-flow">如何推进</h3>
        <ol className={styles.steps}>
          <li>提出问题</li>
          <li>获取资料与分析</li>
          <li>风险检查与简报</li>
        </ol>
        <p className={styles.note}>以上是后续阶段的流程；当前不会执行。</p>
      </section>

      <ServiceStatus />
    </div>
  );
}
