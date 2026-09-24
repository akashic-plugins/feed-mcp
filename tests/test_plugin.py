from __future__ import annotations

import inspect
from pathlib import Path
from typing import cast

from plugins.tools.plugin import TOOLS, ToolCatalog
from agent.plugin_composition.tasks import TaskAdmission
from feed_test_plugin.tools import FEED_TOOLS

import pytest

from feed_test_plugin import plugin  # pyright: ignore[reportMissingImports]
from agent.control.timer import AsyncioOneShotTimer
from agent.plugin_composition import (
    MCP_SERVERS,
    TIMERS,
    CompositionRoot,
    PluginRuntime,
    PluginTimers,
)
from agent.host_bridge.plugin_execution import CodeOwner, ExecutionAccess
from agent.plugin_composition.assets import INSTALLED_ASSETS
from agent.plugin_composition.execution import EXECUTION
from plugins.assets.plugin import Assets
from plugins.mcp.plugin import McpServers
from agent.plugins.composable import ComposablePlugin
from agent.plugins.static_manifest import load_static_plugin_manifest
from feed_test_plugin.content_source import (  # pyright: ignore[reportMissingImports]
    BoundContentSource,
    ContentSourceServices,
)


ROOT = Path(__file__).resolve().parents[1]


class _Content:
    def close(self) -> None:
        return None

    def submit(self, batch_id, items):
        raise AssertionError((batch_id, items))

    def unsettled(self, limit=100):
        raise AssertionError(limit)

    def ack(self, settlement_ref):
        raise AssertionError(settlement_ref)


class _Sources:
    def __init__(self) -> None:
        self.bound: list[str] = []

    def bind(self, source_id: str) -> BoundContentSource:
        self.bound.append(source_id)
        return cast(BoundContentSource, _Content())


def test_pure_v3_exports_and_exact_apply() -> None:
    assert plugin.api_version == 3
    assert plugin.name == "feed"
    assert plugin.version == "3.1.5"
    assert tuple(inspect.signature(plugin.apply).parameters) == ("ctx",)
    assert ComposablePlugin.from_module(
        plugin, load_static_plugin_manifest(ROOT),
    ).inject == plugin.inject
    assert "eventmail.content_source.v1" in inspect.getsource(plugin)


@pytest.mark.asyncio
async def test_apply_registers_user_mcp_and_content_runtime(
    tmp_path: Path,
) -> None:
    root = CompositionRoot("feed:test")
    servers = McpServers(root.context)
    sources = _Sources()
    await root.context.provide(MCP_SERVERS, servers)
    await root.context.provide(INSTALLED_ASSETS, Assets(root.context))
    await root.context.provide(EXECUTION, ExecutionAccess(
        root.instance_token,
        {"feed": CodeOwner("feed:test", ROOT, lambda command, cwd: command)},
        candidate=True,
    ))
    await root.context.provide(TOOLS, ToolCatalog(root.context, cast(TaskAdmission, None)))
    await root.context.provide(
        TIMERS,
        PluginTimers(AsyncioOneShotTimer()),
    )
    await root.context.provide(plugin.CONTENT_SOURCE, sources)
    data_dir = tmp_path / "plugin-data"
    composable = ComposablePlugin.from_module(plugin, load_static_plugin_manifest(ROOT))
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
            config=plugin.FeedConfig().model_dump(mode="json"),
        ),
    )

    mcp = servers._entries["feed"].definition
    assert mcp.required_tools == ("feed_manage", "feed_query")
    assert mcp.candidate_read_only_tools == ()
    assert mcp.candidate_env == {"FEED_BACKEND": "recording"}
    assert sources.bound == ["feed-subscriptions"]
    assert data_dir.is_dir()
    topology = root.topology_view()
    assert topology.listeners == (
        "serial:runtime.started:feed-eventmail-source",
        "serial:runtime.stopping:feed-eventmail-source",
    )
    assert any(item["name"].startswith("mcp_feed__") for item in (ref.description for ref in root.context.require(FEED_TOOLS).refs))
    await root.dispose()


@pytest.mark.asyncio
async def test_apply_keeps_user_mcp_without_eventmail(tmp_path: Path) -> None:
    root = CompositionRoot("feed:without-eventmail")
    servers = McpServers(root.context)
    await root.context.provide(MCP_SERVERS, servers)
    await root.context.provide(INSTALLED_ASSETS, Assets(root.context))
    await root.context.provide(EXECUTION, ExecutionAccess(
        root.instance_token,
        {"feed": CodeOwner("feed:without-eventmail", ROOT, lambda command, cwd: command)},
        candidate=True,
    ))
    await root.context.provide(TOOLS, ToolCatalog(root.context, cast(TaskAdmission, None)))
    await root.context.provide(TIMERS, PluginTimers.candidate_validation())
    composable = ComposablePlugin.from_module(plugin, load_static_plugin_manifest(ROOT))
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
            config=plugin.FeedConfig().model_dump(mode="json"),
        ),
    )

    assert "feed" in servers._entries
    assert any(item["name"].startswith("mcp_feed__") for item in (ref.description for ref in root.context.require(FEED_TOOLS).refs))
    await root.dispose()


def test_static_manifest_freezes_tools_and_data_exclusions() -> None:
    manifest = load_static_plugin_manifest(ROOT)

    assert manifest.name == "feed"
    assert manifest.version == plugin.version == "3.1.5"
    assert manifest.api_version == 3
    assert "mcp/requirements.txt" in manifest.requirements
