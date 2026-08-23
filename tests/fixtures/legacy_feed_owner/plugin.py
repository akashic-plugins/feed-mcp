from __future__ import annotations

from agent.plugin_composition import MCP_SERVERS, Context, McpServerDefinition


api_version = 3
name = "feed"
version = "3.0.0"
desc = "旧 lifespan 轮询 owner 换班 fixture"
inject = (MCP_SERVERS,)
skill_roots = ()


async def apply(ctx: Context, config: object) -> None:
    """注册一个由 lifespan 拥有后台轮询的旧 MCP。"""

    _ = config
    await ctx.require(MCP_SERVERS).register(
        ctx,
        McpServerDefinition(
            name="feed",
            command=("python", "mcp/run_mcp.py"),
            required_tools=("legacy_status",),
            candidate_read_only_tools=("legacy_status",),
        ),
    )
