from __future__ import annotations

import inspect
from pathlib import Path

import pytest
import plugin
from agent.plugin_composition import (
    MCP_SERVERS,
    PROACTIVE_COMPONENTS,
    CompositionRoot,
    PluginProactiveComponents,
    PluginRuntime,
)
from agent.plugin_composition.mcp_slots import (
    PluginMcpServers,
    _freeze_plugin_mcp_servers,
)
from agent.plugin_composition.proactive import _freeze_plugin_proactive_components
from agent.plugins.static_manifest import load_static_plugin_manifest
from agent.plugins.composable import ComposablePlugin
from agent.plugins.manager import _copy_validation_data
from plugin import FeedConfig, FeedProactiveConfig


ROOT = Path(__file__).resolve().parents[1]


def test_pure_v3_exports_and_exact_apply() -> None:
    assert plugin.api_version == 3
    assert plugin.name == "feed"
    assert plugin.version == "3.0.0"
    assert plugin.skill_roots == ("skills",)
    assert tuple(inspect.signature(plugin.apply).parameters) == ("ctx", "config")
    assert ComposablePlugin.from_module(plugin).skill_roots == ("skills",)


@pytest.mark.asyncio
async def test_apply_registers_mcp_and_source_without_data_writes(tmp_path: Path) -> None:
    root = CompositionRoot("feed:test")
    servers = PluginMcpServers(root.instance_token)
    components = PluginProactiveComponents(root.instance_token)
    await root.context.provide(MCP_SERVERS, servers)
    await root.context.provide(PROACTIVE_COMPONENTS, components)
    data_dir = tmp_path / "plugin-data"
    await root.mount(
        ComposablePlugin.from_module(plugin),
        name="feed",
        runtime=PluginRuntime(
            plugin_id="feed",
            plugin_dir=ROOT,
            data_dir=data_dir,
            workspace=tmp_path / "workspace",
            config=FeedConfig(
                proactive=FeedProactiveConfig(enabled=True),
            ),
        ),
    )

    mcp = _freeze_plugin_mcp_servers(servers, root.instance_token)["feed"].definition
    source = _freeze_plugin_proactive_components(
        components,
        root.instance_token,
        {"feed": "feed:test"},
    ).source("subscriptions")
    assert mcp.candidate_env == {"FEED_BACKEND": "recording"}
    assert mcp.candidate_read_only_tools == ("get_proactive_events",)
    assert source is not None
    assert source.definition.channels == ("content",)
    assert source.definition.mcp_server == "feed"
    assert not data_dir.exists()
    await root.dispose()


@pytest.mark.asyncio
async def test_disabled_proactive_omits_source(tmp_path: Path) -> None:
    root = CompositionRoot("feed:disabled")
    servers = PluginMcpServers(root.instance_token)
    components = PluginProactiveComponents(root.instance_token)
    await root.context.provide(MCP_SERVERS, servers)
    await root.context.provide(PROACTIVE_COMPONENTS, components)
    await root.mount(
        ComposablePlugin.from_module(plugin),
        name="feed",
        runtime=PluginRuntime(
            plugin_id="feed",
            plugin_dir=ROOT,
            data_dir=tmp_path / "plugin-data",
            workspace=tmp_path / "workspace",
            config=FeedConfig(
                proactive=FeedProactiveConfig(enabled=False),
            ),
        ),
    )
    catalog = _freeze_plugin_proactive_components(
        components,
        root.instance_token,
        {"feed": "feed:disabled"},
    )
    assert catalog.sources == {}
    await root.dispose()


def test_static_manifest_freezes_recording_and_data_exclusions() -> None:
    manifest = load_static_plugin_manifest(Path(__file__).resolve().parents[1])

    assert manifest.name == "feed"
    assert manifest.version == "3.0.0"
    assert manifest.api_version == 3
    assert manifest.requirements == ("mcp/requirements.txt",)
    assert manifest.exclude_data_paths == (
        "feed_mcp.sqlite3",
        "feed_mcp.sqlite3-wal",
        "feed_mcp.sqlite3-shm",
        "source_scores.json",
        "feed_cache.db",
        "feed_cache.db-wal",
        "feed_cache.db-shm",
        ".feed-v2-migration.json",
    )
    assert len(manifest.mcp_servers) == 1
    server = manifest.mcp_servers[0]
    assert server.required_tools == ("get_proactive_events", "acknowledge_events")
    assert server.candidate_read_only_tools == ("get_proactive_events",)
    assert server.candidate_env == (("FEED_BACKEND", "recording"),)


def test_candidate_copy_excludes_sqlite_and_sidecars(tmp_path: Path) -> None:
    manifest = load_static_plugin_manifest(ROOT)
    source = tmp_path / "workspace" / "plugin-data" / "feed-builtin"
    source.mkdir(parents=True)
    for name in (
        "feed_mcp.sqlite3",
        "feed_mcp.sqlite3-wal",
        "feed_mcp.sqlite3-shm",
        "feed_cache.db",
        "feed_cache.db-wal",
        "feed_cache.db-shm",
        "source_scores.json",
        ".feed-v2-migration.json",
    ):
        (source / name).write_text(f"secret:{name}", encoding="utf-8")
    (source / "candidate-visible.txt").write_text("visible", encoding="utf-8")
    target = tmp_path / "validation" / "feed"

    inventory = _copy_validation_data(  # pyright: ignore[reportPrivateUsage]
        source,
        target,
        manifest.exclude_data_paths,
    )

    assert inventory == ("candidate-visible.txt",)
    assert (target / "candidate-visible.txt").read_text(encoding="utf-8") == "visible"
