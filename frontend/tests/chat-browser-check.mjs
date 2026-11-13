// Run ONLY against tests.fakes.chat_server (disposable, unpaid research).
async (page) => {
  if (new URL(page.url()).port !== "5187")
    throw new Error("Fixture port required");
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("http://127.0.0.1:5187/");
  await page.screenshot({ path: "output/playwright/chat-home.png" });
  await page
    .getByRole("textbox", { name: "研究消息" })
    .fill("浏览器验收：分析毛利率变化");
  await page.getByRole("button", { name: "发送消息" }).click();
  await page
    .getByText("你希望比较哪两个季度？请补充具体期间，我再整理研究计划。", {
      exact: true,
    })
    .waitFor();
  await page.reload();
  await page
    .getByText("你希望比较哪两个季度？请补充具体期间，我再整理研究计划。", {
      exact: true,
    })
    .waitFor();
  await page
    .getByRole("textbox", { name: "研究消息" })
    .fill("比较2025Q1和2025Q2");
  await page.getByRole("button", { name: "发送消息" }).click();
  await page.getByRole("region", { name: "研究计划确认" }).waitFor();
  if (await page.getByRole("textbox", { name: "研究消息" }).isEnabled())
    throw new Error("Gate bypassed");
  await page.getByRole("button", { name: "批准", exact: true }).click();
  await page.getByRole("button", { name: "提交决定", exact: true }).click();
  await page.getByRole("region", { name: "分析结果确认" }).waitFor();
  const reportPath = new URL(page.url()).pathname;
  await page.evaluate((path) => {
    window.fixtureEvents = [];
    window.fixtureSource = new EventSource(`/api${path}/events`);
    for (const event of [
      "report.start",
      "report.delta",
      "report.complete",
      "workspace",
    ]) {
      window.fixtureSource.addEventListener(event, (message) => {
        const value = JSON.parse(message.data);
        window.fixtureEvents.push({
          event,
          offset: value.offset,
          length: value.length,
          busy: value.runtime?.busy,
        });
      });
    }
  }, reportPath);
  await page.getByRole("region", { name: "分析结果确认" }).waitFor();
  await page.getByRole("button", { name: "批准", exact: true }).click();
  await page.getByRole("button", { name: "提交决定", exact: true }).click();
  await page
    .getByRole("article", { name: "流式研究报告" })
    .getByText("完整内容已接收并校验")
    .waitFor({ timeout: 15000 });
  await page.getByRole("region", { name: "报告签署" }).waitFor();
  const status = await page.request
    .get(`http://127.0.0.1:5187/api${reportPath}`)
    .then((response) => response.json());
  if (!status.signoff_ready)
    throw new Error(`Fixture signoff failed: ${JSON.stringify(status.review)}`);
  await page.getByRole("button", { name: "批准", exact: true }).click();
  await page.getByRole("button", { name: "提交决定", exact: true }).click();
  await page.getByText("研究已完成", { exact: true }).waitFor();
  const streamEvents = await page.evaluate(() => {
    window.fixtureSource.close();
    return window.fixtureEvents;
  });
  if (streamEvents.filter((item) => item.event === "report.delta").length < 2)
    throw new Error("Report was not streamed in chunks");
  if (!streamEvents.some((item) => item.event === "workspace" && item.busy))
    throw new Error("No dynamic running state received");
  await page.reload();
  await page
    .getByRole("article", { name: "流式研究报告" })
    .getByText("完整内容已接收并校验")
    .waitFor();
  if (!(await page.getByRole("textbox", { name: "研究消息" }).isEnabled()))
    throw new Error("New chat composer disabled");
  await page.getByRole("button", { name: "查看来源 · 1" }).click();
  await page
    .getByRole("dialog")
    .getByText("交互验收材料", { exact: true })
    .waitFor();
  await page.getByRole("button", { name: "关闭详情" }).click();
  await page.getByRole("button", { name: "用量与记录" }).click();
  await page.getByRole("heading", { name: "运行预算与审核" }).waitFor();
  await page.keyboard.press("Escape");
  if (await page.getByRole("dialog").isVisible())
    throw new Error("Dialog not dismissed");
  await page
    .getByRole("article", { name: "流式研究报告" })
    .evaluate((element) => element.scrollIntoView({ block: "start" }));
  await page.screenshot({ path: "output/playwright/chat-report.png" });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: "output/playwright/chat-mobile.png" });
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth > innerWidth,
  );
  if (overflow) throw new Error("Mobile horizontal overflow");
  await page.getByRole("button", { name: "打开对话列表" }).click();
  await page.getByRole("navigation", { name: "最近对话" }).waitFor();
  await page.keyboard.press("Escape");
  await page.setViewportSize({ width: 1280, height: 800 });
  await page.goto("http://127.0.0.1:5187/reports/blocked-demo");
  await page
    .getByRole("alert")
    .filter({ hasText: "规划回复未形成计划" })
    .waitFor();
  if (!(await page.getByRole("textbox", { name: "研究消息" }).isEnabled()))
    throw new Error("Blocked planning cannot accept clarification");
  await page.screenshot({ path: "output/playwright/chat-blocked.png" });
  if (errors.length) throw new Error(errors.join("\n"));
  return {
    reportPath,
    browserErrors: errors,
    mobileOverflow: overflow,
    signed: true,
    refreshedReportVerified: true,
    streamEvents,
  };
}
