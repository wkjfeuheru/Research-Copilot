# LBO 杠杆收购模型 (Leveraged Buyout)

## 定位

PE/并购基金核心工具。回答"借钱买这家公司能赚多少"——计算 IRR 和 MOIC，判断交易是否值得做（IRR > 20-25% 为门槛）。

## 核心逻辑

```
Entry: 用高杠杆（60-80% 债务）收购目标公司
持有期（3-7年）: 运营改善 + 债务偿还 → 股权份额增厚
Exit: 出售公司，发起人获取增厚后的股权价值
```

### IRR 三大驱动因素

1. **EBITDA 增长** — 运营改善（收入增长 + 利润率提升）
2. **去杠杆化** — 用经营现金流偿债，股权占比扩大
3. **倍数扩张** — 退出倍数 > 入场倍数（不可控，保守假设持平）

## 标准流程

```
Step 1: 入场估值与交易结构
Step 2: Sources & Uses
Step 3: 运营模型（三表预测）
Step 4: 债务调度表与偿还瀑布
Step 5: 退出分析与回报计算
Step 6: 敏感性分析
```

## Step 1: 入场估值

| 项目 | 数值 | 说明 |
|------|------|------|
| LTM EBITDA | `[INPUT]` | |
| Entry Multiple (EV/EBITDA) | `[INPUT]` | 行业典型 6-12x |
| Entry EV | `[CALC]` | = LTM EBITDA × Entry Multiple |
| (-) Existing Cash (留用) | `[INPUT]` | |
| (+) Transaction Fees | `[INPUT]` | 通常 1-2% EV |
| (+) Financing Fees | `[INPUT]` | 通常 2-3% 债务总额 |
| = Total Uses | `[CALC]` | |

## Step 2: Sources & Uses

### Uses（资金用途）

| 项目 | 金额 |
|------|------|
| 企业价值 (收购价) | |
| 再融资现有债务 | |
| 交易费用 (Advisory + Legal) | |
| 融资费用 (OID + Arrangement) | |
| 最低现金余额 | |
| **Total Uses** | |

### Sources（资金来源）

| 来源 | 金额 | 占比 | 利率 | 类型 |
|------|------|------|------|------|
| Revolver (循环信贷) | $0 初始 | | L+__ | 高级有担保 |
| Term Loan A | | | L+__ | 高级有担保，摊还 |
| Term Loan B | | | L+__ | 高级有担保，bullet |
| Senior Notes | | | __% | 高级无担保 |
| Subordinated Notes | | | __% | 次级 |
| Mezzanine | | | __% PIK | 夹层 |
| **Total Debt** | | __% | | |
| Sponsor Equity | | __% | | |
| Management Rollover | | __% | | |
| **Total Sources** | | 100% | | |

**Sources = Uses 必须平衡**

### 入场杠杆统计

| 指标 | 数值 | 参考范围 |
|------|------|---------|
| Total Debt / EBITDA | | 4-6x 典型 |
| Senior Debt / EBITDA | | ≤ 3-4x |
| Equity / Total Cap | | 20-40% |

## Step 3: 运营模型

使用简化版三表预测（参考 three-statement-model.md），重点关注：

- Revenue CAGR 假设
- EBITDA Margin 改善路径（成本削减、经营杠杆）
- CapEx 需求（维护性为主，PE 通常压缩增长性 CapEx）
- 营运资本效率提升

### 产出: 年度 Free Cash Flow Available for Debt Service

```
EBITDA
(-) CapEx
(-) Δ NWC
(-) Cash Interest (不含 PIK)
(-) Cash Taxes
(-) Other (管理费等)
= Free Cash Flow for Debt Repayment
```

## Step 4: 债务偿还瀑布

### 偿还优先级（每年）

```
1. Mandatory Amortization（强制摊还，按合同）
2. Cash Sweep（超额现金流强制偿还）
3. Optional Prepayment（可选提前还款）
```

