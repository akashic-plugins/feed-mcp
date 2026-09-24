from __future__ import annotations

from ._tool_contract import TOOLS
from .tools import register_tools

from pydantic import BaseModel

from agent.plugin_composition import (
    MCP_SERVERS,
    RUNTIME_STARTED,
    RUNTIME_STOPPING,
    TIMERS,
    Context,
    McpServerDefinition,
    ServiceKey,
)

from agent.plugin_composition.assets import INSTALLED_ASSETS
from .content_source import CONTENT_SOURCE_ID, ContentSourceServices, FeedContentRuntime


class FeedConfig(BaseModel):
    pass


CONTENT_SOURCE = ServiceKey[ContentSourceServices]("eventmail.content_source.v1")

api_version = 3
name = "feed"
version = "3.1.5"
desc = "由 Timer 驱动的 Feed Content source 与用户 MCP"
Config = FeedConfig
inject = (TOOLS, MCP_SERVERS, TIMERS, INSTALLED_ASSETS)


async def apply(ctx: Context) -> None:
    """注册用户 MCP 工具和一个普通 Timer 驱动的 Content source。"""

    _ = FeedConfig.model_validate(ctx.config)
    _ = await ctx.require(INSTALLED_ASSETS).register(ctx, "skills", "skills")

    # 1. MCP 只拥有用户触发的订阅管理和缓存查询。
    await ctx.require(MCP_SERVERS).register(
        ctx,
        McpServerDefinition(
            name="feed",
            command=("python", "mcp/run_mcp.py"),
            required_tools=("feed_manage", "feed_query"),
            candidate_read_only_tools=(),
            candidate_env={"FEED_BACKEND": "recording"},
        ),
    )

    await register_tools(ctx, description=desc)

    # 2. EventMail 存在时，独立子 Fiber 才启动主动来源。
    async def apply_eventmail(source_ctx: Context) -> None:
        source = source_ctx.require(CONTENT_SOURCE).bind(CONTENT_SOURCE_ID)
        try:
            _ = await source_ctx.effect(lambda: source.close, label="feed-content-source-binding")
        except BaseException:
            source.close()
            raise
        runtime = FeedContentRuntime(
            source_ctx.data_root,
            source_ctx.require(TIMERS),
            source,
        )

        def setup() -> object:
            return runtime.close

        _ = await source_ctx.effect(setup, label="feed-content-source-runtime")
        _ = await source_ctx.on(RUNTIME_STARTED, lambda _: runtime.start())
        _ = await source_ctx.on(RUNTIME_STOPPING, lambda _: runtime.close())

    _ = await ctx.inject(
        (TIMERS, CONTENT_SOURCE),
        apply_eventmail,
        name="feed-eventmail-source",
    )
