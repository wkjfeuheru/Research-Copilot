import assert from "node:assert/strict";
import { test, after } from "node:test";
import { createServer } from "vite";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { fetchStatus } from "../src/api/client.ts";

// Middleware-only Vite performs local SSR compilation; no browser or listening port.
const vite = await createServer({
  server: { middlewareMode: true, ws: false, watch: null },
  appType: "custom",
});
after(() => vite.close());
const { OverviewPanel } = await vite.ssrLoadModule(
  "/src/pages/OverviewPanel.tsx",
);
const { SignoffGate } = await vite.ssrLoadModule(
  "/src/pages/gates/SignoffGate.tsx",
);
const observation = {
  runtime_version: "v2",
  signed: false,
  signoff_ready: false,
  rework: { phase: "in_progress", version: "1", tasks: [] },
  budget: {
    calls: 80,
    call_limit: 80,
    known_actual_tokens: 1800000,
    unknown_reserved_tokens: 30000,
    unknown_requests: 1,
    remaining_tokens: 0,
    reserved_tokens: 0,
    soft_warning: true,
    usage_verified: false,
    estimated_cost: null,
    cost_complete: false,
    stages: {},
    stage_remaining: {},
  },
  review: {
    checks_complete: false,
    pass_confidence: 0.5,
    outcome: "changes_requested",
    issues: [],
  },
};
const data = {
  report: {
    id: "report-test",
    user_request: "核验毛利率",
    status: "paused",
    created_at: null,
  },
  workflow: {},
  evidence_count: 0,
  plan: null,
  analysis: null,
  interrupts: [],
  runtime: { busy: false },
  report_document: { version: "4", frozen: false },
  ...observation,
};

test("refresh preserves server rework and budget state", async () => {
  const original = globalThis.fetch;
  globalThis.fetch = async () => Response.json(data);
  try {
    for (let i = 0; i < 2; i++) {
      const current = await fetchStatus("report-test");
      assert.deepEqual(current.budget, observation.budget);
      assert.deepEqual(current.rework, observation.rework);
      assert.equal(current.report_document.version, "4");
      assert.equal(current.signed, false);
    }
  } finally {
    globalThis.fetch = original;
  }
});

test("unknown usage and exhausted budget are visible", () => {
  const html = renderToStaticMarkup(createElement(OverviewPanel, { data }));
  assert.match(html, /用量尚未核实/);
  assert.match(html, /预算已接近或达到上限/);
  assert.match(html, /审核尚未完整完成/);
  assert.match(html, /待结算预留/);
});

test("conversation details keep rework progress and unsigned state visible", () => {
  const html = renderToStaticMarkup(createElement(OverviewPanel, { data }));
  assert.match(html, /in_progress/);
  assert.match(html, /未签署/);
});

test("signoff eligibility comes from the server", () => {
  const props = { review: null, choice: null, busy: false, onChoose() {} };
  const blocked = renderToStaticMarkup(
    createElement(SignoffGate, { ...props, signoffReady: false }),
  );
  const ready = renderToStaticMarkup(
    createElement(SignoffGate, { ...props, signoffReady: true }),
  );
  assert.match(blocked, /disabled=""/);
  assert.doesNotMatch(ready, /disabled=""/);
});
