from __future__ import annotations

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

from .content_source import CONTENT_SOURCE_ID, ContentSourceServices, FeedContentRuntime


class FeedConfig(BaseModel):
    pass


CONTENT_SOURCE = ServiceKey[ContentSourceServices]("content.source.v1")

api_version = 3
name = "feed"
version = "3.1.2"
desc = "由 Timer 驱动的 Feed Content source 与用户 MCP"
Config = FeedConfig
inject = (MCP_SERVERS, TIMERS, CONTENT_SOURCE)
skill_roots = ("skills",)


async def apply(ctx: Context, config: object) -> None:
    """注册用户 MCP 工具和一个普通 Timer 驱动的 Content source。"""

    if not isinstance(config, FeedConfig):
        raise TypeError("feed config 必须是 FeedConfig")

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

    # 2. 正式 Root 独占外部轮询与 Content ACK。
    runtime = FeedContentRuntime(
        ctx.data_root,
        ctx.require(TIMERS),
        ctx.require(CONTENT_SOURCE).bind(CONTENT_SOURCE_ID),
    )

    def setup() -> object:
        return runtime.close

    _ = await ctx.effect(setup, label="feed-content-source-runtime")
    _ = await ctx.on(RUNTIME_STARTED, lambda _: runtime.start())
    _ = await ctx.on(RUNTIME_STOPPING, lambda _: runtime.close())
