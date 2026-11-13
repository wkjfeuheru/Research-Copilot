// Loopback-only transport fixture; never imports app configuration or calls a model.
import { createServer } from 'node:http';

const peers = new Set();
const pending = [];
let armed = false;
let version = 1;
let connections = 0;
function workspace() {
  return {
    report: { id: 'stream-fixture', user_request: `快照 ${version}`, status: version > 1 ? 'completed' : 'running', created_at: null },
    runtime: { busy: version === 1, recoverable: false, last_error: null },
    workflow: { version: 'products-v1', phase: 'review', next_action: null, role: null, reason: null },
    budget: null, signoff_ready: false, signed: version > 1, plan: null, analysis: null,
    review: null, rework: null, retrospective: null, evidence_count: version, report_document: null,
    interrupts: [], actions: [],
  };
}
function json(response, value) { response.setHeader('Content-Type', 'application/json'); response.end(JSON.stringify(value)); }
function send(response) { response.write(`event: workspace\ndata: ${JSON.stringify(workspace())}\n\n`); }
createServer((request, response) => {
  response.setHeader('Access-Control-Allow-Origin', 'http://127.0.0.1:5190');
  const path = new URL(request.url, 'http://localhost').pathname;
  if (path === '/api/reports/stream-fixture/events') {
    connections += 1;
    response.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' });
    response.write('retry: 1000\n\n');
    peers.add(response);
    send(response);
    request.on('close', () => peers.delete(response));
  } else if (path === '/api/reports/stream-fixture') {
    if (armed) pending.push({ response, snapshot: workspace() });
    else json(response, workspace());
  } else if (path === '/control/arm') {
    armed = true;
    json(response, { armed });
  } else if (path === '/control/new') {
    version += 1;
    for (const peer of peers) send(peer);
    json(response, { version });
  } else if (path === '/control/release') {
    armed = false;
    for (const entry of pending.splice(0)) json(entry.response, entry.snapshot);
    json(response, { released: true });
  } else if (path === '/control/disconnect') {
    for (const peer of peers) peer.end();
    peers.clear();
    json(response, { disconnected: true });
  } else if (path === '/control/status') {
    json(response, { connections, pending: pending.length, version, peers: peers.size });
  } else {
    response.writeHead(404); response.end();
  }
}).listen(5191, '127.0.0.1', () => console.log('Isolated SSE fixture http://127.0.0.1:5191'));
