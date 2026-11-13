# 投研助手（Research Copilot）规格文档

> **2026-11-12 平台目标设计**：未来产品与架构以[综合投研平台设计基线](design/README.md)为入口。新目标包含项目/任务/实验、五类业务 agent、独立数据与模型/回测产物及用户隔离；设计确认不代表工程已经实施。本文件保留原有规格与历史修订，当前运行方式仍见根 README 与源码。

> **2026-11-07 主 Agent 自主编排修订**：当前新会话采用 `products-v2`。主 Agent 通过原生 todos 规划并动态派单；取消计划、分析、返工批准，仅保留 Reviewer 人工深审、显式澄清与最终签核。冻结 ReportScope 与原子任务登记替代批准计划，派单上限为 12，预算与角色隔离保留。旧会话只读，不迁移检查点。与下文冲突处以[本次实施方案](plans/research-copilot-remove-plan-gate-20261106.md)和[验收记录](plans/autonomous-workflow-acceptance-20261107.md)为准。


> 当前实现入口（2026-11-06）：[修复与单实例工程化记录](plans/engineering-implementation-20261105.md)、[产品工作流](plans/research-copilot-product-workflow.md)和 [README](../README.md)。当前以 Web 工作台为主，CLI 用于运行与维护；已含 Docker 沙盒、复盘经验、持久队列及显式宿主绑定。启动只检查 schema 与绑定，升级须停写并显式执行迁移和登记；仍保持单宿主部署。新任务固定 `products-v1`，历史 `supervisor-v1` 保持兼容。签署终止研究流程，不新增 archive 节点。
>
> 下文 v2.6 是历史业务需求基线，保留原始阶段与旧文件引用供追溯；其中“CLI-only / 不含 Web、沙盒、经验 / M1–M4 完成”等描述不代表当前版本或本轮验收。当前代码和验收状态以上方实施记录为准；真实 PG/Docker/供应商证据不能由离线测试替代。

## 历史业务需求基线

- 版本：2.6（删除采集前置验收，采集直接进入分析准备）
- 日期：2026-10-26
- 状态：M1–M4 工程实施完成；M4修复复审、离线749项、独立PostgreSQL56项及授权真实Writer小样本验收通过。M5 设计已确认，按 M5-A→G 分批实施（[m5-design.md](plans/m5-design.md)），尚未验收、未运行真实审核。M4 报告停在awaiting_review；详情及数据边界见doc/plans/m4-verification.md。新的真实外部源与模型联调仍按用户授权范围执行。
- 依据：研报业务工作流编排图，以及 2026-10-23 的需求对齐问答（Q1–Q62、C1–C3）

---

## 1. 概述

### 1.1 背景与目标

投研助手辅助研究员生成研报。系统按研报业务工作流编排：需求解析与规划 → 数据采集与调研 → 分析与建模 → 章节写作 → 质量控制与合规审查 → 人工签核 → 结束。审核通过的报告保存在当前会话工作区中，不设置独立归档节点。AI 负责采集、计算、起草和检查，研究员在关键节点确认、修改或打回。

本项目推倒重来。旧代码只复用 LLM 客户端与配置管理（`llm/client.py`、`llm/deepseek.py`、`config.py`），其余重写。

### 1.2 用户与使用方式

- 唯一用户角色是研究员。图中所有人工介入点（计划确认、分析确认、签核、返工确认）都由同一人完成。
- 交互方式是本地运行的对话式 CLI。一次只处理一个研报任务，任务可中断，之后恢复。
- 部署在本地开发环境（Windows），PostgreSQL 用 Docker Compose 启动。

### 1.3 本期范围

本期包含：完整工作流（含图中所有回路）、CNEquity MCP 与网页搜索两类数据源、预置财务计算与图表工具、研报模板与分析方法论两类 skill、AI 质量控制与合规审查、版本冻结与变更日志、Markdown 输出。

不在本期：Web 界面与多会话、代码执行沙箱、长期记忆与经验总结、向量检索、PDF 解析、多用户与权限、发布流程（审核通过并保存后任务即结束）。

### 1.4 支持的研报类型

行业研究、公司深度、策略报告、快速点评（事件驱动）。每种类型对应一个研报模板 skill，新增类型只需新增 skill。

---

## 2. 业务流程

### 2.1 工作流与回路

节点名称与回路标签沿用工作流编排图。

```
工作区提出研报需求
  → 需求解析与规划
  → 研究计划确认 ──修改计划──→ 需求解析与规划
        │（需求模糊时先在此澄清，回答后同样返回需求解析与规划）
        └─确认─→ 数据采集与调研
  → 分析输入冻结 → 分析与建模
  → 分析结果与关键假设确认 ──数据不足──→ 数据采集与调研
        ├─重新分析──→ 分析与建模
        └─通过─→ 章节写作与全文整合
  → 冻结送审版本（不可变版本，停在 awaiting_review）
  → 研究员显式开始审核
  → 准备审核快照（固定版本、输入与规则快照）
  → 质量控制 ∥ 合规审查（并行分支）
  → 审核结果复验与持久化（双分支屏障）
  → 双重审核与人工签核（逐项确认 / 驳回，人工可补充 H-*）
        ├─无确认问题且明确提交签核─→ 签核事务 → approved → 结束
        └─有确认问题─→ 问题定位与影响分析 → 返工方案确认
              ├─数据问题──→ 数据采集与调研 → 分析输入冻结
              ├─模型与结论问题──→ 分析与建模
              └─文本问题──→ 章节写作与全文整合
  → 返工沿原路径经过下游确认节点
  → 重新整合全文并冻结新版本（仍停在 awaiting_review）
```

- 冻结与审核分离：`freeze` 仍静态结束于 END 并停在 `awaiting_review`；研究员显式选择“开始审核”后，服务层以 `continue_review(report_id)` 从 `prepare_review` 续接。这样升级或读取旧 M4 会话（`resume` / `status`）不会自动启动审核、不新增模型费用。
- 双分支屏障：`qc` 与 `compliance` 都产出 complete receipt 并由 `review_join` 复验通过后，才进入 `signoff`。空问题清单是有效结果；缺 receipt、分支失败或身份不符无效。问题不直接触发自动返工。
- 返工沿原路径重新经过下游的校验与确认节点，但只重做受影响的部分（见 4.4）；返工后的新冻结版本仍停在 `awaiting_review`，由研究员决定是否开始新一轮审核。新版本必须重新审核全文，旧版本裁决不自动抵扣新问题。
- `confirm_rework` 是独立的人工介入点：`diagnose` 完成并写入 checkpoint 后才中断；确认前不产生采集、分析或写作副作用，取消返工回到本轮裁决流程。
- 不设置 `archive` 节点，不移动或复制归档报告；签核事务成功并标记 `approved` 后直接结束。

### 2.2 节点职责

| 节点 | 执行方 | 职责 |
|------|--------|------|
| 需求解析与规划 | Planner Agent | 需求模糊时产出澄清问题；需求明确时产出 TaskPlan |
| 研究计划确认 | 研究员 | 同一节点两种职责：回答澄清问题；确认或修改 TaskPlan |
| 数据采集与调研 | Researcher Agent | 按采集任务自主选择 CNEquity、TickDB 或网页搜索，写入证据库 |
| 分析与建模 | Analyst Agent | 调用财务计算与图表工具，得出结论与关键假设 |
| 分析结果与关键假设确认 | 研究员 | 通过、数据不足或重新分析 |
| 章节写作与全文整合 | Writer Agent | 按研报模板串行写各章节，整合全文并插入引用 |
| 冻结送审版本 | 确定性代码 | 生成不可变版本文件与变更日志；停在 `awaiting_review` |
| 准备审核 | 确定性代码 | 复验冻结版本、正文 / manifest、证据与图表、模板与规则快照，计算 `review_input_hash` 并固定本轮审核输入 |
| 质量控制 | Reviewer Agent | 溯源、逻辑一致性、数字准确性、格式规范；数字预检由代码完成 |
| 合规审查 | Compliance Agent | 敏感词、强制披露、禁用表述；确定性规则扫描先行，模型只做语境复核 |
| 审核结果复验与持久化 | 确定性代码 | 校验两份 complete receipt 属于当前 version / review_revision / input_hash，持久化本轮审核结果并推进阶段；不新增业务审批 |
| 双重审核与人工签核 | 研究员 + 确定性代码 | 研究员逐项确认或驳回问题，可补充 H-* 问题；零问题也须明确提交；无确认问题后由代码在单事务中保存签核记录、标记 approved |
| 问题定位与影响分析 | Router Agent + 确定性代码 | Router 只做问题分类与目标定位；确定性影响闭包由代码计算 |
| 返工方案确认 | 研究员 | 确认或调整返工入口与执行范围；代码重新计算闭包，不能手工删掉强依赖下游 |


