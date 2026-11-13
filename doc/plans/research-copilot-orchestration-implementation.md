# V2 主编排层实现与验证

日期：2026-10-29。

本文件记录主编排业务逻辑、运行底座及最初的角色占位交付。后续已完成[四个 SubAgent 的业务实现与分阶段验证](research-copilot-subagents-implementation.md)，V2 工厂默认启用真实业务角色；占位仅保留为显式兼容配置。现有 Web/CLI 默认运行入口仍为 V1，没有迁移 V1 checkpoint。下文原阶段的测试数字及占位说明保留为历史记录，以新增实施记录中的当前配置与验收边界为准。

## 已实现

| 入口 | 职责 |
|---|---|
| `research_copilot.orchestration.build_supervisor` | 报告专属 Deep Agents 图、三个声明式角色和一个 CompiledSubAgent；禁用通用代理、Shell 和递归派发 |
| `orchestration.factory.SupervisorFactory` | 将五个模型、共享持久化预算及四个业务角色组合为会话工厂；显式 legacy override 仍支持占位 |
| `subagents` | 三个自主业务角色及 Reviewer 审核图；同时保留历史占位构造器供兼容测试使用 |
| `runtime.business.BusinessCoordinator` | 提出计划、校验派发前置条件、接收结构化角色结果、校验版本绑定批准、生成幂等签署凭据 |
| `runtime.session.SessionService` | start/resume/cancel/get_status；用户和策略核对、运行锁、按 interrupt ID 恢复、显式重试失败 checkpoint |
| `runtime.storage.SqlSessionStore` | V2 独立控制表、事务、PostgreSQL 报告级 advisory lock、持久化批准和预算；SQLite 仅用于离线测试 |
| `runtime.storage.postgres_runtime` | 应用生命周期内管理数据库与 AsyncPostgresSaver；仅显式 initialize 时创建表 |
| `llm.config.open_models` | 从显式厂商/角色配置创建并关闭 SDK 客户端；关闭隐式重试，不加载默认供应商 |
| `llm.sdk.OpenAICompatibleChatModel` | Chat Completions 与 LangChain 消息/工具转换，保留 reasoning、tool-call ID、request ID、finish reason 和 usage |
| `llm.MeteredChatModel` | 顶层、角色和直接摘要请求的共享 token 预算；请求前持久化预占，usage 未知保留费用未知记录及预占 token 额度 |
| `workspace.bundled_resources` | 定位并校验包内规则与角色技能，不依赖外置源目录 |

顶层工具为 `propose_plan`、`get_report_status` 和四个确认工具。增加 `propose_plan` 是为了让新会话能先登记正式计划并获得可供确认的版本引用。

业务状态不会因待办完成、文件存在或 Supervisor 返回文字而变为 approved。未确认计划禁止派发；Writer 还要求当前分析已确认；签署要求当前报告版本已经审核通过。确认工具必须持有 SessionService 校验并持久化的人工决定，直接调用不能绕过批准。

`SessionStatus.idle` 只表示本轮图执行结束。报告是否批准以 `BusinessCoordinator.get_status()` 的批准版本和凭据为准。

## 接入方式

依赖锁定 `deepagents==0.7.32`、`langchain==1.4.7`、`langchain-core==1.7.0`。其他精确版本见 `uv.lock`。新增 `aiosqlite` 仅为开发依赖，用于真实 SQL/文件持久化离线验证。

当前省略 `role_tools` 和 `reviewer_factory` 时，工厂默认装配四个真实业务角色，具体依赖与配置见[SubAgent 实施记录](research-copilot-subagents-implementation.md)。要复现原占位阶段，可显式配置 `role_tools={}`；该兼容模式在角色模型调用前返回 `unavailable`，不创建分析、草稿或审核通过记录，主编排模型调用仍计入预算。

原阶段的显式覆盖接口继续保留：通过 `role_tools` 自定义角色工具、通过 `reviewer_factory` 替换审核图；此兼容模式中未出现的角色继续占位，显式配置某个角色（即使工具列表为空）启用其模型循环。业务默认模式使用 `role_services`，不能与这些覆盖接口混用。下面示例仍展示宿主的基础组合方式，参数均由服务端提供：

