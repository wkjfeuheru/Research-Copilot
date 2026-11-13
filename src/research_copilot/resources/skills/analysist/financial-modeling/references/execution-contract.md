# 沙盒执行与结果契约

代码、输入候选保存在当前 write_prefix，所有轮次独立，不继承 Python 内存或后台进程。
`execute(command, timeout)` 的命令仅在容器内运行；timeout=0 仍受服务上限限制。Docker 不可用时失败，不使用宿主。
授权证据投影在 `/workspace/evidence/<dispatch_id>/`；原值映射的 pointer 指向原始记录的 raw.structured（若有），否则 raw，不能指向投影包装。

写入 `$RC_OUTPUT_DIR/result.json`：

```json
{
  "schema_version": 2,
  "input_refs": [{"key":"revenue", "evidence_id":"E-000001", "content_hash":"原始证据哈希", "pointer":"/revenue", "raw_value":"100", "entity":"AAA", "unit":"CNY", "currency":"CNY", "period":"2025", "basis":"consolidated", "scale":"1", "transformation":"identity"}],
  "metrics": [{"key":"reported_revenue", "value":"100", "unit":"CNY", "input_keys":["revenue"], "description":"来源披露的收入", "semantic_type":"reported_value", "formula_id":"identity", "numerator_basis":"consolidated revenue", "denominator_basis":"not_applicable", "is_estimate":false, "display_precision":2}],
  "charts": [],
  "assumptions": {"wacc":{"value":"0.1", "basis":"说明采用依据", "impact":"解释估值影响"}},
  "checks": ["程序自检，不是独立审核"],
  "limitations": []
}
```

事实 pointer 的值与原始值必须相同，结构化 context/meta 与声明口径一致。文本提取提供 pointer、text_start/text_end 字符偏移、raw_value 和 extraction="text-extraction"，保留来源原文供审核。转换只记说明，原值不可改写。
metric.input_keys 按公式顺序引用本执行 input_refs.key、assumptions 的键或本清单中前序指标的 key；拒绝前向和循环依赖，最终必须追溯到授权事实。宿主沿依赖传播推算属性并保留原始输入口径。推算金额声明 semantic_type=inferred_amount；不能通过假设伪装事实，程序全部依赖是否完整由 reviewer 审核。

图表目录：chart.json、data.json、chart.png；ECharts 增加 option.json，可加 chart.svg。charts.py.render 可自动生成。
result.json 的 charts 只能放图表清单文件的相对路径字符串（如 ["charts/core_perf/chart.json"]），
不能放 render() 返回的字典；返回的 manifest 仅用于自检。
chart.json 字段：schema_version=1、engine、kind、title、theme_version="research-v1"、width、height、data_file、static_file、option_file（ECharts）、svg_file（可选）、metric_keys、input_keys、evidence_ids、alt。
文件名均相对 chart.json 所在目录。metric_keys 引用本轮指标局部 key；input_keys 可直接引用授权事实。宿主补全证据链；不得写空来源绕过验证。
同一派单的每次执行都必须完整提交 TaskBrief.required_metrics 的全部指标（语义类型与单位一致）；
只补图表时也要沿用上次的指标定义并写入本次 result.json，否则按缺少必需指标拒绝。
输出有限十进制字符串，不使用 NaN/Infinity；拒绝链接、越界路径和超限输出。程序不能向正式凭据库写入。

execute 返回 execution_id、status、metric_ids、chart_ids、文件引用和受限日志。探索命令无结果清单时只留执行记录。
相同 tool_call_id 不会重跑；状态不明停止。代码修正后使用新调用身份。执行成功但登记失败可从不可变输出恢复登记。
`save_analysis(candidate_path)` 独立校验保存，引用宿主返回的正式 ID；不会批准分析。
优先调用 `submit_analysis(candidate)` 直接提交精简结论和正式指标 ID，不复制指标定义或原始数据。保存成功后宿主自动构造 AnalysisResult，无需模型重复最终 JSON。submission-only 修复继承原执行的不可变回执，不允许重新计算。


## V2 语义契约（新执行）

result.json 使用 schema_version=2。指标必须包含 semantic_type、formula_id、numerator_basis、denominator_basis、is_estimate 和 display_precision。difference/product/ratio/growth_percent 是宿主复核的二元有序公式；其他 formula_id 由独立审核检查。率差单位是 pp，不是净利率贡献。估计值必须标识且沿依赖传播，舍入误差必须由输入精度推导。

中间计算保留 Decimal 全精度，只在展示时按 display_precision 舍入。宿主保存独立复算的 calculated_value 并用其验证后续公式；不得按已显示的四舍五入值继续计算。归因贡献、结构效应及残差须附 comparison：current_period、base_period、period_basis、entity、reporting_scope、denominator、mode（actual/scenario）。不同区间/分母/范围或实际与情景不得混合；实际归因所需口径未知时写局限，禁止伪造贡献。

容器只能向 RC_OUTPUT_DIR 写产物；/workspace/tasks 是只读快照。宿主 write_file/edit_file 用于修改代码和输入。使用回执 metric_key_to_id 获取ID，不根据返回列表顺序猜测。同一已批准计划/返工版本的分析执行默认总计两次尝试，重新派发不清零；复杂任务额度由人工批准的 ResearchPlan.analysis_execution_limit 提供。成功后的额外执行需要计划中的 execution_purposes。未知状态不能重跑。

```python
from pathlib import Path
import os, json
output_dir = Path(os.environ["RC_OUTPUT_DIR"])
output_dir.mkdir(parents=True, exist_ok=True)
(output_dir / "result.json").write_text(
    json.dumps(result, ensure_ascii=False, allow_nan=False), encoding="utf-8")
```
