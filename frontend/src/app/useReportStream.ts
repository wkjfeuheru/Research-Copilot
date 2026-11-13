/** SSE 传输实际状态和报告分片；断线时只读轮询兜底。 */

import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError, fetchStatus, type WorkspaceResponse } from "../api/client";
import { WorkspaceUpdates } from './workspaceUpdates';
import {
  applyReportDelta,
  type ConnectionState,
  type StreamDocument,
} from "../api/events";

export interface ReportStream {
  data: WorkspaceResponse | null;
  document: StreamDocument | null;
  streamError: string | null;
  loadError: string | null;
  connection: ConnectionState;
  gone: boolean;
  refresh: () => Promise<void>;
  reconnect: () => void;
}

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback;
}

export function useReportStream(reportId: string): ReportStream {
  const [data, setData] = useState<WorkspaceResponse | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [connection, setConnection] = useState<ConnectionState>("idle");
  const [gone, setGone] = useState(false);
  const [reconnectKey, setReconnectKey] = useState(0);
  const previous = useRef<WorkspaceResponse | null>(null);
  const [document, setDocument] = useState<StreamDocument | null>(null);
  const [streamError, setStreamError] = useState<string | null>(null);
  const documentRef = useRef<StreamDocument | null>(null);
  const streamLive = useRef(false);
  const updates = useRef(new WorkspaceUpdates());

  const apply = useCallback((next: WorkspaceResponse) => {
    previous.current = next;
    setData(next);
    setLoadError(null);
    setGone(false);
    if (!streamLive.current)
      setConnection(next.runtime.busy ? "polling" : "idle");
    window.dispatchEvent(
      new CustomEvent("research-status", {
        detail: { id: next.report.id, status: next.report.status },
      }),
    );
  }, []);

  const refresh = useCallback(async () => {
    await updates.current.fetch(() => fetchStatus(reportId), apply, (error) => {
      if (error instanceof ApiError && error.status === 404) {
        setGone(true);
        setConnection("gone");
        return;
      }
      setLoadError(errorMessage(error, "状态加载失败"));
    });
  }, [apply, reportId]);

  useEffect(() => {
    updates.current.invalidate();
    previous.current = null;
    documentRef.current = null;
    setDocument(null);
    setStreamError(null);
    setData(null);
    setLoadError(null);
    setGone(false);
    setConnection("connecting");
    const controller = new AbortController();
    let cancelled = false;
    void updates.current.fetch(() => fetchStatus(reportId, controller.signal), (next) => {
        if (!cancelled && (!streamLive.current || !previous.current))
          apply(next);
      }, (error: unknown) => {
        if (cancelled || controller.signal.aborted) return;
        if (error instanceof ApiError && error.status === 404) {
          setGone(true);
          setConnection("gone");
          return;
        }
        setLoadError(errorMessage(error, "状态加载失败"));
        setConnection("idle");
      });
    return () => {
      cancelled = true;
      updates.current.invalidate();
      controller.abort();
    };
  }, [apply, reconnectKey, reportId]);

  useEffect(() => {
    let disposed = false;
    const source = new EventSource(
      `/api/reports/${encodeURIComponent(reportId)}/events`,
    );
    source.onopen = () => {
      if (disposed) return;
      streamLive.current = true;
      setConnection("live");
      setStreamError(null);
    };
    const listen = <T>(name: string, handler: (value: T) => void) => {
      source.addEventListener(name, (event) => {
        if (disposed) return;
        try {
          handler(JSON.parse((event as MessageEvent).data));
        } catch {
          setStreamError("流式传输未完成，请重新连接以核对完整内容。");
        }
      });
    };
    listen("workspace", (next: WorkspaceResponse) => {
      updates.current.invalidate();
      apply(next);
    });
    listen(
      "report.start",
      (meta: { content_hash: string; version: string; offset: number }) => {
        const current = documentRef.current;
        if (
          meta.offset &&
          (!current ||
            current.content_hash !== meta.content_hash ||
            current.offset !== meta.offset)
        ) {
          setStreamError("分片状态已改变，请重新连接以读取完整报告。");
          return;
        }
        const next =
          meta.offset && current
            ? { ...current, done: false }
            : { ...meta, content: "", done: false };
        documentRef.current = next;
        setDocument(next);
      },
    );
    listen(
      "report.delta",
      (chunk: { content_hash: string; offset: number; text: string }) => {
        if (!documentRef.current) throw new Error("Missing report header");
        const next = applyReportDelta(documentRef.current, chunk);
        documentRef.current = next;
        setDocument(next);
      },
    );
    listen(
      "report.complete",
      (meta: { content_hash: string; length: number }) => {
        const current = documentRef.current;
        if (
          !current ||
          current.content_hash !== meta.content_hash ||
          current.offset !== meta.length
        )
          throw new Error("Incomplete report");
        void crypto.subtle
          .digest("SHA-256", new TextEncoder().encode(current.content))
          .then((hash) => {
            if (disposed || documentRef.current !== current) return;
            const actual = [...new Uint8Array(hash)]
              .map((value) => value.toString(16).padStart(2, "0"))
              .join("");
            if (actual !== meta.content_hash) {
              setStreamError("报告内容校验失败，请重新连接。");
              return;
            }
            const next = { ...current, done: true };
            documentRef.current = next;
            setDocument(next);
          })
          .catch(() => {
            if (!disposed) setStreamError("无法核对报告完整性，请重新连接。");
          });
      },
    );
    listen("report.reset", () => {
      documentRef.current = null;
      setDocument(null);
    });
    listen("gone", () => {
      updates.current.invalidate();
      source.close();
      setGone(true);
      setConnection("gone");
    });
    listen("stream.error", (error: { message: string }) => {
      setStreamError(error.message);
    });
    source.onerror = () => {
      if (!disposed) {
        streamLive.current = false;
        setConnection("polling");
      }
    };
    const fallback = setInterval(() => {
      if (!streamLive.current && !disposed) void refresh();
    }, 3000);
    return () => {
      disposed = true;
      streamLive.current = false;
      source.close();
      clearInterval(fallback);
    };
  }, [apply, reconnectKey, refresh, reportId]);

  const reconnect = useCallback(() => {
    setReconnectKey((value) => value + 1);
  }, []);

  return {
    data,
    document,
    streamError,
    loadError,
    connection,
    gone,
    refresh,
    reconnect,
  };
}
