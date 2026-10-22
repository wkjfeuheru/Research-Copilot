import type { ReactNode } from 'react';

import styles from './EmptyState.module.css';

interface EmptyStateProps {
  symbol: string;
  title: string;
  description: string;
  hint?: string;
  action?: ReactNode;
}

export function EmptyState({
  symbol,
  title,
  description,
  hint,
  action,
}: EmptyStateProps) {
  return (
    <div className={styles.empty} role="status">
      <span className={styles.symbol} aria-hidden="true">
        {symbol}
      </span>
      <h2 className={styles.title}>{title}</h2>
      <p className={styles.description}>{description}</p>
      {hint ? <p className={styles.hint}>{hint}</p> : null}
      {action ? <div className={styles.action}>{action}</div> : null}
    </div>
  );
}
