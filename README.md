# Research Copilot

本地运行的对话式投研助手。使用 Deep Agents 主编排、Searcher / Analysist / Writer / Reviewer 四个子角色及 LangGraph 检查点，由主 Agent 自主维护任务清单、动态派单和安排返工，报告最终由用户签署。

> **综合投研平台目标设计（2026-11-12）**：已确认以“研究项目 → 任务/实验 → 版本化产物”组织多场景研究，采用协调 agent + 五类业务 agent、用户隔离及独立后台执行。参见[设计入口与决策](doc/design/README.md)、[产品设计](doc/design/platform-product-v1.md)、[架构设计](doc/design/platform-architecture-v1.md)和[实施分期](doc/design/platform-roadmap-v1.md)。这些是待实施和验收的目标；下文继续说明当前版本的实际运行方式。

Web 分开展示动态任务清单与实际执行记录，保留显式澄清、Reviewer 人工深审和最终签核。新会话使用 `products-v2`，不再请求计划、分析或返工批准；旧会话只读，不迁移检查点。每份报告最多 12 次有效派单，报告预算仍为 210 万 tokens / 80 次模型调用，角色内部预算不提高。实施与证据见[自主规划改造验收记录](doc/plans/autonomous-workflow-acceptance-20261107.md)。

模型配置不可用时，可使用 `uv run --frozen rc serve --history-only --api-only` 启动只读 API，配合已有 Vite 页面浏览记录；配对构建可去掉 `--api-only`。该入口不需要 `--models`，使用 PostgreSQL 只读连接，验证数据库与工作区配对，不启动模型、执行队列或复盘任务。恢复正常研究需重启为通常的 `serve --models ...` 模式。

审查修复、质量门禁及恢复操作见[工程化实施记录](doc/plans/engineering-implementation-20261105.md)。当前版本启动只检查 schema 和宿主绑定；旧环境须先处理未完成的研究/复盘任务，停写、备份，再显式执行 `rc init-db` 与 `rc bind-host` 后启动，不能依赖 Web 启动自动升级或登记。

Web 与 CLI 已共用持久研究队列和全局运行上限。CLI 只领取本次提交的命令；`resume --operation-id ID` 可用同一标识和决定文件重新等待原命令。退出等待不会撤销已登记任务，撤销请用 `rc cancel`。普通宿主持有工作区共享锁，初始化、绑定、备份和恢复仍需排他锁。本机两个实际 Web 进程已验证共享检查点、全局三任务上限和正常退出后的排队任务接续；跨机器与网络文件系统部署尚未验收。

运行入口与维护入口现共用数据库访问锁：`init-db`、`bind-host`、联合备份和恢复需要排他访问，遇到仍连接的 Web/CLI 入口会直接拒绝；只读查询在维护期间也会被拒绝。当前业务 schema 为 7；schema 6 引入的持久宿主记录仍然保留，正常依赖收尾后才标记关闭；失联记录会阻断维护和新写入，不能因数据库锁释放而自动放行。当前宿主持续检查持锁连接，失权或探测超时后停止主研究/复盘领取并收尾，Web 拒绝新业务请求、CLI 退出等待；数据库恢复后也不会自行重新接单。远端已发送请求仍须核查，不保证瞬时撤销。当前继续保持单宿主部署。

沙盒工具在取得本地执行槽后复核宿主所有权，并在启动容器前登记持久容量占用。进程死亡、Docker 命令异常或执行中取消均不会自动释放未知容量；正常返回且确认容器/输出卷均已清理后才释放。剩余可用槽位仍可使用，全部占用则拒绝新执行。本机两个实际 Web 进程现已通过三报告并行沙盒与跨宿主接续验收：第三个执行等待空槽、原宿主退出后另一宿主完成审核签署、重启后仍可读取报告和图表。模型与来源为合成夹具，复盘提取在该夹具中关闭，不代表真实供应商、跨机器或生产容量验收。

遇到 `Unsettled runtime owners` 时先只读检查：

```powershell
uv run --frozen rc runtime-owners
uv run --frozen rc sandbox-claims
uv run --frozen rc status --report-id REPORT --actor-id ACTOR
# 确认全部旧写入者及其外部执行已停止后，按列表中的具体 ID 登记恢复依据：
uv run --frozen rc recover-runtime --owner-id OWNER_ID --actor-id OPERATOR --owner-stopped --note '已核查的停止与外部执行状态依据'
```

