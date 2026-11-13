# Research Copilot 本期执行范围

标准脚本 scripts/dcf.py 支持显式逐年 FCFF、年末折现、WACC、永续增长率、净债务、稀释股数；scripts/sensitivity.py 支持 WACC × g 矩阵。WACC > g、股数为正，事实输入和假设分别登记。缺少预测时报告缺口，不自动补造。
不声称完成三表预测、自动 WACC 估计、完整 Excel 模型。执行成功不证明公式正确；审核需检查期间、币种、折现时点、终值及假设依据。

# DCF 折现现金流模型 (Discounted Cash Flow)

## 定位

判断一家公司的"内在价值"。将未来自由现金流折现至今日，得出每股内在价值，对比当前股价判断高估/低估。

## 前置条件

- 需要完整利润表预测（来自 three-statement-model 或用户直接提供）
- 适用于有正现金流或可预见转正的公司
- 不适用于：早期亏损公司、银行/保险（用 DDM 或 Excess Return）

## 标准流程

```
Step 1: 确定预测期（通常5年，高增长可延至10年）
Step 2: 预测 UFCF（无杠杆自由现金流）
Step 3: 计算 WACC
Step 4: 折现 Stage 1 现金流
Step 5: 计算终值（Terminal Value）
Step 6: 折现终值
Step 7: EV = PV(Stage 1) + PV(TV)
Step 8: EV → Equity Value（Bridge）
Step 9: 每股内在价值
Step 10: 敏感性分析
```

## UFCF 推导

```
EBIT × (1 - Tax Rate) = NOPAT
+ D&A
- CapEx
- Δ NWC
= UFCF
```

## WACC 计算

```
WACC = E/(D+E) × Ke + D/(D+E) × Kd × (1-T)
```

### 各组件

| 组件 | 计算方法 | 数据源 |
|------|---------|--------|
| Ke（权益成本） | Rf + β × ERP (CAPM) | |
| Rf（无风险利率） | 10年期国债收益率 | Treasury/CGB |
| β（Beta） | 行业 Unlevered Beta → Relever | Bloomberg/Damodaran |
| ERP | 历史 4-7% | Damodaran 年度更新 |
| Kd（债务成本） | 加权平均利率 × (1-T) | 公司财报/信用评级 |
| 资本结构 | 目标或当前市值权重 | |

### Beta 调整（Hamada）

```
Unlevered β = Levered β / [1 + (1-T) × D/E]
Relevered β = Unlevered β × [1 + (1-T) × Target D/E]
```

## 终值计算

### 方法 1: 永续增长法

```
TV = UFCF_终年 × (1 + g) / (WACC - g)
```
g 通常 2-3%，不超过长期名义 GDP 增速。

### 方法 2: 退出乘数法

```
TV = EBITDA_终年 × Exit EV/EBITDA
```
退出倍数参考当前行业 Comps 中位数。

### 交叉验证

- Gordon 法 → 检查隐含退出倍数
- 退出乘数法 → 检查隐含永续增长率
- TV 占总 EV 比例: 60-80% 正常，>90% 需警示

## 估值桥 (EV → Equity → Per Share)

```
Enterprise Value
(-) Total Debt
(+) Cash & Equivalents
(-) Minority Interest / Preferred / Unfunded Pension
= Equity Value
÷ Diluted Shares (Treasury Stock Method)
= 每股内在价值
```

### 年中折现惯例 (Mid-Year Convention)

```
PV = FCF_n / (1 + WACC)^(n - 0.5)
```

## 敏感性分析（必做）

### 矩阵 1: WACC vs 永续增长率 → 每股价值
### 矩阵 2: WACC vs 退出倍数 → 每股价值

各取 5×5 网格，Base Case 居中标注。

## 输出模板

```
结论: [公司] 内在价值约 $XX/股 (Base Case)
      当前股价 $YY → [高估/低估 ZZ%]
估值区间: $AA - $BB
关键风险: 1. [最敏感假设] 2. [次敏感假设]
```

## 常见错误

| 错误 | 后果 | 预防 |
|------|------|------|
| g > WACC | 公式无效 | 硬约束 g < WACC - 1% |
| 用 FCFE 而非 UFCF | 双重计算杠杆 | 确认无杠杆口径 |
| TV 退出倍数用 NTM | 时间错配 | 统一用预测期最后一年 |
| 忘记年中惯例 | 低估 5-8% | 明确声明惯例 |
| 净债务日期不一致 | Bridge 失真 | 用最近季度 BS |
