# 行业适配层 (Industry Overlays)

## 定位

标准六大模型在不同行业需要显著适配。本文件定义各行业的替代估值方法、核心指标和模型调整点。当目标公司属于特定行业时，在标准模型之上叠加本文件的行业规则。

---

## 1. SaaS / 软件

### 标准模型适用性

| 模型 | 适用性 | 说明 |
|------|--------|------|
| 标准 DCF | 仅成熟期 | 增长投入压低短期 FCF，终值主导 |
| EV/EBITDA | 误导 | 被增长期投入扭曲 |
| P/E | 通常无意义 | 多数未盈利 |
| **EV/ARR** | **首选** | 行业标准 |

### 替代估值框架

| 方法 | 适用 | 关键参数 |
|------|------|---------|
| EV/ARR 倍数 | 增长 > 20% | 4-8x（中等），8-15x（premium） |
| EV/NTM Revenue | 有 consensus | 标准公开市场 |
| ARR-based DCF | 盈利 SaaS | Cohort-based revenue build |

### 核心指标体系

| 指标 | 公式 | 优秀标准 |
|------|------|---------|
| Rule of 40 | Revenue Growth % + EBITDA Margin % | ≥ 40% |
| NRR (Net Revenue Retention) | (期初ARR + expansion - churn) / 期初ARR | > 110% |
| LTV/CAC | 客户终身价值 / 获客成本 | > 3:1 |
| CAC Payback | CAC / 月度贡献利润 | < 18 个月 |
| Magic Number | 净新增 ARR / 上季度 S&M 支出 | > 0.75 |
| Burn Multiple | Net Burn / Net New ARR | < 1.5x |

### Cohort-based Revenue Build（替代传统收入预测）

```
Year N Revenue = Σ[Cohort_i 初始 ARR × NRR^(N-i)]
```
每年新增客群为一个 Cohort，各 Cohort 按 NRR 独立衰减/增长。

---

## 2. 银行 / 金融机构

### 标准模型适用性

| 模型 | 适用性 | 原因 |
|------|--------|------|
| FCFF-DCF | **完全失效** | 经营与融资无法分离 |
| EV/EBITDA | 不适用 | 杠杆是经营本身 |
| P/E | 可用 | 需标准化一次性项目 |
| **DDM** | **首选** | 分红受监管约束，天然契合 |
| **P/TBV** | **首选** | 核心估值锚 |
| 剩余收益(RI) | 优秀替代 | ROE vs CoE 直接揭示价值创造 |

### 核心指标

| 指标 | 含义 | 优秀标准 |
|------|------|---------|
| NIM (净利息收益率) | 息差盈利能力 | > 2.5% |
| ROE | 股东回报 | > 12% |
| CET1 | 核心资本充足率 | > 10% |
| NPL% | 不良贷款率 | < 1.5% |
| Cost/Income | 运营效率 | < 50% |
| Credit Cost | 拨备计提率 | 越低越好 |

### 估值核心等式

```
P/TBV = (ROE - g) / (CoE - g)
```
- ROE > CoE → P/TBV > 1（创造价值）
- ROE < CoE → P/TBV < 1（毁灭价值）

### DDM 特殊约束

- Payout 受 CET1 和压力测试(SCB)约束
- g = Retention × ROE（可持续增长率）
- 利率敏感性：NIM 变动 25bps → NI 变动 5-15%

---

## 3. 房地产 / REITs

### 标准模型适用性

| 模型 | 适用性 | 原因 |
|------|--------|------|
| 标准 P/E | **无意义** | GAAP 折旧严重扭曲（不动产通常增值） |
| EV/EBITDA | 被折旧扭曲 | |
| **NAV** | **首选** | 逐物业估值 → 加总 |
| **P/FFO** | **首选** | 替代 P/E |
| **P/AFFO** | 最精确 | 替代 FCF |

### 核心指标

| 指标 | 公式 | 用途 |
|------|------|------|
| FFO | NI + RE D&A + Impairments - Gains on Sales | 替代 EPS |
| AFFO | FFO - Recurring CapEx - Straight-line Rent Adj | "可分配现金流" |
| NOI | 物业收入 - 运营费用 | 物业级盈利 |
| Cap Rate | NOI / 物业市值 | 收益率/隐含折现率 |

### NAV 模型流程

```
1. 逐物业 NOI
2. NOI / 适用 Cap Rate = 物业市值
3. Σ 所有物业市值 + 其他资产 FV
4. (-) 总负债
5. = NAV → NAV/Share
```

### Cap Rate 参考（2025-2026）

| 板块 | Cap Rate | 风险 |
|------|---------|------|
| Industrial/物流 | 5.0% | 低 |
| 数据中心 | 5.5% | 低-中 |
| 住宅 | 5.0% | 低 |
| 办公 | 7.0% | 高（WFH） |
| 零售 | 7.0% | 高（电商） |

### AFFO Payout 安全性判断

- < 75%: 非常安全
- 75-85%: 健康
- 85-95%: 紧张
- > 100%: 分红不可持续

---

## 4. 石油天然气 / 矿业

### 标准模型适用性

| 模型 | 适用性 | 说明 |
|------|--------|------|
| 标准 DCF | 需大幅调整 | 必须结合储量框架和商品价格情景 |
| EV/EBITDA | 可用 | 需用 mid-cycle EBITDA |
| **PV-10 NAV** | **首选** | 储量基础估值 |
| **Strip DCF** | 优选 | 用 NYMEX 期货曲线替代固定假设 |

### 储量 NAV 估值

```
NAV = PV(Proved Reserves FCF) + PV(Probable) + PV(Possible) + Other Assets - Debt
```

