from __future__ import annotations

from pydantic import BaseModel, Field

from agent.plugin_composition import (
    MCP_SERVERS,
    PROACTIVE_COMPONENTS,
    Context,
    McpServerDefinition,
    ProactiveSourceDefinition,
)


class FeedProactiveConfig(BaseModel):
    enabled: bool = True


class FeedConfig(BaseModel):
    proactive: FeedProactiveConfig = Field(default_factory=FeedProactiveConfig)


api_version = 3
name = "feed"
version = "3.0.0"
desc = "Feed MCP plugin"
Config = FeedConfig
inject = (MCP_SERVERS, PROACTIVE_COMPONENTS)
skill_roots = ("skills",)


async def apply(ctx: Context, config: object) -> None:
    """注册 Feed MCP 与可选的订阅主动事件源。"""

    if not isinstance(config, FeedConfig):
        raise TypeError("feed config 必须是 FeedConfig")

    # 1. 只声明 MCP；apply 本身不启动进程、不访问网络或插件数据。
    await ctx.require(MCP_SERVERS).register(
        ctx,
        McpServerDefinition(
            name="feed",
            command=("python", "mcp/run_mcp.py"),
            required_tools=("get_proactive_events", "acknowledge_events"),
            candidate_read_only_tools=("get_proactive_events",),
            candidate_env={"FEED_BACKEND": "recording"},
        ),
    )

    # 2. 主动能力由用户配置决定是否发布。
    if config.proactive.enabled:
        await ctx.require(PROACTIVE_COMPONENTS).register(
            ctx,
            ProactiveSourceDefinition(
                name="subscriptions",
                channels=("content",),
                mcp_server="feed",
                fetch_tool="get_proactive_events",
                ack_tool="acknowledge_events",
                fetch_page_size=50,
            ),
        )
