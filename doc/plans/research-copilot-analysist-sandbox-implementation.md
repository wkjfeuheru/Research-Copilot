# Analysist 技能脚本、Docker 与图表实施记录

日期：2026-10-29。对应 [已确认计划](research-copilot-analysist-sandbox-plan.md)。本记录区分代码交付、离线模型夹具、真实容器、浏览器与真实模型验收。

## 代码与边界

- `resources/skills/analysist/financial_modeling/scripts/` 是可独立导入或运行 CLI 的脚本包，不依赖 V1 Agent、数据库或工具注册器。迁移增长、五种比率、四种估值、可比表、期间差分；增加年末 FCFF DCF 和 WACC × g。Decimal 运算，缺失/除零/口径冲突失败。DCF 同时提供带上下文的 calculate 和供组合代码使用的数值内核 fcff_dcf，数值内核的单位与预测前提必须由调用方核对。
- `charts.py` / `echarts_options.py` 覆盖十种参考图。Matplotlib 与 ECharts 输出冻结数据和 PNG；ECharts 增加受控 option/SVG。Node 和浏览器共享配置白名单及 research-v1 主题，ECharts 均固定 6.1.0，Resvg 固定 2.6.2。中文字体由锁定 Debian 字体包提供，运行期间不下载。
- `sandbox/backend.py` 实现全部文件协议与 execute/aexecute，文件操作委托原受控后端；同步调用通过工作线程投递回绑定的异步服务循环。只有 analysist 获得 execute，模型业务工具仅保留 get_analysis / save_analysis，固定财务工具及 render_chart 不再注册。pip/uv 安装时可能为 .py 资源生成 __pycache__；仅与已登记源文件对应的派生缓存可在安装目录存在，缓存不对 Agent 暴露、不进入容器快照，也不计为技能源码。实际源码仍逐文件校验哈希。
- Deep Agents **0.7.32** 的 FilesystemMiddleware 拒绝同时使用执行后端与文件 permissions。Analysist 显式传空框架 permissions，并用既有角色/业务中间件限制文件路径和写入前缀；其他角色的框架权限不变。SkillsMiddleware 原生渐进加载，analysist 不要求入口全文或固定阅读顺序；writer 仍须完整读取入口与已确认模板。
- `sandbox/receipts.py` 从可信派发获取报告/权限/预算，冻结代码、授权证据及技能；执行前记录身份和 started 状态。每派发使用文件锁串行，全局文件槽限制并发为 2；次数、截止时间和服务 timeout 不能由模型扩大。

## 容器与凭据

Docker 镜像构建命令：

```powershell
docker build -f docker/analysist-sandbox/Dockerfile -t research-copilot-analysist:1 .
```

基础镜像使用 Dockerfile 中固定 digest，Python 依赖使用带哈希的 requirements.lock，Node 使用 package-lock.json。本机验收镜像 Python 为 3.13.15，image ID 为 `sha256:5a7289733b84e571ba7ce9c142ecbb677c65441c2c75731c281c07159637450d`。配置为 `config/sandbox.toml`。可显式填写 `image_digest`；每报告首次执行绑定实际 image ID，后续漂移拒绝，执行期间不拉取镜像。

容器网络禁用、非 root、无 capabilities、no-new-privileges、只读根目录，1 CPU / 1 GiB / 64 PID，单次 120 秒，临时空间 256 MiB，输出 32 MiB / 100 文件，日志 64 KiB。每次新容器，无驻留变量。

宿主仅挂载短路径下的不可变快照副本，避免 Docker Desktop 长 Windows 路径 I/O 错误。独立 `/outputs` 使用 Docker **受大小限制、由 tmpfs 驱动的卷**；普通运行时 tmpfs 无法经 docker cp 取回。本实现先暂停全部进程，复制有界归档，销毁全部进程和卷，再手动校验导入归档。拒绝链接、设备、路径越界、Windows 设备名、别名、超限文件；不使用 tar.extractall。

授权事实逐项核对 evidence_id、原始哈希、原值和 JSON pointer；结构化 context/meta 与单位、币种、期间、主体、口径一致。文本映射保留明确原文偏移和提取标记，交审核核验。假设单列依据与影响。全部候选校验后才事务登记 metric_id/chart_id；stdout 和输出路径不是正式凭据。

执行记录保存 command、代码/输入/技能哈希、image ID、派发/预算/策略身份、退出状态、耗时、受限日志、完整输出文件哈希。无 result.json 的探索命令只有执行记录。相同调用复用记录；输出已冻结但登记失败可恢复登记，不重跑。状态不明会阻断该派发的新调用身份，不能通过改 tool_call_id 绕过。

取消与超时清理容器及全部后代进程，不登记成功指标。启动时仅回收本项目、同宿主且所属应用进程已退出的容器/卷，不清理其他项目或仍存活应用的容器。超时、取消与状态不明同样保存已读取的受限日志。Windows 使用只读进程句柄查询；不使用具有终止行为的 os.kill(pid, 0)。Docker 不可用时明确失败，无宿主 Shell 回退。

## 写作、审核与 Web