### Cash Sweep 机制（典型条款）

| 杠杆水平 | Sweep 比例 |
|---------|-----------|
| > 4.5x Debt/EBITDA | 75% 超额现金流 |
| 4.0-4.5x | 50% |
| 3.5-4.0x | 25% |
| < 3.5x | 0% |

### 年度债务调度表

对每个债务层级逐年追踪：

| Year | 期初余额 | 强制摊还 | Cash Sweep | 期末余额 | 利息费用 |
|------|---------|---------|-----------|---------|---------|
| 1 | | | | | |
| 2 | | | | | |
| ... | | | | | |
| Exit | | | | | |

### 信用统计监控（每年）

| 指标 | Y1 | Y2 | Y3 | Y4 | Y5 | 约束 |
|------|----|----|----|----|----|----|
| Total Debt/EBITDA | | | | | | 逐年下降 |
| Senior Debt/EBITDA | | | | | | ≤ 3-4x |
| Interest Coverage | | | | | | ≥ 2.0x |
| FCCR | | | | | | ≥ 1.2x |

## Step 5: 退出分析与回报计算

### 退出假设

| 项目 | 数值 |
|------|------|
| 退出年份 | Year __ `[INPUT]` |
| 退出 EBITDA | `[CALC]` from 运营模型 |
| Exit Multiple (EV/EBITDA) | `[INPUT]` 通常 = Entry Multiple |
| Exit EV | = EBITDA × Exit Multiple |
| (-) Net Debt at Exit | `[CALC]` from 债务调度表 |
| = Exit Equity Value | |
| (-) Management/Other Claims | |
| = Sponsor Proceeds | |

### 回报计算

```
MOIC = Sponsor Proceeds / Sponsor Equity Invested
IRR = (MOIC)^(1/Years) - 1
```

### 回报归因分解

| 驱动因素 | 贡献 |
|---------|------|
| EBITDA Growth | Entry EBITDA → Exit EBITDA 的增长带来的 EV 增加 |
| Deleveraging | 持有期债务净偿还额 |
| Multiple Expansion | (Exit Multiple - Entry Multiple) × Exit EBITDA |
| **Total Value Creation** | 三者之和 = Exit Equity - Entry Equity |

## Step 6: 敏感性分析

### 核心矩阵: Entry Multiple vs Exit Multiple → IRR

|  | Exit 7x | Exit 8x | Exit 9x | Exit 10x | Exit 11x |
|--|---------|---------|---------|----------|----------|
| Entry 8x | | | | | |
| Entry 9x | | | | | |
| Entry 10x | | | **BASE** | | |
| Entry 11x | | | | | |
| Entry 12x | | | | | |

### 辅助矩阵

- Revenue CAGR vs EBITDA Margin → IRR
- Leverage (Debt/EBITDA) vs Exit Year → IRR/MOIC

## 输出模板

```
结论: Entry @ __x EBITDA, Exit @ __x in Year __
      IRR = __% | MOIC = __x
      [PASS/FAIL vs 20-25% hurdle]

回报归因:
- EBITDA Growth: __% of total
- Deleveraging: __% of total
- Multiple Expansion: __% of total

关键风险:
1. [运营/增长风险]
2. [利率/再融资风险]
3. [退出环境风险]
```

## 投资判断框架

| IRR | MOIC | 判断 |
|-----|------|------|
| > 25% | > 3.0x | 强项目，推进 |
| 20-25% | 2.0-3.0x | 可接受，需运营改善确信度 |
| 15-20% | 1.5-2.0x | 边际项目，需额外催化剂 |
| < 15% | < 1.5x | 不值得做 |

---

## 高阶模块（进阶场景按需展开）

### A. Dividend Recapitalization（分红再资本化）

**机制**：持有期内发行额外债务，将所得作为股息分配给 Sponsor，提前兑现部分收益。

**对回报影响**：
- IRR ↑（货币时间价值：提前收回资金）
- MOIC 不变或略↓（退出时债务更高）

