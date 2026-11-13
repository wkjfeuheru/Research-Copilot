// CLI browser check against Vite + isolated HTTP fixtures; no model requests.
async (page) => {
  if (new URL(page.url()).port !== '5189') throw new Error('Isolated Vite port required');
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  const ref = { artifact_id: 'report-1', version: '2', content_hash: 'a'.repeat(64) };
  let version = 1;
  let mode = 'clarification';
  const submitted = [];
  const summary = (id) => ({ id, status: id === 'history' ? 'read_only' : 'paused', read_only: id === 'history', topic: null, report_type: 'company_report', report_skill: null, current_version: 1, user_request: id === 'history' ? '历史公司研究' : '自主研究验收', created_at: null, updated_at: null });
  const workspace = (id = 'native') => ({
    workflow_version: id === 'history' ? 'products-v1' : 'products-v2', read_only: id === 'history',
    state_revision: version, report: summary(id), runtime: { busy: id !== 'history' && mode === 'rework', recoverable: false, last_error: null },
    workflow: { version: 'products-v2', phase: mode === 'done' ? 'complete' : 'running', next_action: null, role: null, reason: null },
    report_scope: { objective: '自主研究验收', report_type: 'company_report', as_of: '2025-04-30' },
    todos: [{ content: '补齐证据并分析', status: mode === 'rework' ? 'in_progress' : 'completed' }, { content: '独立审核及签署', status: 'pending' }],
    tasks: [{ task_id: 'collect', dispatch_id: 'd1', role: 'searcher', objective: '补充年报证据', status: 'completed' }, { task_id: 'analysis', dispatch_id: 'd2', role: 'analysist', objective: '核对增长率', status: mode === 'rework' ? 'in_progress' : 'completed' }],
    dispatch_count: 2, dispatch_remaining: 10, waiting_for: mode,
    budget: null, signoff_ready: mode === 'signoff', signed: mode === 'done', plan: null, analysis: null,
    review: { outcome: 'passed', checks_complete: true, pass_confidence: 0.99, issues: [] }, rework: null, retrospective: null, evidence_count: 2, report_document: null,
    interrupts: id === 'history' ? [] : mode === 'clarification' ? [{ id: 'q', kind: 'clarification', question: '需要研究哪一家公司？', actions: [] }]
      : mode === 'review' ? [{ id: 'r', kind: 'reviewer_deep_review', actions: [], review: { snapshot_id: 'snapshot-2', review_session_id: 'review-2', checks_complete: true, allowed_decisions: ['approve', 'request_changes', 'reject'], summary: '请核查研究范围', issues: [{ issue_id: 'i1', category: 'reasoning', severity: 'major', location: '结论', reason: '范围需确认', suggestion: '核查', quote: '增长' }], errors: [] } }]
      : mode === 'signoff' ? [{ id: 's', kind: 'tool', tool: 'signoff_report', artifact: ref, actions: [{ name: 'signoff_report', artifact: ref }] }] : [],
    actions: [],
  });
  await page.unrouteAll();
  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    if (!path.startsWith("/api/")) return route.continue();
    const json = value => route.fulfill({ json: value });
    if (path === '/api/reports') {
      if (route.request().method() === 'POST') return json({ report: summary('native') });
      return json({ reports: [summary('native'), summary('history')], active_report_id: 'native' });
    }
    const id = path.split('/')[3];
    if (path.endsWith('/events')) return route.fulfill({ contentType: 'text/event-stream', body: `event: workspace\ndata: ${JSON.stringify(workspace(id))}\n\n` });
    if (path.endsWith('/decisions')) {
      submitted.push(route.request().postDataJSON());
      mode = mode === 'clarification' ? 'review' : mode === 'review' ? 'signoff' : 'done';
      version++;
      return json({ action: 'resume', state: 'running' });
    }
    return json(workspace(id));
  });
  await page.goto('http://127.0.0.1:5189/');
  await page.getByRole('textbox', { name: '研究消息' }).fill('自主研究验收');
  await page.getByRole('button', { name: '发送消息' }).click();
  await page.getByRole('textbox', { name: '补充研究信息' }).waitFor();
  await page.reload();
  await page.getByRole('textbox', { name: '补充研究信息' }).fill('测试公司');
  await page.getByRole('button', { name: '提交决定' }).click();
  await page.getByRole('region', { name: '审核决定' }).waitFor();
  await page.getByRole('button', { name: '通过', exact: true }).click();
  if (await page.getByRole('button', { name: '提交决定' }).isEnabled()) throw new Error('Issue disposition bypassed');
  await page.getByLabel('误报处置').selectOption('false_positive');
  await page.getByLabel('处置理由（判定误报时必填）').fill('已核对本报告范围');
  await page.getByRole('button', { name: '提交决定' }).click();
  await page.getByRole('region', { name: '报告签署' }).waitFor();
  await page.screenshot({ path: 'output/playwright/autonomy-signoff.png', fullPage: true });
  await page.getByRole('button', { name: '批准', exact: true }).click();
  await page.getByRole('button', { name: '提交决定' }).click();
  await page.getByText('研究已完成', { exact: true }).waitFor();
  if (submitted[0].decisions.q.answer !== '测试公司' || submitted[1].decisions.r.issue_dispositions.length !== 1 || submitted[2].decisions.s.decisions[0].type !== 'approve') throw new Error('Wrong decision payload');
  mode = 'rework'; version++;
  await page.reload();
  await page.getByRole('list', { name: '动态任务清单' }).waitFor();
  if (!(await page.getByRole('list', { name: '实际执行任务' }).innerText()).includes('执行中')) throw new Error('Rework progress missing');
  await page.screenshot({ path: 'output/playwright/autonomy-rework.png', fullPage: true });
  await page.goto('http://127.0.0.1:5189/reports/history');
  await page.getByText('历史报告、对话和审核记录仍可查看。继续研究请新建会话。').waitFor();
  if (await page.getByRole('button', { name: '提交决定' }).count()) throw new Error('Historical approval visible');
  const historicalDelete = page.getByRole('button', { name: '删除对话 历史公司研究' });
  if (await historicalDelete.count() !== 1 || await historicalDelete.isEnabled()) throw new Error('Historical delete enabled');
  await page.screenshot({ path: 'output/playwright/autonomy-history.png', fullPage: true });
  if (errors.length) throw new Error(errors.join('\n'));
  console.log(JSON.stringify({ checked: ['new-chat', 'clarification-reload', 'review-disposition', 'signoff', 'dynamic-todos', 'rework', 'history-read-only'], model_calls: 0, page_errors: errors }));
}
