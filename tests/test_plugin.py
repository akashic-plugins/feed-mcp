from __future__ import annotations

import asyncio
import inspect
from datetime import datetime
from pathlib import Path
from typing import cast

from plugins.tools.plugin import TOOLS, ToolCatalog
from feed_test_plugin.tools import FEED_TOOLS

import pytest

from feed_test_plugin import plugin  # pyright: ignore[reportMissingImports]
from plugins.timer.timer import AsyncioOneShotTimer
from agent.plugin_composition import Context, CompositionRoot, Fiber, PluginRuntime
from plugins.timer.contract import TIMERS
from agent.plugin_composition.tasks import TASKS, PluginTasks
from agent.plugins.composable import ComposablePlugin
from agent.plugins.static_manifest import load_static_plugin_manifest
from plugins.watch.contract import WATCH_EVENTS


ROOT = Path(__file__).resolve().parents[1]


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


class _Watch:
    async def emit(
        self,
        source: str,
        *,
        title: str = "",
        text: str = "",
        url: str = "",
        at: datetime | None = None,
        state: bool = False,
        key: str = "",
    ) -> str:
        raise AssertionError((source, title, text, url, at, state, key))


async def _never_sleep(_seconds: float) -> None:
    """Keep the formal source timer pending until Fiber cleanup cancels it."""

    await asyncio.Future()


def test_pure_v3_exports_and_exact_apply() -> None:
    assert plugin.api_version == 3
    assert plugin.name == "feed"
    assert plugin.version == "3.4.0"
    assert plugin.skill_roots == ("skills",)
    assert tuple(inspect.signature(plugin.apply).parameters) == ("ctx",)


@pytest.mark.asyncio
async def test_apply_registers_native_tools_and_dormant_watch_runtime(
    tmp_path: Path,
) -> None:
    root = CompositionRoot("feed:test")
    await root.context.provide(
        TIMERS,
        AsyncioOneShotTimer(),
    )
    await root.context.provide(WATCH_EVENTS, _Watch())
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

        assert data_dir.is_dir()
        topology = root.topology_view()
        assert topology.listeners == (
            "serial:runtime.started:feed/feed-watch-source",
            "serial:runtime.stopping:feed/feed-watch-source",
        )
        assert [ref.name for ref in root.context.require(FEED_TOOLS).refs] == ["feed_manage", "feed_query"]
    finally:
        await tasks.close()
        await tools_fiber.dispose()
        await root.dispose()


@pytest.mark.asyncio
async def test_apply_keeps_native_tools_without_watch(tmp_path: Path) -> None:
    root = CompositionRoot("feed:without-watch")
    await root.context.provide(TIMERS, AsyncioOneShotTimer())
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
                generation_id="feed:without-watch",
                plugin_dir=ROOT,
                data_dir=tmp_path / "plugin-data",
                workspace=tmp_path / "workspace",
                config=plugin.FeedConfig(),
            ),
        )

        assert [ref.name for ref in root.context.require(FEED_TOOLS).refs] == ["feed_manage", "feed_query"]
    finally:
        await tasks.close()
        await tools_fiber.dispose()
        await root.dispose()


def test_static_manifest_reads_identity_and_runtime_requirements() -> None:
    manifest = load_static_plugin_manifest(ROOT)

    assert manifest.name == "feed"
    assert manifest.version == plugin.version == "3.4.0"
    assert manifest.api_version == 3
    assert manifest.requirements == (
        "mcp/requirements.txt",
        "tests/fixtures/legacy_feed_owner/mcp/requirements.txt",
    )
