from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from plugins.timer.contract import TimerReceipt, TimerStatus
from feed_test_plugin import content_source  # pyright: ignore[reportMissingImports]
from feed_test_plugin.content_source import FeedContentRuntime  # pyright: ignore[reportMissingImports]
from feed_test_plugin.feed_runtime import backend  # pyright: ignore[reportMissingImports]


class _TimerHandle:
    def __init__(self, deadline: datetime, now: datetime) -> None:
        self.id = "timer:feed"
        self.deadline = deadline
        self.now = now
        self.future: asyncio.Future[TimerReceipt] = (
            asyncio.get_running_loop().create_future()
        )

    async def result(self) -> TimerReceipt:
        return await self.future

    async def cancel(self) -> TimerReceipt:
        if not self.future.done():
            self.future.set_result(self._receipt(TimerStatus.CANCELLED))
        return await self.future

    async def cleanup(self) -> None:
        return None

    def fire(self) -> None:
        self.future.set_result(self._receipt(TimerStatus.FIRED))

    def _receipt(self, status: TimerStatus) -> TimerReceipt:
        return TimerReceipt(self.id, self.deadline, self.now, status)


class _Timer:
    def __init__(self, now: datetime) -> None:
        self.now = now
        self.handles: list[_TimerHandle] = []

    def schedule(self, deadline: datetime) -> _TimerHandle:
        handle = _TimerHandle(deadline, self.now)
        self.handles.append(handle)
        return handle


class _Watch:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []
        self.fail_once = False

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
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("watch unavailable")
        self.events.append(
            {"source": source, "title": title, "text": text, "url": url, "at": at, "state": state, "key": key}
        )
        return f"event:{len(self.events)}"


def _seed_item(data_root: Path, now: datetime) -> None:
    cfg = backend.load_config(data_root)
    connection = backend._connect(cfg)
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


def _acked(data_root: Path) -> list[str]:
    with sqlite3.connect(data_root / "feed_mcp.sqlite3") as connection:
        rows = connection.execute("SELECT event_id FROM acked_items").fetchall()
    return [str(row[0]) for row in rows]


async def _wait_handles(timer: _Timer, count: int) -> None:
    for _ in range(300):
        if len(timer.handles) == count:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timer did not rearm")


@pytest.mark.asyncio
async def test_poll_emits_each_item_and_settles_immediately(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 23, 10, tzinfo=UTC)
    monkeypatch.setattr(backend, "_now", lambda: now)
    timer = _Timer(now)
    watch = _Watch()
    # 迁移在 start 里完成；之后才出现的条目必须被交出去。
    runtime = FeedContentRuntime(tmp_path, timer, watch, now=lambda: now)
    await runtime.start()
    _seed_item(tmp_path, now)
    timer.handles[0].fire()
    await _wait_handles(timer, 2)

    assert watch.events == [
        {
            "source": "Source",
            "title": "Title",
            "text": "Body",
            "url": "https://example.com/1",
            "at": now,
            "state": False,
            "key": "event-1:revision-1",
        }
    ]
    assert _acked(tmp_path) == ["event-1"]

    # 已结算的条目不会再交，空轮询只更新 deadline。
    timer.handles[1].fire()
    await _wait_handles(timer, 3)
    assert len(watch.events) == 1
    await runtime.close()


@pytest.mark.asyncio
async def test_failed_emit_leaves_item_unsettled_and_retries_with_same_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 23, 10, tzinfo=UTC)
    monkeypatch.setattr(backend, "_now", lambda: now)
    watch = _Watch()
    runtime = FeedContentRuntime(tmp_path, _Timer(now), watch, now=lambda: now)
    _ = backend.retire_content_backlog(data_root=tmp_path, now=now)
    _seed_item(tmp_path, now)
    items = await _prepare(tmp_path, now)

    watch.fail_once = True
    with pytest.raises(RuntimeError, match="watch unavailable"):
        _ = await runtime._emit_items(items)  # pyright: ignore[reportPrivateUsage]
    assert _acked(tmp_path) == []

    assert await runtime._emit_items(items) == 1  # pyright: ignore[reportPrivateUsage]
    assert [event["key"] for event in watch.events] == ["event-1:revision-1"]
    assert _acked(tmp_path) == ["event-1"]


@pytest.mark.asyncio
async def test_first_start_retires_old_backlog_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 23, 10, tzinfo=UTC)
    monkeypatch.setattr(backend, "_now", lambda: now)
    timer = _Timer(now)
    watch = _Watch()
    _seed_item(tmp_path, now)
    runtime = FeedContentRuntime(tmp_path, timer, watch, now=lambda: now)

    await runtime.start()
    assert _acked(tmp_path) == ["event-1"]
    timer.handles[0].fire()
    await _wait_handles(timer, 2)
    assert watch.events == []
    await runtime.close()

    # 标记已写入：再次启动不会把之后新到的条目吞掉。
    assert backend.retire_content_backlog(data_root=tmp_path, now=now) == 0


async def _prepare(data_root: Path, now: datetime) -> tuple[dict[str, object], ...]:
    return await content_source._prepare_content_items(  # pyright: ignore[reportPrivateUsage]
        data_root, now
    )


