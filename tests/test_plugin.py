from __future__ import annotations

import inspect
from pathlib import Path
from typing import cast

import pytest

from feed_test_plugin import plugin
from agent.control.timer import OneShotTimer
from agent.plugin_composition import (
    MCP_SERVERS,
    TIMERS,
    CompositionRoot,
    PluginRuntime,
    PluginTimers,
)
from agent.plugin_composition.mcp_slots import (
    PluginMcpServers,
    _freeze_plugin_mcp_servers,
)
from agent.plugins.composable import ComposablePlugin
from agent.plugins.manager import _copy_validation_data
from agent.plugins.static_manifest import load_static_plugin_manifest
from feed_test_plugin.content_source import BoundContentSource, ContentSourceServices


ROOT = Path(__file__).resolve().parents[1]


class _Content:
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
    assert plugin.version == "3.1.0"
    assert plugin.skill_roots == ("skills",)
    assert tuple(inspect.signature(plugin.apply).parameters) == ("ctx", "config")
    assert ComposablePlugin.from_module(plugin).skill_roots == ("skills",)
    assert "content.source.v1" in inspect.getsource(plugin)


@pytest.mark.asyncio
async def test_apply_registers_user_mcp_and_dormant_content_runtime(
    tmp_path: Path,
) -> None:
    root = CompositionRoot("feed:test")
    servers = PluginMcpServers(root.instance_token)
    sources = _Sources()
    await root.context.provide(MCP_SERVERS, servers)
    await root.context.provide(
        TIMERS,
        PluginTimers(cast(OneShotTimer, object())),
    )
    await root.context.provide(plugin.CONTENT_SOURCE, sources)
    data_dir = tmp_path / "plugin-data"
    await root.mount(
        ComposablePlugin.from_module(plugin),
        name="feed",
        runtime=PluginRuntime(
            plugin_id="feed",
            plugin_dir=ROOT,
            data_dir=data_dir,
            workspace=tmp_path / "workspace",
            config=plugin.FeedConfig(),
        ),
    )

    mcp = _freeze_plugin_mcp_servers(servers, root.instance_token)["feed"].definition
    assert mcp.required_tools == ("feed_manage", "feed_query")
    assert mcp.candidate_read_only_tools == ()
    assert mcp.candidate_env == {"FEED_BACKEND": "recording"}
    assert sources.bound == ["feed-subscriptions"]
    assert not data_dir.exists()
    topology = root.topology_view()
    assert topology.listeners == (
        "serial:runtime.started:feed",
        "serial:runtime.stopping:feed",
    )
    await root.dispose()


def test_static_manifest_freezes_tools_and_data_exclusions() -> None:
    manifest = load_static_plugin_manifest(ROOT)

    assert manifest.name == "feed"
    assert manifest.version == "3.1.0"
    assert manifest.api_version == 3
    assert manifest.requirements == ("mcp/requirements.txt",)
    assert "feed_mcp.sqlite3" in manifest.exclude_data_paths
    assert "feed_mcp.runtime.log.3" in manifest.exclude_data_paths
    assert "feed_source.runtime.log.3" in manifest.exclude_data_paths
    server = manifest.mcp_servers[0]
    assert server.required_tools == ("feed_manage", "feed_query")
    assert server.candidate_read_only_tools == ()
    assert server.candidate_env == (("FEED_BACKEND", "recording"),)


def test_candidate_copy_excludes_sqlite_logs_and_sidecars(tmp_path: Path) -> None:
    manifest = load_static_plugin_manifest(ROOT)
    source = tmp_path / "workspace" / "plugin-data" / "feed-builtin"
    source.mkdir(parents=True)
    for name in manifest.exclude_data_paths:
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"formal:{name}", encoding="utf-8")
    (source / "candidate-visible.txt").write_text("visible", encoding="utf-8")
    target = tmp_path / "validation" / "feed"

    inventory = _copy_validation_data(  # pyright: ignore[reportPrivateUsage]
        source,
        target,
        manifest.exclude_data_paths,
    )

    assert inventory == ("candidate-visible.txt",)
    assert (target / "candidate-visible.txt").read_text() == "visible"
