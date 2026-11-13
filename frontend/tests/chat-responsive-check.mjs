// Read-only browser checks on the disposable fixture server.
async (page) => {
  if (new URL(page.url()).port !== '5187') throw new Error('Fixture port required');
  const results = [];
  for (const path of ['/', '/reports/blocked-demo']) {
    await page.goto(`http://127.0.0.1:5187${path}`);
    await page.getByRole('textbox', { name: '研究消息' }).waitFor();
    for (const [width, height] of [[1440, 1000], [1280, 800], [768, 900], [390, 844], [320, 700]]) {
      await page.setViewportSize({ width, height });
      const dimensions = await page.evaluate(() => ({ width: innerWidth, scrollWidth: document.documentElement.scrollWidth }));
      if (dimensions.scrollWidth > width) throw new Error(`Overflow ${path}: ${width}`);
      await page.getByRole('textbox', { name: '研究消息' }).scrollIntoViewIfNeeded();
      if (!(await page.getByRole('textbox', { name: '研究消息' }).isVisible())) throw new Error('Composer inaccessible');
      results.push({ path, width, height, overflow: false });
    }
  }
  await page.setViewportSize({ width: 1280, height: 800 });
  return results;
}
