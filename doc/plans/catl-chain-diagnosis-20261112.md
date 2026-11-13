# 宁德时代毛利率归因链路实测与改进方案

2026 年 11 月 12 日，以原问题“宁德时代：毛利率环比下滑，碳酸锂涨价和储能占比各贡献多少？”进行真实调用。结论：**当前环境未跑通完整研究链路**。请求成功进入持久队列并调用真实 DeepSeek，但五个 FinanceKB 数据域均因工具合同指纹不匹配而不可用；会话停在澄清环节，未产出财务数据、归因数值或报告。

本轮范围是实测、诊断和改进方案。未修改业务代码、批准合同或预算，未代替用户作审核或签署决定。下面的失败回放是保留的诊断证据，不是已经修复的回归结果。

## 本次实测结果

| 项目 | 结果 |
| --- | --- |
| 提交时间 | 2026-11-12 21:03:42，Asia/Shanghai |
| 报告 ID | `9548afd51c666bb7e623c1102b26abe3` |
| 输入 | 原问题逐字提交，没有补充季度、来源或归因假设 |
| 运行入口 | 正式 FastAPI `POST /api/reports`，HTTP 201；PostgreSQL 持久队列 |
| 后端配置 | `models.local.toml`、现有 production 合规规则、现有执行策略；API 使用 8001，避免占用 FinanceKB 的 8000 |
| 真实模型 | Supervisor / `deepseek-flash`，1 次；模型耗时约 15.74 秒 |
| 已确认用量 | 输入 5,335，输出 2,334，合计 7,669 tokens；未结算及未知请求均为 0；未获得货币成本 |
| 本次预算上限 | 沿用当前配置：6,000,000 tokens / 160 次模型调用；本轮未扩额 |
| 来源发现 | 真实 HTTPS MCP initialize / tools/list；5 个域可连接，14 个既有工具指纹漂移 |
| 来源业务调用 | 0；不能将 tools/list 成功等同于已经取到财务数据 |
| HTTP / SSE 读取 | 工作区 HTTP 200；SSE HTTP 200，收到 `workspace` 事件 |
| 真实浏览器 | Playwright 打开本次会话，显示“等待你补充信息”、实时连接、0 次派单；打开用量面板显示 1 次 / 7,669 tokens，未签署；控制台错误 0 |
| 采集与计算 | Searcher 派单 0、证据 0；未进入 Analysist 或 Docker 计算 |
| 写作与审核 | 未进入 Writer / Reviewer；报告正文接口返回 `Report text unavailable` |
| 最终状态 | `session_status=paused`，澄清中断；`signed=false`，`signoff_ready=false` |

队列命令显示 `succeeded`，只表示此次命令成功运行到持久化中断，不表示研究报告成功完成。应以会话状态、业务产物和审核签署状态共同判断成功。

模型实际要求用户明确两期财报及信息截止时点，说明全部来源“部署合同不匹配”，并询问图表偏好。没有编造两项贡献。由于原问题确实缺少具体季度，澄清季度本身合理；数据源故障才是即使补充季度也无法继续的确定阻断点。

证据目录：`output/catl-chain-diagnosis-20261112-2104/`。其中 `manifest.json`、`observability-latest.json`、`workspace-latest.json`、`events.sse`、`check-sources.json` 保存本次身份、实际用量、输出和来源状态。

浏览器证据为该目录的 `browser-snapshot.yml` 与 `output/playwright/catl-chain-diagnosis-20261112.png`。前端按现有锁文件安装依赖，临时运行配置将 `/api` 代理到 8001，未修改仓库的 Vite 配置。内置 Browser 插件因缺少自身运行文件无法连接，随后用独立 Playwright CLI 完成页面验证；这项工具环境问题未计入项目缺陷。

验收结束已关闭本次浏览器、临时前端和 API，保留数据库中的暂停会话。终端中断后核实本次宿主进程已退出、数据库连接断开、没有沙盒占用或未结算模型请求，再通过 `recover-runtime` 审计清理本次宿主记录；最终 `runtime-owners` 为空，见 `runtime-owners-after.json`。没有自动恢复研究或启动额外模型调用。

## 已证实的问题

### P1 描述字段变化导致五域整体失效

调用链：`mcp_client._tool_info` 对完整 input/output schema 计算指纹 → `discover_mcp_sources` 与批准指纹逐字比较 → 不一致抛出 `Approved MCP schema changed` → `open_financekb_sources` 将该域替换成始终报不可用的占位工具。

