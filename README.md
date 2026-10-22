# Research Copilot

面向 A 股个股与行业研究的投研工作区。当前处于**阶段二：Planner Agent 与单节点 LangGraph 集成**。

> 阶段二验证 Planner 契约、固定测试响应下的规划结构、业务校验和
> `START → Planner → END` 单节点图。DeepSeek 适配器已提供，但测试与健康检查不访问
> 网络；Data Fetcher、检索、搜索、MCP、持久化和研究 HTTP 接口均未接入。
> 本批不代表真实模型规划质量或研究业务已验收。

## 当前范围

已实现：

- 阶段一的工程骨架：FastAPI 应用工厂与 `GET /api/health`、`RC_` 配置、B 风格前端、
  Compose 部署配置与前后端锁文件。
- Planner 输入/输出 Pydantic 契约、JSON Schema 导出与生成脚本。
- Planner 提示词、最小模型客户端协议、严格业务校验和可追踪错误。
- 通过依赖注入模型客户端的 Planner Agent；固定测试响应下可生成
  个股、行业、公司对比三类计划。
- 顶层共享 `ResearchState` 与 `START → Planner → END` 单节点 LangGraph。
- DeepSeek OpenAI 兼容适配器、JSON Output 配置和手动运行脚本；测试使用
  `httpx.MockTransport`，不访问网络。
- 配置/健康接口、Planner 契约/提示词/校验、Planner Agent/图、DeepSeek 适配器的分层测试。

明确未实现：

- Data Fetcher、Analyst、Risk Checker、Writer、交付检查。
- 真实模型连通性和规划质量验收；适配器已提供，但测试和健康检查不发起真实调用。
  真实调用需要单独配置密钥并承担费用。
- `deepseek-chat` 已按官方文档于 2026-07-24 下线；默认使用 `deepseek-flash`，
  如你的兼容网关仍支持旧名，可通过 `RC_RESEARCH_MODEL` 覆盖。
- 网页搜索、CNEquity MCP、附件读取/PDF 解析、数据库连接或迁移。
- 研究任务 HTTP 接口、checkpoint、跨请求恢复、人工暂停。
- 真实技能目录、长期记忆、后台经验总结、正式简报与图表。
- 前端发送/上传仍未接通；阶段二未修改前端行为。
- 鉴权、公开部署保护、执行沙箱。当前只面向本机开发。

## 技术栈

| 区域 | 选型 |
|---|---|
| 后端 | Python 3.13、FastAPI、Pydantic v2、LangGraph（已用于 Planner 单节点图）、OpenAI SDK（DeepSeek 兼容）、uv |
| 前端 | React、TypeScript、Vite、React Router、pnpm |
| 持久化目标 | PostgreSQL + pgvector（本阶段只有部署配置，无业务表与迁移） |
| 部署 | Docker Compose、Nginx 同源反向代理 |

## 目录结构

```text
Research-Copilot/
├── src/research_copilot/
│   ├── __init__.py
│   ├── config.py            # 环境配置与 DeepSeek 模型参数
│   ├── state.py             # 共享 ResearchState（当前 request / plan）
│   ├── api/                 # 应用工厂、路由、响应模型
│   ├── agents/
│   │   └── planner/         # Planner 契约、提示词、校验与 Agent
│   ├── orchestration/       # START -> Planner -> END 单节点图
│   ├── llm/                 # 模型协议、错误类型与 DeepSeek 适配器
│   └── tools/               # 后续数据与文件工具（仅职责边界）
├── frontend/
│   ├── src/
│   │   ├── app/             # 路由、类型、占位技能定义
│   │   ├── pages/           # 研究、记忆、技能、404 页面
│   │   ├── components/      # 头部、输入区、上下文、空状态、服务状态
│   │   └── styles/          # 设计令牌与全局样式
│   ├── package.json
│   └── pnpm-lock.yaml
├── scripts/                 # JSON Schema 导出与 Planner 手动运行脚本
├── tests/                   # 配置、健康接口、Planner 与 DeepSeek 适配器测试
├── deploy/                  # 后端/前端镜像与 Nginx 配置
├── doc/                     # 规格、讨论、阶段计划与可丢弃原型（保留）
├── pyproject.toml
├── uv.lock
├── compose.yaml
├── .env.example
└── README.md
```

## 环境要求

