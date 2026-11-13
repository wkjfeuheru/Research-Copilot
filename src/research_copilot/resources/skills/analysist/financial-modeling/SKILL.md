---
name: financial-modeling
type: analysis_method
description: 投行级财务建模与估值分析。覆盖三表联动、DCF、可比公司、LBO、并购模型、先例交易六大基础模型，以及APV/SOTP/DDM/实物期权等高阶方法和7大行业适配层，按需路由加载。
description_en: "Investment banking grade financial modeling & valuation. Covers 6 core models + advanced techniques (APV, SOTP, DDM, Real Options) + 7 industry overlays, with on-demand reference loading."
version: "1.0"
---

# Financial Modeling Skill

## Research Copilot 执行边界与脚本路由

按任务需要加载脚本、参考资料，无固定阅读顺序或全文读取门槛。仅 analysist 可通过 execute 在 Docker 中执行 Python；技能不增加联网、交易或批准权限。事实和口径缺口交给 searcher。

| 分析目的 | scripts/ 脚本 |
|---|---|
| 同比、环比、CAGR、累计转单季、百分点 | growth.py（calculate / period_change） |
| 毛利率、净利率、ROE、ROA、负债率 | ratios.py |
| 毛利率环比归因、业务结构与原材料价格贡献 | references/gross-margin-attribution.md |
| PE、PB、PS、EV/EBITDA | valuation.py |
| 可比公司表 | comparison.py |
| 显式 FCFF 的年末折现 DCF | dcf.py |
| WACC × 永续增长率 | sensitivity.py |
| Matplotlib 静态图或 ECharts 交互及静态图 | charts.py、echarts_options.py |
| 输入、十进制与结果清单 | io_contract.py；references/execution-contract.md |
| 图种、来源与展示选择 | references/chart-selection.md |

脚本可导入纯函数，也可运行 `python /skills/analysist/financial-modeling/scripts/growth.py input.json output.json`。period_change 可在生成代码中导入调用。
业务数字使用十进制字符串。标准上下文是 entity、period、unit、currency、basis、scale；不可比较或无输入时抛出明确缺口，不把异常当零。
在 write_prefix 中保存代码和输入，执行 `python /workspace/tasks/<dispatch_id>/code/analyze.py`；每次为新容器，当前派发目录为 cwd，输入与技能只读。将结果和图表写到 RC_OUTPUT_DIR。
`result.json` 是候选，不是正式凭据；执行服务登记后才有 metric_id/chart_id。随后独立 save_analysis，分析确认、审核和最终签署保持原流程。
ECharts 6.1.0 离线 SSR 输出 SVG/PNG，使用共享配置白名单，无 JS/HTML、URL、自定义渲染或依赖下载。Matplotlib 适合静态研究图。不得向 writer 开放计算。
DCF 不构建完整三表预测；FCFF、WACC、g 均需显式输入或带依据的假设。三表、LBO、并购等参考文档提供方法，不宣称标准脚本已实现这些模型。生成代码的方法正确性仍需 reviewer 审核。
本期输出结构化分析和图表，不交付 Excel，不调用外部 xlsx 技能，不新增人工暂停节点。

