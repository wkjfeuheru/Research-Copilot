import assert from 'node:assert/strict';
import { test, after } from 'node:test';
import { createServer } from 'vite';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';

const vite = await createServer({ server: { middlewareMode: true, ws: false, watch: null }, appType: 'custom' });
after(() => vite.close());
const { payloadFor, ready } = await vite.ssrLoadModule('/src/pages/GateCard.tsx');
const { ReviewGate } = await vite.ssrLoadModule('/src/pages/gates/ReviewGate.tsx');
const interrupt = { id: 'i', kind: 'reviewer_deep_review', actions: [], review: {
  review_session_id: 'r', snapshot_id: 's', checks_complete: true,
  allowed_decisions: ['approve', 'request_changes', 'reject'], summary: '待处置', errors: [],
  issues: [{ issue_id: 'issue-1', severity: 'major', hard: false, location: '结论', reason: '口径待确认' }],
}};
const choice = { kind: 'review', decision: 'approve', comment: '', snapshot_id: 's', issue_dispositions: [
  { issue_id: 'issue-1', decision: 'false_positive', reason: '已核对原文，口径一致' },
] };

test('review payload retains per-issue identity, decision, reason and frozen snapshot', () => {
  assert.deepEqual(payloadFor(interrupt, choice), {
    decision: 'approve', comment: '', review_session_id: 'r', snapshot_id: 's',
    issue_dispositions: choice.issue_dispositions,
  });
});

test('approval requires every soft blocker disposition and cannot bypass hard or incomplete checks', () => {
  assert.equal(ready(interrupt, choice), true);
  assert.equal(ready(interrupt, { ...choice, issue_dispositions: [] }), false);
  assert.equal(ready(interrupt, { ...choice, issue_dispositions: [{ ...choice.issue_dispositions[0], reason: ' ' }] }), false);
  assert.equal(ready({ ...interrupt, review: { ...interrupt.review, checks_complete: false } }, choice), false);
  assert.equal(ready({ ...interrupt, review: { ...interrupt.review, issues: [{ ...interrupt.review.issues[0], hard: true }] } }, choice), false);
  assert.equal(ready(interrupt, { ...choice, decision: 'request_changes', comment: '修正口径', issue_dispositions: [] }), true);
});

test('review renders labelled disposition controls and keeps hard issue uneditable', () => {
  const props = { interrupt, busy: false, onChange() {} };
  const html = renderToStaticMarkup(createElement(ReviewGate, props));
  assert.match(html, /误报处置/);
  assert.match(html, /处置理由/);
  assert.match(html, /issue-1/);
  const hard = renderToStaticMarkup(createElement(ReviewGate, {
    ...props, interrupt: { ...interrupt, review: { ...interrupt.review, issues: [{ ...interrupt.review.issues[0], hard: true }] } },
  }));
  assert.doesNotMatch(hard, /<select/);
  assert.match(hard, /不能通过/);
});
