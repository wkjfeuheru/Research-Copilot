import assert from "node:assert/strict";
import { test } from "node:test";
import { createReport } from "../src/api/client.ts";

test("a lost create response reuses its key after reload and success clears it", async () => {
  const data = new Map();
  const storage = {
    getItem: (key) => data.get(key) ?? null,
    setItem: (key, value) => data.set(key, value),
    removeItem: (key) => data.delete(key),
  };
  const keys = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (_url, options) => {
    keys.push(options.headers["Idempotency-Key"]);
    if (keys.length === 1) throw Error("response lost");
    return Response.json({ report: { id: "r" } });
  };
  try {
    await assert.rejects(createReport("研究问题", "landing", storage));
    await createReport("研究问题", "landing", storage);
    await createReport("研究问题", "landing", storage);
    assert.ok(keys[0]);
    assert.equal(keys[0], keys[1]);
    assert.notEqual(keys[1], keys[2]);
    assert.equal(data.size, 0);
  } finally {
    globalThis.fetch = originalFetch;
  }
});
