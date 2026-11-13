import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { createReport, fetchServiceHealth, type ServiceHealth } from "../api/client";
import { ConversationComposer } from "../components/ConversationComposer";
import styles from "../components/Conversation.module.css";

const examples = [
  ["业绩解读", "比较一家公司的季度业绩，解释利润变化的原因"],
  ["公司研究", "从业务、财务和风险三个维度研究一家上市公司"],
  ["行业观察", "分析一个行业的竞争格局与关键增长驱动因素"],
  ["数据核验", "核对一项研究判断，区分证据、假设和仍待确认的部分"],
];
export function ReportListPage() {
  const navigate = useNavigate();
  const [creating, setCreating] = useState(false);
  const [example, setExample] = useState("");
  const [health, setHealth] = useState<ServiceHealth | null>(null);
  const [healthError, setHealthError] = useState("");
  const [checking, setChecking] = useState(true);
  const [healthRevision, setHealthRevision] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setChecking(true);
    setHealthError("");
    void fetchServiceHealth(controller.signal)
      .then((value) => { if (!controller.signal.aborted) setHealth(value); })
      .catch(() => { if (!controller.signal.aborted) setHealthError("暂时无法连接研究服务，请恢复连接后重试。"); })
      .finally(() => { if (!controller.signal.aborted) setChecking(false); });
    return () => controller.abort();
  }, [healthRevision]);
  const blocked = Boolean(healthError) || health?.status === "degraded";
  async function send(text: string) {
    setCreating(true);
    try {
      const result = await createReport(text, "landing");
      navigate(`/reports/${result.report.id}`);
    } finally {
      setCreating(false);
    }
  }
  return (
    <section className={styles.landing} aria-label="开始研究对话">
      <p className={styles.eyebrow}>RESEARCH, WITH CONTEXT</p>
      <h1>
        从一个问题，
        <br />
        开始有据可循的研究。
      </h1>
      <p className={styles.intro}>
        告诉我你想了解什么。我会整理研究计划、查找材料、分析数据，
        <br />
        把进度和需要你确认的地方，放在同一段对话里。
      </p>
      <div className={styles.suggestions}>
        {examples.map(([title, text]) => (
          <button
            key={title}
            type="button"
            disabled={creating}
            onClick={() => setExample(text)}
          >
            <strong>
              {title} <span aria-hidden="true">↗</span>
            </strong>
            <small>{text}</small>
          </button>
        ))}
      </div>
      {(checking || blocked) && (
        <div role="status" aria-live="polite">
          <p className={blocked ? styles.error : styles.intro}>
            {checking ? "正在检查研究服务…" : healthError || health?.sources?.detail || health?.message}
          </p>
          {blocked && <button type="button" disabled={checking} onClick={() => setHealthRevision((value) => value + 1)}>重新检查</button>}
        </div>
      )}
      <ConversationComposer
        key={example}
        initial={example}
        onSend={send}
        busy={creating}
        disabled={checking || blocked}
        placeholder="例如：比较宁德时代两个季度的毛利率，并解释主要驱动因素…"
      />
    </section>
  );
}