**典型约束**：
- 分红后 Net Debt/EBITDA < 4.0-4.5x
- 利息覆盖率 > 2.0x
- 通常交易完成后 ≥ 2 年才可执行

**建模要点**：
- 债务 Schedule 新增 Recap 行
- Recap Size = Recap Year EBITDA × 1.0-1.5x
- IRR 计算的中间现金流加入 Recap Dividend

---

### B. Add-on / Bolt-on Acquisitions（补强收购）

**核心价值——倍数套利 (Multiple Arbitrage)**：
- Platform @ 10-12x 收购
- Bolt-on @ 5-7x 收购
- 退出时以 Platform 级倍数估值合并实体

**建模方法**：
1. Platform LBO 单独建模（基准 IRR）
2. 每个 Bolt-on: 收购价 + 独立财务 + 整合成本
3. 合并实体: Combined FCF + 阶梯式协同 → Combined IRR
4. 判断: Combined IRR > Standalone → Accretive

**协同建模**：
- 成本协同: 50-75% 概率纳入 base case，实现周期 12-18 个月
- 收入协同: 25-50% 概率，保守打折
- 整合成本预算: Bolt-on EV 的 5-10%

---

### C. Management Equity & Waterfall（管理层股权瀑布）

**典型结构**：

| 组件 | 市场惯例 |
|------|---------|
| Management Rollover | 15-30%（美国），30-60%（欧洲） |
| Option Pool | 完全稀释的 10-15% |
| Sweet Equity | 完全稀释的 5-15% |
| Ratchet 触发 | 2.0-3.5x MOIC 或 20-25% IRR |

**对 Sponsor 回报影响**：
- Rollover 降低 Sponsor 出资 → 权益从 100% 降至 80-90%
- Option Pool 稀释退出时收益
- Net: Sponsor IRR 下降，但管理层对齐改善运营

**Waterfall 计算**：
1. 偿还优先级债务
2. Sponsor 收回本金
3. 超过 Preferred Return (8-10%) 的部分按 carry 分配
4. Ratchet 触发后管理层额外分配

---

### D. PIK 利息 (Payment-in-Kind)

**机制**：利息不以现金支付，滚入本金复利增长。
```
Debt_n = Initial Debt × (1 + PIK Rate)^n
```

**对回报**：
- IRR 几乎不变（省现金 vs 退出多偿债，抵消）
- MOIC ↓（退出债务膨胀，Equity 减少）

**PIK Toggle**：借款方可每期选择现金或 PIK 支付。连续 PIK 选举是财务压力信号。

**适用场景**：早期现金流紧张、需保留现金偿还优先债或再投资。

---

### E. Covenant 分析

**两大类型**：

| 类型 | 测试频率 | 典型场景 |
|------|---------|---------|
| Maintenance | 季度定期 | 银行贷款、循环信贷 |
| Incurrence | 仅触发动作时 | 高收益债、Cov-lite |

**Headroom 计算**：
```
Headroom = Covenant Limit - Actual Ratio
```
- Downside case 中保持 ≥ 20-25% EBITDA headroom

**Sponsor 谈判要点**：
- Leverage step-down 推迟 18-24 个月
- Equity Cure Rights（2-3 次）
- EBITDA add-backs 上限 ≤ 20%

---

### F. Operating Improvement（运营改善杠杆）

**PE 价值创造优先级**：

| 层级 | 杠杆 | 典型影响 |
|------|------|---------|
| 1 | 定价优化 | 涨价 2-3% 直落 EBITDA |
| 2 | 采购整合 | COGS -3-8% |
| 3 | Working Capital | DSO↓ DPO↑ → FCF 改善 |
| 4 | 组织/IT 精简 | SG&A 永久↓ |
| 5 | CapEx 效率 | Maintenance vs Growth 分离 |

**时间线**：0-100天 Quick Wins → Year 1 系统变革 → Year 2-3 协同兑现
