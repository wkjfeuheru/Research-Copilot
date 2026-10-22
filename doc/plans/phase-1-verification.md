# 第一阶段实施与验证记录

日期：2026-10-22  
分支：`codex/bootstrap`  
范围：可启动的项目骨架。通过本记录不代表原项目复现或研究业务验收完成。

## 已实施

- Python 3.13 + FastAPI 应用工厂、`RC_` 配置、显式健康响应模型与测试。
- React + TypeScript + Vite 前端；B「简报阅读台」方向的对话式研究工作区。
- 根路由研究输入、占位技能选择、本地附件选择；记忆与技能空状态页面；无资料库入口。
- 发送和附件选择明确提示未接入；不创建任务、不生成回复或简报。
- 前端开发代理 `/api`；Docker Compose 前端、后端与 PostgreSQL + pgvector；
  Nginx 生产同源代理与命名卷。
- `uv.lock`、`pnpm-lock.yaml`、`.env.example`、README 与部署文件。

## 验证结果

| 项目 | 命令或入口 | 结果 |
|---|---|---|
| 后端锁文件 | `uv lock --check` | 通过 |
| 后端测试 | `uv run pytest -q` | 9 passed；无模型、搜索、MCP 或数据库调用 |
| 前端锁文件 | `pnpm install --frozen-lockfile` | 通过，锁文件已是最新 |
| 前端类型检查 | `pnpm typecheck` | 通过 |
| 前端生产构建 | `pnpm build` | 通过；44 modules；JS 279.38 kB（gzip 89.01 kB） |
| Compose 配置 | `docker compose config --quiet` | 通过 |
| Compose 启动 | `docker compose up --build -d` | 退出码 0；三个服务均 healthy |
| 生产入口 | `http://127.0.0.1:8080/` | HTTP 200 |
| 生产健康接口 | `http://127.0.0.1:8080/api/health` | HTTP 200；`environment=production` |
| 前端直接路由 | `http://127.0.0.1:8080/memory` | HTTP 200，SPA 回退正常 |

生产健康响应的依赖状态为：数据库 `not_checked`（Compose 提供了配置，但应用不连接、
不验证），模型与研究链路 `not_configured`。响应中的 `status=ok` 只表示 API 进程可用。

## 浏览器验证

使用 Playwright CLI 打开实际 HTTP 入口：

- 根路由、`研究 / 记忆 / 技能` 导航、研究问题输入、技能与附件区域均可见。
- 发送后显示“消息未提交、未创建研究任务、不会生成简报”，页面不新增虚构任务或回复。
- 打开占位技能选择并选中后，输入区与右侧研究上下文同步显示本地选择状态。
- 通过文件选择器添加附件后，输入区与上下文显示文件名/大小，并明确显示仅本地展示、
  未上传、未解析。
- 记忆页与技能页显示空状态；三个页面均未出现“资料库”入口或内容。
- 390px 与 320px 视口无整页横向溢出。
- 控制台 0 errors、0 warnings；仅出现 React DevTools 提示信息。

截图保存在本机 `output/playwright/`（已被 `.gitignore` 忽略，不作为仓库交付物）。

## 边界与说明

- 容器启动验证使用固定镜像标签：`python:3.13.13-slim`、`node:24.18.0-alpine`、
  `nginx:1.29-alpine`、`pgvector/pgvector:0.8.1-pg17`。
- 验证前 Docker daemon 未运行，已启动 Docker Desktop；容器与命名卷保留在本机。
- 数据库未建立业务连接，未创建业务表或迁移；未调用任何模型、搜索、MCP 或文件解析。
- 本阶段不使用 mock、不会因缺少密钥自动降级，也未迁入 RAG 或 native fallback。
