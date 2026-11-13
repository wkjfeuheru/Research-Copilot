# Research Copilot 对话式工作台

连接 `rc serve`：左侧历史对话，主区域为研究消息、真实进度、报告与人工确认；来源、完整报告和预算在详情抽屉中展示。

## 启动

首次运行先按[根目录 README](../README.md#首次配置)配置 `.env`、数据库、沙盒及完整的 `config/models.local.toml`。模型文件须包含五个角色和独立摘要配置。

要求 Node.js 24、pnpm 11。项目根目录启动后端：

```powershell
uv run --frozen rc serve --models config/models.local.toml --api-only
```

开发前端在 `frontend/` 运行 `pnpm dev`，访问 http://127.0.0.1:5174/，`/api` 代理到 8000。

单进程使用构建产物：

```powershell
cd frontend
pnpm build
cd ..
uv run --frozen rc serve --models config/models.local.toml
```

访问 http://127.0.0.1:8000/。操作者仍缺省 `local`，通过 `RC_ACTOR_ID` 或 `--actor-id` 指定。

`pnpm build` 现在需要已同步的项目 Python 环境：构建完成后自动登记后端代码、API、运行依赖与静态文件的配对清单。启动会拒绝缺失或不匹配的清单；可在项目根目录运行 `uv run --frozen rc verify-web frontend/dist` 离线核验。后端或前端更新后重建并重启，运行进程不会热加载替换后的静态文件。`--api-only` 保留 Vite 开发流程，不代表已验证发布配对。

## 交互与传输

- `/` 输入研究问题；示例卡片仅填充输入框，不自动执行。
- `/reports/:id` 恢复持久消息和研究结果。规划阶段可以补充信息；一旦形成研究计划，先处理关口，新的研究范围会新建对话，页面会明确提示。
- `POST /api/reports/:id/messages` 接受带唯一标识的补充消息；同一标识同内容幂等，不清空预算，不批准计划或绕过关口。
- `GET /api/reports/:id/events` 使用 SSE 发送状态和已提交报告的 `report.start` / `report.delta` / `report.complete`；浏览器按 Unicode 码点偏移重组并校验 SHA-256。状态流不执行模型调用。
- 支持 Last-Event-ID 续传、版本切换、失效清空和断线后每 3 秒只读轮询。正文采用真实网络分片，无定时打字机动画；写作提交前只展示实际研究进度。
- 计划、分析、返工、审核、签署均走原决定接口，版本和快照由后端校验。签署条件不满足时不能批准。
- Enter 发送，Shift+Enter 换行；抽屉支持 Escape；阅读历史时不强制滚动到底部。
- 运行中可以停止；删除需确认且不能删除运行中的会话。删除不清理 checkpoint 的既有边界保持不变。

## 验证

```powershell
pnpm typecheck
pnpm build
node --test tests/chat-progress.test.mjs tests/chat-stream.test.mjs tests/observability.test.mjs
```

离线浏览器验证先在项目根目录运行：

```powershell
uv run --frozen python -m tests.fakes.chat_server
npx --yes --package @playwright/cli playwright-cli -s=research-chat open http://127.0.0.1:5187/
npx --yes --package @playwright/cli playwright-cli -s=research-chat run-code --filename frontend/tests/chat-browser-check.mjs
npx --yes --package @playwright/cli playwright-cli -s=research-chat run-code --filename frontend/tests/chat-responsive-check.mjs
```

夹具使用临时 SQLite 和脚本化研究产物，经过真实 HTTP/SSE、检查点和人工确认服务，不调用付费模型，不使用用户业务数据库。浏览器脚本仅允许 5187 端口。完整记录见 [实施说明](../doc/plans/research-copilot-chat-workbench-implementation.md)。
