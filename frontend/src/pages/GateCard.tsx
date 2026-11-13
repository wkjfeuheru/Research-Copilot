import { useState } from 'react';

import { submitDecisions, type InterruptView, type WorkspaceResponse } from '../api/client';
import { AnalysisGate } from './gates/AnalysisGate';
import { PlanGate } from './gates/PlanGate';
import { ReviewGate } from './gates/ReviewGate';
import { ReworkGate } from './gates/ReworkGate';
import { SignoffGate } from './gates/SignoffGate';
import styles from './gates/gates.module.css';
import type { IssueDisposition, ReviewDecision, ToolDecision } from './gates/types';

export interface GateCardProps {
  reportId: string;
  data: WorkspaceResponse;
  onSubmitted: () => void;
  onError: (message: string | null) => void;
}

type Choice =
  | { kind: 'clarification'; answer: string }
  | { kind: 'tool'; decision: ToolDecision }
  | { kind: 'review'; decision: ReviewDecision; comment: string; issue_dispositions: IssueDisposition[]; snapshot_id: string | null | undefined };

export function payloadFor(interrupt: InterruptView, choice: Choice): Record<string, unknown> {
  if (choice.kind === 'clarification') return { answer: choice.answer.trim() };
  if (choice.kind === 'review') {
    return { decision: choice.decision, comment: choice.comment, issue_dispositions: choice.issue_dispositions, review_session_id: interrupt.review?.review_session_id, snapshot_id: choice.snapshot_id };
  }
  const count = Math.max(interrupt.actions.length, 1);
  return { decisions: Array.from({ length: count }, () => ({ type: choice.decision })) };
}

export function ready(interrupt: InterruptView, choice: Choice | undefined): boolean {
  if (!choice) return false;
  if (interrupt.kind === 'clarification') return choice.kind === 'clarification' && choice.answer.trim().length > 0 && choice.answer.length <= 8000;
  if (interrupt.kind === 'reviewer_deep_review') {
    const review = interrupt.review;
    if (choice.kind !== 'review' || !review?.snapshot_id || choice.snapshot_id !== review.snapshot_id || !review.allowed_decisions.includes(choice.decision)) return false;
    if (choice.decision !== 'approve') return choice.comment.trim().length > 0;
    return review.checks_complete === true && review.issues.every(issue => {
      if (issue.hard) return false;
      if (!['major', 'critical'].includes(issue.severity)) return true;
      return Boolean(issue.issue_id) && choice.issue_dispositions.some(item => item.issue_id === issue.issue_id && item.decision === 'false_positive' && item.reason.trim().length > 0);
    });
  }
  return choice.kind === 'tool';
}

export function GateCard({ reportId, data, onSubmitted, onError }: GateCardProps) {
  const interrupts = data.interrupts;
  const [choices, setChoices] = useState<Record<string, Choice>>({});
  const [busy, setBusy] = useState(false);
  const complete = interrupts.length > 0 && interrupts.every((item) => {
    const choice = choices[item.id];
    if (item.tool === 'signoff_report' && choice?.kind === 'tool' && choice.decision === 'approve' && !data.signoff_ready) return false;
    return ready(item, choice);
  });

  async function submit() {
    if (busy || !complete) return;
    setBusy(true);
    onError(null);
    const decisions: Record<string, unknown> = {};
    for (const interrupt of interrupts) {
      const choice = choices[interrupt.id];
      if (!choice) return;
      decisions[interrupt.id] = payloadFor(interrupt, choice);
    }
    try {
      await submitDecisions(reportId, decisions);
      setChoices({});
      onSubmitted();
    } catch (error) {
      onError(error instanceof Error ? error.message : '提交失败');
    } finally {
      setBusy(false);
    }
  }

  if (interrupts.length === 0 || data.read_only) return null;

  return (
    <div>
      {interrupts.map((interrupt) => {
        const choice = choices[interrupt.id];
        const toolChoice = choice?.kind === 'tool' ? choice.decision : null;
        if (interrupt.kind === 'clarification') {
          return <label key={interrupt.id}>{interrupt.question}
            <textarea aria-label="补充研究信息" maxLength={8000} disabled={busy}
              value={choice?.kind === 'clarification' ? choice.answer : ''}
              onChange={event => setChoices(current => ({ ...current, [interrupt.id]: { kind: 'clarification', answer: event.target.value } }))} />
          </label>;
        }
        if (interrupt.kind === 'reviewer_deep_review') {
          return (
            <ReviewGate
              key={`${interrupt.id}:${interrupt.review?.snapshot_id}`}
              interrupt={interrupt}
              busy={busy}
              onChange={(decision, comment, issue_dispositions) => {
                setChoices((current) => {
                  if (!decision) {
                    const next = { ...current };
                    delete next[interrupt.id];
                    return next;
                  }
                  return { ...current, [interrupt.id]: { kind: 'review', decision, comment, issue_dispositions, snapshot_id: interrupt.review?.snapshot_id } };
                });
              }}
            />
          );
        }
        const onChoose = (decision: ToolDecision) =>
          setChoices((current) => ({ ...current, [interrupt.id]: { kind: 'tool', decision } }));
        if (data.workflow_version === 'products-v2' && interrupt.tool !== 'signoff_report') return null;
        if (interrupt.tool === 'confirm_analysis') {
          return (
            <AnalysisGate
              key={interrupt.id}
              analysis={data.analysis}
              choice={toolChoice}
              busy={busy}
              onChoose={onChoose}
            />
          );
        }
        if (interrupt.tool === 'confirm_rework') {
          return (
            <ReworkGate
              key={interrupt.id}
              rework={data.rework}
              choice={toolChoice}
              busy={busy}
              onChoose={onChoose}
            />
          );
        }
        if (interrupt.tool === 'signoff_report') {
          return (
            <SignoffGate
              key={interrupt.id}
              review={data.review}
              signoffReady={data.signoff_ready}
              choice={toolChoice}
              busy={busy}
              onChoose={onChoose}
            />
          );
        }
        return (
          <PlanGate
            key={interrupt.id}
            plan={data.plan}
            choice={toolChoice}
            busy={busy}
            onChoose={onChoose}
          />
        );
      })}
      <div className={styles.actions}>
        <button type="button" className="btn btn--primary" disabled={busy || !complete} onClick={() => void submit()}>
          {busy ? '提交中…' : '提交决定'}
        </button>
      </div>
    </div>
  );
}
