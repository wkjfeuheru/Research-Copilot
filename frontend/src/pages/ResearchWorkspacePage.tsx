import { useRef, useState } from 'react';

import { getSkillNames } from '../app/skills';
import type { LocalAttachment } from '../app/types';
import { ChatComposer } from '../components/ChatComposer';
import { ResearchContext } from '../components/ResearchContext';
import styles from './ResearchWorkspacePage.module.css';

const SUGGESTIONS = [
  {
    label: '个股研究',
    question: '这家公司的利润增长是否有现金流支撑？',
  },
  {
    label: '行业对比',
    question: '三家代表公司的经营质量与风险有何差异？',
  },
] as const;

function createAttachment(file: File): LocalAttachment {
  return {
    id: crypto.randomUUID(),
    name: file.name,
    size: file.size,
  };
}

export function ResearchWorkspacePage() {
  const [question, setQuestion] = useState('');
  const [attachments, setAttachments] = useState<LocalAttachment[]>([]);
  const [selectedSkillIds, setSelectedSkillIds] = useState<string[]>([]);
  const [notice, setNotice] = useState<string | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  function focusComposer() {
    requestAnimationFrame(() => textareaRef.current?.focus());
  }

  function resetWorkspace() {
    setQuestion('');
    setAttachments([]);
    setSelectedSkillIds([]);
    setNotice(null);
    focusComposer();
  }

  function applySuggestion(suggestion: string) {
    setQuestion(suggestion);
    setNotice('已填入示例问题；当前不会提交研究或生成简报。');
    focusComposer();
  }

  function addAttachments(files: File[]) {
    setAttachments((current) => {
      const existingNames = new Set(current.map((item) => item.name));
      const additions = files
        .filter((file) => !existingNames.has(file.name))
        .map(createAttachment);
      return [...current, ...additions];
    });
    setNotice(
      `已选择 ${files.length} 个附件：仅本地展示，未上传、未解析，也不会创建研究任务。`,
    );
  }

  function removeAttachment(id: string) {
    setAttachments((current) => current.filter((item) => item.id !== id));
  }

  function toggleSkill(id: string) {
    setSelectedSkillIds((current) =>
      current.includes(id)
        ? current.filter((item) => item !== id)
        : [...current, id],
    );
  }

  function sendMessage() {
    if (question.trim().length === 0) {
      return;
    }
    setNotice(
      '骨架阶段尚未接入研究服务：消息未提交，未创建研究任务，也不会生成简报。',
    );
  }

  return (
    <div className={styles.workspace}>
      <aside className={styles.outline} aria-label="研究导航">
        <p className={styles.outlineLabel}>最近研究</p>
        <p className={styles.outlineEmpty}>
          还没有研究任务。
          <br />
          阶段一不会创建任务。
        </p>
        <button
          type="button"
          className={styles.newResearch}
          onClick={resetWorkspace}
        >
          <span aria-hidden="true">＋</span> 新建研究
        </button>
        <hr className={styles.divider} />
        <p className={styles.outlineLabel}>研究空间</p>
        <p className={styles.outlineNote}>
          从一个问题开始。资料与技能随消息一起提交；当前只演示本地界面状态。
        </p>
      </aside>

      <section className={styles.main} aria-labelledby="research-heading">
        <div className={styles.titlebar}>
          <div className={styles.titleLeft}>
            <span className={styles.indicator} aria-hidden="true" />
            <span>新建研究</span>
          </div>
          <span className={styles.titleMeta}>个人研究空间 · A 股</span>
        </div>

        <div className={styles.welcome}>
          <div className={styles.emblem} aria-hidden="true">
            <span className={styles.emblemMark}>r</span>
            <span className={styles.emblemText}>研究，由一个好问题开始</span>
          </div>
          <h1 id="research-heading" className={styles.heading}>
            今天，想研究什么？
          </h1>
          <p className={styles.intro}>
            提出一个 A 股个股或行业问题，带上你的资料。
            <br />
            当前是工程骨架：可以体验输入与本地选择，尚不会开始真实研究。
          </p>
          <div className={styles.suggestions}>
            {SUGGESTIONS.map((suggestion) => (
              <button
                key={suggestion.label}
                type="button"
                className={styles.suggestion}
                onClick={() => applySuggestion(suggestion.question)}
              >
                <span className={styles.suggestionLabel}>
                  {suggestion.label}
                </span>
                {suggestion.question}
                <span className={styles.suggestionArrow} aria-hidden="true">
                  →
                </span>
              </button>
            ))}
          </div>
        </div>

        <ChatComposer
          value={question}
          onChange={setQuestion}
          attachments={attachments}
          onAddAttachments={addAttachments}
          onRemoveAttachment={removeAttachment}
          selectedSkillIds={selectedSkillIds}
          onToggleSkill={toggleSkill}
          onSend={sendMessage}
          notice={notice}
          textareaRef={textareaRef}
        />

        <p className={styles.footnote}>
          第一阶段工程骨架 · 未接入模型、搜索、MCP 或文件解析 · 发送不会创建研究任务
        </p>
      </section>

      <aside className={styles.contextPane} aria-label="研究上下文">
        <ResearchContext
          attachments={attachments}
          selectedSkillNames={getSkillNames(selectedSkillIds)}
        />
      </aside>
    </div>
  );
}
