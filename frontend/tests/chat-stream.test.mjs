import test from "node:test";
import assert from "node:assert/strict";
import { applyReportDelta } from "../src/api/events.ts";

test("resumed SSE chunks preserve Unicode offsets and ignore duplicate delivery", () => {
  const start = {
    content: "研究🧪",
    offset: 3,
    content_hash: "abc",
    version: "1",
    done: false,
  };
  const next = applyReportDelta(start, {
    content_hash: "abc",
    offset: 3,
    text: "结论",
  });
  assert.equal(next.content, "研究🧪结论");
  assert.equal(next.offset, 5);
  assert.deepEqual(
    applyReportDelta(next, { content_hash: "abc", offset: 3, text: "结论" }),
    next,
  );
  assert.throws(() =>
    applyReportDelta(next, { content_hash: "abc", offset: 9, text: "跳过" }),
  );
  assert.throws(() =>
    applyReportDelta(next, { content_hash: "other", offset: 5, text: "换版" }),
  );
});
