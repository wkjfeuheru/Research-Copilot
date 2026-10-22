# DeepSeek 真实模型接入记录

日期：2026-10-22  
分支：`codex/bootstrap`  
范围：DeepSeek OpenAI 兼容模型配置与适配器。配置与离线测试已实现；
尚未使用真实 API key 发起联网调用，真实连通性和规划质量待单独验收。

## 官方依据与模型名冲突

- OpenAI 兼容 base URL：`https://api.deepseek.com`  
  <https://api-docs.deepseek.com/>
- Chat Completions 接口与 JSON Output：设置 `response_format={"type":"json_object"}`，
  提示词必须要求 JSON，并设置合理的 `max_tokens`。  
  <https://api-docs.deepseek.com/guides/json_mode/>
  <https://api-docs.deepseek.com/api/create-chat-completion>
- 官方当前列出的模型名为 `deepseek-flash` 和 `deepseek-v4-pro`。
- 官方公告说明 `deepseek-chat` 与 `deepseek-reasoner` 已于 **2026-07-24 15:59 UTC**
  完全退役。  
  <https://api-docs.deepseek.com/news/news260424/>

因此代码默认使用 `deepseek-flash`，不把已退役的 `deepseek-chat` 设为默认值。
如果用户所在兼容网关仍支持 `deepseek-chat`，可通过 `RC_RESEARCH_MODEL=deepseek-chat`
显式覆盖；该情况本次未验证，不能宣称可用。

## 已实施

- 配置新增：
  - `RC_RESEARCH_MODEL`，默认 `deepseek-flash`；
  - `RC_RESEARCH_MODEL_BASE_URL`，默认 `https://api.deepseek.com`；
  - `RC_RESEARCH_MODEL_API_KEY`，使用 `SecretStr`，为空时状态为未配置；
  - `RC_RESEARCH_MODEL_TIMEOUT_SECONDS`，默认 60；
  - `RC_RESEARCH_MODEL_MAX_TOKENS`，默认 8192。
- `DeepSeekModelClient` 使用 OpenAI Python SDK 调用 OpenAI 兼容接口：
  - system/user 消息；
  - `response_format={"type":"json_object"}`；
  - 关闭 SDK 自动重试（`max_retries=0`）；
  - 供应商异常转换为 `ModelCallError`，保留原始异常链；
  - 只返回原始文本；JSON 解析、Schema 和业务校验仍由 Planner 负责。
- `scripts/run_planner.py` 提供手动真实调用入口；缺少 key 时明确退出，不发起请求，
  不转 mock。
- 测试使用 `httpx.MockTransport` 与 OpenAI SDK 的真实请求构造逻辑，不访问网络；
  Planner 与 DeepSeek 适配器之间有端到端固定响应测试。

## 验证结果

| 项目 | 命令 | 结果 |
|---|---|---|
| 后端锁文件 | `uv lock --check` | 通过 |
| 后端全量测试 | `uv run pytest -q` | **79 passed** |
| 真实调用帮助入口 | `uv run python scripts/run_planner.py --help` | 通过，未发起请求 |
| 无密钥保护 | 未配置 `RC_RESEARCH_MODEL_API_KEY` 运行脚本 | 退出码 2，明确提示配置，不访问网络 |
| Compose 配置 | `docker compose config --quiet` | 通过 |
| 后端镜像重建 | `docker compose up --build -d backend` | 通过；backend healthy |
| 容器内适配器导入 | `python -c "from research_copilot.llm.deepseek import DeepSeekModelClient"` | 通过 |
| 前端回归 | `pnpm typecheck` / `pnpm build` | 通过（本次未修改前端） |

离线测试覆盖：

- OpenAI 兼容请求的 URL、Bearer token、system/user 消息、模型、`response_format`、
  `temperature` 和 `max_tokens`；
- 响应内容、模型、结束原因和 token 用量提取；
- 空 choices 内容交给 Planner 显式处理；
- HTTP 错误转换为 `ModelCallError` 并保留 `OpenAIError`；
- 缺少 key 时 `from_settings` 明确失败；
- `deepseek-chat` 可通过配置覆盖，但未验证真实可用性；
- DeepSeek 适配器 + Planner 的固定响应端到端测试。

## 运行真实调用的方式

```powershell
Copy-Item .env.example .env
# 编辑 .env，设置 RC_RESEARCH_MODEL_API_KEY；不要提交 .env
uv run python scripts/run_planner.py `
  --question "分析贵州茅台最近三个年度的利润质量与现金流匹配情况" `
  --cutoff-date 2025-12-31 `
  --skill financial-analysis
```

该命令会产生真实 API 请求和费用，不属于自动测试。

## 未验证项

- 实际 DeepSeek API key 的认证、配额、网络连通性和区域可用性；
- `deepseek-chat` 在用户特定网关/账号下是否仍可用；
- 真实模型对 Planner JSON Schema 的遵循程度、空内容概率和规划质量；
- 真实调用的 token 用量、费用和延迟；
- 真实调用失败后的具体错误码映射与重试策略（本适配器不自动重试）。

只有在用户提供密钥并明确授权后，才执行真实调用并补充上述结果。离线测试通过不能替代
真实模型验收。
