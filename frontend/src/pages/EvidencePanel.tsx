import { useEffect, useMemo, useState } from 'react';

import {
  fetchEvidence,
  fetchEvidenceDetail,
  type EvidenceDetailResponse,
  type EvidenceListItem,
  type EvidencePage,
} from '../api/client';
import { EVIDENCE_KIND_LABELS, SOURCE_LABELS, clockTime } from '../api/labels';
import { EmptyState } from '../components/EmptyState';
import styles from './EvidencePanel.module.css';

const PAGE_SIZE = 20;

function sourceLabel(source: string): string {
  return SOURCE_LABELS[source] ?? source;
}

function kindLabel(kind: string): string {
  return EVIDENCE_KIND_LABELS[kind] ?? kind;
}

function publishDate(value: string | null): string | null {
  if (!value) return null;
  const match = /^\d{4}-\d{2}-\d{2}/.exec(value);
  return match ? match[0] : value;
}

/** Snippet locator shown next to the title; empty for structured records. */
function snippetMeta(item: EvidenceListItem): string {
  const parts: string[] = [];
  if (item.collection) parts.push(item.collection);
  if (item.page !== null && item.page !== undefined && item.page !== '') {
    parts.push(`第 ${item.page} 页`);
  }
  if (item.heading_path) parts.push(item.heading_path);
  if (item.broker) parts.push(item.broker);
  if (item.report_type) parts.push(item.report_type);
  const published = publishDate(item.published_at);
  if (published) parts.push(published);
  return parts.join(' · ');
}

interface EvidencePanelProps {
  reportId: string;
  refs: { id: string; title: string }[];
}

export function EvidencePanel({ reportId, refs }: EvidencePanelProps) {
  const [page, setPage] = useState<EvidencePage | null>(null);
  const [offset, setOffset] = useState(0);
  const [reloadKey, setReloadKey] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [detail, setDetail] = useState<EvidenceDetailResponse | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    fetchEvidence(reportId, { offset, limit: PAGE_SIZE }, controller.signal)
      .then((data) => {
        setPage(data);
        setError(null);
      })
      .catch((err: unknown) => {
        if (!controller.signal.aborted) {
          setError(err instanceof Error ? err.message : '证据加载失败');
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [offset, reloadKey, reportId]);

  const titles = useMemo(
    () => new Map(refs.map((item) => [item.id, item.title])),
    [refs],
  );

  async function openDetail(evidenceId: string) {
    setDetailError(null);
    try {
      setDetail(await fetchEvidenceDetail(reportId, evidenceId));
    } catch (err) {
      setDetailError(err instanceof Error ? err.message : '证据详情加载失败');
    }
  }

  if (error) {
    return (
      <EmptyState
        title="证据加载失败"
        description={error}
        tone="error"
        action={
          <button
            type="button"
            className="btn"
            onClick={() => setReloadKey((value) => value + 1)}
          >
            重试
          </button>
        }
      />
    );
  }

  if (loading && page === null) {
    return (
      <div className={styles.skeleton} aria-busy="true" aria-label="正在加载证据">
        <span />
        <span />
        <span />
      </div>
    );
  }

  if (page && page.total === 0) {
    return <EmptyState title="还没有证据" description="采集完成后，证据会出现在这里。" />;
  }

  return (
    <div className={`${styles.layout} ${detail ? styles.withDetail : ''}`}>
      <section className={styles.listPane} aria-label="证据列表">
        {page && (
          <>
            <table className={styles.table}>
              <thead>
                <tr>
                  <th>编号</th>
                  <th>任务</th>
                  <th>来源</th>
                  <th>类型</th>
                  <th>标题</th>
                </tr>
              </thead>
              <tbody>
                {page.records.map((item) => {
                  const meta = snippetMeta(item);
                  return (
                    <tr key={item.evidence_id}>
                      <td>
                        <button
                          type="button"
                          className={styles.linkButton}
                          onClick={() => void openDetail(item.evidence_id)}
                        >
                          {item.evidence_id}
                        </button>
                      </td>
                      <td>{item.task_id || '—'}</td>
                      <td>{sourceLabel(item.source)}</td>
                      <td>
                        <span
                          className={`${styles.kindBadge} ${
                            item.content_kind === 'search_snippet'
                              ? styles.snippetBadge
                              : ''
                          }`}
                        >
                          {kindLabel(item.content_kind)}
                        </span>
                      </td>
                      <td>
                        {item.title || titles.get(item.evidence_id) || '—'}
                        {item.content_kind === 'search_snippet' && meta ? (
                          <span className={styles.snippetMeta}>{meta}</span>
                        ) : null}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            <div className={styles.pager}>
              <span className="meta">
                {page.total} 条 · {offset + 1}–{offset + page.records.length}
              </span>
              <div className={styles.pagerButtons}>
                <button
                  type="button"
                  className="btn btn--sm"
                  disabled={offset === 0}
                  onClick={() => {
                    setDetail(null);
                    setOffset(Math.max(0, offset - PAGE_SIZE));
                  }}
                >
                  上一页
                </button>
                <button
                  type="button"
                  className="btn btn--sm"
                  disabled={page.next_offset === null}
                  onClick={() => {
                    setDetail(null);
                    setOffset(page.next_offset ?? offset);
                  }}
                >
                  下一页
                </button>
              </div>
            </div>
          </>
        )}
      </section>

      <aside className={styles.detailPane} aria-label="证据详情">
        {detailError && (
          <p className={styles.error} role="alert">
            {detailError}
          </p>
        )}
        {detail ? (
          <>
            <header className={styles.detailHead}>
              <div>
                <p className="meta">{detail.evidence.evidence_id}</p>
                <h3>{detail.evidence.title}</h3>
              </div>
              <button
                type="button"
                className={`btn btn--quiet btn--sm ${styles.closeButton}`}
                onClick={() => setDetail(null)}
              >
                关闭
              </button>
            </header>
            <p className={styles.detailMeta}>
              {detail.evidence.task_id || '—'} ·{' '}
              {sourceLabel(detail.evidence.source)} ·{' '}
              {kindLabel(detail.evidence.content_kind)} ·{' '}
              采集 {clockTime(detail.evidence.collected_at)}
              {publishDate(detail.evidence.published_at)
                ? ` · 发布 ${publishDate(detail.evidence.published_at)}`
                : ''}{' '}
              · {detail.total_length} 字符
              {detail.truncated ? ' · 已截断' : ''}
            </p>
            {detail.evidence.content_kind === 'search_snippet' &&
            snippetMeta(detail.evidence) ? (
              <p className={styles.detailMeta}>
                {snippetMeta(detail.evidence)}
              </p>
            ) : null}
            <p className={styles.summary}>{detail.evidence.summary}</p>
            <pre className={styles.text}>{detail.text}</pre>
          </>
        ) : null}
      </aside>
    </div>
  );
}