## 触发条件
当用户需求涉及以下关键词/场景时触发：
| 关键词 | 路由目标 |
|--------|---------|
| 三表联动、财务预测、利润表预测、资产负债表、现金流量表、财务模型 | three-statement |
| DCF、折现现金流、内在价值、WACC、终值、每股价值 | dcf |
| 可比公司、Trading Comps、PE倍数、EV/EBITDA、相对估值、Football Field | comps |
| LBO、杠杆收购、PE收购、IRR、MOIC、债务偿还、Cash Sweep、Dividend Recap | lbo |
| 并购、M&A、增厚稀释、Accretion/Dilution、协同效应、EPS影响、SPAC、Fairness Opinion | merger |
| 先例交易、Precedent Transactions、控制权溢价、历史交易倍数 | precedent |
| APV、SOTP、分部估值、DDM、剩余收益、实物期权、回归估值、国别风险 | advanced |
| SaaS估值、银行估值、REIT、NAV、rNPV、管线估值、储量估值、新店模型 | industry |
| 综合估值、全套建模、完整估值报告 | full（加载全部） |
## 不负责
- 实时行情获取/交易执行（需要 MCP 数据源接入）
- 技术面分析/K线形态
- 宏观经济预测
- 法律/税务咨询
- 会计准则解读（仅按通用 GAAP/IFRS 建模）
## 路由判定与 Reference 按需加载
### 路由信号判定
| 信号 | 路由 | 说明 |
|------|------|------|
| 用户明确指定模型类型 | 单模型 | 只加载对应 reference |
| 用户要求"估值"且无具体方法 | standard | 加载 dcf + comps |
| 用户要求"全套估值"或"投行级分析" | full | 加载全部 8 个 reference |
| 涉及收购/并购交易 | merger + precedent | 交易驱动场景 |
| 涉及 PE/基金/杠杆收购 | lbo | 可追加 three-statement |
| 用户要求财务预测但不涉及估值 | three-statement | 纯预测场景 |
| 涉及 APV/SOTP/DDM/实物期权等高阶方法 | advanced | 加载 advanced-techniques.md |
| 目标为特定行业（SaaS/银行/REIT/油气/生物科技） | industry | 加载 industry-overlays.md + 对应基础模型 |
### Reference 加载规则
| 路由 | 必须加载 | 可选加载 |
|------|---------|---------|
| three-statement | `three-statement-model.md` | — |
| dcf | `dcf-model.md` | `three-statement-model.md`（如需从头建模） |
| comps | `comps-analysis.md` | — |
| lbo | `lbo-model.md` | `three-statement-model.md`（运营模型部分） |
| merger | `merger-model.md` | `comps-analysis.md`（支付方式判断） |
| precedent | `precedent-transactions.md` | `comps-analysis.md`（对比分析） |
| advanced | `advanced-techniques.md` | 对应基础模型 reference |
| industry | `industry-overlays.md` | 对应基础模型 reference |
| full | 全部 8 个 reference | — |
## 交付物定义
### 单模型路由
通用完整建模场景的目标交付物为含活公式和可调假设的 `.xlsx`；Research Copilot 本阶段按项目适配规则输出指标卡、图表和结构化结论，不承诺 Excel。分析组织结构：
1. **假设总表** — 所有输入假设集中展示，标注 `[INPUT]`
2. **计算过程** — 关键中间步骤，表格化
3. **核心结论** — BLUF 结论先行（如每股价值、IRR、EPS增厚%）
4. **敏感性分析** — 至少一张二维敏感性矩阵
5. **风险/局限** — 模型假设的关键风险点