```python
from contextlib import asynccontextmanager
from research_copilot.llm.config import open_models
from research_copilot.orchestration.factory import SupervisorFactory
from research_copilot.runtime.business import BusinessCoordinator
from research_copilot.runtime.session import SessionService
from research_copilot.runtime.storage import postgres_runtime

@asynccontextmanager
async def v2_sessions(*, database_url, model_config,
                      workspace_root, resources_root, role_tools=None,
                      reviewer_factory=None, review_authorizer=None, on_event=None):
    async with open_models(model_config) as models:
        async with postgres_runtime(database_url) as (store, checkpointer):
            business = BusinessCoordinator(store, review_authorizer=review_authorizer)
            store.resume_validator = business.validate_resume
            factory = SupervisorFactory(
                models=models, role_tools=role_tools,
                reviewer_factory=reviewer_factory, store=store,
                business=business, checkpointer=checkpointer,
                workspace_root=workspace_root, resources_root=resources_root,
            )
            yield SessionService(
                store=store, agent_factory=factory,
                policy_version="v2-policy-1", on_event=on_event,
            )
```

初始化由应用显式选择 `postgres_runtime(..., initialize=True)`。新任务先由服务端调用 `store.create(SessionRecord(..., runtime_version="v2"), authorized_budget.snapshot())` 创建任务和原授权预算。`actor_id` 来源于认证层，不能由模型指定。恢复不会创建新预算或提高原额度。

装配时使用 `bundled_resources()` 定位包内资源。每个 Supervisor 绑定 `workspace_root / report_id`；不要缓存跨报告复用的固定后端。内存 checkpointer 只用于测试，不能作为进程重启恢复的证据。

模型配置结构由 `ModelsConfig` 定义：`providers` 按厂商标识保存 base_url、api_key_env、timeout_seconds；`roles` 必须包含 supervisor/searcher/analysist/writer/reviewer，分别声明 provider_id、model、context_tokens、max_output_tokens 和 tool_calling。密钥只从指定环境变量读取。每个角色可选择不同厂商，不能将不兼容的原生协议当成 Chat Completions 使用。

## 第 3 节：文件、权限、技能与上下文

本节记录设计文档 3.1–3.4 的基础设施交付；当时角色业务保持占位，后续已由独立 SubAgent 实施记录补齐。

| 设计要求 | 落地入口和行为 |
|---|---|
| 报告专属文件系统 | `build_backend` 初始化 supervisor、tasks、inputs、evidence、artifacts；CompositeBackend 的默认 StateBackend 保存框架卸载文件，三个磁盘路由分别访问当前报告、规则、技能 |
| 链接防护 | `ReportFilesystemBackend` 在路径解析前检查符号链接和 Windows junction；递归 glob/grep 检查搜索子树，阻止通过报告内部链接跨目录；目录本身和祖先也检查 |
| 资源只读 | instructions、skills 使用只读 Backend，write/edit/delete/upload 均拒绝；Supervisor 静态权限只允许写自身笔记；角色静态权限加可信 dispatch middleware 仅允许写独占任务目录 |
| 输入导入 | `WorkspaceFiles.import_input/read_input` 使用服务端 actor、报告所有权、V2 策略检查和同一报告运行锁；文件名拒绝穿越、驱动器、Windows 保留名；默认 10 MiB 上限；按内容哈希保存并校验，重复导入保持原件 |
| 独立技能包 | `resources/` 随 V2 Python 包分发，`bundled_resources()` 校验后定位；Analysist 发现 financial_modeling，Writer 只发现 reports，四模板仍是 references 普通文件 |
| 资源校验 | manifest 记录 resource_version、output_hashes；除哈希和固定入口外校验全部文件清单，拒绝未登记规则/技能和目录链接；不扫描 V1 自定义目录 |
| 分层上下文 | Supervisor 和三个自主角色显式加载少量 AGENTS 规则；角色职责留在 system_prompt，技能正文按需 read_file；RunContext 仅包含五个身份/策略引用；服务连接与预算账本留在宿主 |
| 摘要与卸载 | 使用框架原生 `create_summarization_middleware`，token_counter 与 MeteredChatModel 容量检查保持一致；模型感知阈值、历史卸载和最近上下文保留仍由框架执行，摘要请求走共享预算 |
| Reviewer 模板参考 | 公共 `role_read_roots("reviewer")` / `role_permissions("reviewer")` 和 `RoleFileScopeMiddleware("reviewer")` 允许只读 reports/references，不绑定 Writer 技能。后续 CompiledSubAgent builder 必须显式接入这些边界，父 middleware 不自动继承；当前占位图没有文件工具 |

