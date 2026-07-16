from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from plugin import FeedPlugin
from run_mcp import _runtime_dir
from src import feed_backend
from src.feed_backend import _runtime_root, load_config


def test_runtime_entrypoints_reject_missing_data_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AKA_PLUGIN_DATA_DIR", "   ")
    with pytest.raises(RuntimeError, match="AKA_PLUGIN_DATA_DIR"):
        _runtime_dir()
    with pytest.raises(RuntimeError, match="AKA_PLUGIN_DATA_DIR"):
        _runtime_root()
    with pytest.raises(RuntimeError, match="AKA_PLUGIN_DATA_DIR"):
        load_config()


def test_initialize_rejects_missing_context_paths(tmp_path: Path) -> None:
    plugin = FeedPlugin()
    plugin.context = SimpleNamespace(data_dir=None, workspace=tmp_path)
    with pytest.raises(RuntimeError, match="数据目录"):
        asyncio.run(plugin.initialize())

    plugin.context = SimpleNamespace(data_dir=tmp_path, workspace=None)
    with pytest.raises(RuntimeError, match="workspace"):
        asyncio.run(plugin.initialize())


def test_concurrent_fresh_connections_share_one_schema_migration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AKA_PLUGIN_DATA_DIR", str(tmp_path))
    config = feed_backend.load_config()

    def connect_once() -> set[str]:
        connection = feed_backend._connect(config)
        try:
            return {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(items)").fetchall()
            }
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=8) as executor:
        schemas = list(executor.map(lambda _: connect_once(), range(24)))

    required = {"author", "interest_ok", "interest_scored_at"}
    assert all(required <= schema for schema in schemas)
