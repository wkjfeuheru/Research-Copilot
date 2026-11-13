async (page) => {
  const errors = [];
  const network = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('request', request => network.push(request.url()));
  const prefix = '/api/reports/report-1/versions/1/charts/C-1111111111111111';
  let failOptions = false;
  await page.route('**/api/reports/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === prefix) return route.fulfill({ json: { report_id: 'report-1', report_version: '1', chart_id: 'C-1111111111111111', engine: 'echarts', title: '营收增长', alt: 'AAA 营收增长 20%', static_url: prefix + '/png', option_url: prefix + '/option', content_hash: 'test-fixture', theme_version: 'research-v1' } });
    if (path === prefix + '/png' || path.endsWith('/files/figures/historical.png')) return route.fulfill({ response: await page.request.get('http://127.0.0.1:5181/tests/fixtures/chart.png'), contentType: 'image/png' });
    if (path === prefix + '/option') {
      if (failOptions) return route.fulfill({ json: { series: [{ type: 'custom', renderItem: 'alert(1)' }] } });
      return route.fulfill({ response: await page.request.get('http://127.0.0.1:5181/tests/fixtures/option.json'), contentType: 'application/json' });
    }
    if (path.endsWith('/report')) return route.fulfill({ json: { version: 1, is_frozen: true, content: '# Research\n\n## 图表\n\n![Growth](chart:C-1111111111111111)\n\n![历史图片](figures/historical.png)', content_hash: 'fixture', file_path: 'report.md', manifest_hash: 'fixture' } });
    return route.fulfill({ status: 404 });
  });
  await page.goto('http://127.0.0.1:5181/tests/chart-preview.html');
  await page.getByText('悬浮查看数值', { exact: false }).waitFor();
  const figure = page.getByRole('figure');
  await figure.locator('canvas').waitFor();
  const chartState = async action => page.evaluate(async action => {
    const element = document.querySelector('[role="figure"]').firstElementChild;
    const url = performance.getEntriesByType('resource').find(entry => /\/echarts_core\.js/.test(entry.name)).name;
    const echarts = await import(url);
    const chart = echarts.getInstanceByDom(element);
    if (action === 'toggle') chart.dispatchAction({ type: 'legendToggleSelect', name: 'Growth' });
    if (action === 'zoom') chart.dispatchAction({ type: 'dataZoom', start: 10, end: 70 });
    if (action === 'tooltip') chart.dispatchAction({ type: 'showTip', seriesIndex: 0, dataIndex: 0 });
    return { width: chart.getWidth(), selected: chart.getOption().legend[0].selected.Growth,
      zoom: chart.getOption().dataZoom[0].start, series: chart.getOption().series.map(s => ({ name: s.name, data: s.data })),
      tooltip: chart.getZr().storage.getDisplayList().filter(x => x.type === 'tspan').map(x => x.style.text) };
  }, action);
  const initial = await chartState('read');
  if (initial.series[0].data[0] !== 0.2) throw new Error('Wrong frozen data');
  await figure.locator('canvas').hover();
  const tooltip = await chartState('tooltip');
  if (!tooltip.tooltip.some(text => String(text).includes('0.2'))) throw new Error('Tooltip did not expose frozen value');
  if ((await chartState('toggle')).selected !== false) throw new Error('Legend did not toggle');
  if ((await chartState('zoom')).zoom !== 10) throw new Error('Zoom did not update');
  await page.getByRole('button', { name: '调整宽度' }).click();
  await page.waitForTimeout(250);
  if ((await chartState('read')).width >= initial.width) throw new Error('Resize failed');
  await page.emulateMedia({ media: 'print' });
  if (!await figure.locator('img').isVisible() || await figure.locator('canvas').isVisible()) throw new Error('Print must use static PNG');
  await page.emulateMedia({ media: 'screen' });
  await chartState('toggle');
  await page.waitForTimeout(100);
  await page.screenshot({ path: '.playwright-cli/sandbox-interactive.png', fullPage: true });
  await page.getByRole('button', { name: '挂载 / 卸载' }).click();
  if (await page.locator('canvas').count()) throw new Error('Unmount did not release chart');
  failOptions = true;
  await page.getByRole('button', { name: '挂载 / 卸载' }).click();
  await page.getByRole('status').filter({ hasText: '交互不可用' }).waitFor();
  if (!await page.getByRole('figure').locator('img').isVisible()) throw new Error('Static fallback missing');
  await page.screenshot({ path: '.playwright-cli/sandbox-fallback.png', fullPage: true });
  failOptions = false;
  await page.getByRole('button', { name: '完整报告 / 独立图表' }).click();
  await page.getByText('悬浮查看数值', { exact: false }).waitFor();
  await page.getByRole('img', { name: '历史图片', exact: true }).waitFor();
  if (await page.getByRole('figure').locator('img').isVisible()) throw new Error('Report styles must keep interactive PNG hidden on screen');
  if (!await page.getByRole('img', { name: '历史图片', exact: true }).evaluate(img => img.complete && img.naturalWidth > 0)) throw new Error('Historical image failed');
  if (await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)) throw new Error('Report chart caused horizontal overflow');
  await page.screenshot({ path: '.playwright-cli/sandbox-report-panel.png', fullPage: true });
  if (errors.length) throw new Error(errors.join('; '));
  if (network.some(url => /^https?:/.test(url) && !url.startsWith('http://127.0.0.1:5181/'))) throw new Error('External request detected');
  console.log(JSON.stringify({ passed: ['frozen data', 'tooltip', 'legend', 'zoom', 'resize', 'dispose', 'print', 'hostile option fallback', 'report markers', 'historical images', 'offline'], requests: network.length }));
}
