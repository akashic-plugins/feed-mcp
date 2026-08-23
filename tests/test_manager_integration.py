from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
import agent.plugins.manager as plugin_manager_module
from agent.control.timer import TimerReceipt, TimerStatus
from agent.plugins.manager import PluginManager
from bus.event_bus import EventBus
from plugins.content.store import ContentStore

from feed_runtime import backend


ROOT = Path(__file__).resolve().parents[1]
CORE_ROOT = Path(os.environ["AKASHIC_AGENT_ROOT"])


def _fixture_runtime() -> Path:
    artifact_python = Path(os.environ["AKASHIC_PLUGIN_FIXTURE_PYTHON"])
    return artifact_python.parent.parent


class _TimerHandle:
    def __init__(self, timer_id: str, deadline: datetime, now: datetime) -> None:
        self._id = timer_id
        self.deadline = deadline
        self.now = now
        self.future: asyncio.Future[TimerReceipt] = (
            asyncio.get_running_loop().create_future()
        )

    @property
    def id(self) -> str:
        return self._id

    async def result(self) -> TimerReceipt:
        return await asyncio.shield(self.future)

    async def cancel(self) -> TimerReceipt:
        if not self.future.done():
            self.future.set_result(self._receipt(TimerStatus.CANCELLED))
        return await self.future

    async def cleanup(self) -> None:
        _ = await self.cancel()

    def fire(self) -> None:
        self.future.set_result(self._receipt(TimerStatus.FIRED))

    def _receipt(self, status: TimerStatus) -> TimerReceipt:
        return TimerReceipt(self.id, self.deadline, self.now, status)


class _Timer:
    def __init__(self, now: datetime) -> None:
        self.now = now
        self.handles: list[_TimerHandle] = []
        self.schedule_times: list[int] = []

    def schedule(self, deadline: datetime) -> _TimerHandle:
        self.schedule_times.append(time.time_ns())
        handle = _TimerHandle(f"timer:{len(self.handles)}", deadline, self.now)
        self.handles.append(handle)
        return handle


async def _eventually(predicate) -> None:
    for _ in range(300):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition did not settle")


def _stage_plugins(tmp_path: Path) -> tuple[Path, Path]:
    """将真实 Content、Feed 插件装入临时测试环境。"""

    runtime = _fixture_runtime()
    plugins = tmp_path / "plugins"
    content = plugins / "content"
    feed = plugins / "feed"
    shutil.copytree(CORE_ROOT / "plugins" / "content", content)
    shutil.copytree(
        ROOT,
        feed,
        ignore=shutil.ignore_patterns(
            ".git",
            ".akashic-core",
            ".plugin-contracts",
            ".pytest_cache",
            ".venv",
            "__pycache__",
            "tests",
        ),
    )
    (feed / "mcp" / ".venv").symlink_to(runtime, target_is_directory=True)
    return content, feed


def _stage_legacy_plugins(tmp_path: Path) -> tuple[Path, Path]:
    """装入 Content 和旧 lifespan Feed owner fixture。"""

    runtime = _fixture_runtime()
    plugins = tmp_path / "plugins"
    content = plugins / "content"
    feed = plugins / "feed"
    shutil.copytree(CORE_ROOT / "plugins" / "content", content)
    shutil.copytree(ROOT / "tests" / "fixtures" / "legacy_feed_owner", feed)
    (feed / "mcp" / ".venv").symlink_to(runtime, target_is_directory=True)
    return content, feed


def _replace_with_current_feed(feed: Path) -> None:
    """只将临时旧 Feed source 替换为当前插件树。"""

    runtime = _fixture_runtime()
    shutil.rmtree(feed)
    shutil.copytree(
        ROOT,
        feed,
        ignore=shutil.ignore_patterns(
            ".git",
            ".akashic-core",
            ".plugin-contracts",
            ".pytest_cache",
            ".venv",
            "__pycache__",
            "tests",
        ),
    )
    (feed / "mcp" / ".venv").symlink_to(runtime, target_is_directory=True)


