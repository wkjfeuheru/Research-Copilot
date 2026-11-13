import { useEffect, useMemo, useState } from "react";

import {
  ApiError,
  fetchReportText,
  type ReportTextResponse,
} from "../api/client";
import { EmptyState } from "../components/EmptyState";
import { ConversationMarkdown } from "../components/ConversationMarkdown";
import styles from "./ReportPanel.module.css";

function stripLeadingTitle(content: string, topic: string): string {
  const match = /^#\s+([^\n]+)\n+/.exec(content);
  if (!match) return content;
  const heading = match[1].replace(/\s+/g, " ").trim();
  if (heading !== topic.replace(/\s+/g, " ").trim()) return content;
  return content.slice(match[0].length);
}

interface ReportPanelProps {
  reportId: string;
  topic: string;
  version?: string;
  signed: boolean;
}

export function ReportPanel({
  reportId,
  topic,
  version,
  signed,
}: ReportPanelProps) {
  const [report, setReport] = useState<ReportTextResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notReady, setNotReady] = useState(false);
  const [loading, setLoading] = useState(true);
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    fetchReportText(reportId, version, controller.signal)
      .then((data) => {
        setReport(data);
        setError(null);
        setNotReady(false);
      })
      .catch((err: unknown) => {
        if (controller.signal.aborted) return;
        setReport(null);
        if (err instanceof ApiError && err.status === 404) {
          setNotReady(true);
          setError(null);
        } else {
          setError(err instanceof Error ? err.message : "报告加载失败");
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [reloadKey, reportId, version]);

  const rawContent = report?.content ?? "";
  const body = useMemo(
    () => stripLeadingTitle(rawContent, topic),
    [rawContent, topic],
  );
  if (loading && !report) {
    return (
      <div
        className={styles.skeleton}
        aria-busy="true"
        aria-label="正在加载报告"
      >
        <span />
        <span />
        <span />
        <span />
      </div>
    );
  }

  if (error) {
    return (
      <EmptyState
        title="报告加载失败"
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

  if (notReady || !report) {
    return (
      <EmptyState
        title="报告尚未生成"
        description="写作完成后，冻结版本会显示在这里。"
      />
    );
  }

  return (
    <div className={styles.wrapper}>
      <div className={styles.versionBar} role="group" aria-label="报告版本">
        <span className={styles.tabOn}>
          {report.version ? `v${report.version}` : "当前正文"}
        </span>
        <span className={styles.versionMeta}>
          {report.is_frozen ? "已冻结" : "未冻结"} ·{" "}
          {report.content_hash.slice(0, 12)}
        </span>
      </div>

      <article className={styles.paper} aria-label="报告正文">
        <header className={styles.paperHead}>
          <div className={styles.paperTitle}>
            <p className={styles.kicker}>研究报告</p>
            <h2>{topic}</h2>
            {report.file_path && (
              <p className={styles.filePath}>{report.file_path}</p>
            )}
          </div>
          {report.is_frozen && (
            <span className={styles.seal} aria-label="已冻结报告">
              {signed ? "签核" : "冻结"}
            </span>
          )}
        </header>
        <div className={styles.markdown}>
          <ConversationMarkdown
            text={body}
            reportId={reportId}
            version={String(report.version)}
          />
        </div>
      </article>
    </div>
  );
}
