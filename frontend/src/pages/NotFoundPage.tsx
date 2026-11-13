import { Link } from 'react-router-dom';

import styles from './NotFoundPage.module.css';

export function NotFoundPage() {
  return (
    <div className={styles.page}>
      <p className={styles.eyebrow}>404 / 页面不存在</p>
      <h1>没有找到这个页面</h1>
      <p className={styles.lede}>该地址没有对应的任务页面。可返回任务列表继续。</p>
      <p>
        <Link className={styles.primaryLink} to="/">
          返回任务列表
        </Link>
      </p>
    </div>
  );
}
