"""``rc`` entry point for the session host."""

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import typer
from pydantic import ValidationError

from research_copilot import __version__
from research_copilot.runtime.preflight import new_budget
from research_copilot.runtime.host_settings import HostSettings
from research_copilot.runtime.loop import run_async
from research_copilot.runtime.session import SessionRecord

from research_copilot.runtime.identity import DEFAULT_POLICY_VERSION, RUNTIME_VERSION

app = typer.Typer(
    name="rc",
    help="Research Copilot：本地研究会话宿主。",
    no_args_is_help=True,
    add_completion=False,
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"research-copilot {__version__}")
        raise typer.Exit()


@app.callback()
def _main(
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        callback=_version_callback,
        is_eager=True,
        help="显示应用版本并退出。",
    ),
) -> None:
    """Research Copilot 会话宿主。"""


def _settings_or_exit() -> HostSettings:
    try:
        return HostSettings()
    except ValidationError as exc:
        typer.secho("配置校验失败：", fg="red")
        for error in exc.errors(
            include_input=False, include_context=False, include_url=False
        ):
            field = ".".join(str(part) for part in error["loc"])
            typer.echo(f"{field}：配置格式或取值不合法（{error['type']}）")
        raise typer.Exit(code=1) from None


def _print_status(status) -> None:
    typer.echo(
        json.dumps(
            {
                "report_id": status.report_id,
                "status": status.status,
                "interrupts": list(status.interrupts),
            },
            ensure_ascii=False,
            default=str,
        )
    )


def _load_decisions(path: Path | None) -> dict | None:
    if path is None:
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise typer.BadParameter("决定文件必须是 JSON 对象")
    return payload


@app.command("backup")
def backup_command(
    destination: Path, workspace: Path = typer.Option(Path("data"), "--workspace")
) -> None:
    """停写后联合备份数据库/checkpoint/工作区到新目录；不调用模型。"""
    from research_copilot.runtime.backup import backup

    settings = _settings_or_exit()
    if not settings.database_url:
        raise typer.BadParameter("RC_DATABASE_URL is required")
    result = backup(settings.database_url, workspace, destination)
    typer.echo(
        json.dumps(
            {"backup": str(destination.resolve()), "files": len(result["files"])}
        )
    )


@app.command("restore")
def restore_command(
    bundle: Path, workspace: Path = typer.Option(..., "--workspace")
) -> None:
    """恢复到空的专用数据库和不存在的工作区；校验全部文件及业务/checkpoint 行。"""
    from research_copilot.runtime.backup import restore

    settings = _settings_or_exit()
    if not settings.database_url:
        raise typer.BadParameter(
            "RC_DATABASE_URL must identify an empty dedicated target database"
        )
    restore(settings.database_url, bundle, workspace)
    typer.echo("联合恢复和哈希校验通过。")


@app.command("init-db")
def init_db() -> None:
    """显式升级业务 schema 与 LangGraph checkpoint；须先停写并备份。"""

    settings = _settings_or_exit()

    async def _run() -> None:
        from research_copilot.host import initialize_database

        await initialize_database(settings)

    try:
        run_async(_run())
    except Exception as exc:
        typer.secho(f"初始化失败：{type(exc).__name__}: {exc}", fg="red", err=True)
        raise typer.Exit(code=1) from None
    typer.echo("数据库表已就绪。")