### 2.3 人工介入点

共四个阻塞式介入点，用 LangGraph 节点内的动态 `interrupt()` 实现，研究员输入后以 `Command(resume=...)` 恢复。

1. 研究计划确认
   - 澄清模式：展示 Planner 的澄清问题，研究员回答后返回规划。
   - 确认模式：展示 TaskPlan，研究员选择「确认」或「修改计划」（附修改意见）。
2. 分析结果与关键假设确认：展示结论、关键假设、图表和数据缺口，研究员选择「通过 / 数据不足 / 重新分析」并附意见。
3. 双重审核与人工签核（`signoff`）：展示质量控制与合规审查的问题、数字未核实情况与规则来源。签核 payload 固定 `report_id、version、review_revision、review_input_hash、issues_hash`。每条问题必须裁决且只能裁决一次：「确认 / 驳回」，驳回需填写理由；研究员可补充 AI 未发现的问题，使用 `H-*` ID 并遵循同样的定位校验，视为已确认。未知 ID、重复 ID、缺项、旧轮或旧版本答案一律拒绝。零问题也必须明确提交签核，不能按空列表自动批准；暂存选择与 `/quit` 不等于提交，草稿可与 `issues_hash` 绑定保存，重启后校验再恢复。没有确认问题时，完成完整性复验与签核事务后才结束，报告保留在当前会话工作区，不再进入独立归档节点；有确认问题时进入问题定位与返工确认。研究员对 AI 质量 / 合规发现拥有最终裁决权，但不能以驳回绕过文件损坏、审核未完成或版本错配等执行前提。
4. 返工方案确认（`confirm_rework`）：`diagnose` 完成并写入 checkpoint 后才在此中断，展示每个确认问题的分类、确定性影响闭包、最上游入口与每条扩展的依赖原因。研究员可修改定位和回退类别，代码重新计算闭包，不能手工删掉强依赖下游；取消返工回到本轮裁决流程，不隐式批准或丢弃原问题。问题定位（`diagnose`）本身不是人工介入点，模型调用与产物准备放在前一节点，`interrupt()` 前只做纯读取与校验。

交互形式：路由选择用菜单，修改意见用自由文本，任意输入提示处都可使用斜杠命令（见 9.1）。

### 2.4 自动处理规则

- 采集结束直接进入分析准备，自报缺口随数据进入分析；研究员可在分析确认节点选择已有 DC 任务补采，不自动循环补采。
- 质量控制与合规审查只产出问题清单，不自动打回，是否返工由研究员在签核节点决定。
- 节点执行异常（模型、工具、数据库调用在重试后仍失败）时，该节点触发异常介入，研究员可选择重试或终止任务。


### 2.5 TaskPlan

需求解析与规划产出 TaskPlan，写入全局 State。采集、分析、写作三个核心节点各自只读取自己类别下的任务。

```json
{
  "topic": "宁德时代 2024 年报深度分析",
  "report_type": "公司深度研究",
  "report_skill": "company_report",
  "tasks": {
    "data_collection": [
      {"id": "DC-1", "description": "获取宁德时代近三年利润表、资产负债表和现金流量表"},
      {"id": "DC-2", "description": "搜索 2024 年以来动力电池行业政策与价格动态"}
    ],
    "analysis": [
      {"id": "AN-1", "description": "分析营收与毛利率变化及驱动因素"}
    ],
    "writing": [
      {"id": "WR-1", "description": "撰写财务分析章节"}
    ]
  }
}
```

- 任务只有 `id` 和 `description`。数据源、方法、依赖关系不写入计划，由执行 Agent 自主推理。
- `report_skill` 是 Planner 选定的研报模板 skill 名称，Writer 与 Reviewer 读取同一个模板，保证章节结构一致。
- 任务 ID 前缀固定：`DC-`（采集）、`AN-`（分析）、`WR-`（写作）。
- 只有 Planner 能新增或删除任务。返工与补采复用已有任务 ID，补充意见以反馈形式传给执行 Agent。

---

## 3. 技术架构

### 3.1 技术栈

| 层级 | 选型 | 说明 |
|------|------|------|
| 语言 | Python 3.13 | 沿用 `pyproject.toml` 的 `>=3.13,<3.14` |
| 包管理 | uv | 沿用 |
| 工作流编排 | LangGraph | StateGraph、条件路由、`interrupt()`、并行分支 |
| 状态持久化 | langgraph-checkpoint-postgres | 每个节点后自动 checkpoint，可随时恢复 |
| 业务数据库 | PostgreSQL 17 | Docker Compose 启动；SQLAlchemy 2 + psycopg 3 + Alembic |
| LLM | DeepSeek API（OpenAI 兼容） | OpenAI Python SDK 2.x |
| 金融数据 | CNEquity MCP（已部署，stdio） | 官方 `mcp` Python SDK 作为客户端 |
| 网页搜索 | Tavily API + DeepSeek 搜索 API | 封装为单一 `web_search` 工具；DeepSeek 搜索的调用方式待确认（见第 11 节） |
| 计算与图表 | pandas、matplotlib | 只通过预置工具调用 |
| CLI | Typer + Rich | 对话式交互 |
| 测试 | pytest + httpx.MockTransport | 沿用旧项目的测试替身方式 |

运行时整体采用 asyncio（MCP SDK 只提供异步接口）。不再需要 FastAPI 与 uvicorn。

### 3.2 模型与节点映射

DeepSeek 当前可用模型（来源：api-docs.deepseek.com，2026-10 查询）：

- `deepseek-flash`：DeepSeek-V4.1-Flash
- `deepseek-v4-pro`：DeepSeek-V4-Pro

两者都支持思考模式（默认开启）、思考模式下的工具调用、JSON Output，上下文 1M token，思考与非思考同价。旧名 `deepseek-chat`、`deepseek-reasoner` 已不可用，问答中提到的 R1/V3 分工映射为下表（Q22、Q33）。

| 节点 | 默认模型 | 思考模式 |
|------|----------|----------|
| 需求解析与规划 | deepseek-v4-flash | 开启 |
| 数据采集与调研 | deepseek-flash | 开启 |
| 分析与建模 | deepseek-v4-flash | 开启 |
| 章节写作与全文整合 | deepseek-flash | 开启 |
| 质量控制、合规审查、问题定位 | deepseek-v4-flash | 开启 |

映射写在 `config/models.toml`，可按节点调整。实现约束：

- 思考模式下带 `tools` 的多轮调用，必须把每轮 assistant 消息的 `reasoning_content` 原样回传，否则 API 返回 400。
- 思考模式下 `tool_choice` 只支持 `auto` / `none`，不能强制调用某个工具。
- 结构化输出用 JSON Output 加 Pydantic 校验。思考模式与 JSON Output 同时使用的行为需在 M0 实测，不稳定则对该类调用关闭思考模式。


### 3.3 分层结构

```
┌──────────────────────────────────────────────────────┐
│ CLI（Typer + Rich）：对话、菜单路由、斜杠命令          │
└───────────────────────┬──────────────────────────────┘
                        │ astream / Command(resume)
┌───────────────────────▼──────────────────────────────┐
│ LangGraph 工作流：节点、条件路由、interrupt()          │
│ Agents：Planner / Researcher / Analyst / Writer /     │
│         Reviewer / Compliance / Router                │
└───┬──────────────┬──────────────┬─────────────┬──────┘
    │              │              │             │
┌───▼─────┐  ┌─────▼──────┐  ┌────▼──────┐  ┌───▼──────────┐
│LLM 客户端│  │ 工具层      │  │ Skill 库  │  │ PostgreSQL   │
│DeepSeek │  │ CNEquity    │  │ 研报模板  │  │ 业务表       │
│         │  │ web_search  │  │ 分析方法论│  │ checkpoint 表│
│         │  │ 证据/计算/图表│ └───────────┘  └──────────────┘
└─────────┘  └────────────┘
```

依赖方向自上而下：CLI 只依赖工作流；Agent 只通过工具层访问外部数据；数据库访问集中在 repository 层。


### 3.4 目录结构