- Python 3.13（项目现有 `.venv` 已保留并复用）
- [uv](https://docs.astral.sh/uv/) 0.11+
- Node.js 24、pnpm 11
- Docker Engine / Docker Compose（仅部署配置验证与容器启动需要）

## 本地启动

在项目根目录操作。

### 1. 配置

```powershell
Copy-Item .env.example .env
```

`.env` 已被忽略。本地开发使用其中默认值即可；`RC_DATABASE_URL`、
`RC_RESEARCH_MODEL` 与 `RC_RESEARCH_MODEL_API_KEY` 可以留空。阶段二测试使用
测试替身，不需要真实模型密钥。

### 2. 安装后端依赖

```powershell
uv sync
```

`uv.lock` 固定依赖版本。该命令会复用现有 `.venv`，不删除规格文档或原型。

### 3. 启动后端

```powershell
uv run uvicorn research_copilot.api.app:create_app --factory --host 127.0.0.1 --port 8000 --reload
```

- 健康接口：<http://127.0.0.1:8000/api/health>
- 开发期接口文档：<http://127.0.0.1:8000/api/docs>（`production` 环境不暴露）

健康响应示例：

```json
{
  "status": "ok",
  "service": "Research Copilot API",
  "version": "0.1.0",
  "environment": "development",
  "checked_at": "2026-10-22T12:00:00Z",
  "message": "API 进程可用；数据库、模型与研究链路尚未接入，本响应不表示研究能力就绪。",
  "checks": {
    "api": { "status": "ok", "detail": "HTTP 应用已启动，可响应健康检查。" },
    "database": { "status": "not_configured", "detail": "未配置数据库；本阶段不访问 PostgreSQL 或 pgvector。" },
    "model": { "status": "not_configured", "detail": "未配置研究模型密钥或模型名；本接口不发起模型调用。" },
    "workflow": { "status": "not_configured", "detail": "完整 LangGraph 研究链路尚未接入；当前只有离线测试覆盖的 Planner 单节点图，本接口不触发任何编排。" }
  }
}
```

`checked_at` 为实际生成时间；`database`/`model` 在提供配置时显示
`not_checked`，表示健康检查没有验证其可用性；真实模型调用需要显式执行。

### 4. 启动前端

另开终端：

```powershell
Set-Location frontend
pnpm install
pnpm dev
```

打开 <http://127.0.0.1:5173>。Vite 将 `/api` 代理到 `127.0.0.1:8000`。

### 5. 可选：真实 DeepSeek 调用

真实调用会产生 API 费用，且不属于自动测试。先在 `.env` 中配置（不要把密钥提交）：

```dotenv
RC_RESEARCH_MODEL=deepseek-flash
RC_RESEARCH_MODEL_BASE_URL=https://api.deepseek.com
RC_RESEARCH_MODEL_API_KEY=你的密钥
```

然后手动运行一次 Planner：

```powershell
uv run python scripts/run_planner.py `
  --question "分析贵州茅台最近三个年度的利润质量与现金流匹配情况" `
  --cutoff-date 2025-12-31 `
  --skill financial-analysis
```

模型配置和适配器已经接入，但本仓库尚未执行真实调用。`deepseek-chat` 已被官方文档
宣布于 2026-07-24 下线；当前默认使用 `deepseek-flash`，如果你的兼容网关仍支持旧名，
可以设置 `RC_RESEARCH_MODEL=deepseek-chat`，但需要单独核验。真实模型的连接、费用、
输出质量和错误行为不能由离线测试替代。

## 验证命令

```powershell
# 后端全量测试：配置、健康接口、Planner 契约/提示词/校验、Agent 与单节点图
uv run pytest

# 前端类型检查与生产构建
Set-Location frontend
pnpm typecheck
pnpm build
```

阶段二的 Planner 测试使用固定测试替身；DeepSeek 适配器测试使用
`httpx.MockTransport`；两者都不访问网络。测试通过只代表契约、校验、适配器请求构造和
单节点控制逻辑正确，不代表真实模型连通性、规划质量或费用行为。阶段二未修改前端；
浏览器验收仍覆盖阶段一的根路由、导航、对话输入、技能与附件区域、记忆与技能空状态，
并确认页面不存在资料库入口。

## Docker Compose

配置文件：`compose.yaml`；构建文件：`deploy/backend.Dockerfile`、
`deploy/frontend.Dockerfile`；生产入口反向代理：`deploy/nginx.conf`。

```powershell
# 只校验配置合并结果，不代表容器已启动
docker compose config

# 构建并启动前端、后端、PostgreSQL
docker compose up --build -d
docker compose ps
```

生产入口：<http://127.0.0.1:8080>，`/api` 由 Nginx 同源代理到后端。

- PostgreSQL 使用 `pgvector/pgvector` 镜像与命名卷，不发布数据库端口，不接触已有数据库。
- 后端启动时不会连接数据库；`RC_DATABASE_URL` 仅说明后续接入位置。
- 默认 `POSTGRES_PASSWORD` 只用于本机骨架；部署前必须在 `.env` 中替换为独立强密码。
- 停止：`docker compose down`。命名卷会保留；需要清空时使用 `docker compose down -v`（会删除本项目的数据库卷）。

配置校验结果与容器启动结果应分别记录，`docker compose config` 通过不等于容器可运行。

## 配置项

| 变量 | 默认值 | 说明 |
|---|---|---|
| `RC_ENVIRONMENT` | `development` | `development` / `test` / `production` |
| `RC_API_PREFIX` | `/api` | API 路径前缀 |
| `RC_DATABASE_URL` | 空 | 后续 PostgreSQL 连接地址；当前不连接 |
| `RC_RESEARCH_MODEL` | `deepseek-flash` | DeepSeek 模型名；旧名 `deepseek-chat` 已按官方文档下线 |
| `RC_RESEARCH_MODEL_BASE_URL` | `https://api.deepseek.com` | OpenAI 兼容接口地址 |
| `RC_RESEARCH_MODEL_API_KEY` | 空 | DeepSeek API 密钥；不要提交，缺失时不发起调用 |
| `RC_RESEARCH_MODEL_TIMEOUT_SECONDS` | `60` | 单次模型调用超时 |
| `RC_RESEARCH_MODEL_MAX_TOKENS` | `8192` | JSON 输出的最大 token 数 |
| `POSTGRES_DB` | `research_copilot` | Compose 数据库名 |
| `POSTGRES_USER` | `research_copilot` | Compose 数据库用户 |
| `POSTGRES_PASSWORD` | 本机占位值 | Compose 数据库密码，部署前必须修改 |
| `APP_PORT` | `8080` | Compose 前端入口宿主机端口 |

## 设计边界

- 无隐式 mock、无缺失密钥自动降级、无 native fallback。
- 健康接口只说明 API 进程可响应，不宣称数据库、模型或研究链路已就绪。
- 附件选择只记录文件名并在当前页面展示；不读取文件内容、不上传、不解析。
- 技能选择是占位交互；系统不会加载或执行技能。
- Planner 输出先通过 Pydantic Schema 和业务校验，才写入共享 `ResearchState`；
  不生成默认计划、不自动修复非法 JSON。
- 当前图只有 Planner 一个节点；不接入 checkpoint、条件路由或人工恢复。
- DeepSeek 适配器只在显式运行脚本时调用真实 API；健康检查和测试不发起外部请求，
  不自动重试，也不因缺少密钥切换 mock。
- `agents/` 中仅 Planner 已实现；其他 Agent 和数据工具仍是后续批次。
  测试替身只存在于 `tests/`，不进入生产默认路径。
- 已移除独立资料库入口；后续附件与证据仍留在研究任务内。

## 保留与仓库

- 保留 `doc/spec.md`、`doc/design-discussion.md`、`doc/plans/`、
  `doc/prototype/`、`CONTEXT.md` 与现有 `.venv`。
- 未修改任何项目外的 `settings.json`。
- 项目已初始化为独立 Git 仓库，实施分支为 `codex/bootstrap`，不操作父目录仓库。

## 后续开发顺序

1. 原项目基线：固定参考版本，运行示例与测试，记录真实结果及 mock/降级行为。
2. 研究主链路：Agent 契约、LangGraph 路由、任务接口与人工暂停恢复
   （Planner 已完成，DeepSeek 适配器已接入；下一步 Data Fetcher）。
3. 真实数据与交付：CNEquity MCP、并行网页搜索、附件处理、分析图表与引用校验。
4. 技能与持久化：按需加载、可信脚本沙箱、PostgreSQL checkpoint、研报知识记忆。
5. 任务经验总结：终态后的幂等后台总结、失败重试与记忆管理。

## 文档

- [需求规格](doc/spec.md)
- [设计讨论](doc/design-discussion.md)
- [阶段一计划](doc/plans/phase-1-plan.md)
- [第一阶段实施与验证记录](doc/plans/phase-1-verification.md)
- [阶段二计划](doc/plans/phase-2-plan.md)
- [第二阶段实施与验证记录](doc/plans/phase-2-verification.md)
- [DeepSeek 模型接入记录](doc/plans/deepseek-model-integration.md)
- [领域术语](CONTEXT.md)
- [可丢弃前端原型](doc/prototype/index.html)
