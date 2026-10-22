import { Link } from 'react-router-dom';

import styles from './ManagementPage.module.css';

export function NotFoundPage() {
  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <p className={styles.eyebrow}>404 / 页面不存在</p>
        <h1>没有找到这个页面</h1>
        <p className={styles.lede}>
          该地址没有对应的研究工作区页面。可返回研究工作区继续。
        </p>
      </header>
      <p className={styles.notFoundAction}>
        <Link className={styles.primaryLink} to="/">
          返回研究工作区
        </Link>
      </p>
    </div>
  );
}