def test_stage_plugins_uses_explicit_fixture_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = tmp_path / "artifact-runtime"
    artifact_python = runtime / "bin" / "python"
    artifact_python.parent.mkdir(parents=True)
    artifact_python.touch()
    monkeypatch.setenv("AKASHIC_PLUGIN_FIXTURE_PYTHON", str(artifact_python))

    _content, feed = _stage_plugins(tmp_path / "stage")

    assert (feed / "mcp" / ".venv").resolve() == runtime.resolve()


def test_stage_plugins_requires_fixture_python_before_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AKASHIC_PLUGIN_FIXTURE_PYTHON", raising=False)
    stage = tmp_path / "missing-runtime"

    with pytest.raises(KeyError, match="AKASHIC_PLUGIN_FIXTURE_PYTHON"):
        _stage_plugins(stage)

    assert not stage.exists()


def _seed_item(data_root: Path, now: datetime) -> None:
    config = backend.load_config(data_root)
    connection = backend._connect(config)
    try:
        connection.execute(
            """
            INSERT INTO items(
                event_id, source_id, source_name, source_type, title, content,
                url, author, published_at, first_seen_at, last_seen_at,
                emitted_at, content_hash
            ) VALUES('event-1', 'source', 'Source', 'rss', 'Title', 'Body',
                     'https://example.com/1', 'Author', ?, ?, ?, NULL, 'revision-1')
            """,
            (now.isoformat(), now.isoformat(), now.isoformat()),
        )
        connection.commit()
    finally:
        connection.close()


def _sqlite_hashes(path: Path) -> dict[str, str]:
    return {
        candidate.name: hashlib.sha256(candidate.read_bytes()).hexdigest()
        for candidate in sorted(path.parent.glob(path.name + "*"))
    }


@pytest.mark.asyncio
async def test_manager_content_candidate_and_timer_handoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """证明唯一正式轮询 owner、静默候选和有序热更新。"""

    # 1. 加载真实插件，让稳定 Feed Root 提交一条完整 item。
    now = datetime(2026, 8, 23, 10, tzinfo=UTC)
    timers: list[_Timer] = []

    def timer_factory() -> _Timer:
        timer = _Timer(now)
        timers.append(timer)
        return timer

    monkeypatch.setattr(plugin_manager_module, "AsyncioOneShotTimer", timer_factory)
    content_dir, feed_dir = _stage_plugins(tmp_path)
    workspace = tmp_path / "workspace"
    manager = PluginManager(
        plugin_dirs=[content_dir, feed_dir],
        event_bus=EventBus(),
        tool_registry=None,
        workspace=workspace,
        installed_cache_root=tmp_path / "cache",
    )
    await manager.load_all()
    snapshot = manager.current_snapshot
    assert snapshot is not None and snapshot.mcp_server_registry is not None
    runtime = manager.composition_generation_host.get(
        snapshot.generations["feed"].generation_id
    )
    assert runtime is not None and runtime.mcp is not None
    assert runtime.mcp.server("feed").tool_names == (
        "feed_manage",
        "feed_query",
    )
    feed_data = workspace / "plugin-data" / "feed-builtin"
    _seed_item(feed_data, now)
    lifecycle = asyncio.create_task(manager.run_runtime_services())
    formal_reader: sqlite3.Connection | None = None
    try:
        await _eventually(lambda: sum(len(timer.handles) for timer in timers) == 1)
        formal_timer = next(timer for timer in timers if timer.handles)
        formal_timer.handles[0].fire()
        content_path = (
            workspace / "plugin-data" / "content-builtin" / "content.sqlite3"
        )
        content_store = ContentStore(content_path)
        await _eventually(
            lambda: content_store.state_counts().get("pending") == 1
        )
        await _eventually(lambda: len(formal_timer.handles) == 2)
        with sqlite3.connect(feed_data / "feed_mcp.sqlite3") as connection:
            assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
            payload = connection.execute(
                "SELECT payload_json FROM content_exports"
            ).fetchone()[0]
        assert '\"content\":\"Body\"' in payload

        # 2. 候选可以握手私有 MCP，但不能轮询或写正式状态。
        formal_reader = sqlite3.connect(content_path)
        assert formal_reader.execute("SELECT COUNT(*) FROM items").fetchone() == (1,)
        feed_hashes = _sqlite_hashes(feed_data / "feed_mcp.sqlite3")
        content_hashes = _sqlite_hashes(content_path)
        with (feed_dir / "plugin.py").open("a", encoding="utf-8") as handle:
            handle.write("\n# candidate handoff fixture\n")
        candidate = await manager.prepare_candidate("feed")
        assert candidate is not None and candidate.runtime_snapshot is not None
        candidate_root = candidate.runtime_snapshot.composition_root
        assert candidate_root is not None
        assert candidate_root.plugin_runtime("feed").data_dir != feed_data
        assert sum(len(timer.handles) for timer in timers) == 2
        assert _sqlite_hashes(feed_data / "feed_mcp.sqlite3") == feed_hashes
        assert _sqlite_hashes(content_path) == content_hashes

        # 3. 发布先取消旧等待，再由新稳定 Root 注册 Timer。
        result = await manager.publish_prepared("feed")
        assert result["publication_state"] == "committed"
        await _eventually(lambda: sum(len(timer.handles) for timer in timers) == 3)
        assert content_store.state_counts() == {"pending": 1}
        assert (await formal_timer.handles[1].result()).status is TimerStatus.CANCELLED
        active = [
            handle
            for timer in timers
            for handle in timer.handles
            if not handle.future.done()
        ]
        assert len(active) == 1
    finally:
        if formal_reader is not None:
            formal_reader.close()
        lifecycle.cancel()
        _ = await asyncio.gather(lifecycle, return_exceptions=True)
        await manager.terminate_all()

    assert all(handle.future.done() for timer in timers for handle in timer.handles)


