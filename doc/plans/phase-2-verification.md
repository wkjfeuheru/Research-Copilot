# 第二阶段实施与验证记录

日期：2026-10-22  
分支：`codex/bootstrap`  
范围：Planner 输入输出契约、提示词、模型调用边界、业务校验与 `START → Planner → END`
单节点 LangGraph。通过本记录不代表真实模型规划质量或完整研究链路已验收。

## 已实施

- Pydantic 输入/输出契约与两种 JSON Schema，使用 `extra="forbid"` 拒绝未知字段；
  输入只含附件元信息，不读取正文。
- 规划状态 `ready` / `needs_clarification` / `unsupported`，以及步骤依赖、数据需求、
  技能来源、四类能力和状态内容校验。
- Planner 系统提示词与用户提示词；用户输入被明确标记为数据，不作为指令执行。
- `StructuredModelClient` 协议、请求/响应类型和 `ModelCallError`。
- 依赖注入的 Planner Agent：严格 JSON 解析、Schema 校验、业务校验、错误包装；
  不生成默认计划、不静默转 mock、不自动修复非法 JSON。
- 顶层共享 `ResearchState(TypedDict)`，当前只包含 `request` / `plan`。
- `START → Planner → END` 单节点图；节点返回 `plan` 的部分状态更新，状态值为 JSON
  可序列化字典。
- JSON Schema 导出脚本与固定测试替身；测试替身只在 `tests/`，不进入生产路径。

## 主要文件

- 契约：[contracts.py](../../src/research_copilot/agents/planner/contracts.py)
- 提示词：[prompts.py](../../src/research_copilot/agents/planner/prompts.py)
- 业务校验：[validation.py](../../src/research_copilot/agents/planner/validation.py)
- Planner Agent：[agent.py](../../src/research_copilot/agents/planner/agent.py)
- 共享状态：[state.py](../../src/research_copilot/state.py)
- 图编排：[planner.py](../../src/research_copilot/orchestration/planner.py)
- Schema 导出：[export_planner_schemas.py](../../scripts/export_planner_schemas.py)
- 测试：[tests](../../tests)

## 验证结果

| 项目 | 命令或入口 | 结果 |
|---|---|---|
| 后端锁文件 | `uv lock --check` | 通过 |
| 后端全量测试 | `uv run pytest -q` | **71 passed** |
| Schema 重新导出 | `uv run python scripts/export_planner_schemas.py` | 通过，提交文件与契约一致 |
| 前端类型检查 | `pnpm typecheck` | 通过 |
| 前端生产构建 | `pnpm build` | 通过；44 modules；JS 279.38 kB（gzip 89.01 kB） |
| Compose 配置 | `docker compose config --quiet` | 通过 |
| 后端镜像重建 | `docker compose up --build -d backend` | 通过；backend healthy |
| 生产健康接口 | `http://127.0.0.1:8080/api/health` | HTTP 200；workflow 明确为“只有离线测试覆盖的 Planner 单节点图” |

后端测试覆盖：

- 个股、行业、公司对比三类固定响应均能形成合法 `ready` 计划；
- 含糊问题形成 `needs_clarification`；
- 超出 A 股范围形成 `unsupported`；
- 三类状态的内容互斥规则；
- 非法 JSON、缺失字段、未知字段；
- 重复 `step_id` / `requirement_id`；
- 未知步骤引用、自引用、循环依赖；
- 未知数据需求引用、未知技能；
- 缺失 objective / 分析维度 / 数据需求 / 步骤 / 四类能力；
- 未被步骤引用的数据需求；
- 模型调用异常包装与原始异常链；
- 模型调用失败不重试；
- 单节点图输出与直接调用 Planner 结果一致；
- 共享状态保存原始请求和 JSON 形式的计划；
- 原有配置与健康接口测试继续通过。

前端阶段二未修改；`pnpm typecheck` 与 `pnpm build` 作为回归验证执行。

## 验证边界

- 本批所有 Planner 结果都来自**测试目录中的固定模型响应**，只证明程序解析、校验、
  状态保存和控制逻辑，不代表真实模型能生成同样质量的计划。
- 未接入真实模型、Embedding、搜索、MCP、数据库、文件解析或长期记忆。
- 未新增研究 HTTP 接口，未修改前端发送/上传行为。
- 图只有 Planner 一个节点；没有 Data Fetcher、条件路由、checkpoint、跨请求恢复或
  人工暂停。`needs_clarification` 只作为规划结果返回。
- 未执行付费模型调用或真实数据获取。
- 本记录不构成完整研究链路或业务验收；真实模型规划质量需在供应商、模型和预算确定后
  单独验收。
