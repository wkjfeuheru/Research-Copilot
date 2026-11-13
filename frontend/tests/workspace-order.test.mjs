import assert from 'node:assert/strict';
import { test, after } from 'node:test';
import { createServer } from 'vite';

const vite = await createServer({ server: { middlewareMode: true, ws: false, watch: null }, appType: 'custom' });
after(() => vite.close());
const { WorkspaceUpdates } = await vite.ssrLoadModule('/src/app/workspaceUpdates.ts');
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((ok, no) => { resolve = ok; reject = no; });
  return { promise, resolve, reject };
};

test('SSE workspace supersedes an outstanding polling response even after disconnect', async () => {
  const updates = new WorkspaceUpdates();
  const pending = deferred();
  let visible;
  const fetch = updates.fetch(() => pending.promise, value => { visible = value; }, assert.fail);
  updates.invalidate(); // A newer workspace event arrives.
  visible = { report: { status: 'signed' } };
  pending.resolve({ report: { status: 'running' } });
  await fetch;
  assert.equal(visible.report.status, 'signed');
});

test('new requests and report changes invalidate earlier data and errors', async () => {
  const updates = new WorkspaceUpdates();
  const old = deferred();
  const values = [], errors = [];
  const fetch = updates.fetch(() => old.promise, value => values.push(value), error => errors.push(error));
  await updates.fetch(async () => 'new', value => values.push(value), error => errors.push(error));
  old.reject(new Error('stale 404'));
  await fetch;
  assert.deepEqual(values, ['new']);
  assert.deepEqual(errors, []);
  const pending = deferred();
  const change = updates.fetch(() => pending.promise, value => values.push(value), error => errors.push(error));
  updates.invalidate(); // cleanup, reconnect or report identity changes
  pending.resolve('previous report');
  await change;
  assert.deepEqual(values, ['new']);
});
