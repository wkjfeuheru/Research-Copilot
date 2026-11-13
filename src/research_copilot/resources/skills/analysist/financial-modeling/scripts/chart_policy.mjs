// Shared by Node SSR and the Web client; no generated code is ever evaluated.
import policy from './chart_policy.json' with { type: 'json' };
import theme from './chart_theme.json' with { type: 'json' };
export { policy, theme };
export function validateOption(option) {
  if (new TextEncoder().encode(JSON.stringify(option)).length > policy.maxBytes) throw new Error('Chart configuration exceeds size limit');
  if (!option || typeof option !== 'object' || !Array.isArray(option.series) || !option.series.length) throw new Error('Nonempty series required');
  for (const series of option.series) {
    if (!series || !policy.seriesTypes.includes(series.type) || !Array.isArray(series.data) || !series.data.length) throw new Error('Supported nonempty chart series required');
  }
  let count = 0;
  function visit(value, depth = 0) {
    if (++count > policy.maxNodes || depth > 20) throw new Error('Chart configuration exceeds limits');
    if (Array.isArray(value)) value.forEach(item => visit(item, depth + 1));
    else if (value && typeof value === 'object') {
      for (const [key, item] of Object.entries(value)) {
        if (!policy.keys.includes(key)) throw new Error('Unsupported option: ' + key);
        if (key === 'type' && !policy.types.includes(item)) throw new Error('Unsupported chart type');
        if (key === 'renderMode' && item !== 'richText') throw new Error('Text tooltip required');
        if (key === 'formatter' && !policy.formatters.includes(item)) throw new Error('Unapproved formatter');
        visit(item, depth + 1);
      }
    } else if (typeof value === 'string') {
      if (value.length > 2000 || /[<>]|(?:https?:|data:|javascript:|file:|\/\/)|\bfunction\s*\(|=>/i.test(value)) throw new Error('Unsafe chart text');
    } else if (typeof value === 'number' && !Number.isFinite(value)) throw new Error('Finite values required');
  }
  visit(option);
  return { ...option, animation: false, tooltip: { ...(option.tooltip || {}), renderMode: 'richText' } };
}