差异化折现率：
- PDP（已开发已证实）: 10-12%
- PUD（未开发已证实）: 15-18%
- Probable/Possible: 20-25%

### 产量递减曲线 (Decline Curve)

```
双曲线: q(t) = qi / (1 + b·Di·t)^(1/b)
```
- 页岩井第一年递减率: 60-70%
- b 因子: 1.0-1.4（非常规）

### 核心 KPI

| KPI | 含义 |
|-----|------|
| Netback | 桶油净现金回报 |
| F&D Cost | 发现和开发每桶成本 |
| Reserve Replacement | 新增储量/年产量（>100% = 增长） |
| Break-even Price | 覆盖全成本的最低油价 |
| DD&A | 基于产量比例折耗（替代折旧） |

### 价格情景

- Strip Pricing（期货曲线）: 最接近市场预期
- Flat Pricing: 敏感性分析
- Management Case: 公司假设

---

## 5. 生物科技 / 制药

### 标准模型适用性

| 模型 | 适用性 | 原因 |
|------|--------|------|
| 标准 DCF | 无收入时完全失效 | |
| P/E | 无意义 | 多数亏损 |
| **rNPV** | **首选** | 概率加权管线估值 |
| **Pipeline Sum** | 标准 | 各药物 rNPV 加总 |

### rNPV 公式

```
rNPV = Σ[CF_t × Cumulative PoS_t / (1+r)^t]
```

### 临床阶段成功概率

| 阶段 | 整体 | 肿瘤 | 罕见病 |
|------|------|------|--------|
| Phase 1→2 | 65% | 55% | 70% |
| Phase 2→3 | 35% | 28% | 45-55% |
| Phase 3→NDA | 60% | 55% | 65% |
| NDA→Approval | 85% | 85% | 90% |
| **全流程** | **~10%** | **~7%** | **~15-20%** |

### 构建步骤（Backward Induction）

1. 计算获批后商业化 NPV（峰值销售 × 专利期 × 利润率）
2. 每退到一个阶段: rNPV = (下阶段价值 × PoS - 当阶段成本) / (1+r)^duration
3. 管线总估值 = Σ 各药物 rNPV + 现金 - 债务

### 专利悬崖建模

- LOE 后收入通常下降 70-90%
- 建模要素: LOE 时间点、仿制药进入速度、侵蚀曲线

### 折现率选择

- 8-12%（仅反映系统性风险）
- 特异性风险已由 PoS 捕获，不重复计入折现率

---

## 6. 保险

### 标准模型适用性

| 模型 | 适用性 | 说明 |
|------|--------|------|
| FCFF-DCF | 失效 | 与银行同理 |
| EV/EBITDA | 不适用 | |
| **内含价值(EV)** | **寿险首选** | Embedded Value |
| **P/TBV + CR** | **产险首选** | Combined Ratio 驱动 |

### 产险 (P&C) 核心指标

```
Combined Ratio = Loss Ratio + Expense Ratio
```
- < 100%: 承保盈利
- 100-103%: 行业均值
- > 103%: 承保亏损

### 寿险内含价值

```
EV = 调整后净资产 (ANW) + 现有保单业务价值 (VIF)
Appraisal Value = EV + VNB × Multiplier (5-15x)
```
- VIF: 现有全部保单未来利润 NPV
- VNB: 每期新签保单创造的价值

### P/EV 参考: 0.8-1.5x

---

## 7. 零售 / 消费

### 标准模型适用性

| 模型 | 适用性 | 说明 |
|------|--------|------|
| DCF | 可用 | 标准方法 |
| EV/EBITDA | **主要方法** | 4-6x（一般），8-12x（优质） |
| P/E | 可用 | 但净利率低(2-5%)导致波动大 |

### 核心指标

| KPI | 含义 | 优秀标准 |
|-----|------|---------|
| SSS (Same-Store Sales) | 同店增长 | 正值 = 有机健康 |
| 4-Wall EBITDA | 单店盈利（不含总部） | 正且改善 |
| Revenue/Sq Ft | 坪效 | 行业 benchmark |
| Inventory Turnover | 存货周转 | 8-12x/年 |

### 新店经济模型（增长型零售核心）

```
Payback Period = 初始投资 / 年化 4-Wall EBITDA
```
- 初始投资: 装修 + 存货 + 开业前费用
- 年1销售: 成熟店 70-85%
- Ramp-up: 12-18 个月
- 优秀标准: < 2 年回本

### 收入分解公式

```
Total Revenue = 现有店 × 单店均收 × (1 + SSS%)
             + 新店 × 单店均收 × Ramp-up Factor
             + 电商收入
```

### 电商单位经济学 (CM 分层)

- CM1 = AOV - COGS
- CM2 = CM1 - 物流/包装/交易费
- CM3 = CM2 - CAC/留存成本
- 关键: 首单通常不盈利，价值来自复购

---

## 跨行业适用性速查表

| 行业 | 标准DCF | P/E | EV/EBITDA | 首选替代 |
|------|---------|-----|-----------|---------|
| SaaS | 仅成熟 | × | × | EV/ARR + Rule of 40 |
| 银行 | × | 可用 | × | DDM + P/TBV + RI |
| REITs | × | × | × | NAV + P/FFO |
| 油气 | 需储量框架 | 周期性大 | 可用(mid-cycle) | PV-10 NAV |
| 生物科技 | × | × | × | rNPV + Pipeline Sum |
| 保险 | × | lumpy | × | EV(Embedded) + P/TBV |
| 零售 | 可用 | 可用 | **首选** | + SSS + 新店模型 |
