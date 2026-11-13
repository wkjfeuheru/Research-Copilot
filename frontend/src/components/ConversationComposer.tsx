import { useRef, useState } from "react";
import styles from "./Conversation.module.css";

export function ConversationComposer({
  onSend,
  disabled = false,
  busy = false,
  placeholder = "描述你的研究问题…",
  hint = "Enter 发送 · Shift + Enter 换行",
  initial = "",
}: {
  onSend: (text: string) => Promise<void>;
  disabled?: boolean;
  busy?: boolean;
  placeholder?: string;
  hint?: string;
  initial?: string;
}) {
  const [text, setText] = useState(initial);
  const [error, setError] = useState<string | null>(null);
  const submitting = useRef(false);
  async function submit() {
    if (!text.trim() || disabled || busy || submitting.current) return;
    submitting.current = true;
    setError(null);
    try {
      await onSend(text.trim());
      setText("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "发送失败，请重试。");
    } finally {
      submitting.current = false;
    }
  }
  return (
    <div className={styles.composerArea}>
      {error && (
        <p className={styles.error} role="alert">
          {error}
        </p>
      )}
      <form
        className={styles.composer}
        onSubmit={(event) => {
          event.preventDefault();
          void submit();
        }}
      >
        <textarea
          aria-label="研究消息"
          value={text}
          maxLength={8000}
          rows={2}
          disabled={disabled || busy}
          placeholder={placeholder}
          onChange={(event) => setText(event.target.value)}
          onKeyDown={(event) => {
            if (
              event.key === "Enter" &&
              !event.shiftKey &&
              !event.nativeEvent.isComposing
            ) {
              event.preventDefault();
              void submit();
            }
          }}
        />
        <div className={styles.composerFoot}>
          <span>{hint}</span>
          <button
            type="submit"
            disabled={disabled || busy || !text.trim()}
            aria-label="发送消息"
            className={styles.send}
          >
            {busy ? (
              "发送中"
            ) : (
              <>
                <span>发送</span>
                <span aria-hidden="true">↑</span>
              </>
            )}
          </button>
        </div>
      </form>
      <p className={styles.disclaimer}>
        研究结论以来源、分析与审核结果为准。关键决策会在对话中请你确认。
      </p>
    </div>
  );
}
