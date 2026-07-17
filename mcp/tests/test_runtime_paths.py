from __future__ import annotations

import asyncio
import sqlite3
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


def test_concurrent_legacy_connections_share_one_schema_migration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AKA_PLUGIN_DATA_DIR", str(tmp_path))
    config = feed_backend.load_config()

    # 1. 建立真实旧表，让所有连接都必须走 ADD COLUMN 迁移
    config.db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(config.db_path) as connection:
        connection.execute("""
            CREATE TABLE items (
                event_id TEXT PRIMARY KEY,
                source_id TEXT NOT NULL,
                source_name TEXT NOT NULL,
                source_type TEXT NOT NULL,
                title TEXT,
                content TEXT NOT NULL,
                url TEXT,
                published_at TEXT,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                emitted_at TEXT,
                content_hash TEXT NOT NULL
            )
            """)

    # 2. 并发模拟 lifespan poller 与首个 MCP 调用同时启动
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

    # 3. 每个连接都必须看到同一个完整迁移终态
    required = {"author", "interest_ok", "interest_scored_at"}
    assert all(required <= schema for schema in schemas)
