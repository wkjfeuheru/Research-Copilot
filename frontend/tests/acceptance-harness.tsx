// Browser fixture only. No real report ID or backend request is used.
import { useState } from 'react';
import { createRoot } from 'react-dom/client';
import { GateCard } from '../src/pages/GateCard';
import type { WorkspaceResponse, InterruptView } from '../src/api/client';
import '../src/styles/tokens.css';
import '../src/styles/global.css';
import '../src/styles/ui.css';

const received: unknown[] = [];
window.fetch = async (input, init) => {
  if (String(input) !== '/api/reports/offline-fixture/decisions' || init?.method !== 'POST') {
    throw new Error('离线夹具禁止访问其他端点');
  }
  received.push(JSON.parse(String(init.body)));
  return new Response(JSON.stringify({ action: 'decision', state: 'queued' }), {
    status: 200, headers: { 'Content-Type': 'application/json' },
  });
};

function Harness() {
  const [stage, setStage] = useState(0);
  const [hard, setHard] = useState(false);
  const [snapshot, setSnapshot] = useState(1);
  const [error, setError] = useState<string | null>(null);
  const issue = { issue_id: 'issue-1', severity: 'major', hard, category: 'numeric', quote: '', suggestion: '核对冻结披露', location: '收入分析', reason: '口径需要核实' };
  const review: InterruptView = {
    id: 'review-interrupt', kind: 'reviewer_deep_review', tool: null, actions: [], artifact: null,
    review: { review_session_id: 'review-1', snapshot_id: `snapshot-${snapshot}`, checks_complete: true,
      allowed_decisions: ['approve', 'request_changes', 'reject'], summary: '请逐项核查并填写处置理由', errors: [], issues: [issue] },
  };
  const signoff: InterruptView = { id: 'signoff-interrupt', kind: 'tool', tool: 'signoff_report', artifact: null,
    actions: [{ name: 'signoff_report', artifact: null }], review: null };
  const data: WorkspaceResponse = {
    report: { id: 'offline-fixture', user_request: '离线夹具', status: 'paused', created_at: null },
    runtime: { busy: false, recoverable: false, last_error: null },
    workflow: { version: 'products-v1', phase: 'review', next_action: null, role: 'reviewer', reason: null },
    budget: null, signoff_ready: stage === 1, signed: stage === 2, plan: null, analysis: null,
    review: stage > 0 ? { outcome: '人工裁决通过', checks_complete: true, pass_confidence: 0.95, issues: [] } : null,
    rework: null, retrospective: null, evidence_count: 0, report_document: null,
    actions: [], interrupts: stage === 0 ? [review] : stage === 1 ? [signoff] : [],
  };
  function reset(nextHard: boolean) { setHard(nextHard); setStage(0); setSnapshot(value => value + 1); received.length = 0; }
  return <main style={{ maxWidth: 860, margin: '32px auto', padding: 24 }}>
    <h1>离线人工审核验收</h1><p>仅测试界面与请求载荷，不批准真实报告。后端响应为夹具。</p>
    <nav style={{ display: 'flex', gap: 12, marginBottom: 24 }}>
      <button onClick={() => reset(false)}>软问题夹具</button>
      <button onClick={() => reset(true)}>硬问题夹具</button>
      <button onClick={() => setSnapshot(value => value + 1)}>切换冻结快照</button>
    </nav>
    <GateCard key={`${hard}:${stage}`} reportId="offline-fixture" data={data}
      onSubmitted={() => setStage(value => value + 1)} onError={setError} />
    {stage === 2 && <p role="status">夹具签署完成</p>}{error && <p role="alert">{error}</p>}
    <h2>已捕获的夹具请求</h2><pre style={{ whiteSpace: 'pre-wrap' }}>{JSON.stringify(received, null, 2)}</pre>
  </main>;
}

createRoot(document.getElementById('root')!).render(<Harness />);