定位：

- `src/research_copilot/sources/fingerprint.py:19`：对完整 schema JSON 计算 SHA-256，包含描述文字。
- `src/research_copilot/sources/mcp_client.py:607`：将 inputSchema 与 outputSchema 一同送入指纹函数。
- `src/research_copilot/subagents/searcher.py:378`：指纹不等即拒绝，单工具不匹配会中断该域发现。
- `src/research_copilot/sources/financekb.py:166`：捕获不匹配并将整个数据域标为不可用。
- `src/research_copilot/sources/financekb_contracts.json`：本地批准快照尚未同步本次描述变更。

逐字段差异表明：

1. market / fundamentals / macro / news 共 12 个工具的共同变化，是输出 schema 的 `/properties/domain/description` 从四域说明增加了 `/ futures`。news 的关键词说明另有改写。
2. knowledge 的 2 个工具增加了字段解释，并修改了查询长度处理的说明。
3. 14 个工具的差异路径全部是 `description`；JSON Schema 结构约束没有变化。

单变量验证只回退 `get_ohlcv` 的那一个 `domain.description`，保留其他字段及原批准指纹，真实发现适配器便从拒绝变为接受。整个操作只在离线副本上进行，未修改运行合同。见 `schema-diff.json` 与 `single-variable-probe.json`。

因此，“连接或认证失败”不是本轮原因；“只要上游添加新工具，旧工具就天然不兼容”也不是原因。主要问题是把完整描述指纹同时当作结构兼容性判定，叠加部署合同更新没有与消费者协同。

**改进方案：**

- 短期逐项审阅已导出的 `financekb-proposal.json`，同步确认过的既有工具定义和指纹。不要关闭只读校验或无条件接受线上 schema。
- 长期分别维护完整合同指纹与结构约束指纹：完整指纹用于审计、冻结及描述变更审阅；结构指纹用于区分说明变化、兼容扩展和破坏性变更。涉及单位、累计口径、日期或检索边界的描述仍需业务审阅，不能一概视为无害。
- 在 FinanceKB 发布与 Research-Copilot 启动前执行消费者合同兼容性检查，输出工具名和 JSON Pointer 差异；避免只得到笼统的“合同不匹配”。
- 合同改变会影响宿主及报告来源身份。实施修复时须先检查待处理任务和未知用量，按既有维护流程停写、备份、显式更新宿主绑定，再新建验收会话；不应绕过旧报告的冻结合同直接续跑。

### P1 就绪检查未包含来源状态

本次 `/api/health`、`/api/ready` 均为 HTTP 200 / `status=ok`，同一环境的五域实际全部不可用。真实请求仍获准进入队列并消耗一次模型调用后，才向用户报告基础设施故障。

定位：`host.py:157` 只向后续层传递工具、说明、目录和合同，没有传递 `bundle.statuses`；`web/app.py:297` 的健康检查只判断数据库、模型配置和 backend 是否存在，`web/app.py:321` 直接据此决定 readiness。

**改进方案：**

- 将带检查时间的结构化来源状态传递给运行宿主、健康接口和前端；保留 `/api/live` 表示进程存活。
- 对需要外部数据的研究请求，在入队或调用模型前执行来源可用性检查；全域不可用时返回可诊断的阻断状态，提供修复原因和重试入口。
- 部分域不可用时按任务需要判断是否可继续。来源发现成功与一次具体取数成功分别记录，避免健康状态再次混淆这两层。
- 数据源修复后的状态刷新或明确重连必须有入口；当前 `open_financekb_sources` 中生成的不可用占位工具不会因上游恢复自行变成正常工具。

### P2 当前工具接入与归因所需数据不完整

这是代码与接口合同审查发现的后续能力缺口，不是本次已经发生的下游计算错误，因为本轮没有执行取数或计算。

- 上游真实工具目录已经包含 `get_segment_revenue`，但本地 fundamentals 批准目录仍只有 4 个旧工具。`discover_mcp_sources` 会跳过未批准工具，因此 Searcher 无法使用新增业务构成能力。
- 本地 `financekb_contracts.py:20` 仍固定为五域；上游新增 futures 域尚未接入。
- 即使接入 futures，其 `get_commodity_price` 也仅提供商品期货最新报价，不含现货或历史 K 线，不能代替历史季度锂价或宁德时代当期采购成本。
- 上游 `get_segment_revenue` 明确说明“源未标明单季或累计口径”，且不推算未单列的储能收入。它有用，但不足以保证季度储能贡献可以直接计算。

