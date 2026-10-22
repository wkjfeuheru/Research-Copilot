import { NavLink } from 'react-router-dom';

import styles from './AppHeader.module.css';

interface NavItem {
  to: string;
  label: string;
  end?: boolean;
}

const NAV_ITEMS: readonly NavItem[] = [
  { to: '/', label: '研究', end: true },
  { to: '/memory', label: '记忆' },
  { to: '/skills', label: '技能' },
];

function navLinkClass({ isActive }: { isActive: boolean }): string {
  return isActive ? `${styles.navLink} ${styles.active}` : styles.navLink;
}

export function AppHeader() {
  return (
    <header className={styles.header}>
      <NavLink to="/" className={styles.brand} aria-label="Research Copilot 研究工作区">
        <span className={styles.brandMark} aria-hidden="true">
          r
        </span>
        <span className={styles.brandText}>
          <strong className={styles.brandName}>Research Copilot</strong>
          <span className={styles.brandTagline}>投研研究工作区</span>
        </span>
      </NavLink>

      <nav className={styles.nav} aria-label="主导航">
        {NAV_ITEMS.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            end={item.end}
            className={navLinkClass}
          >
            {item.label}
          </NavLink>
        ))}
      </nav>

      <div className={styles.meta}>
        <span className={styles.phase}>阶段一 · 工程骨架</span>
        <span className={styles.avatar} aria-label="当前用户：本机用户">
          我
        </span>
      </div>
    </header>
  );
}
