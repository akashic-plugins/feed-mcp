from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from typing import cast

from plugins.tools.plugin import TOOLS, ToolCatalog
from feed_test_plugin.tools import FEED_TOOLS

import pytest

from feed_test_plugin import plugin  # pyright: ignore[reportMissingImports]
from agent.control.timer import AsyncioOneShotTimer
from agent.plugin_composition import (
    MCP_SERVERS,
    TIMERS,
    Context,
    CompositionRoot,
    Fiber,
    PluginRuntime,
    PluginTimers,
)
from agent.plugin_composition.mcp_slots import McpServerDefinition, McpServers
from agent.plugin_composition.tasks import TASKS, PluginTasks
from agent.plugins.composable import ComposablePlugin
from agent.plugins.static_manifest import load_static_plugin_manifest
from feed_test_plugin.content_source import (  # pyright: ignore[reportMissingImports]
    BoundContentSource,
    ContentSourceServices,
)


ROOT = Path(__file__).resolve().parents[1]


class _RecordingMcpServers:
    """Capture declarations; manager integration tests exercise the real provider."""

    def __init__(self) -> None:
        self.definitions: dict[str, McpServerDefinition] = {}

    async def register(self, _ctx: object, definition: McpServerDefinition) -> None:
        self.definitions[definition.name] = definition


async def _mount_tools(
    root: CompositionRoot, tmp_path: Path,
) -> tuple[PluginTasks, Fiber]:
    """Provide the real tools owner with its required task admission."""

    tasks = PluginTasks()
    await root.context.provide(TASKS, tasks)

    async def apply(ctx: Context) -> None:
        catalog = ToolCatalog(ctx, ctx.require(TASKS).open(ctx))
        _ = await ctx.provide(TOOLS, catalog)

    fiber = await root.mount(
        apply,
        name="tools-provider",
        inject=(TASKS,),
        runtime=PluginRuntime(
            plugin_id="tools-provider",
            generation_id="tools-provider:test",
            plugin_dir=ROOT,
            data_dir=tmp_path / "tools-data",
            workspace=tmp_path / "workspace",
            config={},
        ),
    )
    return tasks, fiber


class _Content:
    def __init__(self) -> None:
        self.closed = 0

    def close(self) -> None:
        self.closed += 1

    def submit(self, batch_id, items):
        raise AssertionError((batch_id, items))

    def unsettled(self, limit=100):
        raise AssertionError(limit)

    def ack(self, settlement_ref):
        raise AssertionError(settlement_ref)


class _Sources:
    def __init__(self) -> None:
        self.bound: list[str] = []
        self.content = _Content()

    def bind(self, source_id: str) -> BoundContentSource:
        self.bound.append(source_id)
        return cast(BoundContentSource, self.content)


async def _never_sleep(_seconds: float) -> None:
    """Keep the formal source timer pending until Fiber cleanup cancels it."""

    await asyncio.Future()


def test_pure_v3_exports_and_exact_apply() -> None:
    assert plugin.api_version == 3
    assert plugin.name == "feed"
    assert plugin.version == "3.1.5"
    assert plugin.skill_roots == ("skills",)
    assert tuple(inspect.signature(plugin.apply).parameters) == ("ctx",)
    assert "eventmail.content_source.v1" in inspect.getsource(plugin)


@pytest.mark.asyncio
async def test_apply_registers_user_mcp_and_dormant_content_runtime(
    tmp_path: Path,
) -> None:
    root = CompositionRoot("feed:test")
    servers = _RecordingMcpServers()
    sources = _Sources()
    await root.context.provide(MCP_SERVERS, cast(McpServers, servers))
    await root.context.provide(
        TIMERS,
        PluginTimers(AsyncioOneShotTimer(sleeper=_never_sleep)),
    )
    await root.context.provide(plugin.CONTENT_SOURCE, sources)
    data_dir = tmp_path / "plugin-data"
    tasks, tools_fiber = await _mount_tools(root, tmp_path)
    try:
        composable = ComposablePlugin.from_module(
            plugin, load_static_plugin_manifest(ROOT)
        )
        await root.mount(
            composable.apply,
            name="feed",
            inject=composable.inject,
            runtime=PluginRuntime(
                plugin_id="feed",
                generation_id="feed:test",
                plugin_dir=ROOT,
                data_dir=data_dir,
                workspace=tmp_path / "workspace",
                config=plugin.FeedConfig(),
            ),
        )

        mcp = servers.definitions["feed"]
        assert mcp.required_tools == ("feed_manage", "feed_query")
        assert mcp.candidate_read_only_tools == ()
        assert mcp.candidate_env == {"FEED_BACKEND": "recording"}
        assert sources.bound == ["feed-subscriptions"]
        assert data_dir.is_dir()
        topology = root.topology_view()
        assert topology.listeners == (
            "serial:runtime.started:feed/feed-eventmail-source",
            "serial:runtime.stopping:feed/feed-eventmail-source",
        )
        assert any(item["name"].startswith("mcp_feed__") for item in (ref.description for ref in root.context.require(FEED_TOOLS).refs))
    finally:
        await tasks.close()
        await tools_fiber.dispose()
        await root.dispose()
    assert sources.content.closed == 1


@pytest.mark.asyncio
async def test_apply_keeps_user_mcp_without_eventmail(tmp_path: Path) -> None:
    root = CompositionRoot("feed:without-eventmail")
    servers = _RecordingMcpServers()
    await root.context.provide(MCP_SERVERS, cast(McpServers, servers))
    await root.context.provide(TIMERS, PluginTimers.candidate_validation())
    tasks, tools_fiber = await _mount_tools(root, tmp_path)
    try:
        composable = ComposablePlugin.from_module(
            plugin, load_static_plugin_manifest(ROOT)
        )
        await root.mount(
            composable.apply,
            name="feed",
            inject=composable.inject,
            runtime=PluginRuntime(
                plugin_id="feed",
                generation_id="feed:without-eventmail",
                plugin_dir=ROOT,
                data_dir=tmp_path / "plugin-data",
                workspace=tmp_path / "workspace",
                config=plugin.FeedConfig(),
            ),
        )

        assert "feed" in servers.definitions
        assert any(item["name"].startswith("mcp_feed__") for item in (ref.description for ref in root.context.require(FEED_TOOLS).refs))
    finally:
        await tasks.close()
        await tools_fiber.dispose()
        await root.dispose()


def test_static_manifest_reads_identity_and_runtime_requirements() -> None:
    manifest = load_static_plugin_manifest(ROOT)

    assert manifest.name == "feed"
    assert manifest.version == plugin.version == "3.1.5"
    assert manifest.api_version == 3
    assert manifest.requirements == (
        "mcp/requirements.txt",
        "tests/fixtures/legacy_feed_owner/mcp/requirements.txt",
    )
