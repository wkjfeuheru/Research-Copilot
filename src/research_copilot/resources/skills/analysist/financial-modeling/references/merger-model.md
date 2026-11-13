# 并购模型 / 增厚稀释分析 (M&A / Merger Model)

## 定位

回答"这笔收购对买方股东好不好"——核心输出 EPS 增厚/稀释分析，决定交易能不能做、出价上限和最优支付方式。

## 核心逻辑

```
买方独立 EPS vs 合并后 Pro Forma EPS
增厚(Accretive): Pro Forma EPS > Standalone EPS → 股东受益
稀释(Dilutive): Pro Forma EPS < Standalone EPS → 需要协同效应证明合理性
```

## 标准流程（8步）

```
Step 1: 交易假设（报价、溢价、支付方式）
Step 2: Sources & Uses / 融资结构
Step 3: 购买价格分配（PPA）& 商誉
Step 4: 合并调整（利息、新股、协同、摊销）
Step 5: Pro Forma 利润表
Step 6: EPS 增厚/稀释计算
Step 7: 盈亏平衡协同效应分析
Step 8: 敏感性分析
```

## Step 1: 交易假设

| 项目 | 数值 | 说明 |
|------|------|------|
| 目标当前股价 | `[MKT]` | "无影响"价格 |
| 收购溢价 | `[INPUT]` | 典型 20-40% |
| 每股报价 | `[CALC]` | = 股价 × (1 + 溢价) |
| 隐含股权价值 | `[CALC]` | = 报价 × Diluted Shares |
| 隐含 EV | `[CALC]` | = Equity + Net Debt |
| 隐含 EV/EBITDA | `[CALC]` | 对比行业合理性 |

### 支付方式

| 方式 | 机制 | 对 EPS 的影响 |
|------|------|-------------|
| 全现金 | 动用现金 / 发新债 | 失去利息收入 或 增加利息支出 |
| 全股票 | 发行新股给目标股东 | 增加流通股数 |
| 混合 | 部分现金 + 部分股票 | 综合影响 |

### 支付方式选择逻辑

- 买方 P/E > 目标 P/E → 全股票更容易增厚
- 买方 P/E < 目标 P/E → 全现金/全债务更优
- 利率低 + 信用好 → 发债收购成本低

## Step 2: Sources & Uses

| Uses | 金额 |
|------|------|
| 目标股权价值 (Equity Purchase Price) | |
| 偿还目标现有债务 | |
| 交易费用 (Advisory 1-2%) | |
| 融资费用 | |
| **Total Uses** | |

| Sources | 金额 | 利率 |
|---------|------|------|
| 买方现金 | | — |
| 新发债务 (Term Loan / Notes) | | __% |
| 新发股票 (Stock Consideration) | | — |
| **Total Sources** | | |

## Step 3: 购买价格分配 (PPA)

```
Purchase Price (Equity Value)
(-) 目标净资产账面值 (BV of Net Assets)
(-) 公允价值调整 (Fair Value Step-ups)
= Goodwill（商誉）
```

### 公允价值调整

| 项目 | 账面值 | 公允价值 | Step-up | 摊销年限 |
|------|--------|---------|---------|---------|
| 客户关系 | 0 | | | 10-15年 |
| 品牌/商标 | 0 | | | 15-20年/不定期 |
| 技术/专利 | 0 | | | 5-10年 |
| PP&E | | | | 按剩余年限 |

**新增摊销 = Step-up 金额 / 摊销年限**（每年减少 Pro Forma 净利润）

## Step 4: 合并调整项

| 调整项 | 影响 NI 方向 | 计算 |
|--------|-------------|------|
| 目标失去的利息收入 | ↓ | 动用现金 × 利率 |
| 新债务利息支出 | ↓ | 新债 × 利率 |
| 无形资产摊销 | ↓ | Step-up / 年限 |
| 成本协同效应 | ↑ | 预计节省额 |
| 收入协同效应 | ↑ | 预计增量收入 × Margin |
| 协同实现成本 | ↓ | 重组费用（一次性/分年） |
| 新发股份 | 分母↑ | 股票对价 / 买方股价 |
| 税盾 | ↑ | (利息 + 摊销) × Tax Rate |

