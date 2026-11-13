export type ToolDecision = 'approve' | 'reject';

export type ReviewDecision = 'approve' | 'request_changes' | 'reject';

export interface IssueDisposition {
  issue_id: string;
  decision: 'false_positive';
  reason: string;
}