@pytest.mark.asyncio
async def test_legacy_mcp_owner_stops_before_new_timer_starts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """证明旧 lifespan owner 退场后，Timer ownership 才开始。"""

    # 1. 启动真实 managed MCP，用 lifespan 表示旧轮询 owner。
    now = datetime(2026, 8, 23, 10, tzinfo=UTC)
    timers: list[_Timer] = []

    def timer_factory() -> _Timer:
        timer = _Timer(now)
        timers.append(timer)
        return timer

    monkeypatch.setattr(plugin_manager_module, "AsyncioOneShotTimer", timer_factory)
    content_dir, feed_dir = _stage_legacy_plugins(tmp_path)
    workspace = tmp_path / "workspace"
    manager = PluginManager(
        plugin_dirs=[content_dir, feed_dir],
        event_bus=EventBus(),
        workspace=workspace,
        installed_cache_root=tmp_path / "cache",
    )
    await manager.load_all()
    owner_log = (
        workspace / "plugin-data" / "feed-builtin" / "legacy-owner.jsonl"
    )
    await _eventually(owner_log.is_file)
    lifecycle = asyncio.create_task(manager.run_runtime_services())
    try:
        assert sum(len(timer.handles) for timer in timers) == 0

        # 2. 准备并发布真实 Timer + Content 实现。
        _replace_with_current_feed(feed_dir)
        candidate = await manager.prepare_candidate("feed")
        assert candidate is not None
        assert sum(len(timer.handles) for timer in timers) == 0
        result = await manager.publish_prepared("feed")
        assert result["publication_state"] == "committed"
        await _eventually(
            lambda: sum(len(timer.handles) for timer in timers) == 1
        )
        await _eventually(
            lambda: '"event": "stopped"' in owner_log.read_text(encoding="utf-8")
        )

        # 3. 对比进程证据，而不只检查进程内对象状态。
        events = [
            json.loads(line)
            for line in owner_log.read_text(encoding="utf-8").splitlines()
        ]
        assert [event["event"] for event in events] == ["started", "stopped"]
        scheduled = [value for timer in timers for value in timer.schedule_times]
        assert len(scheduled) == 1
        assert events[1]["time_ns"] <= scheduled[0]
    finally:
        lifecycle.cancel()
        _ = await asyncio.gather(lifecycle, return_exceptions=True)
        await manager.terminate_all()