上游代码依据：`F:/python project/FinanceKB/src/queries/fundamentals.py:105`、`fundamentals_types.py:50`、`futures.py:12`。财务报表接口 `fundamentals.py:47` 则明确季度利润及现金流为年初至期末累计。

**改进方案：** 审阅并接入业务构成工具，同时建立归因任务的数据要求：季度口径、动力与储能收入权重、业务毛利率、历史锂价及成本传导假设、来源与披露日期。先核实既有公告、新闻和知识库能否补齐；必要时新增可追溯的历史锂价来源。缺少关键数据时，应输出不可识别的项及可支持的估算区间，不能把最新期货价或年度业务占比替换成单季实际值。

## 交互改进

现有 Supervisor 提示词在 `orchestration/__init__.py:149` 强制要求未说明图表时先提问，导致本次同时询问季度、故障处理路径和图表偏好。可将故障恢复与研究参数分开显示：先在运行状态中明确来源阻断；只向用户确认影响计算的季度口径；图表采用合理默认值或稍后调整。

本次模型称“我方当前无系统时间”。建议由宿主显式注入当前时间和时区，并区分“本次执行日期”与“研究信息截止日期”。当前日期可以提供默认 as_of，但不能据此擅自决定用户想比较哪个季度。

## 复现与验证

在仓库根目录运行：

```powershell
# 真实部署发现，无模型调用、无业务取数。
.venv/Scripts/python.exe -X utf8 -c "from research_copilot.cli import main; main()" check-sources

# 回放捕获的真实 schema，通过实际 discover_mcp_sources 代码路径。
.venv/Scripts/python.exe -X utf8 -m pytest output/catl-chain-diagnosis-20261112-2104/test_contract_replay.py -q --tb=short

# 后端 8001 运行时，对比真实五域状态与 HTTP readiness。
.venv/Scripts/python.exe -X utf8 -m pytest output/catl-chain-diagnosis-20261112-2104/test_readiness_live.py -q --tb=short
```

| 验证 | 本次结果 | 能支持的结论 |
| --- | --- | --- |
| 真实 check-sources，重复两次 | 退出码 2；0/5 域通过 | 部署不兼容稳定存在 |
| 每域单工具最小回放 | 5 failed，1.60 秒 | 各域在真实发现适配器上拒绝已捕获 schema |
| 单个描述字段回退 | 原样拒绝，回退后接受 | 描述变化足以触发该故障 |
| 真实 readiness 对照 | 1 failed，3.13 秒；200 != 503 | 全域不可用没有反映到就绪状态 |
| 既有来源合同、诊断与适配单测 | 32 passed，1.26 秒 | 本地单测通过不能代表部署兼容 |
| 前端观测、流、创建相关测试 | 6 passed | 所选前端离线测试通过 |

失败复现刻意放在输出目录，未加入默认测试集合。未实施修复，因此没有宣称这些复现已经转绿，也没有用离线结果代替后续真实端到端验收。

## 修复后的验收顺序

1. 五域合同检查通过；对说明变化与约束变化分别建立可通过、应拒绝的回归案例，确保未知工具不会自动获得批准。
2. 验证五域全部不可用时，页面和 API 显示阻断；研究请求在模型调用前停止。部分域可用时按实际任务需要决定是否继续。
3. 新建会话再次原样提交问题，通过正常澄清确定相邻季度及 as_of，再观察真实采集、证据登记、Docker 计算、写作和 Reviewer。
4. 内容验收必须检查：累计数转单季是否正确、毛利率变化是否使用百分点、两项贡献是否对应同口径证据及假设、其他因素和残差是否保留。不能预设“碳酸锂 + 储能”恰好解释全部变化，也不能把业务内毛利率变化全部归给锂价。
5. 数据充足时，报告给出可复算的贡献值或区间；不足时明确哪些项不能识别、缺什么数据。审核与最终签署仍按真实用户决定进行。

在这些步骤完成前，对采集之后的环节应标注“本轮未覆盖”，不能根据本轮提前阻断推断它们全部正常或全部故障。
