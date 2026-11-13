# 浏览器工程化夹具

这些页面挂载真实 React 组件或 `useReportStream`，但不访问研究后端，不产生真实批准，不调用模型。页面不是生产入口，Vite build 不将它们打包进主应用。

在两个终端从 `frontend` 启动：

```powershell
pnpm exec vite --host 127.0.0.1 --port 5190 --strictPort
node tests/stream-fixture-server.mjs
```

## 人工问题裁决

打开 `http://127.0.0.1:5190/tests/acceptance-harness.html`：

1. 选择通过，未填写问题处置时提交应禁用。
2. 选择误报，理由为空时提交仍禁用；填写理由后才可提交。
3. 切换冻结快照，旧处置、理由和通过选择全部清空。
4. 重新裁决并提交，页面显示请求中的 issue_id、reason、review_session_id 和当前 snapshot_id。
5. 签署夹具经过独立批准及提交后显示完成；它仅捕获前端载荷。
6. 切到硬问题夹具，不显示误报控件，通过按钮禁用。

## HTTP/SSE 竞争与重连

打开 `http://127.0.0.1:5190/tests/stream-harness.html`：

1. 初始快照 1，连接 live。挂起旧 HTTP，读取传输计数确认 pending=1。
2. 推送新 SSE，显示快照 2/completed。
3. 释放旧 HTTP，pending=0，但界面保持快照 2/completed。
4. 断开 SSE，浏览器自动重连；connections 增加，peers 保持 1，快照不倒退。
5. 手动重连，connections 再增加，peers 仍为 1。

传输使用浏览器原生 fetch/EventSource 与 loopback HTTP 服务器，后端数据是夹具。此证据不能替代 FastAPI、持久状态和完整研究工作流的联合 E2E。

2026-11-05 已用 Playwright CLI 执行以上步骤。截图位于仓库 `output/playwright/`：`review-signoff-fixture.png`、`review-hard-block-fixture.png`、`sse-new-survives-old-http.png`、`sse-reconnected.png`。主应用和本次两份夹具均纳入 `pnpm typecheck`；历史 `chart-preview.tsx` 尚未适配当前接口，不在此次夹具验收范围内。

## 实际前端与 PostgreSQL 后端联合验收

仓库根目录运行以下入口。`RC_TEST_DATABASE_URL` 必须指向**隔离测试集群**，具备 CREATE DATABASE 权限。入口只创建并清理自己的随机 `rc_web_*` 数据库；工作目录必须是新目录。

```powershell
# 先在 frontend 运行 pnpm build，准备实际生产前端资源。
.venv/Scripts/python.exe -m tests.fakes.serve_review_acceptance --workspace output/web-pg-acceptance-new --port 5192
```

打开 `http://127.0.0.1:5192/reports/report-1`，按页面填写误报理由、提交审核决定，再在独立签署关口批准并提交。页面应变为研究已完成；重载后仍显示完成，模型调用数不增加。完成后关闭页面，再调用 `POST http://127.0.0.1:5192/__fixture__/stop` 正常退出，清理专用数据库。工作区和日志保留。

该入口使用实际 `SupervisorFactory`、products-v1 调度、`SessionService`、ReviewService、签署工具、FastAPI、PostgreSQL checkpoint 以及前端 dist；模型是可计量的确定性假模型，前置采集、已批准分析和待审核草稿由测试夹具准备。它验证从预制草稿开始的审核/签署闭环，不证明完整规划采集链、图表视觉质量或真实供应商效果。

自动回归位于 `tests/integration/test_web_review_flow.py`，包含 SQLite 和显式 `-m db` 的 PostgreSQL 版本，覆盖缺少处置理由的重提示、无重复审核调用、签署后拒绝旧中断提交、唯一复盘任务、实际 HTTP/SSE 文档哈希与 Last-Event-ID 续传。

2026-11-05 浏览器实测截图：`web-pg-review-ready.png`、`web-pg-signoff-gate.png`、`web-pg-completed.png`；重载快照为 `web-pg-reloaded.txt`。数据库证据 `output/web-pg-durable-evidence.json` 记录人工通过、唯一签署回执、1 条复盘任务、2 次假模型调用共 24 token。初次浏览器夹具未装配经验查询服务，API 返回 503；已在夹具中补齐，并通过自动联合测试验证该接口，不能将最初的 503 计为成功。

## 从空白会话开始的 PG＋Docker 全链路

同一入口增加 `--fresh`，工作目录仍须是新目录；还需预构建 `research-copilot-analysist:1` 镜像和可用的 Docker 引擎：

```powershell
.venv/Scripts/python.exe -m tests.fakes.serve_review_acceptance --fresh --workspace output/web-full-acceptance-new --port 5192
```

打开 `http://127.0.0.1:5192/`，发送标明“离线验收夹具”的消息，依次在界面批准计划、批准分析，填写审核问题的误报理由并通过审核，最后独立批准签署。重载后确认完成状态和图表仍可读取。退出方式同上。

该模式不预置计划、证据、分析、草稿、审批或 checkpoint。生产工具获取冻结的两期原始收入，Docker 执行生成代码、计算同比并渲染图表，后续角色使用实际回执；审核假模型通过图片工具接收真实 PNG。所有模型响应和来源内容仍是确定性夹具，14 次调用/168 token 是合成用量，不能解释为真实模型效率、视觉判断质量或供应商账单。

