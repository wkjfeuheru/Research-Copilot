// ECharts SVG SSR, no browser/network. https://echarts.apache.org/handbook/en/how-to/cross-platform/server/
import * as echarts from 'echarts';
import { Resvg } from '@resvg/resvg-js';
import { readFileSync, writeFileSync } from 'node:fs';
import { validateOption, policy, theme } from './chart_policy.mjs';
const [input, svgFile, pngFile, widthText, heightText] = process.argv.slice(2);
const width = Number(widthText), height = Number(heightText);
if (!Number.isInteger(width) || !Number.isInteger(height) || width < 320 || width > 2400 || height < 240 || height > 1600) throw new Error('Invalid canvas');
if (readFileSync(input).length > 1048576) throw new Error('Option too large');
const option = validateOption(JSON.parse(readFileSync(input, 'utf8')));
echarts.registerTheme(policy.version, theme);
const chart = echarts.init(null, policy.version, { renderer: 'svg', ssr: true, width, height });
try {
  chart.setOption({ ...option, animation: false, textStyle: { ...(option.textStyle || {}), fontFamily: 'Noto Sans CJK SC' } });
  // SSR adds hover CSS even with animation disabled. Static artifacts need no CSS.
  const svg = chart.renderToSVGString().replace(/<style\b[^>]*>[\s\S]*?<\/style>/gi, '');
  if (Buffer.byteLength(svg) > 4 * 1024 * 1024) throw new Error('SVG exceeds limit');
  writeFileSync(svgFile, svg, 'utf8');
  // Minimal images need explicit font files (fontconfig is intentionally absent).
  const png = new Resvg(svg, { font: { loadSystemFonts: false,
    fontFiles: ['/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc', '/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc'],
    defaultFontFamily: 'Noto Sans CJK SC' }, background: 'white' }).render().asPng();
  if (png.length > 16 * 1024 * 1024) throw new Error('PNG exceeds limit');
  writeFileSync(pngFile, png);
} finally {
  chart.dispose();
}