@pytest.mark.asyncio
async def test_poll_scripts_runs_bounded_concurrently_and_isolates_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from feed_test_plugin.feed_runtime import scripts  # pyright: ignore[reportMissingImports]
    from feed_test_plugin.feed_runtime.events import FeedEvent, ScriptOutcome  # pyright: ignore[reportMissingImports]

    now = datetime(2026, 8, 23, 10, tzinfo=UTC)
    monkeypatch.setattr(backend, "_now", lambda: now)

    for idx in range(5):
        scripts.save_script(
            data_root=tmp_path,
            name=f"src-{idx}",
            url=f"https://example.com/{idx}",
            code=f"echo {idx}",
            note="init",
            interval_seconds=300,
            report="status=ok events=1",
        )

    active = 0
    peak = 0

    async def fake_run(source: scripts.ScriptSource) -> ScriptOutcome:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.08)
            if source.name == "src-2":
                raise RuntimeError("boom")
            return ScriptOutcome(
                status="ok",
                events=(
                    FeedEvent(
                        id=f"ev-{source.name}",
                        url=source.url,
                        title=f"Title {source.name}",
                        content="",
                        author=None,
                        published_at=None,
                    ),
                ),
                note="ok",
                issues=(),
            )
        finally:
            active -= 1

    runtime = FeedContentRuntime(
        tmp_path,
        _Timer(now),
        _Watch(),
        now=lambda: now,
        run_script=fake_run,
        max_script_concurrency=2,
    )
    polled = await runtime._poll_scripts()  # pyright: ignore[reportPrivateUsage]
    assert polled == 5
    assert peak == 2

    with sqlite3.connect(tmp_path / "feed_mcp.sqlite3") as conn:
        conn.row_factory = sqlite3.Row
        states = {
            row["name"]: row["last_status"]
            for row in conn.execute(
                "SELECT s.name, p.last_status FROM sources s JOIN poll_state p ON p.source_id = s.id"
            ).fetchall()
        }
    assert states == {
        "src-0": "ok",
        "src-1": "ok",
        "src-2": "error",
        "src-3": "ok",
        "src-4": "ok",
    }


@pytest.mark.asyncio
async def test_poll_scripts_skips_queued_when_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from feed_test_plugin.feed_runtime import scripts  # pyright: ignore[reportMissingImports]
    from feed_test_plugin.feed_runtime.events import ScriptOutcome  # pyright: ignore[reportMissingImports]

    now = datetime(2026, 8, 23, 10, tzinfo=UTC)
    monkeypatch.setattr(backend, "_now", lambda: now)

    for idx in range(3):
        scripts.save_script(
            data_root=tmp_path,
            name=f"close-{idx}",
            url=f"https://example.com/close/{idx}",
            code=f"echo {idx}",
            note="init",
            interval_seconds=300,
            report="status=ok events=0",
        )

    started: list[str] = []
    first_entered = asyncio.Event()
    release_first = asyncio.Event()

    async def fake_run(source: scripts.ScriptSource) -> ScriptOutcome:
        started.append(source.name)
        if len(started) == 1:
            first_entered.set()
            await release_first.wait()
        return ScriptOutcome(status="empty", events=(), note="empty", issues=())

    runtime = FeedContentRuntime(
        tmp_path,
        _Timer(now),
        _Watch(),
        now=lambda: now,
        run_script=fake_run,
        max_script_concurrency=1,
    )
    task = asyncio.create_task(runtime._poll_scripts())  # pyright: ignore[reportPrivateUsage]
    await first_entered.wait()
    runtime._closed = True  # pyright: ignore[reportPrivateUsage]
    release_first.set()
    polled = await task
    assert polled == 1
    assert started == ["close-0"]


@pytest.mark.asyncio
async def test_poll_scripts_drains_all_tasks_even_when_record_outcome_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from feed_test_plugin.feed_runtime import scripts  # pyright: ignore[reportMissingImports]
    from feed_test_plugin.feed_runtime.events import ScriptOutcome  # pyright: ignore[reportMissingImports]

    now = datetime(2026, 8, 23, 10, tzinfo=UTC)
    monkeypatch.setattr(backend, "_now", lambda: now)

    for idx in range(4):
        scripts.save_script(
            data_root=tmp_path,
            name=f"dberr-{idx}",
            url=f"https://example.com/dberr/{idx}",
            code=f"echo {idx}",
            note="init",
            interval_seconds=300,
            report="status=ok events=0",
        )

    running = 0
    orig_record = scripts.record_outcome

    def flaky_record(*, data_root: Path, source: scripts.ScriptSource, outcome: ScriptOutcome, now: datetime) -> None:
        if source.name == "dberr-0":
            raise sqlite3.OperationalError("database is locked")
        orig_record(data_root=data_root, source=source, outcome=outcome, now=now)

    monkeypatch.setattr(scripts, "record_outcome", flaky_record)

    async def fake_run(source: scripts.ScriptSource) -> ScriptOutcome:
        nonlocal running
        running += 1
        try:
            await asyncio.sleep(0.05 if source.name == "dberr-0" else 0.12)
            return ScriptOutcome(status="empty", events=(), note="empty", issues=())
        finally:
            running -= 1

    runtime = FeedContentRuntime(
        tmp_path,
        _Timer(now),
        _Watch(),
        now=lambda: now,
        run_script=fake_run,
        max_script_concurrency=4,
    )
    polled = await runtime._poll_scripts()  # pyright: ignore[reportPrivateUsage]
    assert running == 0
    assert polled == 3