## Step 5: Pro Forma 利润表

| 项目 | 买方独立 | 目标独立 | 调整项 | Pro Forma |
|------|---------|---------|--------|-----------|
| Revenue | A | B | +收入协同 | |
| EBITDA | | | +成本协同 | |
| (-) D&A + 新增摊销 | | | -PPA摊销 | |
| EBIT | | | | |
| (-) Interest | | | -新增利息 | |
| EBT | | | | |
| (-) Tax | | | | |
| **Net Income** | | | | |
| Diluted Shares | X | — | +新发股 | |
| **EPS** | | | | |

## Step 6: EPS 增厚/稀释

```
EPS Accretion/(Dilution) = (Pro Forma EPS / Buyer Standalone EPS) - 1
```

| 指标 | 数值 |
|------|------|
| 买方 Standalone EPS | $__ |
| Pro Forma EPS | $__ |
| 增厚/(稀释) | __% |
| 判定 | Accretive / Dilutive |

### 时间维度

- Year 1: 可能稀释（整合成本 + 协同未完全实现）
- Year 2-3: 协同逐步兑现，转为增厚
- 标准：如 Year 2 增厚，交易通常可接受

## Step 7: 盈亏平衡协同效应 (Break-even Synergies)

回答"需要多少协同才能不稀释 EPS?"

```
Required Pre-tax Synergies = EPS Dilution × Pro Forma Shares / (1 - Tax Rate)
```

| 指标 | 数值 |
|------|------|
| 无协同时 EPS 稀释 | -$__ |
| 盈亏平衡需要的税前协同 | $__M |
| 占目标成本基础的比例 | __% |
| 合理性判断 | 可达/激进/不现实 |

参考：成本协同 5-10% of combined OpEx 通常可实现；>15% 激进。

## Step 8: 敏感性分析

### 矩阵 1: 收购溢价 vs 增厚/稀释%

| | 20% 溢价 | 25% | 30% | 35% | 40% |
|--|---------|-----|-----|-----|-----|
| 全现金 | | | | | |
| 50/50 | | | | | |
| 全股票 | | | | | |

### 矩阵 2: 协同实现率 vs 增厚/稀释%

| | 0% 协同 | 25% | 50% | 75% | 100% |
|--|---------|-----|-----|-----|------|
| Year 1 | | | | | |
| Year 2 | | | | | |
| Year 3 | | | | | |

## 协同效应建模细节

### 成本协同（更可预测）

| 来源 | 典型节省 | 实现时间 |
|------|---------|---------|
| 裁撤重复岗位 (G&A) | 人员重叠 × 30-50% | 6-12月 |
| 采购整合 | 5-10% 供应商成本 | 12-18月 |
| IT 系统合并 | 重复系统运维费 | 18-24月 |
| 设施合并 | 租金 + 运维 | 12-24月 |

### 收入协同（更难实现，通常打折 30-50%）

| 来源 | 逻辑 |
|------|------|
| 交叉销售 | 买方客户 × 目标产品渗透率 |
| 新市场进入 | 利用目标渠道/牌照 |
| 产品组合 bundling | 提高 ARPU |

### 协同净利润影响

```
NI Impact = (Cost Synergies + Revenue Synergies × Margin - Integration Costs) × (1 - Tax)
```

## 输出模板

```
结论: [买方] 收购 [目标] @ __x EV/EBITDA（溢价 __%）
      支付方式: [现金/股票/混合]
      Year 1 EPS: [增厚/稀释] __%
      Year 2 EPS: [增厚/稀释] __%（协同兑现后）

盈亏平衡: 需 $__M 税前协同（占目标成本基础 __%）→ [可达/激进]

建议: [推进/需更高协同确信度/出价过高]
```

---