### Standard 路由（DCF + Comps）
| 交付物 | 内容 |
|--------|------|
| DCF 估值 | 每股内在价值 + WACC/g 敏感性矩阵 |
| Comps 估值 | 可比公司倍数中位数 → 隐含估值区间 |
| 估值协调 | Football Field 对比图（文字描述） |
| 投资建议 | 当前股价 vs 估值范围 → 高估/低估/合理判断 |
### Full 路由
Standard 交付物 + 先例交易估值 + 综合权重分配 + 完整估值报告
## 核心执行纪律
### 数据标注（强制）
| 标签 | 含义 |
|------|------|
| `[INPUT]` | 用户提供或分析师假设的输入值 |
| `[CALC]` | 由公式计算得出 |
| `[MKT]` | 市场公开数据（需标注来源和日期） |
| `[EST]` | 分析师估计值（需说明估计逻辑） |
### 建模规范
1. **假设集中**: 所有可调参数集中在假设表，正文计算中不出现硬编码数字
2. **单位一致**: 全模型统一货币单位（$M 或 ¥亿），开头声明
3. **时间轴对齐**: 所有表格列对应相同财年/季度
4. **勾稽检查**: 三表模型必须验证 BS 平衡（资产=负债+权益），CF 期末现金=BS 现金
5. **情景区分**: 涉及预测时，默认提供 Base Case；用户要求时追加 Bull/Bear
### 材料补采路由（由主控派发 searcher）
| 场景 | 搜索策略 |
|------|---------|
| 用户提供完整财务数据 | 不搜索 |
| 用户指定公司但未提供数据 | 搜索最近财报 + 市场数据（≤5次） |
| 需要可比公司/先例交易 | 搜索行业 peers + 历史交易（≤8次） |
| 需要 WACC 参数 | 搜索无风险利率 + 行业 Beta + ERP（≤3次） |
搜索优先级：公司年报/10-K > 财经数据库摘要 > 券商报告引用
## 执行步骤
1. **识别路由** — 根据用户问题判定模型类型
2. **加载 Reference** — 按加载规则读取对应 reference 文件
3. **确认输入** — 明确用户已提供的数据 vs 需要假设/搜索的数据
4. **执行建模** — 按 reference 中的标准流程逐步构建
5. **输出结论** — BLUF 结论 → 支撑数据 → 敏感性 → 局限性
## 分步验证纪律（Checkpoint 机制）
完整交互建模场景可在以下关键节点确认，防止假设错误传导；Research Copilot 当前将其作为本轮检查要点，使用现有人工确认流程：
| Checkpoint | 触发模型 | 确认内容 |
|-----------|---------|---------|
| CP1: 数据确认 | 全部 | 历史数据录入完毕后，展示给用户确认原始输入无误 |
| CP2: 收入逻辑 | 三表/DCF/LBO | 收入预测方法和 CAGR 假设确认 |
| CP3: WACC | DCF | WACC 各组件计算完毕后，确认再进入终值 |
| CP4: 交易结构 | LBO/M&A | Sources & Uses / 融资结构确认后再执行下游计算 |
| CP5: 估值结论 | 全部 | 最终估值结论展示前，确认关键假设摘要 |
规则：
- 在当前 Analyst 任务中检查适用的 CP 项，将未确认输入记为假设或限制，交由项目现有确认流程审阅。
- 不因本包规则新增 interrupt、暂停节点或额外用户确认。
## 数据源策略（MCP / API 优先级）
当需要获取财务数据时，按以下优先级选择：
| 优先级 | 数据源 | 适用场景 | 说明 |
|--------|--------|---------|------|
| 1 | 用户直接提供 | 所有 | 仍需本轮证据引用与来源、单位、期间核验 |
| 2 | MCP 金融数据服务 | 三大报表、K线、宏观 | 如 westock / neodata / iFinD（需环境支持） |
| 3 | 公司年报/10-K/季报 | 历史财务数据 | WebSearch 定向搜索 |
| 4 | 财经数据库摘要 | 一致预期、倍数 | WebSearch |
| 5 | 分析师估计 | 前瞻数据 | 最后手段，需标 `[EST]` |
当 MCP 金融数据服务可用时（检测到 westock / neodata / iFinD / 东方财富等 connector）：
- 优先通过 API 获取结构化数据
- 避免从 PDF/网页手动抽取已有的标准字段
- WACC 中的无风险利率优先从国债收益率曲线 API 获取
## 模型审计 Checklist（输出前必检）
模型交付前执行以下检查；当前工具无法修复的 FAIL 项明确写入 limitations，不伪造计算结果或修改证据：
### 通用检查（所有模型）
| # | 检查项 | 标准 |
|---|--------|------|
| 1 | 单位一致 | 全模型同一货币单位，开头已声明 |
| 2 | 假设集中 | 正文无硬编码数字，所有假设可追溯至假设表 |
| 3 | 时间轴对齐 | 所有表格列对应同一财年 |
| 4 | 数据标注 | 每个数字有 [INPUT]/[CALC]/[MKT]/[EST] 标签 |
| 5 | 数值合理性 | 毛利率 0-100%，增长率不极端，WACC 5-15% |
### 三表检查
| # | 检查项 | 容差 |
| 6 | BS 平衡 | 资产 - 负债 - 权益 = 0 (±$0.1M) |
| 7 | CF 现金 tie-out | CF 期末现金 = BS Cash |
| 8 | RE 滚动 | 期初 RE + NI - Div = 期末 RE |
### DCF 检查
| 9 | g < WACC | 永续增长率严格小于折现率 |
| 10 | TV 占比 | 60-80% 正常；80-90% 标注观察；>90% 需警示 |
| 11 | 双法交叉 | Gordon 隐含退出倍数 vs Exit Multiple 隐含 g 互验 |
### LBO 检查
| 12 | S&U 平衡 | Sources = Uses 精确匹配 |
| 13 | 杠杆逐年下降 | Debt/EBITDA 呈下降趋势 |
| 14 | 利息覆盖 | EBITDA/Interest ≥ 2.0x 全期间 |
### M&A 检查
| 15 | Pro Forma 完整性 | 融资成本+摊销+协同全部纳入 |
| 16 | 协同合理性 | 成本协同 < combined OpEx 15% |
| 17 | EPS 时间维度 | Year 1/2/3 分别计算（体现协同渐进） |
