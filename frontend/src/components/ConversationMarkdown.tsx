import ReactMarkdown, { defaultUrlTransform } from "react-markdown";
import remarkGfm from "remark-gfm";
import { ReportChart } from "./ReportChart";
import styles from "./Conversation.module.css";

export function ConversationMarkdown({
  text,
  reportId,
  version,
}: {
  text: string;
  reportId: string;
  version?: string;
}) {
  return (
    <div className={styles.answer}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        urlTransform={(url) =>
          /^chart:C-[0-9a-f]{16}$/.test(url) ? url : defaultUrlTransform(url)
        }
        components={{
          img({ src, alt }) {
            const match =
              typeof src === "string"
                ? /^chart:(C-[0-9a-f]{16})$/.exec(src)
                : null;
            if (match && version)
              return (
                <ReportChart
                  reportId={reportId}
                  version={version}
                  chartId={match[1]}
                />
              );
            return <span>{alt || "图表"}（需已登记的图表引用）</span>;
          },
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}