@app.command("runtime-owners")
def runtime_owners_command() -> None:
    """只读列出最多 100 条未正常关闭的宿主记录；不启动模型。"""
    from research_copilot.runtime.runtime_ownership import list_owners

    try:
        owners = run_async(list_owners(_settings_or_exit().database_url))
    except Exception as exc:
        typer.secho(f"宿主查询失败：{type(exc).__name__}: {exc}", fg="red", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(
        json.dumps({"owners": owners, "limit": 100}, ensure_ascii=False, default=str)
    )


@app.command("recover-runtime")
def recover_runtime_command(
    owner_id: str = typer.Option(..., help="runtime-owners 返回的宿主 ID"),
    actor_id: str = typer.Option(...),
    note: str = typer.Option(..., help="进程和外部执行停止的核查依据，不应包含密钥"),
    owner_stopped: bool = typer.Option(
        False, "--owner-stopped", help="明确确认旧进程及其外部执行已停止"
    ),
) -> None:
    """核实外部执行停止后恢复宿主及其沙盒容量；不释放任务名额、清除预算或重发请求。"""
    from research_copilot.runtime.runtime_ownership import recover_owner

    try:
        receipt = run_async(
            recover_owner(
                _settings_or_exit().database_url,
                owner_id,
                actor_id=actor_id,
                note=note,
                owner_stopped=owner_stopped,
            )
        )
    except Exception as exc:
        typer.secho(f"宿主恢复失败：{type(exc).__name__}: {exc}", fg="red", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(json.dumps(receipt, ensure_ascii=False, default=str))


@app.command("sandbox-claims")
def sandbox_claims_command() -> None:
    """只读列出未释放的沙盒容量、宿主和执行身份。"""
    from research_copilot.runtime.sandbox_slots import list_claims

    try:
        rows = run_async(list_claims(_settings_or_exit().database_url))
    except Exception as exc:
        typer.secho(
            f"沙盒容量查询失败：{type(exc).__name__}: {exc}", fg="red", err=True
        )
        raise typer.Exit(code=1) from None
    typer.echo(json.dumps({"claims": rows}, ensure_ascii=False, default=str))


@app.command("maintenance-owners")
def maintenance_owners_command() -> None:
    """只读列出未关闭的维护操作，包括尚未完成业务建表的目标库。"""
    from research_copilot.runtime.maintenance_ownership import list_owners

    try:
        owners = run_async(list_owners(_settings_or_exit().database_url))
    except Exception as exc:
        typer.secho(f"维护查询失败：{type(exc).__name__}: {exc}", fg="red", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(
        json.dumps({"owners": owners, "limit": 100}, ensure_ascii=False, default=str)
    )


@app.command("recover-maintenance")
def recover_maintenance_command(
    owner_id: str = typer.Option(..., help="maintenance-owners 返回的维护 ID"),
    actor_id: str = typer.Option(...),
    note: str = typer.Option(
        ..., help="维护进程及外部工具已停止的核查依据，不应包含密钥"
    ),
    owner_stopped: bool = typer.Option(False, "--owner-stopped"),
) -> None:
    """审计释放遗留维护记录；不修复部分目标、不重试迁移或恢复、不解除宿主与请求阻断。"""
    from research_copilot.runtime.maintenance_ownership import recover_owner

    try:
        receipt = run_async(
            recover_owner(
                _settings_or_exit().database_url,
                owner_id,
                actor_id=actor_id,
                note=note,
                owner_stopped=owner_stopped,
            )
        )
    except Exception as exc:
        typer.secho(f"维护恢复失败：{type(exc).__name__}: {exc}", fg="red", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(json.dumps(receipt, ensure_ascii=False, default=str))


@app.command("check-sources")
def check_sources_command(
    propose: Path | None = typer.Option(
        None,
        "--propose",
        help="把发现结果写入新的审阅文件；不修改批准快照",
    ),
) -> None:
    """只读检查六域部署契约：仅 initialize + 分页 tools/list；不取数、不调用模型、不写数据库。"""
    from research_copilot.sources.financekb_diagnostics import diagnose, write_proposal

    settings = _settings_or_exit()
    if not settings.financekb_base_url or not settings.financekb_api_token:
        typer.secho(
            "未配置 RC_FINANCEKB_BASE_URL / RC_FINANCEKB_API_TOKEN，无法检查六域部署。",
            fg="red",
            err=True,
        )
        raise typer.Exit(code=1)

    async def _run():
        return await diagnose(
            settings.financekb_base_url,
            settings.financekb_api_token.get_secret_value(),
            ca_bundle=settings.financekb_ca_bundle,
            call_timeout=settings.financekb_call_timeout_seconds,
        )

    try:
        report = run_async(_run())
        if propose is not None:
            write_proposal(report, propose)
    except (OSError, ValueError) as exc:
        typer.secho(f"诊断参数错误：{exc}", fg="red", err=True)
        raise typer.Exit(code=1) from None
    except Exception as exc:  # noqa: BLE001 - 只输出安全类别，不回显异常细节
        typer.secho(f"诊断失败：{type(exc).__name__}: {exc}", fg="red", err=True)
        raise typer.Exit(code=1) from None

    public = {
        **report,
        "domains": [
            {key: value for key, value in item.items() if key != "discovered"}
            for item in report["domains"]
        ],
    }
    typer.echo(json.dumps(public, ensure_ascii=False, indent=2, default=str))
    if report["summary"]["verified"] != len(report["domains"]):
        raise typer.Exit(code=2)


@app.command("bind-host")
def bind_host_command(
    models: Path = typer.Option(..., help="ModelsConfig TOML"),
    actor_id: str = typer.Option(..., help="登记操作者标识"),
    note: str = typer.Option(..., help="停写与配置核验依据，不应包含密钥"),
    workspace: Path = typer.Option(Path("data")),
    replace: bool = typer.Option(False, "--replace", help="显式替换已有绑定"),
    policy_version: str = typer.Option(DEFAULT_POLICY_VERSION),
    mode: str = typer.Option("production"),
    rules: Path = typer.Option(Path("config/compliance_rules.yaml")),
    rules_approval: Path | None = typer.Option(None),
    execution_policy: Path = typer.Option(Path("config/execution_policy.toml")),
) -> None:
    """停写后登记数据库、工作区与运行配置绑定；不调用模型。"""
    from research_copilot.host import bind_host_configuration

    try:
        bound = run_async(
            bind_host_configuration(
                _settings_or_exit(),
                models_path=models,
                workspace_root=workspace,
                actor_id=actor_id,
                note=note,
                replace=replace,
                policy_version=policy_version,
                mode=mode,
                rules_path=rules,
                rules_approval_path=rules_approval,
                execution_policy_path=execution_policy,
            )
        )
    except Exception as exc:
        typer.secho(f"绑定失败：{type(exc).__name__}: {exc}", fg="red", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(
        json.dumps(
            {
                key: bound[key]
                for key in (
                    "revision",
                    "workspace_id",
                    "config_digest",
                )
            }
        )
    )


@app.command()
def start(
    report_id: str = typer.Option(..., help="报告标识"),
    actor_id: str = typer.Option(..., help="操作者标识"),
    message: str = typer.Option(..., help="交给 supervisor 的研究需求"),
    models: Path = typer.Option(..., help="ModelsConfig TOML"),
    policy_version: str = typer.Option(DEFAULT_POLICY_VERSION, help="会话策略版本"),
    workspace: Path = typer.Option(Path("data"), help="报告工作区根目录"),
    token_limit: int | None = typer.Option(
        None, min=1, help="新建会话的 token 上限；缺省采用执行策略配置，显式值只能收紧"
    ),
    mode: str = typer.Option("production", help="production or explicit test"),
    rules: Path = typer.Option(Path("config/compliance_rules.yaml")),
    rules_approval: Path | None = typer.Option(None),
    execution_policy: Path = typer.Option(Path("config/execution_policy.toml")),
) -> None:
    """原子登记会话与命令，等待全局名额后仅执行本命令。"""

    settings = _settings_or_exit()

    async def _run():
        from research_copilot.host import open_session
        from research_copilot.runtime.run_admission import create_start
        from research_copilot.runtime.run_queue import RunQueue
        from research_copilot.runtime.local_runner import run_command

        async with open_session(
            settings,
            models_path=models,
            workspace_root=workspace,
            policy_version=policy_version,
            mode=mode,
            rules_path=rules,
            rules_approval_path=rules_approval,
            execution_policy_path=execution_policy,
        ) as (session, store, _services):
            from research_copilot.sources.health import require_available_sources

            require_available_sources(getattr(session, "source_statuses", None))
            record = SessionRecord(
                report_id,
                actor_id,
                RUNTIME_VERSION,
                policy_version,
                report_id,
                user_request=message.strip(),
                created_at=datetime.now(timezone.utc).isoformat(),
            )
            _, job = await create_start(
                store,
                record,
                new_budget(
                    report_id, token_limit=token_limit, policy_path=execution_policy
                ).snapshot(),
            )
            if job is None:
                raise ValueError("会话已启动或已取消，请核查状态后使用 resume")
            typer.echo(
                f"已登记启动命令 {job['id']}，等待全局名额。退出等待不撤销命令；撤销请使用 rc cancel。",
                err=True,
            )
            return await run_command(RunQueue(store.engine), session, job)

    try:
        _print_status(run_async(_run()))
    except Exception as exc:
        typer.secho(f"启动失败：{type(exc).__name__}: {exc}", fg="red", err=True)
        raise typer.Exit(code=1) from None


@app.command()
def resume(
    report_id: str = typer.Option(..., help="报告标识"),
    actor_id: str = typer.Option(..., help="操作者标识"),
    models: Path = typer.Option(..., help="ModelsConfig TOML"),
    decisions: Path | None = typer.Option(None, help="interrupt id 到决定的 JSON 文件"),
    operation_id: str | None = typer.Option(
        None, help="稳定恢复请求标识；重入时沿用同一值与决定文件"
    ),
    policy_version: str = typer.Option(DEFAULT_POLICY_VERSION, help="会话策略版本"),
    workspace: Path = typer.Option(Path("data"), help="报告工作区根目录"),
    mode: str = typer.Option("production", help="production or explicit test"),
    rules: Path = typer.Option(Path("config/compliance_rules.yaml")),
    rules_approval: Path | None = typer.Option(None),
    execution_policy: Path = typer.Option(Path("config/execution_policy.toml")),
) -> None:
    """按当前中断恢复会话。省略决定文件时只续跑未完成节点。"""

    settings = _settings_or_exit()
    supplied = _load_decisions(decisions)

    async def _run():
        from research_copilot.host import open_session
        from research_copilot.runtime.run_queue import RunQueue
        from research_copilot.runtime.run_commands import parse_command
        from research_copilot.runtime.local_runner import run_command

        async with open_session(
            settings,
            models_path=models,
            workspace_root=workspace,
            policy_version=policy_version,
            mode=mode,
            rules_path=rules,
            rules_approval_path=rules_approval,
            execution_policy_path=execution_policy,
        ) as (session, store, _services):
            payload = {"decision": supplied}
            parse_command({"kind": "resume", "payload": payload})
            queue = RunQueue(store.engine)
            job = await queue.enqueue(
                report_id=report_id,
                actor_id=actor_id,
                operation_id=operation_id or uuid4().hex,
                kind="resume",
                payload=payload,
            )
            typer.echo(
                f"已登记恢复命令 {job['id']}；重入时使用 --operation-id {job['operation_id']} 与相同决定文件。退出等待不撤销命令；撤销请使用 rc cancel。",
                err=True,
            )
            return await run_command(queue, session, job)

    try:
        _print_status(run_async(_run()))
    except Exception as exc:
        typer.secho(f"恢复失败：{type(exc).__name__}: {exc}", fg="red", err=True)
        raise typer.Exit(code=1) from None


@app.command()
def message(
    report_id: str = typer.Option(..., help="报告标识"),
    actor_id: str = typer.Option(..., help="操作者标识"),
    message: str = typer.Option(..., help="追加给 supervisor 的用户消息"),
    message_id: str = typer.Option(..., help="稳定消息标识；重入时沿用同一标识与内容"),
    models: Path = typer.Option(..., help="ModelsConfig TOML"),
    operation_id: str | None = typer.Option(
        None, help="稳定命令标识；缺省由消息标识派生"
    ),
    policy_version: str = typer.Option(DEFAULT_POLICY_VERSION, help="会话策略版本"),
    workspace: Path = typer.Option(Path("data"), help="报告工作区根目录"),
    mode: str = typer.Option("production", help="production or explicit test"),
    rules: Path = typer.Option(Path("config/compliance_rules.yaml")),
    rules_approval: Path | None = typer.Option(None),
    execution_policy: Path = typer.Option(Path("config/execution_policy.toml")),
) -> None:
    """登记一条用户消息并推进会话；不改变已冻结的研究范围。"""

    settings = _settings_or_exit()

    async def _run():
        from research_copilot.host import open_session
        from research_copilot.runtime.run_queue import RunQueue
        from research_copilot.runtime.run_commands import parse_command
        from research_copilot.runtime.local_runner import run_command
        from research_copilot.workspace import safe_component

        payload = {"message": message, "message_id": message_id}
        parse_command({"kind": "message", "payload": payload})
        safe_component(message_id)
        derived = operation_id or "message-" + hashlib.sha256(
            f"{report_id}\0{message_id}".encode()
        ).hexdigest()

        async with open_session(
            settings,
            models_path=models,
            workspace_root=workspace,
            policy_version=policy_version,
            mode=mode,
            rules_path=rules,
            rules_approval_path=rules_approval,
            execution_policy_path=execution_policy,
        ) as (session, store, _services):
            queue = RunQueue(store.engine)
            job = await queue.enqueue(
                report_id=report_id,
                actor_id=actor_id,
                operation_id=derived,
                kind="message",
                payload=payload,
            )
            typer.echo(
                f"已登记消息命令 {job['id']}；重入时沿用 --operation-id {job['operation_id']} 与同一消息。退出等待不撤销命令；撤销请使用 rc cancel。",
                err=True,
            )
            return await run_command(queue, session, job)

    try:
        _print_status(run_async(_run()))
    except Exception as exc:
        typer.secho(f"消息失败：{type(exc).__name__}: {exc}", fg="red", err=True)
        raise typer.Exit(code=1) from None


@app.command()
def status(
    report_id: str = typer.Option(..., help="报告标识"),
    actor_id: str = typer.Option(..., help="操作者标识"),
    models: Path | None = typer.Option(None, help="ModelsConfig TOML"),
    policy_version: str = typer.Option(DEFAULT_POLICY_VERSION, help="会话策略版本"),
    workspace: Path = typer.Option(Path("data"), help="报告工作区根目录"),
) -> None:
    """查看会话状态；不推进图。"""

    settings = _settings_or_exit()

    async def _run():
        from research_copilot.runtime.storage import postgres_runtime
        from research_copilot.web.status import status_payload

        async with postgres_runtime(
            settings.database_url, initialize=False, read_only=True
        ) as (
            store,
            _,
        ):
            return await status_payload(store, actor_id, report_id)

    try:
        typer.echo(json.dumps(run_async(_run()), ensure_ascii=False))
    except Exception as exc:
        typer.secho(f"查询失败：{type(exc).__name__}: {exc}", fg="red", err=True)
        raise typer.Exit(code=1) from None


@app.command()
def cancel(
    report_id: str = typer.Option(..., help="报告标识"),
    actor_id: str = typer.Option(..., help="操作者标识"),
    models: Path | None = typer.Option(None, help="ModelsConfig TOML"),
    policy_version: str = typer.Option(DEFAULT_POLICY_VERSION, help="会话策略版本"),
    workspace: Path = typer.Option(Path("data"), help="报告工作区根目录"),
) -> None:
    """请求取消当前会话。"""

    settings = _settings_or_exit()

    async def _run():
        from research_copilot.runtime.storage import postgres_runtime
        from research_copilot.web.status import status_payload

        async with postgres_runtime(settings.database_url, initialize=False) as (
            store,
            _,
        ):
            await store.request_cancel(report_id, actor_id)
            from research_copilot.runtime.run_queue import RunQueue

            await RunQueue(store.engine).cancel_waiting(report_id, actor_id)
            return await status_payload(store, actor_id, report_id)

    try:
        typer.echo(json.dumps(run_async(_run()), ensure_ascii=False))
    except Exception as exc:
        typer.secho(f"取消失败：{type(exc).__name__}: {exc}", fg="red", err=True)
        raise typer.Exit(code=1) from None


@app.command("preflight")
def check_preflight(
    mode: str = typer.Option("production"),
    rules: Path = typer.Option(Path("config/compliance_rules.yaml")),
    rules_approval: Path | None = typer.Option(None),
    execution_policy: Path = typer.Option(Path("config/execution_policy.toml")),
) -> None:
    """Validate local resources, budget policy and rule approval without external calls."""
    from research_copilot.runtime.preflight import preflight

    _, result = preflight(
        rules_path=rules,
        mode=mode,
        approval_path=rules_approval,
        policy_path=execution_policy,
    )
    typer.echo(json.dumps(result, ensure_ascii=False))


@app.command("recover-rework")
def recover_rework(
    report_id: str = typer.Option(...),
    actor_id: str = typer.Option(...),
    review_id: str = typer.Option(...),
    workspace: Path = typer.Option(Path("data")),
) -> None:
    """Explicitly restore immutable report/analysis pointers; no model or budget reset."""
    settings = _settings_or_exit()

    async def restore():
        from research_copilot.runtime.storage import postgres_runtime
        from research_copilot.runtime.business import BusinessCoordinator
        from research_copilot.subagents.services import RoleServices
        from research_copilot.contracts import RunContext

        async with postgres_runtime(settings.database_url, initialize=False) as (
            store,
            _,
        ):
            async with store.lock(report_id):
                record = await store.load(report_id, actor_id)
                context = RunContext(
                    actor_id,
                    report_id,
                    record.run_id,
                    record.budget_id,
                    record.policy_version,
                )
                business = BusinessCoordinator(store)
                RoleServices(
                    repository=store, workspace_root=workspace, business=business
                )
                return await business.recover_rework(context, review_id)

    typer.echo(json.dumps(run_async(restore()), ensure_ascii=False))


@app.command("reconcile-usage")
def reconcile_usage(
    report_id: str = typer.Option(...),
    actor_id: str = typer.Option(...),
    corrections: Path = typer.Option(
        ..., help='JSON：{"request_id": {"usage": int, "note": str}}'
    ),
) -> None:
    """按 provider 账单校正预算账本中的未知/暂定用量；不重置任何累计值。"""
    settings = _settings_or_exit()
    items = json.loads(corrections.read_text("utf-8"))

    async def apply():
        from research_copilot.contracts import RunContext
        from research_copilot.runtime.storage import postgres_runtime

        async with postgres_runtime(settings.database_url, initialize=False) as (
            store,
            _,
        ):
            async with store.lock(report_id):
                record = await store.load(report_id, actor_id)
                context = RunContext(
                    actor_id,
                    report_id,
                    record.run_id,
                    record.budget_id,
                    record.policy_version,
                )
                budget = await store.load_budget(context)
                before = budget.status()
                applied = {}
                for request_id, item in items.items():
                    await budget.reconcile(
                        request_id,
                        int(item["usage"]),
                        details={
                            "actor_id": actor_id,
                            "source": "provider_bill",
                            **{k: v for k, v in item.items() if k != "usage"},
                        },
                    )
                    applied[request_id] = int(item["usage"])
                return {"before": before, "after": budget.status(), "applied": applied}

    try:
        typer.echo(json.dumps(run_async(apply()), ensure_ascii=False))
    except Exception as exc:
        typer.secho(f"对账失败：{type(exc).__name__}: {exc}", fg="red", err=True)
        raise typer.Exit(code=1) from None


@app.command()
def recover_request(
    report_id: str = typer.Option(...),
    actor_id: str = typer.Option(...),
    request_id: str = typer.Option(..., help="本地 reservation ID"),
    note: str = typer.Option(..., help="恢复依据，必须填写"),
    usage: int | None = typer.Option(
        None, min=0, help="核实后的实际 token；未知时省略"
    ),
    authorize_retry: bool = typer.Option(
        False, help="明确允许该节点再次发起可能收费的请求；不自动运行"
    ),
    provider_request_id: str | None = typer.Option(None),
) -> None:
    """恢复遗留 pending 或核实用量，并可独立授权重试；保留原累计账本。"""
    settings = _settings_or_exit()

    async def apply():
        from research_copilot.runtime.request_recovery import recover_request as recover
        from research_copilot.runtime.storage import postgres_runtime

        async with postgres_runtime(settings.database_url, initialize=False) as (
            store,
            _,
        ):
            return await recover(
                store,
                report_id=report_id,
                actor_id=actor_id,
                request_id=request_id,
                note=note,
                usage=usage,
                authorize_retry=authorize_retry,
                provider_request_id=provider_request_id,
            )

    try:
        typer.echo(json.dumps(run_async(apply()), ensure_ascii=False))
    except Exception as exc:
        typer.secho(f"请求恢复失败：{type(exc).__name__}: {exc}", fg="red", err=True)
        raise typer.Exit(code=1) from None


@app.command("extend-stage-budget")
def extend_stage_budget(
    report_id: str = typer.Option(...),
    actor_id: str = typer.Option(...),
    stage: str = typer.Option(..., help="要扩额的阶段，如 analysist"),
    tokens: int | None = typer.Option(
        None, min=1, help="新的阶段 token 上限，只能上调"
    ),
    calls: int | None = typer.Option(
        None, min=1, help="新的阶段模型调用上限，只能上调"
    ),
    branch: str | None = typer.Option(
        None, help="要扩额的审核分支，如 quality"
    ),
    branch_calls: int | None = typer.Option(
        None, min=1, help="新的分支模型调用上限，只能上调，需与 --branch 同给"
    ),
    note: str = typer.Option(..., help="扩额依据，不应包含密钥"),
) -> None:
    """审计扩展阶段 token/调用上限并解除该阶段预算失败；不调用模型、不重置已用额度。"""
    if tokens is None and calls is None and branch_calls is None:
        typer.secho(
            "扩额失败：必须指定 --tokens、--calls 或 --branch-calls",
            fg="red",
            err=True,
        )
        raise typer.Exit(code=1)
    settings = _settings_or_exit()

    async def apply():
        from research_copilot.runtime.budget_adjustment import extend_stage_budget
        from research_copilot.runtime.storage import postgres_runtime

        async with postgres_runtime(settings.database_url, initialize=False) as (
            store,
            _,
        ):
            return await extend_stage_budget(
                store,
                report_id=report_id,
                actor_id=actor_id,
                stage=stage,
                tokens=tokens,
                calls=calls,
                note=note,
                branch=branch,
                branch_calls=branch_calls,
            )

    try:
        typer.echo(json.dumps(run_async(apply()), ensure_ascii=False))
    except Exception as exc:
        typer.secho(f"扩额失败：{type(exc).__name__}: {exc}", fg="red", err=True)
        raise typer.Exit(code=1) from None


@app.command("recover-collection")
def recover_collection(
    report_id: str = typer.Option(...),
    actor_id: str = typer.Option(...),
    review_id: str | None = typer.Option(
        None, "--review", help="返工审核 ID；首轮采集省略"
    ),
    note: str = typer.Option(..., help="重置依据，不应包含密钥"),
) -> None:
    """审计重置指定审核轮的采集无进展计数；不放开来源调用/事实/执行额度。"""
    settings = _settings_or_exit()

    async def apply():
        from research_copilot.runtime.collection_recovery import recover_collection
        from research_copilot.runtime.storage import postgres_runtime

        async with postgres_runtime(settings.database_url, initialize=False) as (
            store,
            _,
        ):
            return await recover_collection(
                store,
                report_id=report_id,
                actor_id=actor_id,
                review_id=review_id,
                note=note,
            )

    try:
        typer.echo(json.dumps(run_async(apply()), ensure_ascii=False))
    except Exception as exc:
        typer.secho(f"采集恢复失败：{type(exc).__name__}: {exc}", fg="red", err=True)
        raise typer.Exit(code=1) from None


@app.command("extend-executions")
def extend_executions(
    report_id: str = typer.Option(...),
    actor_id: str = typer.Option(...),
    review_id: str | None = typer.Option(
        None, "--review", help="返工审核 ID；首轮分析省略"
    ),
    additional: int = typer.Option(..., min=1, max=20, help="新增沙盒执行尝试次数"),
    note: str = typer.Option(..., help="扩额依据，不应包含密钥"),
) -> None:
    """审计扩展指定审核轮的沙盒执行次数并解除该失败；不删除已有尝试。"""
    settings = _settings_or_exit()

    async def apply():
        from research_copilot.runtime.execution_adjustment import extend_executions
        from research_copilot.runtime.storage import postgres_runtime

        async with postgres_runtime(settings.database_url, initialize=False) as (
            store,
            _,
        ):
            return await extend_executions(
                store,
                report_id=report_id,
                actor_id=actor_id,
                review_id=review_id,
                additional=additional,
                note=note,
            )

    try:
        typer.echo(json.dumps(run_async(apply()), ensure_ascii=False))
    except Exception as exc:
        typer.secho(f"执行扩额失败：{type(exc).__name__}: {exc}", fg="red", err=True)
        raise typer.Exit(code=1) from None


@app.command("extend-global-budget")
def extend_global_budget_command(
    report_id: str = typer.Option(...),
    actor_id: str = typer.Option(...),
    tokens: int | None = typer.Option(
        None, min=1, help="新的全局 token 上限，只能上调"
    ),
    calls: int | None = typer.Option(
        None, min=1, help="新的全局模型调用上限，只能上调"
    ),
    note: str = typer.Option(..., help="扩额依据，不应包含密钥"),
) -> None:
    """审计扩展全局 token/调用上限并解除预算失败；不调用模型、不重置已用额度。"""
    if tokens is None and calls is None:
        typer.secho("扩额失败：必须指定 --tokens 或 --calls", fg="red", err=True)
        raise typer.Exit(code=1)
    settings = _settings_or_exit()

    async def apply():
        from research_copilot.runtime.budget_adjustment import extend_global_budget
        from research_copilot.runtime.storage import postgres_runtime

        async with postgres_runtime(settings.database_url, initialize=False) as (
            store,
            _,
        ):
            return await extend_global_budget(
                store,
                report_id=report_id,
                actor_id=actor_id,
                tokens=tokens,
                calls=calls,
                note=note,
            )

    try:
        typer.echo(json.dumps(run_async(apply()), ensure_ascii=False))
    except Exception as exc:
        typer.secho(f"全局扩额失败：{type(exc).__name__}: {exc}", fg="red", err=True)
        raise typer.Exit(code=1) from None


@app.command("extend-dispatches")
def extend_dispatches_command(
    report_id: str = typer.Option(...),
    actor_id: str = typer.Option(...),
    additional: int = typer.Option(..., min=1, max=12, help="新增派单额度"),
    note: str = typer.Option(..., help="扩额依据，不应包含密钥"),
) -> None:
    """审计扩展报告派单额度；不删除已有派单，不改变任务与预算上限。"""
    settings = _settings_or_exit()

    async def apply():
        from research_copilot.runtime.dispatch_adjustment import extend_dispatches
        from research_copilot.runtime.storage import postgres_runtime

        async with postgres_runtime(settings.database_url, initialize=False) as (
            store,
            _,
        ):
            return await extend_dispatches(
                store,
                report_id=report_id,
                actor_id=actor_id,
                additional=additional,
                note=note,
            )

    try:
        typer.echo(json.dumps(run_async(apply()), ensure_ascii=False))
    except Exception as exc:
        typer.secho(f"派单扩额失败：{type(exc).__name__}: {exc}", fg="red", err=True)
        raise typer.Exit(code=1) from None


@app.command("repair-dispatch")
def repair_dispatch_command(
    report_id: str = typer.Option(...),
    actor_id: str = typer.Option(...),
    dispatch_id: str = typer.Option(..., help="待解除结果校验失败的派单"),
    note: str = typer.Option(..., help="修复依据：已诊断的具体错误与收窄后的重派说明"),
) -> None:
    """审计解除派单的结果校验失败，允许收窄后重派；不重置用量、派单与预算。"""
    settings = _settings_or_exit()

    async def apply():
        from research_copilot.runtime.dispatch_repair import repair_dispatch_result
        from research_copilot.runtime.storage import postgres_runtime

        async with postgres_runtime(settings.database_url, initialize=False) as (
            store,
            _,
        ):
            return await repair_dispatch_result(
                store,
                report_id=report_id,
                actor_id=actor_id,
                dispatch_id=dispatch_id,
                note=note,
            )

    try:
        typer.echo(json.dumps(run_async(apply()), ensure_ascii=False))
    except Exception as exc:
        typer.secho(f"派单修复失败：{type(exc).__name__}: {exc}", fg="red", err=True)
        raise typer.Exit(code=1) from None


@app.command()
def serve(
    models: Path | None = typer.Option(None, help="ModelsConfig TOML"),
    history_only: bool = typer.Option(
        False, help="只读浏览已有报告；无需模型配置，不启动执行队列"
    ),
    actor_id: str | None = typer.Option(
        None, help="本地操作者。缺省读取 RC_ACTOR_ID，再缺省为 local"
    ),
    port: int = typer.Option(8000, min=1, max=65535),
    policy_version: str = typer.Option(DEFAULT_POLICY_VERSION, help="会话策略版本"),
    workspace: Path = typer.Option(Path("data"), help="报告工作区根目录"),
    token_limit: int | None = typer.Option(
        None, min=1, help="新建会话的 token 上限；缺省采用执行策略配置，显式值只能收紧"
    ),
    mode: str = typer.Option(
        "test",
        help="本地工作台缺省 test，以使用仓库里的示例合规规则；正式规则改为 production 并提供审批文件",
    ),
    rules: Path = typer.Option(Path("config/compliance_rules.yaml")),
    rules_approval: Path | None = typer.Option(None),
    execution_policy: Path = typer.Option(Path("config/execution_policy.toml")),
    static_dir: Path | None = typer.Option(
        None, help="前端构建目录。缺省在 frontend/dist 存在时使用它"
    ),
    api_only: bool = typer.Option(
        False, help="仅运行 API，不自动加载已有静态目录；用于 Vite 开发"
    ),
) -> None:
    """在本机 127.0.0.1 启动工作台 API。"""

    if api_only and static_dir is not None:
        raise typer.BadParameter("--api-only and --static-dir cannot be combined")

    import os

    import uvicorn

    from research_copilot.web.app import build_server_app
    from research_copilot.workspace import safe_component

    from research_copilot.llm.config import load_models_config

    settings = _settings_or_exit()
    if not history_only:
        if models is None:
            raise typer.BadParameter(
                "--models is required unless --history-only is selected"
            )
        try:
            model_config = load_models_config(models)
        except (OSError, ValueError) as exc:
            raise typer.BadParameter(f"模型配置无效：{exc}") from None
        for provider in model_config.providers.values():
            if os.environ.get(provider.api_key_env):
                continue
            if (
                provider.api_key_env == "RC_DEEPSEEK_API_KEY"
                and settings.deepseek_api_key is not None
            ):
                os.environ[provider.api_key_env] = (
                    settings.deepseek_api_key.get_secret_value()
                )
                continue
            raise typer.BadParameter(f"缺少模型密钥环境变量 {provider.api_key_env}")
    chosen_actor = actor_id or os.environ.get("RC_ACTOR_ID") or "local"
    try:
        safe_component(chosen_actor)
    except ValueError:
        raise typer.BadParameter(
            "操作者标识只能包含字母、数字、下划线和连字符"
        ) from None
    # serve 进程日志落盘：运行失败堆栈（PermissionError 等）必须可离线追溯，
    # 后台进程 stderr 在验收环境无法回看（WLY-20261108 第九轮教训）。
    log_dir = Path(workspace) / ".logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=log_dir / "serve.log",
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    frontend = static_dir
    if frontend is None and not api_only:
        candidate = Path("frontend/dist")
        frontend = candidate if (candidate / "index.html").is_file() else None
    server = build_server_app(
        settings=settings,
        history_only=history_only,
        models_path=models,
        workspace_root=workspace,
        actor_id=chosen_actor,
        policy_version=policy_version,
        token_limit=token_limit,
        mode=mode,
        rules_path=rules,
        rules_approval_path=rules_approval,
        execution_policy_path=execution_policy,
        static_dir=frontend,
    )
    uvicorn.run(
        server,
        host="127.0.0.1",
        port=port,
        loop="research_copilot.runtime.loop:serve_loop",
        proxy_headers=False,
        workers=1,
    )


@app.command("seal-web")
def seal_web_command(
    directory: Path, frontend_source: Path = typer.Option(Path("frontend"))
):
    """在前端构建之后，记录当前后端/API/运行环境和静态文件的配对身份。"""
    from research_copilot.runtime.web_release import seal_web

    try:
        result = seal_web(directory, frontend_source=frontend_source)
    except (OSError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from None
    typer.echo(result["release_id"])


@app.command("verify-web")
def verify_web_command(directory: Path):
    """离线核验静态包与当前后端配对；不打开数据库或模型。"""
    from research_copilot.web.app import create_app
    import json

    try:
        result = create_app(static_dir=directory).state.web_release
    except (OSError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from None
    typer.echo(json.dumps(result, ensure_ascii=False))


@app.command("evaluate-runs")
def evaluate_runs_command(
    suite: Path = typer.Option(..., help="冻结案例、断言和预算档位 JSON"),
    bindings: Path = typer.Option(..., help="案例/预算档位与已有报告身份的绑定 JSON"),
    output: Path = typer.Option(..., help="新建评测结果文件；不覆盖已有文件"),
    actor_id: str = typer.Option(...),
) -> None:
    """只读评测已有报告；空绑定仅准备矩阵，不连接数据库或模型。"""
    from research_copilot.runtime.run_evaluation import Bindings, Suite, evaluate_runs
    from research_copilot.runtime.storage import postgres_runtime

    try:
        spec = Suite.model_validate_json(suite.read_text("utf-8"))
        bound = Bindings.model_validate_json(bindings.read_text("utf-8"))
        if output.exists():
            raise ValueError("Evaluation output already exists")

        async def collect():
            if not bound.runs:
                return await evaluate_runs(
                    None, actor_id, spec.model_dump(), bound.model_dump()
                )
            settings = _settings_or_exit()
            async with postgres_runtime(settings.database_url, read_only=True) as (
                store,
                _,
            ):
                return await evaluate_runs(
                    store, actor_id, spec.model_dump(), bound.model_dump()
                )

        result = run_async(collect())
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    except Exception as exc:
        typer.secho(f"评测失败：{type(exc).__name__}: {exc}", fg="red", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(
        json.dumps(
            {
                "output": str(output),
                "complete": result["complete"],
                "suite_sha256": result["suite_sha256"],
            }
        )
    )


@app.command("compare-evaluations")
def compare_evaluations_command(
    suite: Path = typer.Option(..., help="两次评测共用的冻结案例 JSON"),
    baseline: Path = typer.Option(..., help="基线 evaluate-runs 输出"),
    candidate: Path = typer.Option(..., help="待比较 evaluate-runs 输出"),
    output: Path = typer.Option(..., help="新建比较结果；不覆盖已有文件"),
) -> None:
    """离线比较冻结断言和账本；退出码 0/2/3 对应无退化/退化/不可比较。"""
    from research_copilot.runtime.evaluation_comparison import compare_runs

    try:
        if output.exists():
            raise ValueError("Comparison output already exists")
        result = compare_runs(
            json.loads(suite.read_text("utf-8")),
            json.loads(baseline.read_text("utf-8")),
            json.loads(candidate.read_text("utf-8")),
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    except Exception as exc:
        typer.secho(f"比较失败：{type(exc).__name__}: {exc}", fg="red", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(json.dumps({"output": str(output), "status": result["status"]}))
    raise typer.Exit(
        code={"no_regression": 0, "regressed": 2, "not_comparable": 3}[result["status"]]
    )


def main() -> None:
    app()
