import assert from "node:assert/strict";
import { test, after } from "node:test";
import { createServer } from "vite";

const vite = await createServer({
  server: { middlewareMode: true, ws: false, watch: null },
  appType: "custom",
});
after(() => vite.close());
const { resolveStages } = await vite.ssrLoadModule("/src/app/stages.ts");

test("an early blocked plan never claims collection or analysis is complete", () => {
  const stages = resolveStages({
    signed: false,
    plan: null,
    analysis: null,
    report_document: null,
    workflow: { phase: "blocked" },
    report: { status: "failed" },
    interrupts: [],
    runtime: { busy: false },
  });
  assert.equal(stages[0].state, "waiting");
  assert.ok(stages.slice(1).every((stage) => stage.state === "pending"));
});

test("native workflow derives each role independently and reflects rework", () => {
  const stages = resolveStages({
    workflow_version: "products-v2", signed: false, report: { status: "running" },
    workflow: { phase: "running" }, interrupts: [], runtime: { busy: true },
    tasks: [
      { role: "writer", status: "completed" },
      { role: "analysist", status: "in_progress" },
    ],
  });
  assert.equal(stages.find(s => s.key === "collect").state, "pending");
  assert.equal(stages.find(s => s.key === "analyze").state, "active");
  assert.equal(stages.find(s => s.key === "review").state, "pending");
});
