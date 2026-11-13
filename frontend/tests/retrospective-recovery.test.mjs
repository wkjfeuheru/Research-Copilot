import assert from 'node:assert/strict';
import { test, after } from 'node:test';
import { createServer } from 'vite';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';

const vite = await createServer({ server: { middlewareMode: true, ws: false, watch: null }, appType: 'custom' });
after(() => vite.close());

test('retrospective recovery separates reconciliation from one explicit paid retry', async () => {
  const client = await vite.ssrLoadModule('/src/api/client.ts');
  assert.equal(typeof client.recoverRetrospective, 'function');
  const { recoveryPayload, RetrospectiveRecovery } = await vite.ssrLoadModule('/src/pages/RetrospectiveRecovery.tsx');
  const job = { id: 'job', status: 'failed', recovery_revision: 4, usage: { attempts: [{ attempt_id: 'attempt', attempt_number: 1, usage: {} }] } };
  const fields = { attemptId: 'attempt', input: '5', output: '3', note: '核对供应商回执', acknowledge: false };
  const fixed = recoveryPayload(job, fields, 'reconcile', 'operation');
  assert.equal(fixed.expected_revision, 4);
  assert.equal(fixed.input_tokens, 5);
  assert.equal(fixed.acknowledge_duplicate_charge, undefined);
  assert.equal(recoveryPayload(job, fields, 'authorize_retry', 'operation'), null);
  const retry = recoveryPayload(job, { ...fields, acknowledge: true }, 'authorize_retry', 'operation');
  assert.equal(retry.acknowledge_duplicate_charge, true);
  assert.equal(retry.attempt_id, undefined);
  assert.equal(recoveryPayload({ ...job, status: 'running' }, fields, 'reconcile', 'operation'), null);
  assert.equal(recoveryPayload(job, { ...fields, input: '' }, 'reconcile', 'operation'), null);
  assert.equal(recoveryPayload(job, { ...fields, output: '-1' }, 'reconcile', 'operation'), null);
  assert.equal(recoveryPayload(job, { ...fields, note: ' ' }, 'reconcile', 'operation'), null);
  const html = renderToStaticMarkup(createElement(RetrospectiveRecovery, { reportId: 'report', job, onChanged() {} }));
  assert.match(html, /核实用量/);
  assert.match(html, /可能重复收费/);
  assert.match(html, /授权再尝试一次/);
  assert.match(html, /disabled/);
});
