from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Protocol, cast

from agent.plugin_composition import PluginTimers, TimerHandle, TimerStatus

from .feed_runtime import CONTENT_SOURCE_ID, backend


class BoundContentSource(Protocol):
    def submit(
        self, batch_id: str, items: Sequence[Mapping[str, object]]
    ) -> Mapping[str, object]: ...

    def unsettled(self, limit: int = 100) -> tuple[Mapping[str, object], ...]: ...

    def ack(self, settlement_ref: str) -> Mapping[str, object]: ...


class ContentSourceServices(Protocol):
    def bind(self, source_id: str) -> BoundContentSource: ...


class FeedContentRuntime:
    """轮询 Feed、提交精确 revision，并收束 provider ACK。"""

    def __init__(
        self,
        data_root: Path,
        timers: PluginTimers,
        content: BoundContentSource,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._data_root = data_root
        self._timers = timers
        self._content = content
        self._now = now or (lambda: datetime.now(UTC))
        self._handle: TimerHandle | None = None
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        self._log = logging.Logger("feed-content-source", level=logging.INFO)
        self._log.propagate = False
        self._log_handler: RotatingFileHandler | None = None

    async def start(self) -> None:
        """恢复 source deadline，并只注册一个 Timer。"""

        if self._closed:
            raise RuntimeError("Feed Content runtime 已关闭")
        if self._handle is not None:
            return
        self._start_diagnostics()
        deadline = await asyncio.to_thread(
            backend.content_source_deadline,
            data_root=self._data_root,
            now=self._aware_now(),
        )
        self._arm(deadline)

    async def close(self) -> None:
        """收束进行中的轮询、取消等待并释放诊断日志。"""

        if self._closed:
            return
        self._closed = True
        handle = self._handle
        task = self._task
        if handle is not None:
            _ = await handle.cancel()
        if task is not None and task is not asyncio.current_task():
            await task
        if handle is not None:
            await handle.cleanup()
        self._handle = None
        self._task = None
        self._stop_diagnostics()

    def _arm(self, deadline: datetime) -> None:
        if self._closed or self._handle is not None:
            return
        if deadline.tzinfo is None:
            raise ValueError("Feed Content deadline 必须包含时区")
        handle = self._timers.schedule(deadline)
        self._handle = handle
        self._task = asyncio.create_task(
            self._wait_poll_rearm(handle), name="feed-content-source:poll"
        )

    async def _wait_poll_rearm(self, handle: TimerHandle) -> None:
        """消费一个 Timer，完成一次 source 事务后再注册下一次。"""

        try:
            receipt = await handle.result()
            if receipt.status is TimerStatus.CANCELLED or self._closed:
                return

            # 1. 先完成 provider ACK，再获取下一份待处理快照。
            settled = await self._settle_delivered()

            # 2. 由唯一外部轮询 owner 刷新持久 Feed 缓存。
            await asyncio.to_thread(
                backend.poll_feeds_only, data_root=self._data_root
            )
            items = await asyncio.to_thread(
                backend.prepare_content_items, data_root=self._data_root
            )

            # 3. 非空 Content 提交成功后才能推进 source deadline。
            submitted = 0
            if items:
                result = self._content.submit(_batch_id(items), items)
                inserted = result["inserted"]
                if not isinstance(inserted, list):
                    raise TypeError("Feed Content submit receipt inserted 必须是 list")
                submitted = len(inserted)
            config = await asyncio.to_thread(
                backend.load_config, self._data_root
            )
            next_due = self._aware_now() + timedelta(
                seconds=config.poll_ttl_seconds
            )
            await asyncio.to_thread(
                backend.commit_content_source_deadline,
                data_root=self._data_root,
                deadline=next_due,
            )
            self._log.info(
                "poll committed items=%d inserted=%d settled=%d next_due=%s",
                len(items),
                submitted,
                settled,
                next_due.isoformat(),
            )
        finally:
            self._handle = None
            self._task = None
            await handle.cleanup()
        if not self._closed:
            deadline = await asyncio.to_thread(
                backend.content_source_deadline,
                data_root=self._data_root,
                now=self._aware_now(),
            )
            self._arm(deadline)

    async def _settle_delivered(self) -> int:
        """用精确 Feed revision 收束每条已投递 Content receipt。"""

        settled = 0
        while rows := self._content.unsettled(100):
            for row in rows:
                ref = _mapping(row["ref"], "Feed unsettled ref")
                result = await asyncio.to_thread(
                    backend.settle_content_item,
                    _string(ref["item_id"], "Feed item_id"),
                    _string(ref["revision"], "Feed revision"),
                    data_root=self._data_root,
                )
                if result.get("status") != "committed" or result.get(
                    "disposition"
                ) not in {"acknowledged", "obsolete_revision", "not_pending"}:
                    raise RuntimeError(f"Feed provider ACK 未提交: {result!r}")
                settlement_ref = _string(
                    row["settlement_ref"], "Feed settlement_ref"
                )
                receipt = self._content.ack(settlement_ref)
                if receipt.get("settled") is not True:
                    raise RuntimeError(f"Feed Content ACK 未提交: {receipt!r}")
                settled += 1
        return settled

    def _aware_now(self) -> datetime:
        value = self._now()
        if value.tzinfo is None:
            raise ValueError("Feed Content clock 必须包含时区")
        return value.astimezone(UTC)

    def _start_diagnostics(self) -> None:
        if self._log_handler is not None:
            return
        self._data_root.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            self._data_root / "feed_source.runtime.log",
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)-8s %(name)s | %(message)s"
            )
        )
        self._log.addHandler(handler)
        self._log_handler = handler

    def _stop_diagnostics(self) -> None:
        handler = self._log_handler
        if handler is None:
            return
        self._log.removeHandler(handler)
        handler.close()
        self._log_handler = None


def _batch_id(items: Sequence[Mapping[str, object]]) -> str:
    identities = [
        {
            "item_id": _string(item["item_id"], "Feed item_id"),
            "revision": _string(item["revision"], "Feed revision"),
        }
        for item in items
    ]
    encoded = json.dumps(
        identities, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return f"feed-content:{hashlib.sha256(encoded).hexdigest()}"


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} 必须是 Mapping")
    return cast(Mapping[str, object], value)


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise TypeError(f"{label} 必须是非空字符串")
    return value
