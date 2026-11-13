"""One real session; persistent budget/checkpoints, no automatic approvals."""
import argparse
import asyncio
import json
import os
import sys
import traceback
from contextlib import AsyncExitStack
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

from research_copilot.runtime.host_settings import HostSettings
from research_copilot.llm.config import ModelsConfig, load_models_config, open_models
from research_copilot.orchestration.factory import SupervisorFactory
from research_copilot.runtime.budget import TokenBudget
from research_copilot.runtime.identity import RUNTIME_VERSION
from research_copilot.runtime.business import BusinessCoordinator
from research_copilot.runtime.session import SessionRecord, SessionService
from research_copilot.runtime.storage import postgres_runtime
from research_copilot.sources.financekb import open_financekb_sources
from research_copilot.subagents.services import RoleServices
from research_copilot.workspace import bundled_resources
from scripts.acceptance_budget import CallBudget

QUESTION = ('请以贵州茅台2024年年度报告及相关公开披露为依据，分析2024年相较2023年的营业收入、'
            '归母净利润和经营活动现金流量净额变化，判断盈利质量是否改善。报告类型为flash_comment。'
            '限定2023和2024两个年度，不需要实时行情、投资评级或目标价。搜索并提取真实来源，'
            '用沙盒计算增长率及经营现金流/归母净利润，至少生成一张比较图表，提交分析、写作和审核。'
            '缺少数据必须明确，不得编造。按系统要求请求各阶段人工确认。')


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding='utf-8')


async def run(args):
    root = Path(args.output).resolve()
    settings = HostSettings()
    existing = root / 'manifest.json'
    if not existing.exists():
        models = load_models_config(Path('config/models.example.toml')).model_dump()
        deepseek = models['providers'].get('deepseek')
        if isinstance(deepseek, dict):
            deepseek['base_url'] = settings.deepseek_base_url
        # Identity defaults preserve the existing acceptance report and budget.
        save(existing, dict(report_id=root.name, actor_id='local-v2-acceptance', policy_version='v2-live-1',
                            question=QUESTION, token_limit=300000,
                            limits={'model':48,'source':120},
                            timeout=600,
                            models=models,
                            source='financekb', automatic_approval=False))
    manifest = json.loads(existing.read_text('utf-8'))
    config = ModelsConfig.model_validate(manifest['models'])
    if args.action == 'prepare':
        print(json.dumps({'prepared':str(root),'report_id':manifest['report_id'],'paid_calls':0}))
        return
    os.environ['RC_DEEPSEEK_API_KEY'] = settings.deepseek_api_key.get_secret_value()
    if not settings.financekb_base_url or not settings.financekb_api_token:
        raise RuntimeError(
            "RC_FINANCEKB_BASE_URL and RC_FINANCEKB_API_TOKEN are required"
        )
    calls = CallBudget(root / 'calls.json', manifest['limits'])
    async with postgres_runtime(settings.database_url, initialize=True) as (store, checkpointer):
        async with open_models(config) as models, AsyncExitStack() as sources_stack:
            # Record only API payloads, never client state or authentication headers.
            for role, model in models.items():
                original = model.client.chat.completions.create
                async def create(_role=role, _original=original, **kwargs):
                    calls.reserve('model')
                    ident = calls.state['used']['model']
                    save(root / 'requests' / f'{ident:03}-{_role}.json', kwargs)
                    print(f'model_request {ident} {_role}', flush=True)
                    result = await _original(**kwargs)
                    save(root / 'responses' / f'{ident:03}-{_role}.json', result.model_dump())
                    return result
                model.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
            bundle = await sources_stack.enter_async_context(
                open_financekb_sources(
                    settings.financekb_base_url,
                    settings.financekb_api_token.get_secret_value(),
                    ca_bundle=settings.financekb_ca_bundle,
                    call_timeout=settings.financekb_call_timeout_seconds,
                )
            )

            def counted(source):
                original_fetch = source.fetch

                async def fetch(args, _fetch=original_fetch, _name=source.name):
                    calls.reserve('source')
                    try:
                        return await _fetch(args)
                    except Exception as exc:
                        save(
                            root / 'source-errors' / f"{calls.state['used']['source']:03}.json",
                            {
                                'tool': _name,
                                'type': type(exc).__name__,
                                'code': getattr(exc, 'code', None),
                            },
                        )
                        raise

                return replace(source, fetch=fetch)

            source_tools = tuple(counted(source) for source in bundle.tools)
            business = BusinessCoordinator(store)
            services = RoleServices(repository=store, workspace_root=root / 'workspace', business=business)
            factory = SupervisorFactory(models=models, store=store, business=business, checkpointer=checkpointer,
                workspace_root=root / 'workspace', resources_root=bundled_resources(), role_services=services,
                source_tools=source_tools, source_note=bundle.note,
                source_catalog=bundle.catalog, source_contract=bundle.contract)
            store.resume_validator = business.validate_resume
            session = SessionService(store=store, agent_factory=factory, policy_version=manifest['policy_version'],
                on_event=lambda event: print(json.dumps(event,ensure_ascii=False,default=str),flush=True))
            report, actor = manifest['report_id'], manifest['actor_id']
            try:
                if args.action == 'start':
                    record = SessionRecord(report, actor, RUNTIME_VERSION, manifest['policy_version'], report)
                    await store.create(record, TokenBudget(limit=manifest['token_limit'],budget_id=report).snapshot())
                    status = await asyncio.wait_for(session.start(report,actor,manifest['question']),manifest['timeout'])
                elif args.action == 'resume':
                    decisions = json.loads(Path(args.decisions).read_text('utf-8')) if args.decisions else None
                    status = await asyncio.wait_for(session.resume(report,actor,decisions),manifest['timeout'])
                else:
                    status = await session.get_status(report,actor)
                save(root / 'status.json',asdict(status))
                print(json.dumps(asdict(status),ensure_ascii=False,default=str))
            except Exception:
                (root / 'failure.txt').write_text(traceback.format_exc(),encoding='utf-8')
                raise
            finally:
                try:
                    record = await store.load(report,actor)
                    context = session._context(record)
                    save(root / 'control.json',(await store.read_control(context)).model_dump(mode='json'))
                    save(root / 'token-budget.json',(await store.load_budget(context)).snapshot())
                finally:
                    await sources_stack.aclose()


if __name__ == '__main__':
    # Deep Agents' ripgrep pipe follows the interpreter's default encoding.
    # Re-exec before any run/paid request, never restart after an execution error.
    if sys.platform == 'win32' and not sys.flags.utf8_mode:
        os.execv(sys.executable, [sys.executable, '-X', 'utf8', '-m',
                                 'scripts.verify_live', *sys.argv[1:]])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['prepare','start','status','resume'])
    parser.add_argument('--output',default='output/v2-live-20261029-maotai')
    parser.add_argument('--decisions',help='Explicit human decisions keyed by interrupt ID; never synthesized')
    args = parser.parse_args()
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(run(args))
