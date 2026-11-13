import assert from 'node:assert/strict';
import { test, after } from 'node:test';
import { createServer } from 'vite';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';

const vite = await createServer({ server: { middlewareMode: true, ws: false, watch: null }, appType: 'custom' });
after(() => vite.close());

test('run recovery separates pending classification, usage, paid retry and ownership release', async () => {
  const { requestRecoveryPayload, releasePayload, RunRecoveryForm } = await vite.ssrLoadModule('/src/pages/RunRecoveryPanel.tsx');
  const view = { command: { id: 'job', status: 'recovery_required', claim_token: 'claim', worker_id: 'owner' }, pending_requests: ['req'], unresolved_requests: ['req'], budget_revision: 2, budget: { usage_verified: false }, can_release: false, receipt: null, requests: [{ id: 'req', pending: true, sent: true, operation_id: 'op' }] };
  const fields = { requestId: 'req', usage: '', note: '核查依据', ownerStopped: false, acknowledge: false };
  assert.equal(requestRecoveryPayload(view, fields, 'resolve_pending'), null);
  const acknowledged = { ...fields, ownerStopped: true };
  const resolve = requestRecoveryPayload(view, acknowledged, 'resolve_pending');
  assert.equal(resolve.expected_revision, 2);
  assert.equal(resolve.usage, undefined);
  assert.equal(resolve.acknowledge_duplicate_charge, undefined);
  assert.equal(releasePayload(view, acknowledged), null);
  const settled = { ...view, pending_requests: [], can_release: true, requests: [{ ...view.requests[0], pending: false }] };
  assert.equal(requestRecoveryPayload(settled, acknowledged, 'reconcile'), null);
  assert.equal(requestRecoveryPayload(settled, { ...acknowledged, usage: '-1' }, 'reconcile'), null);
  assert.equal(requestRecoveryPayload(settled, { ...acknowledged, usage: '8' }, 'reconcile').usage, 8);
  assert.equal(requestRecoveryPayload(settled, acknowledged, 'authorize_retry'), null);
  assert.equal(requestRecoveryPayload(settled, { ...acknowledged, acknowledge: true }, 'authorize_retry').acknowledge_duplicate_charge, true);
  assert.deepEqual(releasePayload(settled, acknowledged), { claim_token: 'claim', owner_stopped: true, note: '核查依据' });
  const html = renderToStaticMarkup(createElement(RunRecoveryForm, { reportId: 'report', view, onChanged() {} }));
  assert.match(html, /仍未知/);
  assert.match(html, /旧执行者/);
  assert.match(html, /释放执行名额/);
  assert.match(html, /disabled/);
});
