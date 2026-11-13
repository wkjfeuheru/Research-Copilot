import { useCallback, useEffect, useState } from "react";
import { Link, Route, Routes, useLocation } from "react-router-dom";
import { deleteReport, fetchReports, type ReportSummary } from "../api/client";
import { SESSION_STATUS_LABELS } from "../api/labels";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { NotFoundPage } from "../pages/NotFoundPage";
import { ReportListPage } from "../pages/ReportListPage";
import { ReportWorkspacePage } from "../pages/ReportWorkspacePage";
import styles from "../components/Conversation.module.css";

export function App() {
  const location = useLocation();
  const [reports, setReports] = useState<ReportSummary[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const [target, setTarget] = useState<ReportSummary | null>(null);
  const [deleting, setDeleting] = useState(false);
  const activeId = /^\/reports\/([^/]+)/.exec(location.pathname)?.[1];
  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      const result = await fetchReports(signal);
      if (!signal?.aborted) {
        setReports(result.reports);
        setError(null);
      }
    } catch {
      if (!signal?.aborted) setError("对话列表未能加载");
    }
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    setOpen(false);
    return () => controller.abort();
  }, [load, location.pathname]);
  useEffect(() => {
    const update = (event: Event) => {
      const detail = (event as CustomEvent<{ id: string; status: string }>)
        .detail;
      setReports((current) =>
        current.map((report) =>
          report.id === detail.id
            ? { ...report, status: detail.status }
            : report,
        ),
      );
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    window.addEventListener("research-status", update);
    window.addEventListener("keydown", escape);
    return () => {
      window.removeEventListener("research-status", update);
      window.removeEventListener("keydown", escape);
    };
  }, []);
  async function remove() {
    if (!target) return;
    setDeleting(true);
    try {
      await deleteReport(target.id);
      setReports((current) =>
        current.filter((report) => report.id !== target.id),
      );
      setTarget(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "删除失败");
    } finally {
      setDeleting(false);
    }
  }
  return (
    <div className={styles.shell}>
      <a className="skip-link" href="#main-content">
        跳到对话
      </a>
      <aside
        id="conversation-sidebar"
        className={styles.sidebar}
        data-open={open}
        aria-label="研究对话列表"
      >
        <Link to="/" className={styles.brand}>
          <span className={styles.mark} aria-hidden="true">
            RC
          </span>
          Research Copilot
        </Link>
        <Link to="/" className={styles.newChat} onClick={() => setOpen(false)}>
          <span aria-hidden="true">＋</span>新建研究对话
        </Link>
        <nav className={styles.history} aria-label="最近对话">
          <p className={styles.overline}>最近的研究</p>
          {reports.length === 0 && !error && (
            <p className={styles.overline}>你的研究对话将保存在这里</p>
          )}
          {reports.map((report) => (
            <div
              key={report.id}
              className={`${styles.historyItem} ${activeId === report.id ? styles.selected : ""}`}
            >
              <Link
                to={`/reports/${report.id}`}
                aria-current={activeId === report.id ? "page" : undefined}
                onClick={() => setOpen(false)}
              >
                <strong>
                  {report.topic || report.user_request || "新的研究"}
                </strong>
                <small>
                  {SESSION_STATUS_LABELS[report.status] || report.status}
                </small>
              </Link>
              <button
                className={styles.trash}
                type="button"
                aria-label={`删除对话 ${report.user_request}`}
                disabled={report.read_only}
                title={report.read_only ? "历史会话只读" : "删除对话"}
                onClick={() => setTarget(report)}
              >
                ×
              </button>
            </div>
          ))}
          {error && (
            <p className={styles.error} role="alert">
              {error}{" "}
              <button className={styles.quiet} onClick={() => void load()}>
                重试
              </button>
            </p>
          )}
        </nav>
        <p className={styles.sidebarFoot}>本地研究空间 · 来源可追溯</p>
        {open && (
          <button className={styles.quiet} onClick={() => setOpen(false)}>
            收起对话列表
          </button>
        )}
      </aside>
      <div className={styles.main}>
        <header className={styles.topbar}>
          <button
            className={styles.menu}
            onClick={() => setOpen(!open)}
            aria-label="打开对话列表"
            aria-controls="conversation-sidebar"
            aria-expanded={open}
          >
            ☰
          </button>
          <span className={styles.title}>
            {reports.find((report) => report.id === activeId)?.user_request ||
              "研究对话"}
          </span>
          <span className={styles.local}>本地工作台</span>
        </header>
        <main id="main-content" className={styles.page}>
          <Routes>
            <Route path="/" element={<ReportListPage />} />
            <Route
              path="/reports/:reportId"
              element={<ReportWorkspacePage key={activeId} />}
            />
            <Route path="*" element={<NotFoundPage />} />
          </Routes>
        </main>
      </div>
      <ConfirmDialog
        open={target !== null}
        title="删除这段研究对话？"
        description="对话及其研究工作区会被清理，正在运行的对话无法删除。"
        confirmLabel="删除对话"
        cancelLabel="保留"
        tone="danger"
        busy={deleting}
        onConfirm={() => void remove()}
        onCancel={() => {
          if (!deleting) setTarget(null);
        }}
      />
    </div>
  );
}
