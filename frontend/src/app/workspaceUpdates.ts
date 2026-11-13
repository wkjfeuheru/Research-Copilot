import type { WorkspaceResponse } from '../api/client';

/** Order HTTP snapshots against new requests, SSE events and lifecycle changes. */
export class WorkspaceUpdates {
  private revision = 0;

  invalidate() {
    this.revision += 1;
  }

  async fetch(
    load: () => Promise<WorkspaceResponse>,
    accept: (value: WorkspaceResponse) => void,
    reject: (error: unknown) => void,
  ) {
    const expected = ++this.revision;
    try {
      const value = await load();
      if (expected === this.revision) accept(value);
    } catch (error) {
      if (expected === this.revision) reject(error);
    }
  }
}
