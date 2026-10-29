from __future__ import annotations

from plugins.tools.contract import TOOLS
from .tools import register_tools

from pydantic import BaseModel

from agent.plugin_composition import RUNTIME_STARTED, RUNTIME_STOPPING, Context
from plugins.timer.contract import TIMERS

from .content_source import CONTENT_SOURCE_ID, FeedContentRuntime
from .feed_runtime.events import ScriptOutcome, failed
from .feed_runtime.scripts import ScriptSource
from .script_runner import FEED_SCRIPTS, ScriptRunner
from .script_tool import register_script_tool
from plugins.host_execution.contract import PROCESSES


class FeedConfig(BaseModel):
    pass


from plugins.eventmail.contract import EVENTMAIL_CONTENT_SOURCE

api_version = 3
name = "feed"
version = "3.3.2"
desc = "订阅 RSS 与脚本来源，定时抓取为格式统一的事件并投递"
inject = (TOOLS, TIMERS)
skill_roots = ("skills",)


async def apply(ctx: Context) -> None:
    """注册 Feed 工具组、Timer 驱动的 Content source 与宿主脚本执行。"""
    _ = FeedConfig.model_validate(ctx.config)

    # 1. 订阅管理与缓存查询是原生工具，直接读写本插件数据目录。
    await register_tools(ctx, description=desc)

    # 2. EventMail 存在时，独立子 Fiber 才启动主动来源。
    async def apply_eventmail(source_ctx: Context) -> None:
        content = source_ctx.require(EVENTMAIL_CONTENT_SOURCE).bind(CONTENT_SOURCE_ID)
        _ = await source_ctx.effect(
            lambda: content.close,
            label="feed-content-source-binding",
        )

        async def run_script(source: ScriptSource) -> ScriptOutcome:
            with source_ctx.borrow(FEED_SCRIPTS) as runner:
                if runner is None:
                    return failed("unavailable", "宿主进程服务未就绪，脚本来源暂不执行")
                return await runner.run(owner=f"feed-source:{source.source_id}", code=source.code)

        runtime = FeedContentRuntime(
            source_ctx.data_root,
            source_ctx.require(TIMERS),
            content,
            run_script=run_script,
        )
        _ = await source_ctx.effect(
            lambda: runtime.close,
            label="feed-content-source-runtime",
        )
        _ = await source_ctx.on(RUNTIME_STARTED, lambda _: runtime.start())
        _ = await source_ctx.on(RUNTIME_STOPPING, lambda _: runtime.close())

    _ = await ctx.inject(
        (TIMERS, EVENTMAIL_CONTENT_SOURCE),
        apply_eventmail,
        name="feed-eventmail-source",
    )

    # 3. 宿主进程服务存在时，独立子 Fiber 提供脚本执行与 feed_script 工具；缺席不影响 RSS。
    async def apply_scripts(scripts_ctx: Context) -> None:
        runner = ScriptRunner(scripts_ctx, scripts_ctx.require(PROCESSES))
        _ = await scripts_ctx.provide(FEED_SCRIPTS, runner)
        _ = await register_script_tool(scripts_ctx, runner)

    _ = await ctx.inject((TOOLS, PROCESSES), apply_scripts, name="feed-scripts")
