from __future__ import annotations

import shutil
import sqlite3
import sys
from pathlib import Path

import pytest
from agent.plugin_composition.proactive import FetchEmpty
from agent.plugins.generation_activity_host import ActivityHost
from agent.plugins.generation_proactive_host import (
    ProactiveActivityAdapter,
    ProactiveRuntimeBinding,
)
from agent.plugins.manager import PluginManager
from bus.event_bus import EventBus


ROOT = Path(__file__).resolve().parents[1]


def _stage_plugin(tmp_path: Path) -> Path:
    """复制可执行插件，并复用当前测试解释器的依赖环境。"""

    source = tmp_path / "plugins" / "feed"
    shutil.copytree(
        ROOT,
        source,
        ignore=shutil.ignore_patterns(
            ".git",
            ".akashic-core",
            ".pytest_cache",
            "__pycache__",
            "tests",
        ),
    )
    runtime = Path(sys.executable).parent.parent
    (source / "mcp" / ".venv").symlink_to(runtime, target_is_directory=True)
    return source


@pytest.mark.asyncio
async def test_manager_boots_formal_feed_fetches_empty_and_drains(
    tmp_path: Path,
) -> None:
    """走真实 stdio 与 exact source lease，并证明空库运行不访问外部 Feed。"""

    # 1. staging 中没有订阅源，正式 poller 只会初始化临时数据库。
    plugin_root = _stage_plugin(tmp_path)
    workspace = tmp_path / "workspace"
    manager = PluginManager(
        plugin_dirs=[plugin_root.parent],
        event_bus=EventBus(),
        tool_registry=None,
        workspace=workspace,
        installed_cache_root=tmp_path / "cache",
    )
    adapter = ProactiveActivityAdapter(manager.composition_generation_host)
    activity = ActivityHost((adapter,))
    manager.bind_activity_host(activity)

    # 2. 通过 committed Activity binding 调用真实 MCP source。
    snapshot = None
    generation_id = None
    lease = None
    try:
        await manager.load_all()
        snapshot = manager.current_snapshot
        assert snapshot is not None and snapshot.mcp_server_registry is not None
        generation = next(iter(snapshot.generations.values()))
        generation_id = generation.generation_id
        runtime = manager.composition_generation_host.get(generation_id)
        assert runtime is not None and runtime.mode == "formal"
        assert runtime.mcp is not None and runtime.mcp.state == "ready"
        assert "get_proactive_events" in runtime.mcp.server("feed").tool_names

        binding = activity.active
        assert binding is not None
        proactive = binding.child_bindings["proactive_components"]
        assert isinstance(proactive, ProactiveRuntimeBinding)
        lease = manager.snapshot_store.lease(snapshot.snapshot_id)
        result = await proactive.source("subscriptions").fetch(lease)
        assert isinstance(result, FetchEmpty)
    finally:
        if lease is not None:
            await lease.release()
        await manager.terminate_all()

    # 3. formal SQLite 完整，terminate 后 runtime、Root 与 activity 全释放。
    database_path = workspace / "plugin-data" / "feed-builtin" / "feed_mcp.sqlite3"
    with sqlite3.connect(database_path) as database:
        assert database.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert activity.active is None
    assert manager.composition_generation_host.get(generation_id) is None
    assert snapshot is not None and snapshot.composition_root is not None
    assert snapshot.composition_root.receipt().effects == ()
    assert snapshot.composition_root.topology_view().listeners == ()
