# 三表联动财务模型 (Three-Statement Model)

## 定位

所有估值模型的底层引擎。产出未来 3-5 年利润表 + 资产负债表 + 现金流量表的完整预测。

## 构建顺序

```
Step 1: 历史数据录入（3年）
Step 2: 收入预测
Step 3: 成本与利润率预测
Step 4: 营运资本调度表
Step 5: PP&E / 折旧调度表
Step 6: 债务调度表
Step 7: 利润表完成
Step 8: 资产负债表完成
Step 9: 现金流量表完成
Step 10: 勾稽检查 + 循环引用处理
```

## Step 1: 历史数据录入

录入最近 3 个完整财年数据。来源优先级：年报/10-K > 季报汇总 > 数据库。

同步计算历史比率作为预测锚点：
- 毛利率、营业利润率、净利率
- DSO、DIO、DPO
- CapEx/Revenue、D&A/PP&E
- 有效税率

## Step 2: 收入预测

### 方法选择

| 方法 | 适用场景 | 驱动因素 |
|------|---------|---------|
| 增长率法 | 成熟稳定业务 | YoY Growth % |
| 价量拆分 | 零售/制造/订阅 | Price × Volume |
| 分部拆分 | 多元化公司 | 按产品/地域/渠道分别预测 |
| 驱动因素法 | SaaS/平台 | 用户数 × ARPU、门店数 × 单店收入 |

注意：增长率应逐年向行业长期增速收敛（Mean Reversion）。

## Step 3: 成本与利润率预测

| 项目 | 预测方法 | 典型假设 |
|------|---------|---------|
| COGS | 毛利率 % × Revenue | 参考历史趋势 ± 管理层指引 |
| SG&A | 占收入比 % | 经营杠杆：收入增时比例下降 |
| R&D | 占收入比 % | 科技公司通常较稳定 |
| D&A | 来自 PP&E 调度表 | 不在此假设 |
| Interest | 来自债务调度表 | 不在此假设 |
| Tax | 有效税率 % × EBT | 参考历史有效税率 |

## Step 4: 营运资本调度表

### 公式

```
应收账款 = Revenue × DSO / 365
存货 = COGS × DIO / 365
应付账款 = COGS × DPO / 365
净营运资本 (NWC) = 应收 + 存货 + 其他流动资产 - 应付 - 其他流动负债
Δ NWC = 本期 NWC - 上期 NWC
```

## Step 5: PP&E / 折旧调度表

```
期末 PP&E = 期初 PP&E + CapEx - 折旧 - 处置
```

| 指标 | 预测方法 |
|------|---------|
| CapEx | 占收入比 %（区分维护性 vs 增长性） |
| 折旧 | 期初 PP&E × 折旧率，或按资产类别加权年限 |
| 无形资产摊销 | 按剩余年限直线摊销 |

## Step 6: 债务调度表

逐层追踪每个债务层级：期初余额 → 新增 → 强制摊还 → 可选偿还 → 期末余额 → 利率 → 利息费用。

### 循环引用处理

利息费用 → 净利润 → 现金流 → Revolver → 利息费用

- 方案 A（推荐初始）: 用期初余额计算利息，无循环
- 方案 B（精确）: 平均余额法 + 迭代/断路器开关

## Step 7-9: 三表结构

### 利润表

Revenue → COGS → Gross Profit → SG&A → R&D → D&A → EBIT → Interest → EBT → Tax → Net Income → EPS

### 资产负债表

资产: Cash + A/R + Inventory + PP&E + Goodwill + Intangibles
负债: A/P + Accrued + Short-term Debt + Long-term Debt
权益: Common Stock + Retained Earnings

### 现金流量表

经营: Net Income + D&A - Δ NWC + 非现金调整
投资: -CapEx ± 资产处置/收购
融资: 债务净变动 + 股份发行/回购 - 分红

## Step 10: 勾稽检查（模型无效则停止）

| 检查项 | 公式 | 容差 |
|--------|------|------|
| BS 平衡 | 总资产 - 总负债 - 权益 = 0 | ±$0.1M |
| CF→BS 现金 | CF 期末现金 = BS Cash | 精确匹配 |
| 留存收益 | 期初RE + NI - Dividends = 期末RE | 精确匹配 |
| 债务余额 | 调度表期末 = BS Debt | 精确匹配 |

## 情景分析（可选）

Base/Bull/Bear 三情景，假设集中切换：Revenue CAGR、EBITDA Margin 终态、CapEx/Revenue。

## 行业特殊考量（概要，详见 industry-overlays.md）

| 行业 | 特殊调度表 | 注意事项 |
|------|-----------|---------|
