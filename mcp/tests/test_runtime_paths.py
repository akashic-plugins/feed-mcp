from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from feed_test_plugin import plugin
from run_mcp import _runtime_dir
from feed_runtime import backend as feed_backend
from feed_runtime.backend import _config_path, _runtime_root, load_config


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


def test_v3_module_keeps_skill_root_and_identity_exports() -> None:
    assert plugin.api_version == 3
    assert plugin.name == "feed"
    assert plugin.version == "3.1.4"
    assert plugin.skill_roots == ("skills",)
    assert _config_path() == Path(__file__).resolve().parents[1] / "feed_mcp.json"


def test_feed_manage_pause_resume_and_update_preserve_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AKA_PLUGIN_DATA_DIR", str(tmp_path))
    assert "已订阅" in feed_backend.feed_manage(
        action="subscribe",
        name="OpenAI Blog",
        url="http://localhost:1200/openai/news",
    )
    config = feed_backend.load_config()
    connection = feed_backend._connect(config)
    try:
        source = connection.execute(
            "SELECT id FROM sources WHERE name = 'OpenAI Blog'"
        ).fetchone()
        assert source is not None
        source_id = str(source["id"])
        connection.execute(
            """
            INSERT INTO items (
                event_id, source_id, source_name, source_type, title, content,
                first_seen_at, last_seen_at, content_hash
            ) VALUES ('event-1', ?, 'OpenAI Blog', 'rss', 'title', 'content',
                      '2026-08-29T00:00:00+00:00', '2026-08-29T00:00:00+00:00', 'hash')
            """,
            (source_id,),
        )
        connection.execute(
            """
            INSERT INTO acked_items (event_id, acked_at, expires_at)
            VALUES ('event-1', '2026-08-29T00:00:00+00:00', '2026-08-30T00:00:00+00:00')
            """
        )
        connection.execute(
            """
            INSERT INTO poll_state (source_id, last_polled_at, last_success_at, last_error)
            VALUES (?, 'old-poll', 'old-success', 'old-error')
            """,
            (source_id,),
        )
        connection.commit()
    finally:
        connection.close()

    assert "已暂停" in feed_backend.feed_manage(action="pause", name="OpenAI Blog")
    assert "已恢复" in feed_backend.feed_manage(action="resume", name="openai blog")
    result = feed_backend.feed_manage(
        action="update",
        name="OpenAI Blog",
        url="http://rsshub:1200/openai/news",
    )
    assert "已更新" in result

    connection = feed_backend._connect(config)
    try:
        source = connection.execute(
            "SELECT url, enabled FROM sources WHERE id = ?", (source_id,)
        ).fetchone()
        assert source is not None
        assert source["url"] == "http://rsshub:1200/openai/news"
        assert source["enabled"] == 1
        assert connection.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM acked_items").fetchone()[0] == 1
        poll_state = connection.execute(
            "SELECT last_polled_at, last_success_at, last_error FROM poll_state WHERE source_id = ?",
            (source_id,),
        ).fetchone()
        assert poll_state is not None
        assert tuple(poll_state) == (None, None, None)
    finally:
        connection.close()


def test_feed_manage_mutations_require_one_exact_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AKA_PLUGIN_DATA_DIR", str(tmp_path))
    assert "已订阅" in feed_backend.feed_manage(
        action="subscribe", name="OpenAI Blog", url="https://example.com/openai.xml"
    )
    assert "没有找到" in feed_backend.feed_manage(action="pause", name="OpenAI")
    assert "update 需要 url" in feed_backend.feed_manage(action="update", name="OpenAI Blog")


def test_concurrent_legacy_connections_share_one_schema_migration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AKA_PLUGIN_DATA_DIR", str(tmp_path))
    config = feed_backend.load_config()

    # 1. 建立真实旧表，让所有连接都必须走 ADD COLUMN 迁移。
    config.db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(config.db_path) as connection:
        connection.execute(
            """
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
            """
        )

    # 2. 并发模拟正式 poller 与首个 MCP 调用同时启动。
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

    # 3. 每个连接都必须看到同一个完整迁移终态。
    required = {"author", "interest_ok", "interest_scored_at"}
    assert all(required <= schema for schema in schemas)
