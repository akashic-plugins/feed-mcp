from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from agent.control.timer import TimerReceipt, TimerStatus
from agent.plugin_composition import PluginTimers
from feed_test_plugin.content_source import FeedContentRuntime
from feed_runtime import backend


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


class _Content:
    def __init__(self) -> None:
        self.submissions: list[tuple[str, tuple[dict[str, object], ...]]] = []
        self.rows: list[dict[str, object]] = []
        self.fail_ack_once = False
        self.ack_receipt: dict[str, object] = {
            "settled": True,
            "duplicate": False,
        }

    def submit(self, batch_id, items):
        frozen = tuple(dict(item) for item in items)
        self.submissions.append((batch_id, frozen))
        return {"inserted": [item["item_id"] for item in frozen]}

    def unsettled(self, limit=100):
        return tuple(self.rows[:limit])

    def ack(self, settlement_ref):
        if self.fail_ack_once:
            self.fail_ack_once = False
            raise RuntimeError("crash after provider ACK")
        if self.ack_receipt.get("settled") is True:
            self.rows = [
                row
                for row in self.rows
                if row["settlement_ref"] != settlement_ref
            ]
        return dict(self.ack_receipt)


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


@pytest.mark.asyncio
async def test_timer_poll_submits_nonempty_once_and_empty_poll_has_no_history(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 23, 10, tzinfo=UTC)
    timer = _Timer(now)
    content = _Content()
    _seed_item(tmp_path, now)
    runtime = FeedContentRuntime(
        tmp_path,
        PluginTimers(timer),
        content,
        now=lambda: now,
    )

    await runtime.start()
    assert len(timer.handles) == 1
    handler = runtime._log_handler  # pyright: ignore[reportPrivateUsage]
    assert handler is not None
    assert handler.backupCount == 3
    assert handler.maxBytes == 5 * 1024 * 1024
    timer.handles[0].fire()
    for _ in range(200):
        if content.submissions and len(timer.handles) == 2:
            break
        await asyncio.sleep(0.01)
    assert len(content.submissions) == 1
    payload = cast(Mapping[str, object], content.submissions[0][1][0]["payload"])
    assert payload["content"] == "Body"

    # ACK the only item, then the next empty poll only updates current deadline.
    _ = backend.settle_content_item(
        "event-1", "revision-1", data_root=tmp_path
    )
    timer.handles[1].fire()
    for _ in range(200):
        if len(timer.handles) == 3:
            break
        await asyncio.sleep(0.01)
    assert len(content.submissions) == 1
    with sqlite3.connect(tmp_path / "feed_mcp.sqlite3") as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM content_exports"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM metadata WHERE key='content_source_next_due'"
        ).fetchone() == (1,)
    await runtime.close()


@pytest.mark.asyncio
async def test_provider_ack_retries_after_crash_before_content_ack(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 23, 10, tzinfo=UTC)
    _seed_item(tmp_path, now)
    content = _Content()
    content.rows = [
        {
            "ref": {"item_id": "event-1", "revision": "revision-1"},
            "settlement_ref": "delivery:1",
        }
    ]
    content.fail_ack_once = True
    runtime = FeedContentRuntime(
        tmp_path,
        PluginTimers(_Timer(now)),
        content,
        now=lambda: now,
    )

    with pytest.raises(RuntimeError, match="crash after provider ACK"):
        await runtime._settle_delivered()  # pyright: ignore[reportPrivateUsage]
    assert content.rows
    assert await runtime._settle_delivered() == 1  # pyright: ignore[reportPrivateUsage]
    assert content.rows == []
    with sqlite3.connect(tmp_path / "feed_mcp.sqlite3") as connection:
        assert connection.execute(
            "SELECT event_id FROM acked_items"
        ).fetchall() == [("event-1",)]


@pytest.mark.asyncio
async def test_duplicate_content_ack_is_already_settled(tmp_path: Path) -> None:
    now = datetime(2026, 8, 23, 10, tzinfo=UTC)
    _seed_item(tmp_path, now)
    content = _Content()
    content.rows = [
        {
            "ref": {"item_id": "event-1", "revision": "revision-1"},
            "settlement_ref": "delivery:1",
        }
    ]
    content.ack_receipt = {"settled": True, "duplicate": True}
    runtime = FeedContentRuntime(
        tmp_path,
        PluginTimers(_Timer(now)),
        content,
        now=lambda: now,
    )

    assert await runtime._settle_delivered() == 1  # pyright: ignore[reportPrivateUsage]
    assert content.rows == []


@pytest.mark.asyncio
async def test_unsettled_content_ack_fails_loud(tmp_path: Path) -> None:
    now = datetime(2026, 8, 23, 10, tzinfo=UTC)
    _seed_item(tmp_path, now)
    content = _Content()
    content.rows = [
        {
            "ref": {"item_id": "event-1", "revision": "revision-1"},
            "settlement_ref": "delivery:1",
        }
    ]
    content.ack_receipt = {"settled": False, "reason": "state_mismatch"}
    runtime = FeedContentRuntime(
        tmp_path,
        PluginTimers(_Timer(now)),
        content,
        now=lambda: now,
    )

    with pytest.raises(RuntimeError, match="Feed Content ACK 未提交"):
        await runtime._settle_delivered()  # pyright: ignore[reportPrivateUsage]
    assert content.rows