宿主装配可直接使用已适配资源，避免运行时依赖 V1 技能目录：

```python
from research_copilot.workspace import bundled_resources
from research_copilot.workspace.inputs import WorkspaceFiles

resources_root = bundled_resources()
# 将 resources_root 传给已有 SupervisorFactory。
files = WorkspaceFiles(
    store=store, workspace_root=workspace_root,
    policy_version="v2-policy-1",
)
# actor_id 由认证层取得；report_id 必须是已创建的 V2 会话。
reference = await files.import_input(report_id, actor_id, "原始材料.txt", content_bytes)
original = await files.read_input(report_id, actor_id, reference)
```

导入只返回 `InputFile(report_id, path, content_hash, byte_size)`，不产生 evidence_id、批准或正式产物。输入内容发生变化时得到新的哈希路径；读回发现篡改则报错。Web 上传路由和证据登记服务仍由后续交互/角色阶段接入。

工作区、规则和技能文件属于宿主受控目录。链接检查防止预先存在的路径别名，并非操作系统沙箱；宿主进程具有同时替换目录或硬链接的权限时，须由部署权限隔离，不能以本地检查替代宿主安全边界。真实文件不会因 checkpoint 回退自动回滚。链接解析防护覆写的是锁定 Deep Agents 0.7.32 的 `_resolve_path` 接缝，升级时须重新验证路径、权限与框架卸载行为。

`tests/unit/test_workspace_policy.py` 验证实际 Windows junction、只读 Backend、资源清单、身份/哈希导入、真实原生文件工具、技能发现与正文隔离、大结果卸载后回读，以及触发原生摘要后的预算和 checkpoint 历史入口。模板选择中的业务判断仍通过角色执行规则约束；占位角色没有执行真实写作或审核。

## 角色接入契约

- 三个自主业务角色使用各自的 SearchResult / AnalysisResult / WritingResult，父侧显式映射到既有 `RoleResult`，保留主层批准语义；具体映射见 SubAgent 实施记录。显式 legacy override 仍可直接返回原 RoleResult。文件工具之外的工具需要各自鉴权。
- 显式占位通过 `PlaceholderRoleMiddleware` 结束声明式角色执行；Reviewer 占位图只含一个 unavailable 节点。两者不运行角色模型或业务工具。低层手动装配可通过 `RoleBinding(..., implemented=False)` 声明自主角色占位。
- task 入口将原描述包装成服务端生成的 JSON 派发消息，包含 description、report_id、dispatch_id、write_prefix。只能在 write_prefix 写入；文件 middleware 不信任模型自报目录。读取限于报告材料、角色技能、规则及框架卸载入口。
- 角色服务通过 `ControlRepository.mutate_control()` 在事务内登记权威 `ArtifactRef`；引用必须对应真实产物和其版本/哈希，不能只把模型输出登记为已验证证据。analysis、rework、report 为主层门禁使用的当前产物键；其他证据和指标使用独立键。写入这些键的角色业务服务负责输入快照和版本规则。
- `reviewer_factory(model=..., context=..., business=...)` 返回编译后的 messages 图；使用传入的计量模型，不在构造时发起评估。Reviewer 本身不能调用最终签署工具。
- Reviewer 末条 AIMessage 使用 `ReviewResult` JSON：status 为 passed / changes_requested / rejected，包含 reviewed 版本引用、summary、issues、comments。父侧核对冻结派发版本和当前版本后才记录结果。
- 尚未实现的 Reviewer 使用 `UnavailableReviewResult`：role 为 reviewer、status 为 unavailable、references 为空并给出 gaps；没有 reviewed 引用，也不会生成虚构的审核决定。
- Reviewer 原生中断的 kind 为 reviewer_deep_review；恢复值为 decision 和 comment。`review_authorizer(context, payload, decision)` 验证审核人、当前快照及意见，并由审核业务服务实现幂等记录。未注入时主层拒绝人工审核恢复。
- 0.85 路由、独立核验、人工反馈节点和审核记录写入属于 Reviewer 内部实现，本次没有实现这些节点。
- 四个顶层工具审批的恢复值为 `{"decisions": [{"type": "approve"}]}` 或 reject。SessionService 的 decision 参数是 `{interrupt_id: 对应恢复值}`；多个中断必须逐 ID 对齐，不能把 Reviewer 决定套进工具审批列表。
- `resume(..., decision=None)` 仅在存在未完成、无待人工决定的 checkpoint 节点时显式续跑；不自动重启完整报告。