## 高阶模块（进阶场景按需展开）

### A. Contribution Analysis（贡献度分析）

**用途**：Stock-for-stock merger 或"Merger of Equals"中，确定合理的持股比例分配。

**分析矩阵**：

| 指标 | Company A 贡献% | Company B 贡献% |
|------|----------------|----------------|
| Revenue | A/(A+B) | B/(A+B) |
| EBITDA | | |
| Net Income | | |
| Total Assets | | |
| Free Cash Flow | | |

**逻辑链**：各指标贡献% → 加权平均 → 隐含持股比例 → Exchange Ratio

---

### B. 338(h)(10) Tax Election（税务选举）

**机制**：法律上 Stock Purchase，税务上视为 Asset Purchase → 买方获得 tax basis step-up。

**NPV 计算**：
```
Step-up = Purchase Price - Historical Tax Basis
Annual Shield = Step-up / 15 years × Tax Rate (§197 intangibles)
NPV Benefit = Σ[Annual Shield / (1+r)^t], t=1..15
```

**决策规则**：NPV(买方税盾) > 卖方额外税负(Gross-up) → 选举创造 net value。

**适用条件**：目标为合并集团子公司或 S-Corp，买方为 Corporation。

---

### C. Earn-out / 对赌（或有对价）

**估值方法**：

| 方法 | 适用 |
|------|------|
| 概率加权情景法(PWERM) | 线性 payout、离散里程碑 |
| Monte Carlo | 非线性 payoff（caps/floors/tiers） |
| 期权定价 | 类似期权的结构 |

**折现率**：
- 事件型（监管审批）: 8-10%
- 市场型（Revenue/EBITDA 挂钩）: 12-18%

**会计处理（ASC 805）**：
- Day 1: Fair Value 计入 Purchase Consideration → 影响 Goodwill
- 后续: 每期重估 FV，差异进损益

---

### D. SPAC / De-SPAC 分析

**稀释来源**：

| 来源 | 典型影响 |
|------|---------|
| Sponsor Promote (20% Founder Shares) | 直接稀释 |
| Public Warrants ($11.50 行权) | 扩大 share count 10-15% |
| PIPE | 视规模 5-20% |
| Redemption | 减少到账现金 |

**关键变量**：
```
Pro Forma Price = Enterprise Value / Post-Merger Fully Diluted Shares
```

**Redemption 情景**：低赎回(20%) → 充足现金；高赎回(80%+) → 需更大 PIPE → 更多稀释。

---

### E. Fairness Opinion（公允意见）

**标准分析组合**：
1. DCF
2. Comparable Company Analysis
3. Precedent Transaction Analysis
4. SOTP（如适用）
5. Control Premium Analysis
6. Contribution Analysis（stock-for-stock）

**法律标准**：
- Business Judgment Rule（无利益冲突时）
- Entire Fairness（控制股东交易）
- Revlon Duties（控制权变更）

**核心产出**：Football Field chart 展示估值区间 vs Offer Price。

---

### F. Day 1 Pro Forma Balance Sheet（开业日资产负债表）

**构建步骤**：

1. **Sources & Uses** 平衡
2. **目标调整**：消除现金/旧 Goodwill，调整 AR/Inventory 至 FV
3. **Purchase Price Allocation (PPA)**：
   - PP&E Step-up = FV - BV
   - 新增无形资产（客户关系、品牌、技术）
   - New DTL = Tax Rate × Total Step-ups
   - Goodwill = Purchase Price - Net Assets at FV + DTL
4. **融资调整**：消除旧债，记录新债各 tranche，资本化融资费用
5. **权益重置**：消除旧 equity，记录 Sponsor + Rollover equity

**DTL 后续处理**：
```
年度 Deferred Tax Expense = 额外 D&A from Write-ups × Tax Rate
DTL 逐年下降（Book Taxes > Cash Taxes → DTL Unwind）
```

**校验**：Total Assets = Total Liabilities + Equity，Goodwill 为 plug item。