- 保存分析继续独立验证凭据及文件哈希，不自动批准。writer 仅引用已确认分析的 chart_id，授权图表另行投影到当前派发的只读材料目录。
- Reviewer 冻结生成代码、输入、环境、执行记录、图表数据/配置/PNG；完整性检查兼容旧公式版本与 sandbox-code-v1。质量分支读取静态图的多模态内容；仅显式 `profile.image_inputs=True` 的模型接收图像。未声明该能力时视觉检查强制不完整，转人工；MeteredChatModel 保留宿主声明的能力。执行成功、自检或高分不能消除完整性问题。
- `web/charts.py` 按 report_id / version / chart_id 读取；验证认证 actor、该版本的正式引用、分析版本和文件哈希，仅返回受控 PNG/option/data 地址，不开放任意路径或 SVG 页面。
- 当前默认 Web 服务仍为原 V1 本地单用户服务。V2 路由提供 `create_chart_router(ChartService(role_services), actor_dependency)`；也可向原 create_app 同时传 chart_service 和 chart_actor_dependency。**必须由宿主传入真实鉴权依赖**，没有默认 actor 或从请求头猜身份的回退。本次未切换 Web 默认运行时，也未部署 V2 Web 会话服务。
- ReportPanel 识别正式 chart: 引用；ReportChart 渲染受控配置，处理 resize/dispose；配置失败显示同版本 PNG，打印使用 PNG；历史普通图片保留原路径。浏览器不使用 eval/生成 HTML、CDN 或远程字体。

## 验证记录

| 层级 | 已执行结果 |
|---|---|
| 完整单元与 V1 离线链路基线 | **1525 passed**；随后追加系列格式拒绝的 3 个用例，在最终定向测试中覆盖 |
| 最终脚本与沙盒契约 | **61 passed**，包含 3 个新增系列格式用例、安装缓存隔离、Windows 进程探测和视觉能力保留 |
| V2 全部集成 | **51 passed**，显式执行真实 Docker，不把跳过计为通过 |
| 最终镜像定向复测 | **2 passed**：原生 Agent 失败修正→执行→ECharts→保存→writer→reviewer，及两引擎共 20 张参考图 |
| 浏览器 | Playwright 真实 Chromium + HTTP API 夹具；提示、缩放、系列切换、resize、dispose、打印、恶意配置 PNG 回退、ReportPanel 标记、历史图片及无横向溢出通过；PNG/option 取自真实 Docker SSR |
| 构建与安装 | pnpm typecheck/build 通过；wheel 构建并实际安装，编译字节码后仍有 30 个校验通过的源资源，包含 9 个 Python 脚本；lint、锁文件与 diff 检查通过 |
| 未执行 | 真实付费模型、真实外部数据源、生产 PostgreSQL 和生产 Web 部署 |

```powershell
uv run --frozen pytest tests/unit tests/integration/test_writing_offline_chain.py --ignore=tests/unit/test_live_model_scenarios.py --ignore=tests/unit/test_m5_live_budget.py -q
uv run --frozen pytest tests/unit/test_analysis_scripts.py tests/unit/test_sandbox_contracts.py -q
$v2Tests = Get-ChildItem tests/integration/test_v2*.py -File | ForEach-Object { $_.FullName }
uv run --frozen pytest $v2Tests -m 'docker or not docker' -q
uv run --frozen pytest tests/integration/test_analysist.py tests/integration/test_chart_rendering.py -m docker -q
```

两个 ignore 是仓库既有的缺失验证脚本收集障碍，不是技能或沙盒测试排除。真实 Docker 测试标记为 docker，默认跳过以免普通离线测试依赖本机服务；上面的显式命令已实际运行它们。OOM、超时、取消、日志洪泛、子进程、遗留容器回收、畸形/链接/超量输出、幂等与登记恢复、状态不明不重跑均有真实容器结果。图表 API 以 ASGI + SQLite 实测跨 actor、跨 report、跨版本及哈希错误拒绝。

浏览器复现（另一个终端启动 Vite）：

```powershell
cd frontend
pnpm exec vite --host 127.0.0.1 --port 5181 --strictPort
# 仓库根目录的另一个终端：
npx --yes --package @playwright/cli playwright-cli -s=analysist-chart open http://127.0.0.1:5181/tests/chart-preview.html
npx --yes --package @playwright/cli playwright-cli -s=analysist-chart run-code --filename frontend/tests/chart-browser-check.mjs
```

截图位于 [.playwright-cli/sandbox-interactive.png](../../.playwright-cli/sandbox-interactive.png)、[静态回退](../../.playwright-cli/sandbox-fallback.png) 与 [正文及历史图片](../../.playwright-cli/sandbox-report-panel.png)。它们是夹具页面证据，不能当作已部署 V2 Web 的证明。前端构建仍提示 ECharts 按需加载块约 687 KiB（gzip 229 KiB）；没有关闭体积提示或执行额外付费验收。

已定位并补回归的实际问题：Windows Selector 循环不支持 asyncio 子进程（改为有界 Popen 传输）、Docker Desktop 深路径读取失败、普通 tmpfs 无法复制、SSR 多余 hover CSS、最小镜像字体枚举造成静态 PNG 缺字、Matplotlib TTC 字体名称与默认 SC 名称不同、计费包装器丢失显式视觉能力。

## 官方接口依据

- [Deep Agents 沙盒](https://docs.langchain.com/oss/python/deepagents/sandboxes)、[后端协议](https://docs.langchain.com/oss/python/deepagents/backends)、[渐进技能加载](https://docs.langchain.com/oss/python/deepagents/skills)。实际协议与 permissions 行为另经本地锁定 0.7.32 源码和原生图测试核对。
- [Docker 运行限制与挂载](https://docs.docker.com/reference/cli/docker/container/run/)。
- [ECharts 官方 SVG SSR](https://echarts.apache.org/handbook/en/how-to/cross-platform/server/)、[Resvg 字体参数](https://github.com/thx/resvg-js/blob/main/index.d.ts)、[React 外部组件生命周期](https://react.dev/reference/react/useEffect#connecting-to-an-external-system)。
