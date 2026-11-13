import { useEffect, useRef, useState } from 'react';
import { chartUrl, fetchReportChart, type ReportChartResponse } from '../api/client';
import styles from './ReportChart.module.css';

export function ReportChart({ reportId, version, chartId }: { reportId: string; version: string; chartId: string }) {
  const mount = useRef<HTMLSpanElement>(null);
  const [manifest, setManifest] = useState<ReportChartResponse | null>(null);
  const [interactive, setInteractive] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [imageFailed, setImageFailed] = useState(false);
  const prefix = chartUrl(reportId, version, chartId);

  useEffect(() => {
    const controller = new AbortController();
    let chart: { resize(): void; dispose(): void } | undefined;
    let observer: ResizeObserver | undefined;
    setManifest(null); setInteractive(false); setError(null); setImageFailed(false);
    async function load() {
      try {
        const data = await fetchReportChart(reportId, version, chartId, controller.signal);
        if (controller.signal.aborted) return;
        // Asset URLs are version-bound same-origin routes; reject redirects/remote paths.
        if (data.report_id !== reportId || data.report_version !== version || data.chart_id !== chartId ||
          data.static_url !== `${prefix}/png` || (data.option_url && data.option_url !== `${prefix}/option`)) throw new Error('图表版本不匹配');
        setManifest(data);
        if (data.engine !== 'echarts') return;
        if (!data.option_url || data.theme_version !== 'research-v1') throw new Error('图表配置不可用');
        const response = await fetch(data.option_url, { signal: controller.signal, redirect: 'error' });
        if (!response.ok || Number(response.headers.get('Content-Length') || '0') > 1048576) throw new Error('图表配置读取失败');
        const text = await response.text();
        if (new TextEncoder().encode(text).length > 1048576) throw new Error('图表配置过大');
        const { mountChart } = await import('./chartEngine');
        if (controller.signal.aborted || !mount.current) return;
        chart = mountChart(mount.current, JSON.parse(text));
        observer = new ResizeObserver(() => chart?.resize());
        observer.observe(mount.current);
        setInteractive(true);
      } catch {
        if (!controller.signal.aborted) setError('交互不可用，显示同版本静态图');
      }
    }
    void load();
    // React effects own external instances and always undo them, including StrictMode.
    // https://react.dev/reference/react/useEffect#connecting-to-an-external-system
    return () => { controller.abort(); observer?.disconnect(); chart?.dispose(); };
  }, [chartId, prefix, reportId, version]);

  return <span className={styles.root} role="figure" aria-label={manifest?.title ?? '报告图表'}>
    <span ref={mount} className={`${styles.interactive} ${interactive ? '' : styles.inactive}`} aria-hidden={!interactive} />
    <img className={interactive ? styles.printOnly : styles.static} src={`${prefix}/png`}
      alt={manifest?.alt ?? '冻结版本图表'} onError={() => setImageFailed(true)} />
    <span className={styles.caption}>{manifest?.title ?? chartId} · 版本 {version}</span>
    {error && <span className={styles.notice} role="status">{error}</span>}
    {imageFailed && <span className={styles.notice} role="status">静态图读取失败，请重新加载报告</span>}
    {interactive && <span className={styles.hint}>悬浮查看数值，可缩放或切换系列；打印使用冻结静态图。</span>}
  </span>;
}
