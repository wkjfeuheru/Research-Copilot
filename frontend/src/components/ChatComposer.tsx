import {
  useId,
  useRef,
  useState,
  type ChangeEvent,
  type KeyboardEvent,
  type RefObject,
} from 'react';

import { formatBytes } from '../app/format';
import { PLACEHOLDER_SKILLS } from '../app/skills';
import type { LocalAttachment } from '../app/types';
import styles from './ChatComposer.module.css';

interface ChatComposerProps {
  value: string;
  onChange: (value: string) => void;
  attachments: readonly LocalAttachment[];
  onAddAttachments: (files: File[]) => void;
  onRemoveAttachment: (id: string) => void;
  selectedSkillIds: readonly string[];
  onToggleSkill: (id: string) => void;
  onSend: () => void;
  notice: string | null;
  textareaRef: RefObject<HTMLTextAreaElement | null>;
}

const ACCEPTED_FILES = '.pdf,.md,.txt,.csv,.xlsx,.json';

export function ChatComposer({
  value,
  onChange,
  attachments,
  onAddAttachments,
  onRemoveAttachment,
  selectedSkillIds,
  onToggleSkill,
  onSend,
  notice,
  textareaRef,
}: ChatComposerProps) {
  const [isSkillPickerOpen, setIsSkillPickerOpen] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const skillPickerId = useId();
  const skillPickerNoteId = useId();
  const canSend = value.trim().length > 0;
  const selectedSkills = PLACEHOLDER_SKILLS.filter((skill) =>
    selectedSkillIds.includes(skill.id),
  );

  function handleFiles(event: ChangeEvent<HTMLInputElement>) {
    const files = Array.from(event.currentTarget.files ?? []);
    if (files.length > 0) {
      onAddAttachments(files);
    }
    event.currentTarget.value = '';
  }

  function handleKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (
      event.key === 'Enter' &&
      !event.shiftKey &&
      !event.nativeEvent.isComposing
    ) {
      event.preventDefault();
      if (canSend) {
        onSend();
      }
    }
  }

  return (
    <section className={styles.composer} aria-label="研究消息输入区">
      {attachments.length > 0 ? (
        <div className={styles.files} aria-label="本地选择的附件">
          {attachments.map((attachment) => (
            <span key={attachment.id} className={styles.fileChip}>
              <span className={styles.fileName}>{attachment.name}</span>
              <span className={styles.fileSize}>
                {formatBytes(attachment.size)}
              </span>
              <button
                type="button"
                className={styles.removeChip}
                onClick={() => onRemoveAttachment(attachment.id)}
                aria-label={`移除附件 ${attachment.name}`}
              >
                ×
              </button>
            </span>
          ))}
        </div>
      ) : null}

      <label className="visually-hidden" htmlFor="research-question">
        研究问题
      </label>
      <textarea
        id="research-question"
        ref={textareaRef}
        className={styles.textarea}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        onKeyDown={handleKeyDown}
        placeholder="输入研究问题，或补充你的想法…"
        rows={4}
      />

      {selectedSkills.length > 0 ? (
        <div className={styles.chips} aria-label="已选择的占位技能">
          {selectedSkills.map((skill) => (
            <button
              key={skill.id}
              type="button"
              className={styles.chip}
              onClick={() => onToggleSkill(skill.id)}
              aria-label={`移除技能 ${skill.name}`}
            >
              {skill.name}
              <span aria-hidden="true">×</span>
            </button>
          ))}
        </div>
      ) : null}

      <div
        id={skillPickerId}
        className={styles.skillPicker}
        hidden={!isSkillPickerOpen}
        aria-describedby={skillPickerNoteId}
      >
        <div className={styles.skillPickerHeader}>
          <strong className={styles.skillPickerTitle}>
            选择研究方法（占位）
          </strong>
          <button
            type="button"
            className={styles.closePicker}
            onClick={() => setIsSkillPickerOpen(false)}
            aria-label="关闭技能选择"
          >
            ×
          </button>
        </div>
        <p id={skillPickerNoteId} className={styles.pickerNote}>
          技能目录尚未接入；这里只展示选择状态，不会加载或执行技能。
        </p>
        {PLACEHOLDER_SKILLS.map((skill) => {
          const isSelected = selectedSkillIds.includes(skill.id);
          return (
            <button
              key={skill.id}
              type="button"
              className={
                isSelected
                  ? `${styles.skillOption} ${styles.skillOptionSelected}`
                  : styles.skillOption
              }
              onClick={() => onToggleSkill(skill.id)}
              aria-pressed={isSelected}
            >
              <span className={styles.skillName}>{skill.name}</span>
              <span className={styles.skillDescription}>
                {skill.description}
              </span>
              <span className={styles.skillMark} aria-hidden="true">
                {isSelected ? '✓' : '＋'}
              </span>
            </button>
          );
        })}
      </div>

      <div className={styles.toolbar}>
        <div className={styles.tools}>
          <button
            type="button"
            className={styles.tool}
            onClick={() => fileInputRef.current?.click()}
          >
            <span aria-hidden="true">＋</span> 附件
          </button>
          <button
            type="button"
            className={
              isSkillPickerOpen
                ? `${styles.tool} ${styles.toolActive}`
                : styles.tool
            }
            onClick={() => setIsSkillPickerOpen((open) => !open)}
            aria-expanded={isSkillPickerOpen}
            aria-controls={skillPickerId}
          >
            <span aria-hidden="true">⌘</span> 技能
            {selectedSkillIds.length > 0
              ? ` · ${selectedSkillIds.length}`
              : ''}
          </button>
        </div>
        <div className={styles.sendGroup}>
          <span className={styles.hint}>Enter 发送 · Shift+Enter 换行</span>
          <button
            type="button"
            className={styles.send}
            onClick={onSend}
            disabled={!canSend}
            aria-label="发送研究消息"
          >
            →
          </button>
        </div>
      </div>

      <input
        ref={fileInputRef}
        type="file"
        multiple
        hidden
        accept={ACCEPTED_FILES}
        onChange={handleFiles}
      />

      {notice ? (
        <p className={styles.notice} role="status">
          {notice}
        </p>
      ) : (
        <p className={styles.footnote}>
          骨架阶段：附件仅本地展示，消息不会提交。
        </p>
      )}
    </section>
  );
}