```
Research-Copilot/
├── src/research_copilot/
│   ├── cli/              # main.py（Typer 入口）、session.py（对话循环）、
│   │                     # commands.py（斜杠命令）、render.py（Rich 展示）
│   ├── workflow/         # state.py、graph.py、routing.py、nodes/（每个节点一个文件）
│   ├── agents/           # base.py（工具调用循环）、planner.py、researcher.py、analyst.py、
│   │                     # writer.py、reviewer.py、compliance.py、router.py、prompts/
│   ├── tools/            # registry.py、mcp_client.py、cnequity.py、web_search.py、
│   │                     # evidence.py、financial.py、charting.py、skills.py
│   ├── skills/           # loader.py（解析 SKILL.md）
│   ├── llm/              # client.py、deepseek.py（复用旧代码并扩展）
│   ├── db/               # models.py、repository.py、migrations/（Alembic）
│   └── config.py         # 复用旧代码并扩展
├── skills/
│   ├── reports/          # industry_report/、company_report/、strategy_report/、flash_comment/
│   └── financial_modeling/ # SKILL.md + references/ 下 8 份参考文档
├── config/
│   ├── models.toml       # 节点 → 模型映射
│   └── compliance_rules.yaml  # 合规规则（不走 skill 机制）
├── data/                 # 运行产物：研报版本与图表（加入 .gitignore）
├── tests/                # unit/、integration/、fakes/
├── doc/spec.md
├── compose.yaml          # 只保留 postgres 服务
└── pyproject.toml
```

---

## 4. 核心模块设计

### 4.1 State

State 只存 JSON 可序列化的 dict，时间用 ISO 8601 字符串，节点边界用 Pydantic 模型校验（沿用旧代码约定）。证据全文等大体量内容存数据库，State 只放引用和摘要。

```python
class Task(TypedDict):
    id: str
    description: str

class TaskPlan(TypedDict):
    topic: str
    report_type: str
    report_skill: str
    tasks: dict[str, list[Task]]   # data_collection / analysis / writing

class EvidenceRef(TypedDict):
    id: str                        # 如 E-0012，全文在 evidence 表
    task_id: str
    source: str                    # cnequity / tavily / deepseek_search
    title: str
    summary: str

class AnalysisResult(TypedDict):
    id: str                        # 如 A-0003
    task_id: str
    conclusion: str
    key_assumptions: list[str]
    metrics: list[dict]            # 计算工具的输出，每项含 evidence_ids
    chart_ids: list[str]
    evidence_ids: list[str]        # 血缘：输入证据

class Section(TypedDict):
    id: str                        # S-01、S-02……
    task_id: str | None            # 兼容字段：task_ids 的第一个；无任务章节为 null
    task_ids: list[str]            # 本章承接的全部 WR-*（可空）
    title: str
    content: str                   # Markdown 正文，含 [^E-000012] 与 charts/ 图片
    summary: str                   # 20–300 字，供下一章衔接
    citation_evidence_ids: list[str]  # 正文实际脚注（首次出现顺序）
    read_evidence_ids: list[str]      # 工具实际返回过正文的证据
    declared_analysis_ids: list[str]  # 模型声明的分析 ID（审计用）
    prior_section_ids: list[str]      # 提供过摘要的前章
    evidence_ids: list[str]        # 完整依赖：引用 + 分析 + 图表 + 指标 + 实际读取 + 前章
    analysis_ids: list[str]        # 保守完整依赖
    chart_ids: list[str]
    writing_revision: int
```


```python
class ReviewIssue(TypedDict):
    id: str                        # Q-0001 / C-0001 / H-0001（研究员补充）
    source: str                    # qc / compliance / human；模型不能伪造 human
    category: str                  # 数字 / 证据 / 逻辑 / 结构 / 语言 / 披露 / 敏感词 /
                                   # 禁用表述；不同于 Router 的返工类别
    severity: str                  # high / medium / low：只影响展示排序，不自动决定返工或批准
    title: str
    description: str
    suggestion: str
    locations: list[dict]          # section_id 或 document/appendix 范围；可含片段与字符区间
    evidence_ids: list[str]        # 只能引用该冻结版本快照中的对象，允许为空
    analysis_ids: list[str]
    metric_ids: list[str]
    origin: str                    # deterministic / model，代码赋值
    rule_id: str | None            # 确定性规则来源
    check_id: str | None           # 确定性检查来源

class ReworkPlan(TypedDict):
    base_version: int              # 返工基准冻结版本
    manifest_hash: str             # 基准版本 manifest hash
    issue_ids: list[str]           # 本轮要处理的确认问题
    route: str                     # 最上游入口：collect / analyze / write
    task_ids: list[str]            # 需重做的 DC/AN 任务（按完整计划映射，不重排 ID）
    evidence_ids: list[str]        # 受影响的证据、分析、章节
    analysis_ids: list[str]
    section_ids: list[str]
    plan_hash: str                 # 计划内容 hash

class WorkflowState(TypedDict, total=False):
    report_id: str                 # 同时作为 LangGraph thread_id
    user_request: str
    clarifications: list[dict]     # 澄清问答记录
    planner_questions: list[str]   # 非空表示处于澄清模式
    task_plan: TaskPlan | None
    evidence: list[EvidenceRef]
    data_gaps: list[str]
    analysis_results: list[AnalysisResult]
    sections: list[Section]
    report_markdown: str | None
    version: int                   # 当前冻结版本号
    qc_issues: list[ReviewIssue]
    compliance_issues: list[ReviewIssue]
    confirmed_issues: list[ReviewIssue]
    rework: ReworkPlan | None      # 非空表示返工模式
    feedback: list[dict]           # 每次人工介入的选择与意见
    retry_count: dict[str, int]
    errors: list[dict]
    # M4 写作与冻结（实现扩展）
    writing_revision: int
    writing_started_at: str | None
    writing_input_hash: str | None
    writing_status: str            # pending / running / complete / stale
    writing_feedback: list[dict]   # M5 返工意见预留
    writing_artifacts: list[dict]
    writing_limits: list[str]      # 篇幅/零引用等可解释提醒
    writing_outline: list[dict]    # section_id → writing_task_ids
    writing_draft_hash: str | None
    freeze_status: str             # pending / freezing / frozen / failed
    freeze_artifacts: list[dict]
    # M5 审核、签核与返工（实现扩展）
    review_revision: int
    review_version: int
    review_input_hash: str | None
    review_started_at: str | None
    review_status: str             # pending / running / partial / complete / decided / failed / stale
    review_artifacts: list[dict]   # 本轮审核产物路径 / hash 摘要
    qc_result: dict | None         # 身份、complete、receipt 路径/hash 与问题
    compliance_result: dict | None
    signoff_decision: dict | None
    rework_revision: int
    rework_plan_hash: str | None
```

- 模型只产出候选问题；身份、定位与引用合法性由代码校验，规则命中不会被模型删除。每分支最多 100 个问题、单问题描述 2000 字，超限报错而不是静默截断或标记通过。精确重复可按 `source + rule_id + 定位 + 描述` 去重，不能因语义相似自动合并两个分支的问题；模型可附上下文意见，最终裁决由研究员完成。
- `ReworkPlan` 另保存复用条目（`ArtifactRef` / `ReuseReceipt`）、规则 / 模板 / 图表影响与每条闭包扩展的依赖原因，作为确认页展示与执行范围授权的依据。

phase 取值：`planning`、`awaiting_plan`、`awaiting_collection`、`collecting`、
`validating`、`awaiting_analysis`、`analyzing`、`awaiting_analysis_confirmation`、
`awaiting_writing`、`writing`、`freezing`、`awaiting_review`；M5 增加 `reviewing`、
`awaiting_signoff`、`diagnosing`、`awaiting_rework_confirmation`、`reworking`、`approved`。
M4 结束于 `awaiting_review`，只有 M5 签核事务成功才会写 `approved`；`reworking` 用于准备 /
展示，进入原节点后使用对应的 collecting / analyzing / writing 阶段。

M5 审核身份为 `(report_id, version, review_revision)`，`review_revision` 在报告内单调递增：
同次失败重试保留身份、时间与输入 hash，不创建新审核轮；同版本重新审核必须显式开启新轮，旧结果
与裁决保留；新版本不继承旧轮完成标记。`qc` 只写 `qc_result` / `qc_issues`，`compliance` 只写
`compliance_result` / `compliance_issues`，不并发写 `phase`、`errors`、`review_status` 或共享
列表；`prepare_review` 初始化公共字段，`review_join` 统一推进阶段。旧 checkpoint 缺字段按
pending / 空读取；空问题清单不等于审核完成或通过，不能以空列表代表审核已运行。

列表字段采用整体替换语义：节点返回完整的新列表；返工时按 ID 替换受影响条目，其余保持不变。
M4 的章节是执行单元：模板章节 `S-*` 固定，`WR-*` 任务通过一次写作大纲多对多映射到章节，
每个任务至少由一章承接；`task_ids` 保存完整关联，`task_id` 只是兼容字段。


### 4.2 图结构

