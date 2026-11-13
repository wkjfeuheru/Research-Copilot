import { StrictMode, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { ReportChart } from '../src/components/ReportChart';
import { ReportPanel } from '../src/pages/ReportPanel';
import type { StatusResponse } from '../src/api/client';
import '../src/styles/tokens.css';
import '../src/styles/global.css';

function Preview() {
  const [show, setShow] = useState(true);
  const [narrow, setNarrow] = useState(false);
  const [panel, setPanel] = useState(false);
  return <main style={{ margin: '2rem auto', width: narrow ? 500 : 1000, maxWidth: '95vw' }}>
    <h1>冻结图表验收夹具</h1>
    <button onClick={() => setShow(!show)}>挂载 / 卸载</button>
    <button onClick={() => setNarrow(!narrow)}>调整宽度</button>
    <button onClick={() => setPanel(!panel)}>完整报告 / 独立图表</button>
    {show && (panel ? <ReportPanel reportId="report-1" status={{ business_version: 1, task_plan: null } as StatusResponse['status']} businessStatus="approved" /> : <ReportChart reportId="report-1" version="1" chartId="C-1111111111111111" />)}
  </main>;
}
createRoot(document.getElementById('root')!).render(<StrictMode><Preview /></StrictMode>);
