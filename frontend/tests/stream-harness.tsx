import { useState } from 'react';
import { createRoot } from 'react-dom/client';
import { useReportStream } from '../src/app/useReportStream';
import '../src/styles/tokens.css';
import '../src/styles/global.css';

const base = 'http://127.0.0.1:5191';
const nativeFetch = window.fetch.bind(window);
window.fetch = (input, init) => {
  if (!String(input).startsWith('/api/reports/stream-fixture')) throw new Error('Unexpected fixture request');
  return nativeFetch(base + input, init);
};
const NativeEventSource = window.EventSource;
window.EventSource = class extends NativeEventSource {
  constructor(url: string | URL, options?: EventSourceInit) { super(base + url, options); }
};
const control = (name: string) => nativeFetch(`${base}/control/${name}`).then(value => value.json());

function Harness() {
  const stream = useReportStream('stream-fixture');
  const [transport, setTransport] = useState('');
  async function refreshTransport() { setTransport(JSON.stringify(await control('status'))); }
  async function hold() { await control('arm'); void stream.refresh(); }
  return <main style={{ maxWidth: 850, margin: '40px auto' }}>
    <h1>离线 SSE 竞争验收</h1>
    <p>真实浏览器、HTTP 与 SSE；服务器数据为夹具，不访问研究后端。</p>
    <p role="status">连接：{stream.connection}；工作区：{stream.data?.report.user_request}；状态：{stream.data?.report.status}</p>
    <button onClick={hold}>挂起旧 HTTP</button>{' '}
    <button onClick={() => control('new')}>推送新 SSE</button>{' '}
    <button onClick={() => control('release')}>释放旧 HTTP</button>{' '}
    <button onClick={() => control('disconnect')}>断开 SSE</button>{' '}
    <button onClick={stream.reconnect}>手动重连</button>{' '}
    <button onClick={refreshTransport}>读取传输计数</button>
    <pre>{transport}</pre>
    {(stream.loadError || stream.streamError) && <p role="alert">{stream.loadError ?? stream.streamError}</p>}
  </main>;
}
createRoot(document.getElementById('root')!).render(<Harness />);