| 节点 ID | 对应流程节点 | 出边 |
|---------|--------------|------|
| `plan` | 需求解析与规划 | → `confirm_plan` |
| `confirm_plan` | 研究计划确认 | 澄清已回答 / 修改计划 → `plan`；确认 → `collect` |
| `collect` | 数据采集与调研 | → `prepare_analysis` |
| `prepare_analysis` | 冻结分析输入 | 只做确定性冻结 → `analyze` |
| `analyze` | 分析与建模 | → `confirm_analysis` |
| `confirm_analysis` | 分析结果与关键假设确认 | 通过 → END（`phase=awaiting_writing`）；数据不足 → `collect`；重新分析 → `prepare_analysis` |
| `prepare_writing` | 冻结写作输入（模板/规则/证据/分析/图表） | 确定性冻结 → `write`（仅由研究员显式选择“开始写作”后续接） |
| `write` | 章节写作与全文整合 | 大纲 → 串行章节 → 草稿；→ `freeze` |
| `freeze` | 冻结送审版本 | 版本文件 + manifest + `report_versions` → END（`phase=awaiting_review`）；审核由 `continue_review` 显式续接 |
| `prepare_review` | 准备审核快照（M5） | 确定性复验与冻结 → `qc`、`compliance`（并行） |
| `qc` | 质量控制（M5） | 数字预检 + Reviewer → `review_join` |
| `compliance` | 合规审查（M5） | 规则扫描 + Compliance → `review_join` |
| `review_join` | 审核结果复验与持久化（M5） | 两份 complete receipt 校验通过 → `signoff` |
| `signoff` | 双重审核与人工签核（M5） | 有确认问题 → `diagnose`；无确认问题且明确提交 → 签核事务 → END（`phase=approved`） |
| `diagnose` | 问题定位与影响分析（M5） | Router 定位 + 确定性闭包 → `confirm_rework` |
| `confirm_rework` | 返工方案确认（M5） | 确认后按最上游入口 → `collect` / `prepare_analysis` / `prepare_writing`；取消 → 回到本轮裁决 |

- 并行分支：`add_edge([qc, compliance], review_join)` 是实现双分支屏障的必需写法，不是两条各自触发汇合的普通边；只有 join 复验通过后才进入 `signoff`。两个分支只写各自的 `qc_result` / `compliance_result` 键，不并发写 `phase`、`errors`、`review_status` 等共享键。
- 保留 `add_edge("freeze", END)`：审核不自动启动，由服务层 `continue_review(report_id)` 以持久化 `Command(goto="prepare_review")` 显式续接；返工后的新冻结版本同样停在 `awaiting_review`。
- 并行失败恢复：一个分支成功而另一个失败时，保留原 superstep 与已成功的 receipt，审核尝试日志独立保存、不修改并行中的公共 State，由服务错误视图列出全部失败 / 未完成分支，join 成功后再汇总；研究员选择重试时使用原 thread 恢复，不用 `Command(goto=某个分支)` 重新调度，也不先 `aupdate_state` 写重试计数。`review_join` 校验两份 complete receipt 均属于当前 version / review_revision / input_hash；空问题清单有效，缺 receipt、失败或身份不符无效。进程在模型响应到达但 receipt 尚未写入时崩溃，无法保证零重复计费：必须显示未决尝试，显式重试后才允许重做，不承诺 exactly-once 模型调用。
- 人工节点恢复会从节点开头执行，因此模型调用与产物准备放在前一节点，`interrupt()` 前只做纯读取 / 校验。
- `signoff`、`confirm_rework` 等介入节点返回 `Command(goto=..., update=...)` 路由；`diagnose` 以静态边进入 `confirm_rework`。
- 分析确认通过后不自动写作：CLI 在 `awaiting_writing` 显示“开始写作”菜单，经研究员选择后以持久化 `Command(goto=prepare_writing)` 从 `confirm_analysis` 已确认分支续接，保持费用闸门与旧 M3 checkpoint 兼容；分析返工后同样停在 `awaiting_writing`。
- 多类确认问题从最上游入口进入返工，但保留其余各类的显式目标并集；调整为更下游时必须逐问题明确重新分类并说明理由，再校验闭包，不允许只改入口枚举而保留不一致的根因。
- 编译时挂载 `AsyncPostgresSaver`，`thread_id = report_id`。
- 不注册 `archive` 节点。`signoff` 通过分支在人工输入恢复后，以数据库单事务保存签核记录（锁定报告与版本，检查 `current_version`、in_progress、本轮审核完成、两份 receipt hash、决定 hash，且无确认问题），将 `reports.status` 与当前 `report_versions.status` 更新为 `approved`，冻结文件不改写；approved / superseded 状态取数据库与签核记录，manifest 内 `status=frozen` 仅代表产物创建时状态。签核事务失败不显示已完成；事务成功但 checkpoint 未提交时，重启按匹配的签核决定幂等补完 `phase=approved`，不再询问同一签核；发现数据库已批准而待恢复答案不同则阻断。决定带稳定的 `idempotency_key`：完全相同请求返回原结果，同 key 不同载荷冲突。

### 4.3 人工介入实现

```python
async def confirm_plan(state: WorkflowState) -> Command:
    payload = build_plan_payload(state)        # 纯函数：澄清问题或 TaskPlan
    answer = interrupt(payload)                # 恢复后 answer 为研究员的输入
    return route_plan_answer(state, answer)    # Command(goto=..., update=...)
```

- 节点恢复时从头重新执行，`interrupt()` 之前的代码必须无副作用；写库等副作用放在 `interrupt()` 之后。
- payload 统一格式：`{"gate", "title", "body", "options", "allow_comment"}`，CLI 按 `gate` 渲染。
- CLI 用 `graph.astream(..., config={"configurable": {"thread_id": report_id}})` 驱动；遇到中断就渲染、收集输入，再以 `Command(resume=answer)` 继续。


### 4.4 血缘追踪与增量返工

血缘在执行时记录，不写入 TaskPlan：

```
采集任务 DC-x → 证据 E-* → 分析结果 A-*（evidence_ids、指标、图表）→ 章节 S-*（evidence_ids、analysis_ids）
规则 / 模板 → 附录或相关章节                        前章 S-*（摘要）→ 后章 S-*
```

- Router Agent 只负责定位：把每个确认问题归类为数据 / 模型与结论 / 文本，并定位到证据、分析或章节；不新增或删除 DC/AN/WR 任务。未知问题 ID、遗漏问题、越权任务或无法定位时报错或返回 `needs_clarification`，不能猜测目标后执行。
- 影响范围由确定性代码计算：先把 Router 根目标归一到有效对象（数据目标扩展为相关 DC 执行单元），再沿“被引用、被提供给模型、指标输入、图表输入、前章摘要”求下游闭包直至稳定；规则 / 模板变化影响附录或全部相关章节。不能只看正文展示脚注作为依赖。
- 分析上下文若收到全部证据，其实际依赖按完整输入保守记录；缺血缘或缺输入凭据时保守扩展并在确认页显示原因，不声称安全局部复用。
- 一个 DC 任务重做时，其活动结果集可能整体变化，该任务现有活动证据集一并列入潜在影响；数据返回后计算实际差异，可减少不必要执行并记录理由，但不得擅自扩展付费任务；新依赖超出批准范围须重新确认。
- 例：S-03 文本变化通常使 S-04 到末章进入闭包（后章消费前文摘要）；S-01 变化可能重写全部章节。只有不在闭包且输入上下文未变的条目可保留。
- 返工模式下（`rework` 非空），采集、分析、写作节点只处理 `rework` 中列出的条目，其余原样保留；校验和确认节点只展示变化部分。“未受影响”以上述闭包为准，不以模型是否显式引用某证据为准。
- 三类入口：数据 → `collect → prepare_analysis`（再进入分析与写作）；模型与结论 → `prepare_analysis → analyze → confirm_analysis`；文本 → `prepare_writing → write`。多类问题从最上游进入，但保留其余各类显式目标的并集；调整为更下游时须逐问题明确重新分类并说明理由。
- 增量执行按任务过滤，不重排 ID：采集重做使用新 cycle（不复用需要刷新的旧 source-call 结果，同内容去重可复用 E-ID，新证据追加不覆盖旧记录，旧版本的来源时间与 metadata 从旧冻结快照读取）；分析生成新 `analysis_revision`，先按完整计划映射确定 A-ID，再过滤目标执行，不能按过滤后的索引重新分配 A-ID。
- 复用需要独立凭据：按来源 plan 与来源契约版本验证旧 receipt，重新计算当前可见输入，确认输入等价、不在闭包且提示词 / 工具契约 / 公式版本与模型配置等执行条件相同，再保存本轮引用旧产物的 `ReuseReceipt`（`ArtifactRef`: kind、id、origin_revision、relative_path、sha256）。来源正文、摘要、依赖、创建时间与原始模型用量不改写，新轮用量计 0；版本不兼容时旧产物只可阅读，不能作为当前轮执行结果复用。写作每轮新建 `writing/rNNNN`，计划绑定 `base_version`、`rework_plan_hash`、目标章节与复用清单，不复制旧 receipt 改 revision / hash 伪装重新执行；全文确定性整合、脚注重排、附录重新渲染。
- 返工后的整合、冻结、质量控制与合规审查都针对全文，避免局部修改引入新的不一致；前序摘要变动即使正文改动很小也会影响后章输入，默认按闭包重做。新版本必须重新审核全文，旧问题裁决不自动抵扣新问题。
- 每次冻结生成变更日志，分开记录 `addressed_issue_ids`、重新审核后验证解决的 `resolved_issue_ids`、被替换 ID 与复用 ID；v1 记为初始版本。不能在重新审核前宣称问题已全部解决。

