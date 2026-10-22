# Research Copilot

面向 A 股个股与行业研究的投研工作区。当前处于**阶段一：可启动的项目骨架**。

> 本批完成仅代表前后端工程、健康检查和部署配置可运行，不代表参考项目已复现，
> 也不代表研究业务已验收。数据库、模型、搜索、MCP、文件解析和 LangGraph 研究链路
> 均未接入；界面不会伪造任务、回复或报告。

## 当前范围

已实现：

- 可安装的 Python 3.13 后端包，FastAPI 应用工厂与 `GET /api/health` 响应模型。
- 配置读取（`RC_` 前缀环境变量或 `.env`），无模型密钥也能启动。
- 后端配置与健康接口测试，测试过程不调用模型、搜索、MCP 或数据库。
- React + TypeScript + Vite 前端骨架，B「简报阅读台」视觉方向。
- 根路由对话式研究工作区：研究问题输入、占位技能选择、本地附件选择。
- 研究记忆与技能空状态页面；导航中不存在资料库入口。
- 发送、上传未接入时的明确提示；附件仅本地展示，不生成虚构任务或报告。
- 前端 `/api` 开发代理；Docker Compose 生产同源反向代理。
- Compose 前端、后端、PostgreSQL + pgvector 服务配置与命名卷。
- 环境变量示例、启动与验证说明、前后端锁文件。

明确未实现：

- 研究 Agent、LangGraph 状态图、节点契约与编排。
- 研究模型、Embedding、网页搜索、CNEquity MCP 调用。
- 上传、PDF 文本提取、文件工具、数据库连接或迁移。
- 研究任务、简报、图表、长期记忆、后台经验总结、真实技能目录。
- 鉴权、公开部署保护、执行沙箱。当前只面向本机开发。

## 技术栈

| 区域 | 选型 |
|---|---|
| 后端 | Python 3.13、FastAPI、Pydantic v2、LangGraph（依赖已安装，尚未编排）、uv |
| 前端 | React、TypeScript、Vite、React Router、pnpm |
| 持久化目标 | PostgreSQL + pgvector（本阶段只有部署配置，无业务表与迁移） |
| 部署 | Docker Compose、Nginx 同源反向代理 |

## 目录结构

```text
Research-Copilot/
├── src/research_copilot/
│   ├── __init__.py
│   ├── config.py            # 环境配置，不要求外部密钥
│   ├── api/                 # 应用工厂、路由、响应模型
│   ├── agents/              # 后续各研究 Agent（仅职责边界）
│   ├── llm/                 # 后续模型客户端（仅职责边界）
│   └── tools/               # 后续数据与文件工具（仅职责边界）
├── frontend/
│   ├── src/
│   │   ├── app/             # 路由、类型、占位技能定义
│   │   ├── pages/           # 研究、记忆、技能、404 页面
│   │   ├── components/      # 头部、输入区、上下文、空状态、服务状态
│   │   └── styles/          # 设计令牌与全局样式
│   ├── package.json
│   └── pnpm-lock.yaml
├── tests/                   # 配置与健康接口测试
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

`.env` 已被忽略。阶段一使用其中默认值即可；`RC_DATABASE_URL`、
`RC_RESEARCH_MODEL` 与 `RC_RESEARCH_MODEL_API_KEY` 可以留空。

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
    "model": { "status": "not_configured", "detail": "未完整配置研究模型；本阶段不发起任何模型调用。" },
    "workflow": { "status": "not_configured", "detail": "LangGraph 研究链路尚未实现；本接口不触发任何编排。" }
  }
}
```

`checked_at` 为实际生成时间；`database`/`model` 在提供配置时显示
`not_checked`，表示本阶段没有验证其可用性。

### 4. 启动前端

另开终端：

```powershell
Set-Location frontend
pnpm install
pnpm dev
```

打开 <http://127.0.0.1:5173>。Vite 将 `/api` 代理到 `127.0.0.1:8000`。

## 验证命令

```powershell
# 后端配置与健康接口测试（无外部调用）
uv run pytest

# 前端类型检查与生产构建
Set-Location frontend
pnpm typecheck
pnpm build
```

本阶段浏览器验收范围：根路由、导航、对话输入、技能与附件区域、记忆与技能空状态，
并确认页面不存在资料库入口。发送与附件选择只产生明确的未接入提示，不创建任务或报告。

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
| `RC_DATABASE_URL` | 空 | 后续 PostgreSQL 连接地址；阶段一不连接 |
| `RC_RESEARCH_MODEL` | 空 | 后续研究模型名称；阶段一不调用 |
| `RC_RESEARCH_MODEL_API_KEY` | 空 | 后续模型密钥；不要提交 |
| `POSTGRES_DB` | `research_copilot` | Compose 数据库名 |
| `POSTGRES_USER` | `research_copilot` | Compose 数据库用户 |
| `POSTGRES_PASSWORD` | 本机占位值 | Compose 数据库密码，部署前必须修改 |
| `APP_PORT` | `8080` | Compose 前端入口宿主机端口 |

## 设计边界

- 无隐式 mock、无缺失密钥自动降级、无 native fallback。
- 健康接口只说明 API 进程可响应，不宣称数据库、模型或研究链路已就绪。
- 附件选择只记录文件名并在当前页面展示；不读取文件内容、不上传、不解析。
- 技能选择是占位交互；系统不会加载或执行技能。
- `agents/`、`llm/`、`tools/` 只建立包与职责说明，不放置空实现，等待编排阶段再添加
  `state.py`、`orchestrator.py` 与节点契约。
- 已移除独立资料库入口；后续附件与证据仍留在研究任务内。

## 保留与仓库

- 保留 `doc/spec.md`、`doc/design-discussion.md`、`doc/phase-1-plan.md`、
  `doc/prototype/`、`CONTEXT.md` 与现有 `.venv`。
- 未修改任何项目外的 `settings.json`。
- 项目已初始化为独立 Git 仓库，实施分支为 `codex/bootstrap`，不操作父目录仓库。

## 后续开发顺序

1. 原项目基线：固定参考版本，运行示例与测试，记录真实结果及 mock/降级行为。
2. 研究主链路：Agent 契约、LangGraph 路由、任务接口与人工暂停恢复。
3. 真实数据与交付：CNEquity MCP、并行网页搜索、附件处理、分析图表与引用校验。
4. 技能与持久化：按需加载、可信脚本沙箱、PostgreSQL checkpoint、研报知识记忆。
5. 任务经验总结：终态后的幂等后台总结、失败重试与记忆管理。

## 文档

- [需求规格](doc/spec.md)
- [设计讨论](doc/design-discussion.md)
- [阶段一计划](doc/phase-1-plan.md)
- [领域术语](CONTEXT.md)
- [可丢弃前端原型说明](doc/prototype/README.md)
