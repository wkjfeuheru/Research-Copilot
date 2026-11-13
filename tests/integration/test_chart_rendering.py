"""Real offline Docker rendering gallery for both engines, including CJK labels."""

import json
import pytest
from research_copilot.runtime.control import active_dispatch
from tests.integration.test_searcher import dispatch
from tests.integration.test_sandbox import service
from tests.unit.test_orchestration import context
from tests.unit.test_analysis_scripts import (
    test_reference_charts_produce_data_options_and_png as _reference_cases,
)

pytestmark = pytest.mark.docker


@pytest.mark.asyncio
async def test_real_offline_gallery_for_both_engines(role_env):
    cases = next(
        mark.args[1]
        for mark in _reference_cases.pytestmark
        if mark.name == "parametrize"
    )
    scope = await dispatch(role_env, "analysist", "gallery")
    token = active_dispatch.set(scope)
    try:
        services = role_env[0]
        executor = service(role_env)
        task = services.report_root(context()) / "tasks/gallery"
        task.mkdir(parents=True, exist_ok=True)
        (task / "cases.json").write_text(json.dumps(cases), encoding="utf8")
        (task / "render.py").write_text(
            """import json,os
from pathlib import Path
from charts import render
for kind,data in json.loads(Path('cases.json').read_text()):
    for engine in ['echarts','matplotlib']:
        render(kind=kind,title='研究图表 / '+kind,unit='CNY',data=data,output_dir=str(Path(os.environ['RC_OUTPUT_DIR'])/(engine+'-'+kind)),engine=engine)
print('rendered 20 charts offline')
""",
            encoding="utf8",
        )
        output = await executor.execute(context(), "gallery", "python render.py")
        assert output["status"] == "completed", output
        assert "rendered 20" in output["output"]
        assert "Glyph" not in output["output"]
        from research_copilot.sandbox.contracts import (
            validate_png,
            validate_svg,
            validate_option,
        )

        root = services.report_root(context())
        pngs = [
            root / path.removeprefix("/workspace/")
            for path in output["files"]
            if path.endswith(".png")
        ]
        assert len(pngs) == 20
        for path in pngs:
            validate_png(path.read_bytes(), 1200, 720)
            if (path.parent / "chart.svg").exists():
                validate_svg((path.parent / "chart.svg").read_bytes())
            validate_option(json.loads((path.parent / "option.json").read_bytes()))
    finally:
        active_dispatch.reset(token)