### 4.5 重试与异常

| 场景 | 处理 |
|------|------|
| 模型调用 429 / 5xx / 超时 | 指数退避重试 2 次（SDK 自动重试保持关闭，由上层处理，沿用旧约定） |
| JSON 解析或 Schema 校验失败 | 把错误回传模型重新生成，1 次 |
| 工具调用失败 | 错误作为工具结果返回给 Agent，由其决定换源或记为数据缺口 |
| CNEquity 进程退出 | 重建 MCP 会话 1 次 |
| 采集自报数据缺口 | 传入分析与报告；由分析确认的人工补采入口或审核返工处理 |
| 审核并行分支部分失败 | 保留原 superstep 与成功 receipt；研究员显式重试时用原 thread 恢复，不用 `Command(goto=分支)` 重新调度，也不先 `aupdate_state` 写重试计数 |
| 规则正则扫描超时 | 视为执行失败（不是无命中），阻断并提示调整审核范围 |
| 以上重试用尽 | 节点触发异常介入：重试或终止任务 |


旧采集验收 checkpoint 仅在显式恢复／重试时作一次迁移：确认计划与有效引用一致后追加新 checkpoint 到分析准备；保留原证据、成功凭据、历史记录和全部预算，旧量化限制并入 data_gaps。未知格式或引用损坏时拒绝迁移；状态查询不执行迁移。

### 4.6 Agent 设计

所有 Agent 共用 `agents/base.py` 中的工具调用循环：

1. 组装 system prompt、任务描述和上下文摘要。
2. 调用模型（带 tools）。有 tool_calls 就执行工具并把结果追加进消息，连同 `reasoning_content` 一起回传。
3. 没有 tool_calls 或达到步数上限（默认 12）时，要求输出最终 JSON，用 Pydantic 校验。

每个任务单独跑一个循环，任务之间只传摘要，控制上下文长度。

| Agent | 读取 | 可用工具 | 输出 |
|-------|------|----------|------|
| Planner | 用户需求、澄清记录、修改意见 | `search_skills` | 澄清问题或 TaskPlan |
| Researcher | `data_collection` 任务、补采意见 | CNEquity/TickDB 工具、`web_search`、`get_evidence` | 证据引用、数据缺口 |
| Analyst | `analysis` 任务、证据摘要 | `get_evidence`、财务计算、`render_chart`、`load_skill` | AnalysisResult |
| Writer | `writing` 任务、分析结果 | `load_skill`、`get_evidence` | Section、全文 Markdown |
| Reviewer | 冻结版本全文、血缘 | `get_evidence`、`load_skill` | 质量控制问题清单 |
| Compliance | 冻结版本全文、合规规则 | 无（规则扫描由代码完成） | 合规问题清单 |
| Router | 确认问题、冻结血缘与任务映射 | 无 | 每个问题的分类、目标 ID 与理由；闭包由代码计算 |

- Writer 按模板章节顺序串行写作，每章写完后把章节摘要传给下一章，保持前后连贯。
- Reviewer 的数字准确性检查先用代码建立事实目录、从正文提取带上下文的数值声明（排除股票代码、日期、章节号、metric ID 与脚注编号），再用 Decimal 规范化单位与比例；只有实体、期间、指标口径、单位与来源能唯一绑定时给出 matched / mismatch，其余为 unresolved，须由 Reviewer 说明，不能静默忽略未核实清单。
- Compliance 先跑确定性规则扫描，再由模型复核上下文。

---

## 5. Skill 系统

### 5.1 格式

每个 skill 是一个目录，入口文件 `SKILL.md`：YAML frontmatter + Markdown 正文。

```markdown
---
name: company_report
type: report_template
description: 公司深度研究报告模板，适用于单一上市公司的基本面与估值分析
---
# 公司深度研究报告模板

模板说明放在第一个章节之前。

## 1. 投资要点

<!-- writing_prompt: 3-5 条核心观点，每条附关键数据与引用 -->
<!-- length: 300-500 字 -->

## 2. 公司概况

<!-- writing_prompt: 主营业务、收入结构与竞争位置；只写有证据支持的事实 -->
```

M4 起模板正文使用严格语法：章节标题必须是 `## <序号>. <标题>` 且序号从 1 连续递增；
每章恰好一个 `writing_prompt`，可选一个 `length`；全文只允许一个 H1 与 H2 章节，
不允许更深标题、原始 HTML、图片、脚注或章节正文中的其他内容。解析器校验章节数
3–12、标题唯一且 ≤80 字符、指引 10–2000 字符、篇幅 50–4000 字；失败时在调用模型前
给出模板名与具体原因，不用目录名或旧缓存兜底。


### 5.2 研报模板与方法论

- V2 研报模板：`src/research_copilot/resources/skills/writer/reports/`，入口路由至四类参考模板，保留章节结构、写作指导和篇幅要求。
- V2 财务方法论包：`src/research_copilot/resources/skills/analysist/financial_modeling/`，统一入口负责路由、加载边界和项目限制，保留 8 份参考文档。V1 只支持显式配置的自定义目录及报告内历史快照。
- 合规规则不做成 skill，单独维护在 `config/compliance_rules.yaml`（见 8.3）。

### 5.3 Skill 工具

| 工具 | 使用方 | 返回 |
|------|--------|------|
| `search_skills(query, type)` | Planner | 只返回目录：name、type、description，不加载正文 |
| `load_skill(name, reference=None)` | Analyst | 入口或包内已登记参考文档正文、content_hash、available_references、package_hash |
| `load_skill(name)` | Writer、Reviewer | 本轮冻结的研报模板正文；拒绝 reference 参数 |

Planner 只看目录，选定 `report_skill` 写入 TaskPlan；Writer 和 Reviewer 加载同一模板。Analyst 按任务需要自行加载方法论 skill。

Analyst 首先加载 `financial_modeling` 入口，再按任务使用 `reference="dcf-model.md"` 等文件名按需读取；不接受路径。
普通估值使用 DCF + Comps 的方法论，full 路由加载全部 8 份参考文档。加载方法论不等于获得相应计算引擎。
当前仍由预置财务及图表工具生成可追溯结果，输出指标卡、图表和结构化结论；未实现的模型计算列为限制。
Excel 交付、沙箱 coding agent 和 Python 执行属于后续扩展；不新增 CP1–CP5 中断或联网权限。

分析准备时在本地冻结入口及全部参考文档，模型每次仅获得指定正文。
`content_hash` 是返回正文的 SHA-256；`package_hash` 是入口正文哈希与按文件名排序的参考文档哈希映射的规范 JSON 的 SHA-256。
整包哈希纳入轮次输入及逐任务复用指纹，参考文档变化使新轮次不可复用旧结果。
`skills-manifest.json` 固定整轮包集合；同轮恢复校验入口、参考文档及清单，不回退读取当前源包。历史快照不改写，旧快照支持原正文加载，参考文档不可用时明确返回失败。

---

## 6. 工具层

### 6.1 MCP 数据源

Researcher 的外部采集工具由宿主按 FinanceKB 五域统一装配；每域独立会话、独立批准清单，单域不可用只影响该域，不得回退其他来源。以下 6.1.1、6.1.2 与 6.2 为历史设计记录，不再参与运行；现行接入见 6.1.3。

#### 6.1.1 CNEquity MCP