`runtime-owners` 最多列出 100 条未关闭记录，不调用模型；`status` 使用数据库强制只读连接。`recover-runtime` 必须在其他运行连接退出后执行，保留原宿主身份及恢复审计，并以相同核查依据原子恢复其尚占用的沙盒容量；它不会释放研究队列名额、消除未知用量、授权重试或自动启动任务。宿主恢复后仍需按各自契约处理请求账本和失联命令；不要直接修改或删除所有权表。

本文对应当前 `research_copilot` 入口。旧版配置、历史验收数据与原 README 已保留到 [历史说明](doc/history/README-before-startup-20261031.md)，其中的旧启动命令不适用于当前版本。

## 命名说明

快捷入口：[首次配置](#首次配置) · [日常启动](#日常启动速查) · [启动故障排查](#启动故障排查)

当前 Python 包名为 `research_copilot`；模型示例为 `config/models.example.toml`，预算策略为 `config/execution_policy.toml`，测试位于 `tests/unit/` 与 `tests/integration/`。已有环境更新后需同步依赖并重启旧后端进程。

历史表名、线程键、审批身份与冻结资源内容保持兼容，不按命名重构修改数据。详细映射及验证见[命名统一记录](doc/plans/research-copilot-naming-unification-20261101.md)。

## 环境要求

| 依赖 | 要求 / 用途 |
| --- | --- |
| Python | 3.13，项目限制 `>=3.13,<3.14` |
| uv | 安装锁定的 Python 依赖、运行 `rc` |
| Node.js / pnpm | Node.js 24、pnpm 11；前端声明 pnpm 11.22.0 |
| PostgreSQL | 会话、业务状态与 LangGraph 检查点；可使用现有实例或仓库 Compose |
| Docker | 使用 Compose 数据库及 Analysist 计算/图表沙盒时需要 Docker Engine |
| 模型凭证 | 配置文件指定的 OpenAI 兼容服务密钥；实际研究会产生供应商调用费用 |
| FinanceKB | 本机六域 MCP（market/fundamentals/macro/news/knowledge/futures）；Streamable HTTP + Bearer |

以下 PowerShell 命令均从项目根目录执行；本机路径为 `F:\python project\Research-Copilot`。前端命令会明确切换到 `frontend`。

## 日常启动速查

首次运行请先完成下方的依赖安装、`.env`、模型 TOML、数据库初始化、宿主绑定及沙盒镜像构建。绑定配置未变化的环境，每次只需启动数据库、后端和前端。

终端 1（保持运行）：

```powershell
Set-Location 'F:\python project\Research-Copilot'
# 使用本项目 Compose 数据库时执行；使用其他现有实例时跳过。
docker compose up -d postgres
docker compose ps postgres
# 数据库显示 healthy 后启动后端：
uv run --frozen rc serve --models config/models.local.toml --mode test --api-only
```

终端 2（保持运行）：

```powershell
Set-Location 'F:\python project\Research-Copilot\frontend'
pnpm dev
```

访问 **http://127.0.0.1:5174/**（当前 Vite 配置端口）；API 健康检查为 **http://127.0.0.1:8000/api/health**。使用配对构建时，去掉 `--api-only` 并按下方方式一运行，访问 **http://127.0.0.1:8000/**；构建产物不会随源码自动更新。

`config/models.local.toml` 是首次配置时创建的本地副本；若使用其他完整模型配置文件，将 `--models` 改成该文件的实际路径。`--mode test` 仅允许示例合规规则，不代表使用免费或模拟模型。

## 首次配置

### 1. 安装依赖

```powershell
uv sync --frozen --all-groups
uv run --frozen rc --help
Push-Location frontend
pnpm install --frozen-lockfile
Pop-Location
```

`uv run` 使用项目内 `.venv`。查看帮助和校验配置不会发起模型请求。

### 2. 配置本地环境变量

```powershell
# 已存在时保留原文件，避免覆盖凭证。
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

编辑根目录 `.env`，至少填写数据库连接和模型密钥：

```dotenv
RC_DATABASE_URL=postgresql://research_copilot:research_copilot_local_dev@127.0.0.1:5432/research_copilot
RC_DEEPSEEK_API_KEY=填写你的密钥
# 外部数据采集要求同时填写 FinanceKB 源站与令牌：
RC_FINANCEKB_BASE_URL=
RC_FINANCEKB_API_TOKEN=
```

当前 `HostSettings` 的读取顺序是：进程环境变量 > 根目录 `.env` > 代码默认值；`rc serve` 的命令行选项另行控制端口、工作区与规则文件。`.env` 不应提交到 Git。

| 配置 | 当前用途 |
| --- | --- |
| `RC_DATABASE_URL` | 必填；支持 `postgresql://` 或 `postgresql+psycopg://`。凭证、数据库和端口须与实际实例一致 |
| `RC_DEEPSEEK_API_KEY` | 默认模型示例的密钥；`serve` 会将 `.env` 中此值提供给声明该变量名的 provider |
| `RC_FINANCEKB_BASE_URL` | FinanceKB 源站 origin（仅 HTTPS）；与 `RC_FINANCEKB_API_TOKEN` 必须同时配置 |
| `RC_FINANCEKB_API_TOKEN` | Bearer 令牌；只放在请求头，不进入模型上下文、日志或证据 |
| `RC_FINANCEKB_CA_BUNDLE` | 可选；本机受信 CA 文件，仅用于 FinanceKB 客户端且保留主机名校验 |
| `RC_FINANCEKB_CALL_TIMEOUT_SECONDS` | 可选；单次调用期限（含队列等待），默认 150 秒 |
| `RC_ACTOR_ID` | Web 操作者，默认 `local`；命令行 `--actor-id` 优先。历史任务仅对同一操作者可见 |
| `POSTGRES_DB` / `POSTGRES_USER` / `POSTGRES_PASSWORD` | 仅由 Compose 配置数据库容器；已有实例应使用它自己的凭证 |
| `POSTGRES_HOST_PORT` | Compose 映射的本机端口，默认 5432；修改后同步修改 `RC_DATABASE_URL` |

模型服务地址以模型 TOML 的 `providers.*.base_url` 为准；`RC_DEEPSEEK_BASE_URL` 不会自动覆盖它。自定义 provider 的 `api_key_env` 需要导出到当前终端进程，例如 `$env:MY_PROVIDER_KEY = '你的密钥'`；任意自定义密钥名不会仅因写入 `.env` 就自动导出。

`.env.example` 仅保留当前配置项；旧 `RC_COLLECTION_*` / `RC_SKILLS_DIR` / `RC_CNEQUITY_*` / `RC_TAVILY_*` / `RC_SEARCH_*` 不参与运行。新研究的外部采集只使用 FinanceKB 六域 MCP；运行前可执行 `uv run --frozen rc check-sources` 只读检查连接、鉴权与工具合同。检查会区分结构变化与说明变化，并列出 JSON Pointer；仅公共输出 `domain.description` 的说明漂移可兼容，单位、日期、口径等业务说明仍需审阅。未知工具不会自动挂载。数据源接线以 [host.py](src/research_copilot/host.py) 为准。

`/api/health` 和 `/api/ready` 提供宿主启动时的来源发现状态及检查时间，不代表具体取数成功。全部域不可用时 readiness 为 503，新研究在入队及模型调用前被阻止；`/api/live` 仍表示进程存活。修复来源后重启研究服务重新发现工具，再点击首页“重新检查”。部分域可用时允许研究继续，缺少的数据仍须在报告中说明。

业务构成工具不保证单季口径，商品工具只提供最新期货报价；两者不能直接替代单季业务权重及历史采购成本。毛利率归因须先核实累计转单季数据；无法识别的锂价、储能贡献明确保留数据缺口，不强行拆出数值。

报告会冻结创建时的来源合同（批准目录、schema 指纹与归一化版本）；合同变更后旧报告只读，写入端拒绝续跑。请新建研究，并由操作者确认无正在执行或待结清的运行后，再用 `rc bind-host --replace` 登记新宿主指纹。

### 3. 配置完整的模型文件

不接受旧 `[nodes.*]` / `[flash_analysis]` 格式。请使用完整示例创建本地配置；已存在的本地配置不会被下列命令覆盖：

```powershell
if (-not (Test-Path config/models.local.toml)) {
    Copy-Item config/models.example.toml config/models.local.toml
}
```

编辑 [模型示例](config/models.example.toml) 对应的本地副本。它包含：

| 区域 | 作用 |
| --- | --- |
| `[providers.deepseek]` | `base_url`、密钥环境变量名 `api_key_env`、请求超时 |
| `[roles.supervisor]` | 主编排模型 |
| `[roles.searcher]` | 采集模型 |
| `[roles.analysist]` | 分析模型，名称必须按代码拼写为 `analysist` |
| `[roles.writer]` | 写作模型 |
| `[roles.reviewer]` | 审核模型 |
| `[summarizer]` | 独立摘要配置；真实 SDK 模型运行时必须显式提供 |

每个角色声明 `provider_id`、`model`、`context_tokens`、`max_output_tokens`、`tool_calling`；五个角色均要求支持工具调用，输出容量须小于上下文容量。示例沿用仓库模型名与容量，实际可用模型及容量应按服务账户调整，本次仅离线校验配置格式。

摘要配置必须放在**传给 `--models` 的同一 TOML 文件**内。修改模型配置前先处理未完成任务、停写；修改后使用 `rc bind-host --replace` 显式登记，再重启后端。

离线校验（不连接模型）：

```powershell
uv run --frozen python -c "from pathlib import Path; from research_copilot.llm.config import load_models_config; c = load_models_config(Path('config/models.local.toml')); assert c.summarizer is not None; print('model config OK')"
uv run --frozen rc preflight --mode test
```

### 4. 准备数据库与计算沙盒

使用仓库 Compose 数据库时，先核对 `.env` 中数据库变量和连接串。现有 PostgreSQL 已可用时，可跳过启动容器：

```powershell
docker compose up -d postgres
docker compose ps postgres
uv run --frozen rc init-db
uv run --frozen rc bind-host --models config/models.local.toml --workspace data --actor-id local --note '首次核验数据库、工作区与测试规则配置' --mode test
```

Compose 仅提供 PostgreSQL，默认绑定 `127.0.0.1:5432`；Web 和前端由本机命令启动。`init-db` 显式创建/升级当前所需表；`serve` 启动只核验，不执行迁移。`bind-host` 不连接模型，登记数据库、工作区路径和身份、模型/规则/执行策略及搜索范围的摘要；密钥值不进入摘要或记录。绑定和启动必须使用相同的模式、策略版本、模型与规则配置。

已有绑定的配置变更必须提供 `--replace --note '核验依据'`；同配置重复登记幂等。研究命令 pending/claimed/running/recovery_required 或复盘 pending/running 会阻止初次登记和改绑，须在升级或改配置前用原匹配宿主处理；登记不会自动取消任务或释放未知请求。数据库凭证轮换无需改绑，数据库地址须保持同一规范写法。不要删除 `.research-copilot-host.json` 规避检查；联合恢复到新库/路径后需要检查配对备份，再显式改绑。详见实施记录中的恢复契约。

宿主配置指纹格式现为 2，新增完整 `config/sandbox.toml` 快照（含镜像、资源上限、并发和排队等待）；宿主启动后向工厂传入这一不可变快照，不在每次构图时重新读取文件。旧格式绑定升级时须在原匹配版本处理未完成任务、停写并备份，再显式 `bind-host --replace`；不要为改绑自动清除待恢复任务。此配置机制不改变历史报告执行清单，运行中的宿主不会热加载修改后的沙盒配置。当前 schema 7 另新增持久沙盒容量账本，旧 schema 6 环境须用匹配旧版本停写备份，再显式执行 `rc init-db`；旧版本备份用匹配版本恢复后再迁移。仅此次 schema 升级不要求重建已有格式 2 的配置绑定。

联合备份会先核对数据库绑定、工作区规范路径及身份标记；不匹配时，在创建备份目录和调用 `pg_dump` 前拒绝。尚未绑定的数据库只允许搭配无标记工作区备份，仍须满足当前 schema 要求。该检查不代替应用版本、模型配置和密钥的配套保管。

显式维护另在目标库的 `rc_maintenance` 登记区持久化操作身份；业务表尚未创建或正在恢复时也有效。维护连接失联或进程被强杀后，新宿主及新维护都会被遗留记录阻断。先运行 `rc maintenance-owners` 查询；核实旧维护进程和 `pg_dump`/`pg_restore` 等外部工具均已停止后，使用 `rc recover-maintenance --owner-id ID --actor-id OPERATOR --owner-stopped --note '核查依据'` 审计释放。该命令不重试操作、不修复部分目标，也不解除独立的宿主、任务或预算阻断。正常异常收尾记录为 closed/failed，仅说明执行已退出，不代表备份或恢复成功。

该登记区由首次显式维护创建，操作角色须有创建 schema 的权限；普通服务启动只检查，不建表。已有同名但不兼容或含无关对象的 schema 会被拒绝。联合包仍只包含 public 业务数据，目标维护历史不随包导入或覆盖；“空恢复目标”允许只有本工具兼容的维护登记区。部分恢复失败后仍须保留现场，重新恢复到另一个空库与新目录。

经 `postgres_runtime` 管理的写入宿主现在让报告锁共用已登记的持锁连接；取得报告锁后再检查遗留失联宿主。一个报告的连接失联时，其他已在线宿主也不能重叠进入；必须先核查旧执行并完成宿主恢复。同宿主内阻止报告锁重入，关闭宿主会等待报告操作退出。文件导入/读取收到取消时先等待已开始的文件线程结束，再释放报告锁；不保证文件系统挂起时有界退出。

工作区根路径旁的 `.host.lock` 使用原生共享/排他锁，释放时保留文件，不能手动删除或替换。锁定依赖为 `portalocker[win32]==4.0.0`；Windows 共享锁依赖该 extra，不使用超时失效或软锁兜底。共享运行须使用同一规范路径、数据库绑定和配置；所有宿主停止后才能维护。当前验证范围为本机 Windows 文件系统，网络挂载必须另验原生锁语义；部署默认仍按单 Web 进程操作。

状态构图不创建报告目录，也不替换正在使用的审核授权器、沙盒引用和运行清单。状态投影从同一会话行读取身份、控制记录和预算，避免任务启动修改 run_id 时拼接旧身份产生 HTTP 500；队列与 checkpoint 仍独立观察，不承诺跨表/检查点的原子快照。删除报告现在与运行、导入共用报告锁，并持锁等待文件清理线程结束；清理拒绝根目录、报告目录及其后代的链接。数据库删除与文件清理仍非原子操作，清理失败会通过 `cleanup.workspace=false` 和 `cleanup.errors` 返回，保留现场而不宣称文件已删除。

分析计算和图表需要预先构建沙盒镜像（首次或镜像内容变更时执行）：

```powershell
docker build -f docker/analysist-sandbox/Dockerfile -t research-copilot-analysist:1 .
docker image inspect research-copilot-analysist:1 --format '{{.Id}}'
```

镜像名须与 [config/sandbox.toml](config/sandbox.toml) 一致。只有 Analysist 具有受限执行权限，沙盒不可用时不回退到宿主机执行。

## 启动方式

### 方式一：构建前端后，用一个服务提供页面和 API

```powershell
Push-Location frontend
pnpm build
Pop-Location
uv run --frozen rc serve --models config/models.local.toml --mode test
```

打开 **http://127.0.0.1:8000/**。后端只接受带有效配对清单的 `frontend/dist`。`pnpm build` 会构建静态文件并调用当前 Python 环境生成清单；旧版仅含 index.html 的目录会被拒绝。后端源码、依赖环境或前端变更后，重新构建、核验并重启服务。运行中的进程使用启动时已校验的静态字节快照，替换磁盘目录不会热更新页面。模型配置变更仍须先处理未完成任务并显式改绑。

可在不连接数据库、不构造模型的情况下核验：

```powershell
uv run --frozen rc verify-web frontend/dist
```

`GET /api/release` 返回发布 ID、API 版本及配对校验状态；不公开依赖清单或密钥。API-only 模式返回 `static_verified=false`。配对清单记录后端及资源字节、OpenAPI 摘要、Python 精确版本/平台、启用 extras 后的生产依赖版本、前端版本和所有静态文件 hash。它校验一致性，不提供签名或来源认证；构建和部署目录须可信。目标运行环境应与清单一致，不把 Windows 构建直接用于 Linux。`seal-web` 是构建后的登记步骤，不应对旧静态文件手工重新登记来绕过不匹配。

`--mode test` 允许仓库示例合规规则，**不会切换为假模型**；发送研究问题、补充消息或推进任务仍可能调用真实模型。

### 方式二：前后端分别运行，开发时自动更新前端

终端 1，项目根目录：

```powershell
uv run --frozen rc serve --models config/models.local.toml --mode test --api-only
```

终端 2，项目根目录：

```powershell
Set-Location frontend
pnpm dev
```

打开 **http://127.0.0.1:5174/**。Vite 将 `/api`（含 SSE）代理到 `127.0.0.1:8000`，需同时运行后端。`--api-only` 显式忽略已有静态目录，与 `--static-dir` 不能同时使用。单独 `pnpm preview` 不提供业务 API，不作为完整工作台启动方式。

### 成对发布产物

CI 的 frontend 作业会构建并验证后端 wheel、带 hash 的生产依赖清单和配对静态目录，随后从独立环境安装 wheel 再核验；远端 CI 是否通过以实际运行结果为准。本机第二十八批产物在 `output/web-release-package-batch28.zip`，目标为 Windows AMD64 / CPython 3.13.13；旧包仍保留，不能与后续源码混配。解包后进入包目录，可用独立环境验证：

```powershell
uv venv .venv --python 3.13.13
uv pip install --python .venv/Scripts/python.exe --require-hashes -r requirements.txt
uv pip install --python .venv/Scripts/python.exe --no-deps backend/research_copilot-0.1.0-py3-none-any.whl
.venv/Scripts/rc.exe verify-web frontend
```

该产物不包含密钥、本地模型/规则配置、业务数据库、工作区或 Docker 镜像。运行服务前仍须按首次配置与迁移/绑定契约准备这些依赖；不能把核验静态包当成业务服务部署完成。回滚使用匹配的后端 wheel、依赖和静态包，并遵守数据库备份恢复边界。

### 启动参数与配置文件

服务当前仅面向可信本机操作者，`actor-id` 不是远程登录认证。读取与写入均限制本地 Host/同源请求，CLI 显式关闭代理头解释并固定单 worker。扩展外部访问前的认证、TLS、密钥分发及轮换要求见 [部署运维契约](doc/plans/deployment-operations.md)；当前没有可直接公开的远程入口。

```powershell
uv run --frozen rc serve --help
# 自定义端口、操作者和工作区示例：
uv run --frozen rc serve --models config/models.local.toml --port 8001 --actor-id local --workspace data --mode test
```

| 参数 / 文件 | 默认值或用途 |
| --- | --- |
| `--models` | 必填，完整 模型 TOML 路径 |
| `--port` | 8000；固定监听 `127.0.0.1`。开发模式改端口时同步修改 Vite 的代理目标 |
| `--actor-id` | 命令行 > `RC_ACTOR_ID` > `local` |
| `--workspace` | `data`；当前会话产物保存在 `data/<report_id>/`。`RC_DATA_DIR` 不是此入口的替代参数 |
| `--policy-version` | `v2-policy-1`；恢复历史任务须保持对应策略身份 |
| `--execution-policy` | `config/execution_policy.toml`；全局/角色调用次数与 token 上限 |
| `--token-limit` | 新建会话默认 2,100,000，实际不超过执行策略上限；不重置已有会话预算 |
| `--rules` | `config/compliance_rules.yaml`；报告风险提示与合规规则 |
| `--mode` | `serve` 默认 `test`；正式规则使用 `production` |
| `--rules-approval` | 正式规则审批 JSON，须匹配规则版本及哈希 |
| `--static-dir` | 可指定其他前端构建目录；否则自动检查 `frontend/dist` |
| `config/sandbox.toml` | 沙盒镜像、CPU、内存、超时、输出大小和执行次数；默认单次 120 秒、1 CPU、1024 MiB |
| 包内 `resources/` | 技能和提示资源；不需要创建根目录 `skills/` |

正式模式须先由研究员准备非示例规则及匹配审批文件，完成对应宿主绑定后再启动；已有测试绑定时先停写、处理未完成任务，并在绑定命令增加 `--replace`：

```powershell
uv run --frozen rc bind-host --models config/models.local.toml --actor-id local --note '核验正式规则、审批和运行配置' --mode production --rules config/compliance_rules.production.yaml --rules-approval config/compliance_rules.approval.json
uv run --frozen rc serve --models config/models.local.toml --mode production --rules config/compliance_rules.production.yaml --rules-approval config/compliance_rules.approval.json
```

上述两个正式规则文件需自行准备；仓库示例规则不能直接作为正式审批依据。

## 经验复盘与长期经验

完整交付并人工签署后，服务会在同一数据库事务内登记一条复盘任务（报告 + 交付版本唯一）。暂停、取消和最终失败不会触发。`rc serve` 启动一个后台 worker，按租约和所有权领取；未开始的过期领取可重新排队，已开始后失联的尝试需核查用量并人工恢复，不自动重发。经验写入与复盘完成标记在同一事务提交，因此每个交付版本最多产生一份有效复盘。相关表仅由显式 `rc init-db` 创建/升级。

- 复盘读取当次交付的计划、审阅意见、返工方案和角色结果，只提炼可跨标的复用的方法；没有依据时允许零条，不收集用户偏好或标的专属结论。
- 后续角色派发时按角色、阶段（采集 / 分析 / 写作）和已知问题类型召回已启用经验，默认最多 3 条；同等匹配优先近期经验，停用后立即不再召回。
- 经验以带来源的参考材料写入该任务的 `experiences.json`，不会覆盖任务边界。只有角色结构化结果显式写入 `used_experience_ids` 时才记为“已采用”；仅收到记为“仅收到”。
- 报告页的“经验复盘”抽屉显示复盘状态、本次收到与本报告产出的经验、来源报告/版本、过程依据以及采用记录，并可在该处停用或重新启用经验。
- 首期使用受控标签和 PostgreSQL 结构化查询，不生成 embedding、不启用 pgvector。真实模型提炼经验的准确性和对后续研究的改善需另行授权评估；离线验收只覆盖存储、恢复、筛选和追溯。
- 复盘模型默认使用模型 TOML 中的 `summarizer`，没有时回退 `reviewer`；每个复盘任务记录模型用量，不占用报告既有阶段预算。

## 检查、停止与再次启动

启动后检查 API：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/health
```

健康接口的 `ok` 表示本地宿主已就绪，不代表已验证模型服务、外部数据或 Docker 研究全链路。页面或只读状态接口不会推进任务；已有失败任务需在界面显式补充信息或恢复，不会因打开页面自动重跑。

停止应用：在后端终端按 **Ctrl+C**；开发模式还需在前端终端按 **Ctrl+C**。确认端口释放：

```powershell
Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
    Where-Object { $_.LocalPort -in 8000, 5173 }
```

仅当本项目 Compose 数据库也需要停机时执行 `docker compose stop postgres`；下次执行 `docker compose start postgres`。停止应用不需要删除数据库卷或工作区。

再次启动使用上面的方式一或方式二；依赖未变时无需重新安装，镜像未变时无需重新构建。保持原操作者、工作区及策略参数才能读取对应历史任务。

## 启动故障排查

### PostgreSQL 5432 端口已被占用

可以使用已有 PostgreSQL 并填写对应的连接串；也可以把本项目 Compose 的本机端口改为 55432。在 `.env` 中同时修改下面两项，凭证保持与实际数据库一致：

```dotenv
POSTGRES_HOST_PORT=55432
RC_DATABASE_URL=postgresql://research_copilot:research_copilot_local_dev@127.0.0.1:55432/research_copilot
```

然后重新执行 `docker compose up -d postgres`，确认容器健康，再重启后端。不要通过删除数据库卷解决端口冲突。已有数据卷的数据库密码不会因修改 `.env` 自动变更。

### 页面提示 OperationalError，或研究在调用模型前失败

先保留后端终端中的完整异常栈，不能仅根据异常类型判断是连接、表结构还是 checkpoint 问题。可用以下命令启动后端并同时保存日志（替代普通后端启动命令，不要同时启动两个后端）：

```powershell
New-Item -ItemType Directory -Force logs | Out-Null
uv run --frozen rc serve --models config/models.local.toml --mode test 2>&1 |
    Tee-Object -FilePath logs/backend.log -Append
```

检查 `docker compose ps postgres`、`.env` 的连接串，以及是否有旧后端进程仍监听 8000。确认目标数据库正确后，可运行 `uv run --frozen rc init-db` 初始化所需表；这不能修复所有连接错误。分享日志前移除密钥和连接凭证。不要反复发送研究问题代替故障诊断。

### 前端能打开，但 API 无法连接或页面仍是旧版本

- 开发模式需要后端 8000（`--api-only`）和前端 5174 同时运行；改变后端端口后，同步修改 `frontend/vite.config.ts` 的代理目标并重启 Vite。
- 静态包出现配对不匹配时，先确认后端/运行环境与目标版本一致，再重新执行 `pnpm build`、`rc verify-web frontend/dist` 并重启；不要手工改清单。
- 更新包名、依赖或后端源码后，执行 `uv sync --frozen --all-groups` 并重启后端；不要继续使用更新前的进程。

## 历史产物与兼容边界

历史文件与现有数据库不随代码清理删除。`scripts/report_usage.py` 保留旧 receipt / progress 的只读统计；缺少模型身份时仅标记为当前配置估计，不伪造历史调用次数或成本。当前的占位角色显式模式、`supervisor-v1` 检查点路由、数据源适配器和资源兼容校验仍有调用方，因此继续保留。

```powershell
uv run --frozen python -m scripts.report_usage --reports data/reports --json
```

## 验证与代码目录

```powershell
uv run --frozen pytest -q
Push-Location frontend
pnpm typecheck
pnpm build
node --test tests/chat-progress.test.mjs tests/chat-stream.test.mjs tests/observability.test.mjs
Pop-Location
```

默认测试使用离线依赖；Docker / 数据库 / 付费模型测试须按标记另行选择，跳过项不算真实全链路验收。本次 README 和示例配置仅做离线配置/命令核对，未启动模型研究。

需要复核 PostgreSQL 检查点、预算恢复与经验复盘任务时，使用测试库连接串显式运行数据库用例（会创建并清理测试报告行）：

```powershell
$env:RC_TEST_DATABASE_URL = "postgresql://research_copilot:research_copilot_local_dev@127.0.0.1:55432/research_copilot"
uv run --frozen pytest -q -m db tests/integration/test_experience_postgres.py
Remove-Item Env:RC_TEST_DATABASE_URL
```

| 目录 | 内容 |
| --- | --- |
| `src/research_copilot/` | 主编排、运行时、角色业务、沙盒、Web API、包内技能资源 |
| `frontend/` | React 对话工作台、API/SSE 客户端、确认界面与图表 |
| `config/` | 模型示例、预算、规则、沙盒等配置 |
| `docker/analysist-sandbox/` | 分析计算与图表执行镜像 |
| `data/` | 本地会话产物，不提交到 Git |
| `tests/unit/` / `tests/integration/test_*` | 当前 测试 |
| `doc/plans/` | 设计与实施记录；其中历史环境结果需区分于当前验证 |

更多说明：[前端启动](frontend/README.md)、[对话工作台实施记录](doc/plans/research-copilot-chat-workbench-implementation.md)、[Analysist 沙盒计划](doc/plans/research-copilot-analysist-sandbox-plan.md)。

本次清理范围、保留理由与验证结果见 [冗余代码清理记录](doc/plans/research-copilot-dead-code-cleanup-20261031.md)。

### DeepSeek 返回 Thinking mode does not support this tool_choice

这是思考模式与强制工具选择冲突导致的 HTTP 400。对 `config/models.local.toml`
中的五个 `[roles.*]` 配置段，均设置：

```toml
required_tool_choice = "auto"
```

示例配置已包含此项；已有本地配置需要同步修改。该设置保留思考模式，
由 ToolStrategy 继续校验结构化结果，不将普通文本视为成功产物。
修改后用 Ctrl+C 停止后端，再按原命令启动（无需重新启动数据库或清空数据）：

```powershell
uv run --frozen rc serve --models config/models.local.toml --mode test
```

刷新工作台并检查原任务的可恢复状态；配置修复不会自动重跑失败任务，
也不会重置已累计的调用次数和 token。
协议依据：[DeepSeek Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/)。
