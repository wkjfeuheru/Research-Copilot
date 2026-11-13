import type { ReactNode } from 'react';

import styles from './EmptyState.module.css';

interface EmptyStateProps {
  title: string;
  description?: string;
  action?: ReactNode;
  tone?: 'neutral' | 'error';
}

export function EmptyState({
  title,
  description,
  action,
  tone = 'neutral',
}: EmptyStateProps) {
  return (
    <div
      className={`${styles.empty} ${tone === 'error' ? styles.error : ''}`}
      role={tone === 'error' ? 'alert' : 'status'}
    >
      <p className={styles.title}>{title}</p>
      {description && <p className={styles.description}>{description}</p>}
      {action && <div className={styles.action}>{action}</div>}
    </div>
  );
}