- 启动：stdio 子进程，`python -m cnequity mcp --config "F:\python project\CNEquity\configs\cnequity.demo.toml"`。解释器路径、参数、工作目录都写在配置里（CNEquity 可能在独立的虚拟环境中）。
- 生命周期：首次使用时启动，整个任务复用同一会话，CLI 退出时关闭。会话由单一所有者异步任务创建和关闭，请求经队列串行执行；子进程退出至多重建一次。
- 工具发现：启动后调用 `list_tools()`（遍历分页），记录服务版本、工具名、inputSchema/outputSchema 与指纹。实际工具清单在授权的只读探测后记录，不硬编码金融 API 包装函数。
- 只读边界：`config/cnequity.toml` 保存“工具名 + schema 指纹”批准清单；未批准、schema 变化或含写入/维护语义的工具默认不暴露。`readOnlyHint` 只作线索；SQL 工具需核实服务端只读边界后才能显式放行。
- 调用去重：同一任务内按请求键复用已落库证据；失败与崩溃窗口见 7.1。
- 每次成功调用写一条证据，`source = cnequity`，`source_ref` 记录工具名、参数与 schema 指纹，绝不包含认证信息。

#### 6.1.2 TickDB MCP

- 连接：TickDB 托管 Streamable HTTP 端点（默认 `https://mcp.tickdb.ai/`），请求头 `X-TickDB-Key` 携带 `RC_TICKDB_API_KEY`；不启动本地子进程。认证头只由 HTTP 客户端持有，不进入日志、错误消息、证据或 `repr`。
- 生命周期：首次采集时连接，整个任务复用同一 MCP 会话；请求经串行队列发送，连接中断至多重建一次。超时、401/403 与网络失败返回安全类别，不泄露 URL 参数或 key。
- 工具范围：`config/tickdb.toml` 按名称批准 `get_ticker`、`get_kline`、`get_order_book`、`get_recent_trades`、`get_intraday`、`get_trade_days`、`get_market_metrics`、`get_capital_flow` 等只读行情工具；未批准或含写入/维护语义的工具不暴露。
- schema 指纹：条目可填 64 位 sha256 指纹以锁定 schema；留空表示按名称批准并在运行时警告。`scripts/inspect_tickdb` 只调用 `list_tools()` 生成指纹草案，不调用工具、不写库。
- 证据：每次成功调用写一条证据，`source = tickdb`，`source_ref` 记录工具名、参数、schema 指纹与服务版本；行情是调用时点快照，`published_at` 通常为空，不能当作公司披露原文。同一请求键复用已落库证据，失败按任务缓存。

#### 6.1.3 FinanceKB 五域 MCP（当前实现）

- 端点：`https://<RC_FINANCEKB_BASE_URL>/mcp/{market|fundamentals|macro|news|knowledge}`，Streamable HTTP + `Authorization: Bearer`；令牌只存在于请求头，不进入模型上下文、日志、证据或 `repr`。
- 工具目录：market（`resolve_symbol`、`get_ohlcv`、`get_realtime_quote`）、fundamentals（`get_financial_statements`、`get_valuation_metrics`、`get_company_profile`、`get_dividend_history`）、macro（`get_macro_indicator`、`get_economic_calendar`）、news（`search_news`、`get_filings`、`get_earnings_calendar`）、knowledge（`search_research`、`search_policy`），对模型统一加 `financekb_` 前缀。
- 批准合同：`sources/financekb_contracts.json` 保存已审阅的 inputSchema/outputSchema 与指纹；未固定指纹的域不挂载，schema 漂移、只读声明缺失或返回不符合 outputSchema 时只禁用该域并给出安全类别，不在启动时自动更新批准。
- 连接：`RC_FINANCEKB_CA_BUNDLE` 只配置该客户端的 SSLContext，保留证书链与主机名校验；`RC_FINANCEKB_CALL_TIMEOUT_SECONDS`（默认 150 秒）包含当前队列等待，不叠加重试。域掉线时保留已批准工具定义，调用返回安全的不可用结果。
- 返回契约：结构化四域使用 `available/domain/items`；`available=false` 或 `items=[]` 保留审计但不登记有效证据。knowledge 使用 `collection/as_of/page/page_size/returned/has_more/next_page/items`，每次只取一页，`page_size<=50` 且 `page*page_size<=200`；拒绝不前进的分页。
- 证据：结构化响应每个非空结果登记一条 `financial_data` 证据，pointer 指向 `/items/...`；knowledge 每个合格命中登记一条 `search_snippet`，整页原始响应保存在不可变采集 journal，`source.provenance` 记录响应哈希与命中 pointer。稳定身份排除排序分数等检索噪声；重复命中不重复计进展，片段不等于全文。
- 时间：研究截止日、报告期、发表/披露日、服务 `as_of` 与采集时间分别保留；无历史可得性证明时不得宣称 PIT。knowledge 命中按自身日期与任务相关性筛除，未知或越界命中保留审计并报 warning。
- 降级：缺必需数据返回 partial 或 blocked；仅可选缺口时允许下游继续并保留限制说明；单次查询失败只记录该查询缺口，同参数失败不重复请求，其他域在剩余预算内继续。
- 合同冻结：报告首次派单时把获批五域目录、schema 指纹、合同哈希与归一化版本写入 `runtime_manifest`，临时域健康不参与身份。来源合同变化后旧报告只读，写入端拒绝续跑；新研究须由操作者通过 `rc bind-host --replace` 显式登记新指纹，不自动改写旧 manifest/checkpoint。

### 6.2 web_search（历史设计，已停用）

```python
web_search(query: str, max_results: int = 8, recency_days: int | None = None) -> list[SearchHit]
```

- provider：Tavily 与 DeepSeek Search，按 `RC_SEARCH_PROVIDERS` 的显式顺序启用；缺凭证或单路失败时返回另一路并附具体 provider 状态警告，两路都不可用时报工具错误而不创建证据。
- 来源白名单：`RC_SEARCH_DOMAIN_ALLOWLIST` 按域名后缀限定来源（子域自动放行），`RC_SEARCH_DOMAIN_BLOCKLIST` 优先排除；Tavily 下推 `include_domains` / `exclude_domains`，DeepSeek Search 无域名参数，由聚合层在落库前客户端过滤并回报过滤条数。白名单为空表示不限制。
- 两路 HTTP 请求可并行，返回后统一落库；按规范 URL 合并展示并保留每路证据 ID。每路最多请求 `max_results` 条，展示最多 `max_results` 组。
- `recency_days` 映射为以本轮首次采集 UTC 基准固定的日期窗口；恢复时不滚动。DeepSeek 查询超过 128 字符时明确报参数错误，不截断需求。
- 每条命中写一条证据（`source_ref` 含 URL、provider 与请求标识，`content_kind = search_snippet`）；搜索摘要不等于已读取网页正文。
- `extract_evidence(evidence_id)` 对已落库网页摘要调用 Tavily /extract，生成新的证据 ID 并关联原条目，旧摘要内容不变；HTTP 200 不等于提取成功，失败保留原摘要并返回明确错误。瞬时类别（`extract_failed`、`extract_empty`、`timeout`、`connection`、`rate_limit`、`server_error`）同参重试一次，确定性失败不重试；最终失败把 provider 原因写入日志与 `source_calls.warnings`，仅保存可展示的安全消息。白名单外来源在发请求前以 `domain_not_allowed` 失败。


### 6.3 证据、计算与图表工具

- `get_evidence(evidence_id)`：返回证据全文和来源信息。Agent 上下文里只有摘要，需要细节时调用。证据由工具层自动写入，Agent 不直接写库。
- 财务计算（MVP）：`calc_growth`（同比、环比、CAGR）、`calc_ratios`（毛利率、净利率、ROE、ROA、资产负债率等）、`calc_valuation`（PE、PB、PS、EV/EBITDA）、`build_comparison_table`（多公司指标对比）。输入数值必须带 `evidence_id` 和字段路径，输出保留引用，供质量控制复核。
- `render_chart(type, data, title)`：折线、柱状、堆叠柱、饼图，matplotlib 输出 PNG，配置中文字体。图表文件不可变，返工生成新的 chart_id。
- 代码执行沙箱不在本期，后续出现复杂建模需求时再加。

---

## 7. 数据设计

### 7.1 业务表

| 表 | 主要字段 |
|----|----------|
| `reports` | id、topic、report_type、report_skill、status（in_progress / approved / aborted）、current_version、created_at、updated_at |
| `report_versions` | id、report_id、version_no、status（frozen / approved / superseded）、file_path、content_hash、manifest_hash、writing_input_hash、change_log、qc_issues（JSONB）、compliance_issues（JSONB）、signoff（JSONB）、created_at；`UNIQUE(report_id, version_no)` |
| `review_runs` | report_id、review_revision（报告内唯一）、version（FK）、input_hash、snapshot_path / hash、qc / compliance receipt hash、active_decision_id、status（prepared / running / complete / decided / failed / stale）、created_at |
| `review_decisions` | 审核身份、decision_id（唯一）、payload_hash、完整逐项决定及人工问题、submitted_at；同 key 异载荷拒绝 |
| `rework_runs` | report_id / rework_revision（唯一）、base_version、decision_id、plan_hash、计划与确认载荷、状态、结果 version；保留取消与重做方案历史 |