## 验证及边界

新增测试位于 `tests/unit/v2`，覆盖真实 Deep Agents 装配、角色工具隔离、业务批准、并发取消、嵌套中断、会话重建、SQLite 持久化，以及通过 SDK MockTransport 验证的两组厂商协议夹具。占位注册增加 7 项测试，验证四角色返回不可用、零角色模型调用、产物和批准不变、逐角色替换以及配置错误拒绝。第 3 节增加 24 项工作区、资源与上下文验收。故意放宽批准前置条件、反转占位选择条件或反转只读 Backend 权限时，对应测试均会失败，随后已恢复正确源码并重跑相关回归。

最终执行记录：

```text
uv run --frozen pytest tests/unit/v2 tests/integration/test_writing_offline_chain.py -q
70 passed（69 项 V2 测试 + 1 项既有离线写作链）

uv run --frozen pytest tests/unit tests/integration/test_writing_offline_chain.py \
  --ignore=tests/unit/test_live_model_scenarios.py \
  --ignore=tests/unit/test_m5_live_budget.py -q
1458 passed

uv lock --check
python -m compileall -q src/research_copilot
uv build --wheel
全部通过；wheel 中校验到 16 个 V2 资源文件和 2 个技能入口
```

已有收集障碍：

- `tests/unit/test_live_model_scenarios.py` 缺少 `scripts.verify_live_models`。
- `tests/unit/test_m5_live_budget.py` 缺少 `scripts.verify_m5_live`。
- 三个 integration 文件依赖缺失的 `scripts.probes`。

全量 integration 组合在缺失导入被排除后仍未完成，本次已中止；不记为通过。单独的 `test_writing_offline_chain.py` 通过。没有修复或删除这些范围外文件。

本次未连接真实 PostgreSQL、真实模型、真实数据源或 Langfuse。PostgreSQL 锁、真正进程退出后的嵌套恢复和外部业务服务集成仍需单独验收。SQLite 的事务验证不代表 PostgreSQL 并发验收。

当前 SDK 适配使用完整响应；未实现原生 token 分片转发，LangChain 的 astream 仅退回完整结果，不伪造 token 流。预算使用保守的 UTF-8 字节估算和显式上下文容量，可能提前拒绝接近容量上限的请求。财务技能按设计保留 financial_modeling 标识；Deep Agents 会对下划线名称给出规范警告，但仍可发现并加载该技能。

## 官方依据

- [Deep Agents 定制与 middleware 替换](https://docs.langchain.com/oss/python/deepagents/customization)
- [LangChain 自定义 middleware 与结束跳转](https://docs.langchain.com/oss/python/langchain/middleware/custom)
- [Profiles：禁用通用代理](https://docs.langchain.com/oss/python/deepagents/profiles)
- [SubAgent / CompiledSubAgent](https://docs.langchain.com/oss/python/deepagents/subagents)
- [文件权限范围](https://docs.langchain.com/oss/python/deepagents/permissions)
- [后端路由](https://docs.langchain.com/oss/python/deepagents/backends)
- [技能发现与按需加载](https://docs.langchain.com/oss/python/deepagents/skills)
- [上下文卸载与摘要](https://docs.langchain.com/oss/python/deepagents/context-engineering)
- [工具人工审批](https://docs.langchain.com/oss/python/deepagents/human-in-the-loop)
- [LangGraph 中断与恢复](https://docs.langchain.com/oss/python/langgraph/interrupts)
- [LangGraph 持久化](https://docs.langchain.com/oss/python/langgraph/persistence)
- [OpenAI Python SDK](https://github.com/openai/openai-python)
