import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import {
  cancelReport,
  correctReport,
  createReport,
  resumeReport,
  sendMessage,
  type ActionSpec,
} from "../api/client";
import { ROLE_LABELS, SESSION_STATUS_LABELS } from "../api/labels";
import { useReportStream } from "../app/useReportStream";
import { useTypewriter } from "../app/useTypewriter";
import { describeWorkspace } from "../app/feed";
import { resolveStages } from "../app/stages";
import { ConversationComposer } from "../components/ConversationComposer";
import { ConversationMarkdown } from "../components/ConversationMarkdown";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { GateCard } from "./GateCard";
import { EvidencePanel } from "./EvidencePanel";
import { ExperiencePanel } from "./ExperiencePanel";
import { OverviewPanel } from "./OverviewPanel";
import { ReportPanel } from "./ReportPanel";
import { RunRecoveryPanel } from "./RunRecoveryPanel";
import styles from "../components/Conversation.module.css";

// 占位文案在打字机之前替换，保证动画目标与最终展示一致
function messageText(content: string): string {
  return content === "Report signed and workflow complete."
    ? "报告已签署，本次研究已完成。"
    : content;
}

export function ReportWorkspacePage() {
  const { reportId = "" } = useParams();
  const navigate = useNavigate();
  const {
    data,
    document,
    streamError,
    loadError,
    connection,
    gone,
    refresh,
    reconnect,
  } = useReportStream(reportId);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<ActionSpec | null>(null);
  const [drawer, setDrawer] = useState<
    "evidence" | "report" | "usage" | "experience" | "recovery" | null
  >(null);
  const modal = useRef<HTMLDialogElement>(null);
  const scroll = useRef<HTMLDivElement>(null);
  const end = useRef<HTMLDivElement>(null);
  const following = useRef(true);
  const pendingMessage = useRef<{ text: string; id: string } | null>(null);
  const [showJump, setShowJump] = useState(false);
  // 页面打开时已存在的消息 id 集合：历史与重连恢复的内容直接全文显示，不做打字机
  const seenMessages = useRef<{ reportId: string; ids: Set<string> } | null>(
    null,
  );
  // 消息列表提前到条件返回之前计算，保证下方 Hook 调用顺序无条件执行
  const messages = data?.messages?.length
    ? data.messages
    : data
      ? [
          {
            id: "initial",
            role: "user" as const,
            content: data.report.user_request,
            kind: "text",
            created_at: data.report.created_at || "",
          },
        ]
      : [];
  if (data && seenMessages.current?.reportId !== reportId) {
    seenMessages.current = {
      reportId,
      ids: new Set(messages.map((item) => item.id)),
    };
  }
  // 只让"最后一条新到达的助手消息"播放打字机，其余消息直接全文
  let animatedId: string | null = null;
  const seen = seenMessages.current;
  if (seen && seen.reportId === reportId) {
    for (const message of messages) {
      if (message.role !== "user" && !seen.ids.has(message.id))
        animatedId = message.id;
    }
  }
  const animatedMessage = animatedId
    ? messages.find((item) => item.id === animatedId)
    : undefined;
  const { displayed: messageDisplayed, typing: messageTyping } = useTypewriter(
    animatedMessage ? messageText(animatedMessage.content) : "",
    { enabled: Boolean(animatedMessage) },
  );
  const { displayed: reportDisplayed, typing: reportTyping } = useTypewriter(
    document?.content ?? "",
    { enabled: Boolean(document && !document.done) },
  );
  useEffect(() => {
    if (drawer && modal.current && !modal.current.open)
      modal.current.showModal();
    if (!drawer && modal.current?.open) modal.current.close();
  }, [drawer]);
  useEffect(() => {
    if (following.current) end.current?.scrollIntoView({ block: "end" });
  }, [
    data?.messages?.length,
    data?.interrupts.length,
    data?.runtime.busy,
    document?.offset,
    error,
    messageDisplayed.length,
    reportDisplayed.length,
  ]);

  async function send(text: string) {
    if (!data) return;
    setSending(true);
    setError(null);
    try {
      if (data.read_only || data.report_scope || data.plan || data.signed || data.report.status === "cancelled") {
        const result = await createReport(text, reportId);
        navigate(`/reports/${result.report.id}`);
      } else {
        const message =
          pendingMessage.current?.text === text
            ? pendingMessage.current
            : { text, id: `user-${crypto.randomUUID()}` };
        pendingMessage.current = message;
        await sendMessage(reportId, text, message.id);
        pendingMessage.current = null;
        following.current = true;
        await refresh();
      }
    } finally {
      setSending(false);
    }
  }
  async function action() {
    if (!confirm) return;
    setSending(true);
    setError(null);
    try {
      if (confirm.key === "cancel") await cancelReport(reportId);
      else if (confirm.key === "correct") {
        if (!confirm.dispatch_call_id) throw new Error("缺少失败派发标识，请刷新后重试");
        await correctReport(reportId, confirm.dispatch_call_id);
      } else await resumeReport(reportId);
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "未能完成操作");
    } finally {
      setSending(false);
      setConfirm(null);
    }
  }
  if (gone)
    return (
      <section className={styles.landing}>
        <h1>这段对话已不可访问</h1>
        <p className={styles.intro}>对话可能已删除。</p>
        <Link to="/">开始新的研究</Link>
      </section>
    );
  if (!data)
    return (
      <section className={styles.landing} aria-busy={!loadError}>
        {loadError ? (
          <>
            <p role="alert">{loadError}</p>
            <button className={styles.quiet} onClick={reconnect}>
              重新连接
            </button>
          </>
        ) : (
          <p>正在打开研究对话…</p>
        )}
      </section>
    );
  const busy = data.runtime.busy || sending;
  const queued = data.runtime.queue_state === "pending" || data.runtime.queue_state === "claimed";
  const recoveryRequired = data.runtime.queue_state === "recovery_required";
  const pending = data.interrupts.length > 0;
  const activeRoles = [...new Set((data.tasks ?? []).filter(task => task.status === "in_progress").map(task => ROLE_LABELS[task.role] || "研究"))];
  const role = activeRoles.length ? activeRoles.join("、") : data.workflow.role ? ROLE_LABELS[data.workflow.role] || "研究" : "规划";
  const status = data.read_only ? "历史会话只读" : data.waiting_for === "clarification" ? "等待你补充信息" : recoveryRequired
    ? "执行结果待核查"
    : queued
    ? "等待执行"
    : data.runtime.busy
    ? `正在${role}`
    : data.signed
      ? "研究已完成"
      : data.report.status === "awaiting_input"
        ? "等待你补充信息"
        : pending
          ? "需要你的确认"
          : data.report.status === "failed"
            ? "需要补充或处理"
            : SESSION_STATUS_LABELS[data.report.status] || "等待下一步";
  const lastActivity = data.activity?.at(-1);
  const latest = queued
    ? "任务已登记，等待执行"
    : lastActivity
    ? `${ROLE_LABELS[lastActivity.role] || "研究"} · ${lastActivity.event === "started" ? "正在处理" : "已完成一次处理"}`
    : describeWorkspace(data);
  const hasNewQuestion = Boolean(
    data.read_only || data.report_scope || data.plan || data.signed || data.report.status === "cancelled",
  );
  const cause =
    data.runtime.error_detail ||
    (data.workflow.phase === "blocked" ? data.workflow.reason : null) ||
    (data.runtime.last_error
      ? `研究未完成（${data.runtime.last_error}）。已有进度会保留。`
      : null);
  const stop = data.actions.find((item) => item.key === "cancel");
  return (
    <section className={styles.page} aria-label="研究对话">
      <div
        className={styles.scroll}
        ref={scroll}
        onScroll={() => {
          const target = scroll.current;
          if (target) {
            following.current =
              target.scrollHeight - target.scrollTop - target.clientHeight <
              150;
            setShowJump(!following.current);
          }
        }}
      >
        <div className={styles.transcript}>
          {messages.map((message) =>
            message.role === "user" ? (
              <div
                key={message.id}
                className={styles.user}
                aria-label="你的消息"
              >
                {message.content}
              </div>
            ) : (
              <article
                key={message.id}
                className={styles.assistant}
                aria-label="研究助手的回复"
              >
                <div className={styles.assistantHead}>
                  <span className={styles.avatar} aria-hidden="true">
                    R
                  </span>
                  研究助手
                </div>
                <ConversationMarkdown
                  text={
                    message.id === animatedId
                      ? messageTyping
                        ? `${messageDisplayed}▍`
                        : messageDisplayed
                      : messageText(message.content)
                  }
                  reportId={reportId}
                />
              </article>
            ),
          )}
          {document && (
            <article
              className={styles.assistant}
              aria-label="流式研究报告"
              aria-busy={!document.done}
            >
              <div className={styles.assistantHead}>
                <span className={styles.avatar} aria-hidden="true">
                  R
                </span>
                研究报告
                <span className={styles.badge}>
                  v{document.version} · {data.signed ? "已签署" : "待审核草稿"}
                </span>
              </div>
              <ConversationMarkdown
                text={reportTyping ? `${reportDisplayed}▍` : reportDisplayed}
                reportId={reportId}
                version={document.version}
              />
              <p className={styles.meta}>
                {document.done ? "完整内容已接收并校验" : "正在接收报告分片…"}
              </p>
            </article>
          )}
          <article className={styles.assistant} aria-label="研究进度">
            <div className={styles.assistantHead}>
              <span className={styles.avatar} aria-hidden="true">
                R
              </span>
              研究助手<span className={styles.meta}>· 进度与确认</span>
            </div>
            <div className={styles.progress}>
              <div className={styles.progressHead}>
                <span role="status">
                  {busy && <span className={styles.pulse} aria-hidden="true" />}
                  {status}
                </span>
                <span className={styles.meta}>
                  {connection === "live"
                    ? "实时连接"
                    : connection === "polling"
                      ? "重连中 · 状态轮询"
                      : "连接中"}
                </span>
              </div>
              <ol className={styles.steps} aria-label="研究阶段">
                {resolveStages(data).map((stage) => (
                  <li
                    key={stage.key}
                    className={styles.step}
                    data-state={stage.state}
                  >
                    {stage.state === "done" ? "✓" : "○"} {stage.label}
                  </li>
                ))}
              </ol>
              {data.workflow_version === "products-v2" && <>
                <h3 className={styles.taskHeading}>动态计划</h3>
                <ul className={styles.taskList} aria-label="动态任务清单">{(data.todos ?? []).map((todo, index) =>
                  <li key={`${index}:${todo.content}`}>{todo.status === "completed" ? "已完成" : todo.status === "in_progress" ? "进行中" : "待处理"} · {todo.content}</li>
                )}</ul>
                {!data.todos?.length && <p>主 Agent 尚未发布任务清单。</p>}
                <h3 className={styles.taskHeading}>实际执行</h3>
                <ul className={styles.taskList} aria-label="实际执行任务">{(data.tasks ?? []).map(task =>
                  <li key={task.dispatch_id}>{ROLE_LABELS[task.role] ?? task.role} · {task.objective} · {({ completed: "已提交", passed: "审核通过", in_progress: "执行中", failed: "失败", needs_input: "存在缺口", changes_requested: "需要返工", rejected: "未通过", stale: "版本已变化", unavailable: "不可用" } as Record<string, string>)[task.status] ?? "待核查"}{task.issue_ids?.length ? ` · 关联问题：${task.issue_ids.join("、")}` : ""}</li>
                )}</ul>
                <p>已派单 {data.dispatch_count ?? 0} 次，剩余 {data.dispatch_remaining ?? 12} 次。任务清单完成不代表报告已签核。</p>
              </>}
              {data.read_only && <p>历史报告、对话和审核记录仍可查看。继续研究请新建会话。</p>}
              <p className={styles.activity}>
                {data.evidence_count} 项来源
                {data.budget ? ` · ${data.budget.calls} 次模型调用` : ""}
                {latest ? ` · ${latest}` : ""}
              </p>
            </div>
            {cause && !data.runtime.busy && (
              <p className={styles.error} role="alert">
                {cause}
              </p>
            )}
            {error && (
              <p className={styles.error} role="alert">
                {error}
              </p>
            )}
            {loadError && (
              <p className={styles.error} role="alert">
                {loadError}
              </p>
            )}
            {streamError && (
              <p className={styles.error} role="alert">
                {streamError}{" "}
                <button className={styles.quiet} onClick={reconnect}>
                  重新连接
                </button>
              </p>
            )}
            {!data.runtime.busy && !recoveryRequired && pending && (
              <GateCard
                key={data.interrupts.map((item) => item.id).join(":")}
                reportId={reportId}
                data={data}
                onSubmitted={() => {
                  following.current = true;
                  void refresh();
                }}
                onError={setError}
              />
            )}
            <div className={styles.messageActions}>
              {!data.read_only && !data.runtime.busy && <button className={styles.quiet} onClick={() => setDrawer("recovery")}>运行恢复</button>}
              {data.evidence_count > 0 && (
                <button
                  className={styles.quiet}
                  onClick={() => setDrawer("evidence")}
                >
                  查看来源 · {data.evidence_count}
                </button>
              )}
              <button
                className={styles.quiet}
                onClick={() => setDrawer("usage")}
              >
                用量与记录
              </button>
              <button
                className={styles.quiet}
                onClick={() => setDrawer("experience")}
              >
                经验复盘
                {data.retrospective && data.retrospective.produced_count > 0
                  ? ` · ${data.retrospective.produced_count}`
                  : ""}
              </button>
              {data.report_document && (
                <button
                  className={styles.quiet}
                  onClick={() => setDrawer("report")}
                >
                  完整报告
                </button>
              )}
              {data.actions
                .filter((item) => item.key === "correct" || item.key === "resume")
                .map((item) => (
                  <button
                    key={item.key}
                    className={styles.quiet}
                    disabled={busy}
                    onClick={() => setConfirm(item)}
                  >
                    {item.key === "correct" ? item.label : data.report.status === "created" ? "启动研究" : "从检查点继续"}
                  </button>
                ))}
              {data.runtime.busy && stop && (
                <button
                  className={styles.quiet}
                  disabled={sending}
                  onClick={() => setConfirm(stop)}
                >
                  停止研究
                </button>
              )}
            </div>
          </article>
          <div ref={end} />
        </div>
        {showJump && (
          <button
            className={styles.jump}
            onClick={() => {
              following.current = true;
              end.current?.scrollIntoView({ behavior: "smooth" });
              setShowJump(false);
            }}
          >
            ↓ 回到最新
          </button>
        )}
      </div>
      <ConversationComposer
        onSend={send}
        busy={busy}
        disabled={pending || recoveryRequired}
        placeholder={
          recoveryRequired
            ? "执行结果待核查，请先处理运行恢复…"
            : queued
              ? "任务正在等待执行，可查看进度或停止研究…"
            : pending
            ? "请先处理上方待确认事项…"
            : busy
              ? "正在研究，你可以先查看进度…"
              : hasNewQuestion
                ? "输入新的问题，开始下一段研究…"
                : "补充研究期间、对象或你想了解的重点…"
        }
        hint={
          pending
            ? "普通消息不会替代批准、审核或签署"
            : hasNewQuestion
              ? "发送将新建研究对话，当前结果会保留"
              : "Enter 发送 · Shift + Enter 换行"
        }
      />
      <dialog
        ref={modal}
        className={styles.drawer}
        aria-labelledby="research-detail-title"
        onCancel={() => setDrawer(null)}
        onClick={(event) => {
          if (event.target === event.currentTarget) setDrawer(null);
        }}
      >
        <div className={styles.drawerHead}>
          <h2 id="research-detail-title">
            {drawer === "evidence"
              ? "研究来源"
              : drawer === "report"
                ? "完整报告"
                : drawer === "recovery"
                  ? "运行恢复"
                : drawer === "experience"
                  ? "经验复盘"
                  : "用量与研究记录"}
          </h2>
          <button
            className={styles.quiet}
            onClick={() => setDrawer(null)}
            aria-label="关闭详情"
          >
            关闭
          </button>
        </div>
        {drawer === "evidence" && (
          <EvidencePanel reportId={reportId} refs={[]} />
        )}
        {drawer === "report" && (
          <ReportPanel
            reportId={reportId}
            topic={data.plan?.objective || data.report.user_request}
            version={data.report_document?.version}
            signed={data.signed}
          />
        )}
        {drawer === "usage" && <OverviewPanel data={data} />}
        {drawer === "experience" && <fieldset disabled={data.read_only}><ExperiencePanel reportId={reportId} /></fieldset>}
        {drawer === "recovery" && <RunRecoveryPanel key={reportId} reportId={reportId} onChanged={refresh} />}
      </dialog>
      <ConfirmDialog
        open={confirm !== null}
        title={confirm?.key === "cancel" ? "停止本次研究？" : confirm?.key === "correct" ? "纠正采集结果？" : "继续研究？"}
        description={confirm?.description}
        confirmLabel={confirm?.key === "cancel" ? "停止研究" : confirm?.key === "correct" ? "授权纠正一次" : "继续研究"}
        cancelLabel="返回对话"
        tone={confirm?.key === "cancel" ? "danger" : "primary"}
        busy={sending}
        onConfirm={() => void action()}
        onCancel={() => {
          if (!sending) setConfirm(null);
        }}
      />
    </section>
  );
}