- `rework_runs.plan_hash` 覆盖审核身份、问题清单与类别、Router 定位、最上游入口、DC/AN/图表/章节/证据目标、复用清单与每条闭包扩展原因；确认/取消必须携带匹配的 plan_hash，人工调整后由代码重算闭包并固化新计划。返工决定（含人工新增问题）先写入 `review_decisions` 并切换审核轮的有效决定，再进入 `diagnose`；取消返工只取消方案，不隐式批准或删除原问题，回到本轮签核裁决。`rework/rNNNN/{diagnosis,plan}.json` 为只追加产物，若同轮内容不同则拒绝覆盖。
| `research_tasks` | report_id、task_id、category、description、plan_revision |
| `evidence` | report_id、id、task_id（首次采集任务）、plan_revision、source、source_ref（JSONB）、source_key、title、content（JSONB，不可变快照）、summary、content_hash、collected_at、published_at（可空）、content_kind、parent_evidence_id（可空） |
| `evidence_tasks` | report_id、plan_revision、task_id、evidence_id；联合主键防重复，引用授权以本表为准 |
| `source_calls` | report_id、request_key、provider、tool_name、safe_arguments（JSONB）、schema_hash、status（running / success / empty / failed）、evidence_ids（JSONB）、warnings（JSONB）、attempts（JSONB）、started_at、completed_at |

- 证据 ID 由 PostgreSQL sequence 分配并格式化为 `E-000012`，不用 max(id)+1；跨报告读取必须同时带 `report_id`。
- 正文去重唯一键为 `(report_id, source, source_key, content_hash)`；正文更新或网页提取产生新 ID，旧 ID 内容不变。
- 请求与证据在同一业务事务提交；`source_calls` 在证据提交后才标为 success。失败不永久占用请求键；崩溃窗口保留 running 记录，由研究员显式重试。
- LangGraph checkpoint 表由 `AsyncPostgresSaver.setup()` 创建，不纳入 Alembic 管理。
- 分析结果和章节存在 State（随 checkpoint 持久化），冻结时随版本文件落盘，不单独建表。
- `report_versions` 的 `qc_issues` / `compliance_issues` 展示最近已完成轮次，`signoff` 保存最终批准的审核身份 / 决定 hash / 时间；历史以 `review_runs` / `review_decisions` 为准，正在执行的空结果不覆盖已完成记录。
- 审核、决定、返工关联必须绑定同一报告 / 版本 / 轮次，使用复合外键或事务内严格核验。取消返工后重新提交裁决生成新 `decision_id`，以比较并交换方式更新 `active_decision_id`，旧决定不删除；执行返工或批准时必须仍指向当前 `active_decision_id` 与当前版本，过期方案不能恢复执行。
- 签核记录可记录本地操作者标签，但不宣称具备认证或法律电子签名。

### 7.2 版本与文件

每个研报任务对应一个会话，`report_id` 同时标识任务、会话与 checkpoint 线程。`RC_DATA_DIR/reports/<report_id>/` 是该会话的工作区，默认目录如下；无需新增独立归档目录或在审核通过后搬移文件。

```
data/reports/<report_id>/
├── analysis/r0001/{metrics,tasks,skills}/...   # M3 分析凭据与快照；M5 新轮或显式复用引用
├── charts/C-*.png / .json                     # M3 不可变图表
├── writing/r0001/
│   ├── template.json                          # 模板快照
│   ├── plan.json                              # 输入冻结 + input_hash
│   ├── outline.json                           # WR 任务 → 章节映射
│   ├── sections/S-01.json ...                 # 逐章 receipt（不可覆盖）
│   ├── draft.md / draft.json                  # 草稿与 manifest
│   └── freeze.json                            # 冻结意图（版本号/UTC 时间/manifest）
├── review/r0001/
│   ├── plan.json                              # 审核快照与 review_input_hash
│   ├── numeric.json                           # 数字检查与未核实覆盖清单
│   ├── rules.json                             # 确定性规则命中
│   ├── qc.json                                # 质量控制分支成功 receipt
│   ├── compliance.json                        # 合规审查分支成功 receipt
│   └── decision-<id>.json                     # 决定导出；数据库事务为权威
├── rework/r0001/
│   ├── diagnosis.json                         # Router 定位 receipt
│   └── plan.json                              # 返工计划与 plan_hash
├── report_v1.md                               # 冻结正文
└── report_v1.manifest.json                    # 冻结快照
```

- 冻结 manifest 保存章节、引用、图表、模板/规则 hash 与变更日志；数据库只存版本索引与 hash，不把整篇正文塞进 JSONB。旧 M4 版本按原 schema 读取；新 manifest 注明 schema 与来源映射（含混合 `origin_revision` 的复用条目），读者显式按版本校验，不靠放宽所有 revision 检查实现复用。
- `freeze.json` 是恢复凭据，不是成功标记：文件先发布、数据库后提交；同草稿重试按 `content_hash + writing_input_hash` 复用版本，目标文件内容不同则阻断，不覆盖、不自动改版本号。
- 版本 `created_at` 来自发布版本文件之前持久化的 `freeze.json`，重试复用同一 UTC 时间；不使用数据库 `now()` 或重试时钟反向生成 manifest。
- 审核与返工目录按 `review_revision` / `rework_revision` 单调递增，规范 JSON、路径白名单、原子发布与不覆盖原则沿用 artifacts 模块。准备 / 成功 receipt 先文件、再幂等写数据库；数据库与文件没有跨资源事务。分支成功 receipt 保存问题正文、数字未核实项解释、模型用量与工具调用摘要；State 只保存摘要，恢复时可按 receipt 重建问题清单。审核尝试日志独立保存于 `review/rNNNN/attempts/`（只追加、不含请求正文），不写入并行中的公共 State。
- 审核快照 `review/rNNNN/plan.json` 保存版本、正文与 manifest hash、`writing_revision`、已确认 `plan_revision`、冻结正文与章节区间、模板快照与 hash、规则完整快照与 hash、证据 ID / content_hash 清单、分析与指标凭据、图表 hash、章节完整依赖与前文链、提示词 / 数字检查 / 规则检查 / 输出契约版本、模型配置与固定 UTC 开始时间。`review_input_hash` 只对上述语义输入计算，不含当前时间与机器绝对路径；模型配置变化不改变已开始轮次的输入身份。`review_runs.snapshot_hash` 记录快照**文件字节**的 sha256，与语义 `input_hash` 分开核验，不能混用。
- 审核旧 M4 版本时从已验证 manifest 定位 `writing/rNNNN` 与分析产物，不取 State 中可能已返工的最新文本或指标；证据工具只开放快照白名单并核对 `content_hash`，找不到旧内容时明确失败，不拿同名最新数据冒充。
- 签核以数据库单事务为权威；决定导出失败显示“签核已保存，导出待修复”，不回滚已批准为未批准，不改冻结文件。

- 冻结版本不可变。返工产生新版本（每轮新建 `writing/rNNNN`）；冻结事务提交新版本及变更日志后，才将旧版本标记为 superseded，文件已发布但事务未成功时旧版本仍有效；签核通过时由 `signoff` 保存签核记录，将当前版本及任务标记为 approved，然后直接结束。返工冻结显式区分 `base_version` 与目标发布状态；旧报告版本不等于“当前草稿已冻结”，版本号仍由数据库 `current_version + 1` 分配，不清零当前版本。
- 已审核报告继续保存在会话工作区原有的 `report_vN.md` 路径中，图表及历史版本一并保留；签核不改写冻结正文、不额外生成归档副本。CLI 退出或新建后续任务不删除该工作区。
- 版本对比只提供变更日志，不做 diff。

---

## 8. 输出规范

### 8.1 Markdown 研报

- 结构遵循 `report_skill` 模板：章节顺序与标题只来自模板快照，Writer 不写 H1/H2、脚注定义或文末附录。
- 引用用脚注：正文 `[^E-000012]`，文末由确定性渲染器按首次出现顺序生成来源列表（标题、来源、URL 或工具调用、采集时间），只列正文实际引用的证据。
- 图表用相对路径 `![说明](charts/C-<16位hex>.png)` 引用；冻结时复验 PNG 与 manifest hash。
- 文末依次为「数据与假设说明」（仅在存在缺口/限制/假设时）、「参考来源」、「风险提示与免责声明」（文本来自本轮规则快照，Writer 不能改写）。
- 正文不写版本号、签核状态或审核结论；这些属于冻结 manifest 与数据库。