自动回归：`tests/integration/test_full_product_flow.py`。`-m docker` 执行 SQLite 版本，`-m 'db and docker'` 执行独立 PostgreSQL/checkpointer 版本；后者已加入 CI 的 PostgreSQL 作业。2026-11-05 两个版本本地均通过，浏览器从创建到重载也通过。证据：`output/full-chain-image-green.*`、`output/full-chain-pg-first.*`、`output/playwright/full-pg-*`、`output/web-full-durable-evidence.json`。在此次全链路中发现并修正审核提示词禁止图片读取的冲突，先红证据保留为 `output/full-chain-image-red.*`。

## 持久研究队列与刷新恢复

空白会话模式增加 `--hold-queue`，仅在夹具中暂停 worker 启动，方便验证尚未执行的命令：

```powershell
.venv/Scripts/python.exe -m tests.fakes.serve_review_acceptance --fresh --hold-queue --workspace output/web-durable-acceptance-new --port 5192
```

创建研究后应显示“等待执行”；刷新后该状态不变，`runtime.queue_state` 为 pending，模型调用数为 0。调用夹具专用 `POST http://127.0.0.1:5192/__fixture__/start-queue` 后，继续在页面完成计划、分析、审核和独立签署，再刷新检查完成状态和图表。该接口不进入生产应用。退出时关闭浏览器并调用 `POST /__fixture__/stop`，只清理夹具自己的随机数据库。

2026-11-05 已使用真实前端、PG/checkpointer、Docker 执行该流程。`output/web-durable-queue-evidence.json` 记录五条命令（start 与四条 resume）全部 succeeded，14 次假模型调用/168 个合成 token；`output/web-durable-pending.json` 与 `output/web-durable-completed.json` 保存前后状态。截图为 `output/playwright/durable-queue-pending.png`、`durable-completed.png`、`durable-completed-status.png`、`durable-chart.png`、`durable-mobile.png`，390px 宽度无横向溢出。它证明数据库持久调度与流程连通，不代表多 Web 宿主部署、真实模型质量或成本验收。

## 研究运行恢复面板

用 `--run-recovery` 启动独立 PG 夹具，**不与 `--fresh` 合用**：

```powershell
.venv/Scripts/python.exe -m tests.fakes.serve_review_acceptance --run-recovery --workspace output/web-run-recovery-new --port 5192
```

打开 `http://127.0.0.1:5192/reports/report-1`。此模式先准备审核草稿，再注入已失联的研究命令和已发送 pending 请求；不启动真实模型或真实旧执行者，不把它当成进程强杀测试。页面应显示“执行结果待核查”，普通审批和消息暂不可操作。

1. 打开“运行恢复”：未确认旧执行者及相关执行停止、未填写依据时，写操作按钮禁用；pending 未登记时名额释放仍禁用。
2. 填写明确标识为离线夹具的核查依据，确认停机，点击“登记为已发送、用量未知”。只减少 pending，不记录重试授权、不启动研究；选中的请求保持不变。
3. 分别重新确认停机并填写依据：先核实总用量 8 token，再单独勾选可能重复收费的确认，记录一次性重试授权。核实用量不能替代重试确认，授权也不立即执行。
4. 单独填写名额释放依据并确认停机，释放名额。展开人工处理记录，再重载页面重新打开恢复面板，确认登记、用量核实、未使用授权和名额释放均保留。
5. 检查 390px 宽度下表单和审计可滚动阅读，无横向溢出。关闭浏览器后 `POST /__fixture__/stop` 清理其随机库。

2026-11-05 已完成上述实际前端/FastAPI/PG 浏览器流程。API 证据为 `output/web-run-recovery-unknown.json`、`web-run-recovery-reconciled.json`、`web-run-recovery-released.json` 和 `web-run-recovery-final.json`；截图/快照在 `output/playwright/run-recovery-*`。原两次审核假调用 24 token 加一条注入的请求，账本调用数全程保持 3；核实后合计 32 个合成 token，授权没有 consumed_by，研究未自动恢复。桌面及 390px 页面已检查（页面宽/滚动宽 390/390，抽屉 355/355）。这证明界面与账本/队列的恢复操作连通，不证明真实供应商用量或自动恢复能力。真实两进程强杀证据另见 `test_run_worker.py` 及实施记录。

## 复盘核实与一次性重试

同一入口支持 `--retrospective-recovery`，用于真实前端/HTTP/PG 的恢复操作验收：

```powershell
.venv/Scripts/python.exe -m tests.fakes.serve_review_acceptance --retrospective-recovery --workspace output/web-retro-recovery-new --port 5192
```

按前述预制草稿流程完成审核与签署，再打开“经验复盘”。本地假提炼器的首个调用模拟响应丢失，任务应停在失败状态、用量未知且不自动重试。展开“核实用量与恢复”：

1. 未填输入/输出用量或理由时，核实按钮禁用。填入冻结测试值与依据后保存，任务仍为失败，尝试数保持 1。
2. 重新展开，确认已记录核实值和人工记录。只填理由不能启动重试，必须单独勾选可能重复收费的确认项。
3. 点击“授权再尝试一次”，任务通过后台 worker 完成，尝试数为 2/2。完成状态不再显示授权重试按钮。
4. 查看两条人工处理记录，重载后确认复盘和已签署报告保持完成。桌面与 390px 宽度都可操作。

2026-11-05 已完成此流程，截图/快照为 `output/playwright/retro-*`，核实后与完成后的 API 证据分别为 `output/web-retro-reconciled.json`、`output/web-retro-completed.json`。核实测试值 10 输入/2 输出加上第二次假调用 12 token，汇总为 24 个合成 token；不代表真实供应商账单或实际恢复调用。该模式不创建真实模型客户端。结束后关闭浏览器并调用同一 `POST /__fixture__/stop`，清理其随机 PG 数据库。
