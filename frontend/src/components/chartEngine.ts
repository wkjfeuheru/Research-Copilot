// SSR and browser use exactly the same versioned JSON-only policy.
import { validateOption, policy, theme } from '../../../src/research_copilot/resources/skills/analysist/financial-modeling/scripts/chart_policy.mjs';
import * as echarts from 'echarts/core';
import { BarChart, LineChart, ScatterChart, HeatmapChart, CandlestickChart, TreemapChart, SankeyChart } from 'echarts/charts';
import { TitleComponent, TooltipComponent, LegendComponent, GridComponent, DataZoomComponent, VisualMapComponent } from 'echarts/components';
import { CanvasRenderer } from 'echarts/renderers';

echarts.use([BarChart, LineChart, ScatterChart, HeatmapChart, CandlestickChart, TreemapChart, SankeyChart,
  TitleComponent, TooltipComponent, LegendComponent, GridComponent, DataZoomComponent, VisualMapComponent, CanvasRenderer]);
echarts.registerTheme(policy.version, theme);

export function mountChart(element: HTMLElement, option: unknown) {
  const safe = validateOption(option);
  const chart = echarts.init(element, policy.version);
  try {
    chart.setOption(safe);
    return chart;
  } catch (error) {
    chart.dispose();
    throw error;
  }
}