### 8.2 导出

Markdown 是唯一源文件。docx / PDF 导出用 pandoc（外部依赖），放在 M6，属于次要功能。


### 8.3 合规规则

`config/compliance_rules.yaml` 示例结构：

```yaml
version: 1
sensitive_words: []           # 敏感词表（M5 审核，按字面匹配）
required_disclosures:         # 强制披露项（M5 审核，仅以下两个键）
  - risk_warning              # 风险提示
  - disclaimer                # 免责声明
prohibited_patterns:          # 禁用表述（M5 审核，预编译正则）
  - "保证.{0,4}收益"
  - "稳赚|必涨|零风险"
risk_warning_template: "..."  # M4 起追加到文末附录
disclaimer_template: "..."    # M4 起追加到文末附录
```

规则由研究员提供，仓库内 `config/compliance_rules.yaml` 是**示例**，未覆盖正式合规要求。
M4 消费 `risk_warning_template` 与 `disclaimer_template` 渲染文末附录；M5 起三类规则均用于
审核。写作或审核前缺失、格式错误或 hash 不符会明确失败，不使用代码内置文本兜底。

- 确定性扫描针对冻结全文（含附录与表格，不只 Writer 正文）：`sensitive_words` 按字面匹配；
  `prohibited_patterns` 使用预编译正则，记录 `rule_id`、命中片段与区间；`required_disclosures`
  当前只接受已定义的 `risk_warning` / `disclaimer` 两个键，检查指定附录及冻结规则模板实际出现，
  未知键在审核前报配置错误，不能按字符串碰巧出现当作满足。
- 规则快照与 hash 随写作 `plan.json` 与审核 `review/rNNNN/plan.json` 冻结；审核轮开始后源配置
  变化不影响已冻结轮次。需要新规则时由研究员显式选择“更新规则并生成新版本”：正文未变可确定性
  复用，重新渲染附录、冻结 vN+1 再审核，不用新规则重解释旧版本的免责声明。
- 正则扫描在可终止的独立进程中执行并设总超时；超时属于执行失败，不是无命中。规则缺失、损坏或
  hash 不符在调用模型前阻断。

---

## 9. CLI、配置与测试

### 9.1 CLI

- `rc new`：新建任务，进入对话。
- `rc resume`：恢复未完成的任务，先展示待处理的中断。
- `rc status`：查看当前任务状态。
- 对话内斜杠命令：`/status`（阶段、采集/分析/写作进度、版本与数据缺口；M5 增加版本、审核轮、双分支进度、问题数、规则是否示例、返工目标）、`/plan`（TaskPlan）、`/evidence [id]`（证据列表或详情，只读本报告数据库）、`/analysis`（最近一次分析结果）、`/review [vN]`（只读查看审核轮、问题与规则命中）、`/rework`（只读查看返工目标与确认载荷）、`/report [vN]`（优先冻结版本，否则当前写作轮次草稿；读取时复验 hash）、`/quit`（保存退出，checkpoint 已自动保存；暂存裁决不等于提交签核）。
- `/review` 与 `/rework` 只读，不启动审核、不执行返工、不产生模型费用；`approved` 会话正常显示终态，不误认为还有待重试节点，approved 任务只读、不引入重新打开已签核任务的回路；`/report vN` 仍读取原冻结版本。
- 本期同时只有一个活动任务：`rc new` 时若有未完成任务，提示恢复或终止。数据模型按 `report_id` 隔离，后续 Web 界面可直接支持多会话。


### 9.2 配置

- 密钥与连接放 `.env`（`RC_` 前缀，pydantic-settings 加载，密钥用 `SecretStr`）：`RC_DEEPSEEK_API_KEY`、`RC_DEEPSEEK_BASE_URL`、`RC_DATABASE_URL`、`RC_FINANCEKB_BASE_URL`、`RC_FINANCEKB_API_TOKEN`、`RC_FINANCEKB_CA_BUNDLE`、`RC_FINANCEKB_CALL_TIMEOUT_SECONDS`。
- 旧配置项 `RC_RESEARCH_MODEL*` 改为上述命名，模型选择移到 `config/models.toml`。

```toml
[nodes.plan]
model = "deepseek-v4-pro"
thinking = true
reasoning_effort = "high"

[nodes.write]
model = "deepseek-flash"
thinking = true
```

### 9.3 测试

- 单元测试：财务计算、图表、证据去重、FinanceKB 两类返回归一化与降级、skill 解析、路由函数、血缘闭包、Pydantic 契约。
- Agent 测试：固定响应的假模型客户端，覆盖工具调用循环、`reasoning_content` 回传、JSON 校验失败重试。
- 集成测试：假 MCP server + 假搜索 provider + 测试库，跑完整图，覆盖每条回路和中断恢复。
- 默认测试全部替身化，不访问网络；真实联调测试单独标记（`pytest -m live`），手动运行。

---

## 10. 实施计划

### 10.1 里程碑

| 里程碑 | 内容 | 完成标准 |
|--------|------|----------|
| M0 工程整理 | 清理 FastAPI 与前端依赖；扩展配置；LLM 客户端支持工具调用与思考模式；实测思考模式与 JSON Output；compose 只保留 postgres | 单元测试通过；DeepSeek 真实调用验证完成 |
| M1 规划回路 | State、图骨架、checkpoint、Planner、`search_skills`、研究计划确认（澄清 + 确认）、CLI 对话与斜杠命令 | 从需求到确认 TaskPlan 跑通，中途退出后可恢复 |
| M2 采集 | MCP 客户端与 CNEquity 联调、web_search、证据库、Researcher、采集输出引用校验与恢复 | 真实采集写入证据库，来源可溯 |
| M3 分析建模 | 财务计算与图表工具、方法论 skill、Analyst、分析结果确认的两条回路 | 产出带图表和关键假设的分析结果 |
| M4 写作与冻结 | 四个研报模板、Writer、引用与来源列表、冻结、版本表与变更日志 | 生成可追溯到证据的 report_v1.md（工程、独立数据库及授权真实小样本验收已通过；不代表M5审核/签核通过） |
| M5 审核闭环 | Reviewer、Compliance 并行、签核、Router、确定性影响闭包与增量返工；签核通过后直接结束并保留会话工作区报告；详细设计见 [m5-design.md](plans/m5-design.md)，按 M5-A→G 分批实施 | 设计已确认，实施中；目标为三类问题各自回退并生成 v2、签核通过直接到 END、工作区报告及签核记录可读取、图中不存在独立 archive 节点；尚未验收 |
| M6 验收与导出 | 端到端验收场景、pandoc 导出 | 验收场景全部通过 |

验收场景默认沿用旧设计：个股财务分析、行业三家公司对比，具体标的按 CNEquity demo 数据覆盖确定。

### 10.2 风险

| 风险 | 应对 |
|------|------|
| Windows 上 psycopg 异步需要 SelectorEventLoop，而 stdio 子进程需要 ProactorEventLoop | M0 验证；冲突时 MCP 客户端放到独立线程的事件循环中运行 |
| 思考模式与 JSON Output 组合不稳定 | M0 实测，不稳定则结构化输出调用关闭思考模式 |
| CNEquity demo 配置的数据覆盖有限 | 验收标的按实际覆盖选择 |
| CNEquity 工具清单运行时才能确定，可能含 SQL 查询类工具 | M2 记录清单；确认只读，必要时用工具白名单 |
| 模型自行写入数字导致幻觉 | 数字只来自计算工具和证据，质量控制做代码比对 |
| 网页内容中的提示注入 | 工具结果一律作为数据处理；本期无代码执行和写文件类工具 |

---

## 11. 待确认事项

1. ~~DeepSeek 搜索 API 是否存在独立接口~~：已按官方 Search API 实现适配器；真实账户权限与余额仍需在授权联调时验证。
2. 人工介入从 `interrupt_before` 改为节点内动态 `interrupt()`（Q29 原选 `interrupt_before`；LangGraph 官方把静态断点定位为调试用途）。
3. 签核时研究员可以补充 AI 未发现的问题（`source = human`）。
4. TaskPlan 不含 `assumptions` 字段：需求模糊时先澄清，关键假设改由分析结果承载（Q62 未作答）。
5. 合规规则（敏感词表、强制披露项）的具体内容由研究员提供。
6. 节点到模型的默认映射（3.2）。
7. 工作区中保留的 `frontend/`、`deploy/` 与 compose 的 backend / frontend 服务：本期不使用，M0 是否删除。
8. M6 验收场景与具体标的。
9. 真实 CNEquity 只读工具清单与数据覆盖：需在授权探测后写入 `config/cnequity.toml`。

