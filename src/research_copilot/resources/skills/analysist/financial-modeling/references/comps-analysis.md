# 可比公司分析 (Comparable Company Analysis / Trading Comps)

## 定位

快速回答"市场认为这类公司值多少钱"。通过同类上市公司的当前交易倍数，推导目标公司的隐含估值区间。**不含控制权溢价**。

## 标准流程

```
Step 1: 选取可比公司 (Screen Universe)
Step 2: 收集财务数据 (Spread Comps)
Step 3: 计算估值倍数
Step 4: 标准化与基准化
Step 5: 应用倍数 → 隐含估值范围
```

## Step 1: 选取可比公司

| 维度 | 标准 | 优先级 |
|------|------|--------|
| 行业 | 相同 GICS/SIC 子行业 | 最高 |
| 商业模式 | 收入模型、客户类型一致 | 最高 |
| 规模 | EV 在目标 0.3x-3x | 高 |
| 增长特征 | 收入增速/利润率相近 | 高 |
| 地域 | 同区域或可比市场 | 中 |

选取数量: 初筛 10-20 → 最终 6-12 → 核心组 3-5 家

## Step 2-3: 数据收集与倍数计算

### 按行业选择核心倍数

| 行业 | 首选倍数 | 原因 |
|------|---------|------|
| 通用工业/消费 | EV/EBITDA | 资本结构中性 |
| 科技/SaaS | EV/Revenue, EV/ARR | 多数未盈利 |
| 金融/银行 | P/E, P/TBV | 杠杆是核心业务 |
| REITs | P/FFO | FFO 替代净利润 |
| 零售 | EV/EBITDA, EV/EBIT | 考虑租赁资本化 |
| 能源 | EV/EBITDA, EV/DACF | 高折旧 |
| 电信/公用事业 | EV/EBITDA, Div Yield | 稳定现金流 |

### EV 计算

```
EV = Market Cap + Total Debt + Minority Interest + Preferred - Cash
```

### LTM 计算（财年结束日不同时）

```
LTM = 最近完整财年 + 最近季报 YTD - 去年同期 YTD
```

## Step 4: 标准化

| 调整项 | 处理方式 |
|--------|---------|
| 一次性收入/费用 | 加回重组费、扣除资产出售收益 |
| 股份支付(SBC) | 全组统一处理（加回或不加回） |
| 收购整合成本 | 加回 |
| 会计政策差异 | IFRS16/ASC842 统一 |
| 异常值 | > 中位数 ± 2σ 标注为 Outlier |

## Step 5: 应用倍数

### 统计汇总

| 倍数 | Mean | Median | 25th | 75th | 核心组 Median |
|------|------|--------|------|------|--------------|
| EV/LTM EBITDA | | | | | |
| EV/NTM EBITDA | | | | | |
| P/E (NTM) | | | | | |

### 隐含估值

```
隐含 EV = 目标公司指标 × Comps 倍数中位数
隐含 Equity = EV - Net Debt
隐含每股 = Equity / Diluted Shares
```

### 估值区间表

| 方法 | Low (25th) | Mid (Median) | High (75th) |
|------|-----------|--------------|-------------|
| EV/EBITDA | | | |
| P/E | | | |
| EV/Revenue | | | |

## Football Field（完整规则详见 advanced-techniques.md §8）

水平条形图描述各方法的估值范围，标注当前股价位置。

## 溢价/折价调整

| 调整 | 场景 | 范围 |
|------|------|------|
| 规模折价 | 目标远小于 Comps | -10% to -25% |
| 流动性折价(DLOM) | 非上市公司 | -15% to -30% |
| 增长溢价 | 目标增速显著高于 Comps | 按 PEG 归一 |
| 地域折价 | 新兴 vs 成熟市场 | -5% to -20% |
