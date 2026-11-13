/** Durable report transport. Offsets count Unicode code points, matching Python. */

export type ConnectionState = 'idle' | 'connecting' | 'live' | 'reconnecting' | 'polling' | 'gone';

export interface StreamDocument {
  content: string;
  offset: number;
  content_hash: string;
  version: string;
  done: boolean;
}

export function applyReportDelta(current: StreamDocument, chunk: { content_hash: string; offset: number; text: string }): StreamDocument {
  if (chunk.content_hash !== current.content_hash) throw new Error('报告版本已变更');
  const end = chunk.offset + Array.from(chunk.text).length;
  if (end <= current.offset) return current;
  if (chunk.offset !== current.offset) throw new Error('报告分片不连续');
  return { ...current, content: current.content + chunk.text, offset: end };
}
