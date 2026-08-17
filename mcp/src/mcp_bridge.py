from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from mcp.server.fastmcp import FastMCP

logger = logging.getLogger(__name__)


def _recording_backend() -> bool:
    return os.environ.get("FEED_BACKEND", "").strip().lower() == "recording"


def _live_backend() -> Any:
    if _recording_backend():
        raise RuntimeError("feed recording backend 不允许访问正式 Feed 后端")
    from src import feed_backend

    return feed_backend


class FeedPoller:
    """在正式运行时后台维护 Feed 缓存刷新。"""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("Feed poller 已启动")
        self._task = asyncio.create_task(self._run(), name="feed-cache-poller")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is None:
            return
        task = self._task
        task.cancel()
        caller_cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                if not task.done():
                    caller_cancelled = True
            except Exception:
                break
        self._task = None
        if not task.cancelled():
            task.result()
        if caller_cancelled:
            raise asyncio.CancelledError

    async def poll_now(self) -> None:
        async with self._lock:
            worker = asyncio.create_task(
                asyncio.to_thread(_live_backend().poll_feeds_only),
                name="feed-cache-poll-worker",
            )
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                while not worker.done():
                    try:
                        await asyncio.shield(worker)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                error = worker.exception()
                if error is not None:
                    logger.error(
                        "[feed] poll worker 在取消收束期间失败",
                        exc_info=(type(error), error, error.__traceback__),
                    )
                raise

    async def _run(self) -> None:
        """首次立即刷新，随后按缓存 TTL 持续刷新。"""

        # 1. 首次刷新失败必须暴露，同时保留后续重试能力。
        try:
            await self.poll_now()
        except Exception:
            logger.exception("[feed] 首次缓存刷新失败")

        # 2. 正式后端按配置周期刷新，recording 不会进入此生命周期。
        while not self._stop.is_set():
            try:
                interval = _live_backend().load_config().poll_ttl_seconds
            except Exception:
                logger.exception("[feed] 读取轮询配置失败")
                interval = 60
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
                return
            except TimeoutError:
                pass
            try:
                await self.poll_now()
            except Exception:
                logger.exception("[feed] 后台缓存刷新失败")


def create_mcp_server() -> FastMCP:
    """创建 Feed MCP，并让 recording 生命周期保持零轮询、零数据库访问。"""

    poller = None if _recording_backend() else FeedPoller()

    @asynccontextmanager
    async def lifespan(_: FastMCP) -> AsyncIterator[None]:
        if poller is None:
            yield None
            return
        await poller.start()
        try:
            yield None
        finally:
            await poller.stop()

    mcp = FastMCP("feed-mcp", lifespan=lifespan)

    @mcp.tool()
    def feed_manage(
        action: str,
        name: str = "",
        url: str = "",
        source_type: str = "rss",
        note: str = "",
    ) -> str:
        """管理 RSS 订阅源：添加、删除、列出订阅。"""

        return _live_backend().feed_manage(
            action=action,
            name=name,
            url=url,
            source_type=source_type,
            note=note,
        )

    @mcp.tool()
    def feed_query(
        action: str,
        source: str = "",
        keyword: str = "",
        limit: int = 5,
        page: int = 1,
        page_size: int = 20,
    ) -> str:
        """查询 RSS 订阅内容。"""

        return _live_backend().feed_query(
            action=action,
            source=source,
            keyword=keyword,
            limit=limit,
            page=page,
            page_size=page_size,
        )

    @mcp.tool()
    async def poll_feeds() -> str:
        if poller is None:
            raise RuntimeError("feed recording backend 不允许轮询")
        await poller.poll_now()
        return "ok"

    @mcp.tool()
    async def get_proactive_events(
        offset: int = 0,
        limit: int = 50,
        cursor: str | None = None,
    ) -> str:
        events = await asyncio.to_thread(
            _fetch_proactive_events,
            offset=offset,
            limit=limit,
            cursor=cursor,
        )
        return json.dumps(events, ensure_ascii=False)

    @mcp.tool()
    def acknowledge_events(
        event_ids: list[str], feedback: str | None = None
    ) -> str:
        return json.dumps(
            _acknowledge_proactive_events(event_ids, feedback=feedback),
            ensure_ascii=False,
        )

    return mcp


def _proactive_fetch_payload(
    events: list[dict[str, Any]],
    *,
    cursor: str | None = None,
) -> dict[str, Any]:
    """把正式后端结果编码成 Core 可识别的 typed empty/items。"""

    if not events:
        return {"status": "empty"}
    payload: dict[str, Any] = {"status": "items", "items": events}
    if cursor is not None:
        payload["cursor"] = cursor
    return payload


def _fetch_proactive_events(
    *,
    offset: int = 0,
    limit: int = 50,
    cursor: str | None = None,
) -> dict[str, Any]:
    """recording 固定返回 typed empty，正式运行才读取 Feed 数据库。"""

    if _recording_backend():
        return {"status": "empty"}
    if limit < 1:
        raise ValueError("Feed proactive limit 必须大于零")
    if cursor is not None:
        if offset != 0:
            raise ValueError("Feed proactive cursor 不能与 offset 同时使用")
        prefix = "feed-offset:"
        if not cursor.startswith(prefix) or not cursor[len(prefix) :].isdigit():
            raise ValueError("Feed proactive cursor 无效")
        offset = int(cursor[len(prefix) :])
    backend = _live_backend()
    events = backend.get_proactive_events(offset=offset, limit=limit + 1)
    has_more = len(events) > limit
    return _proactive_fetch_payload(
        events[:limit],
        cursor=f"feed-offset:{offset + limit}" if has_more else None,
    )


def _acknowledge_proactive_events(
    requested: list[str], *, feedback: str | None = None
) -> dict[str, Any]:
    """只有全部请求 ID 持久确认后才编码 committed。"""

    if not requested:
        return {"status": "skipped", "reason": "no_ids"}
    if _recording_backend():
        raise RuntimeError("feed recording backend 不允许确认事件")
    result = _live_backend().acknowledge_events(requested, feedback=feedback)
    return _proactive_ack_payload(requested, result)


def _proactive_ack_payload(
    requested: list[str], result: dict[str, list[str]]
) -> dict[str, Any]:
    """把 Feed ack 结果转换为完整 committed 或明确 failure。"""

    if not requested:
        return {"status": "skipped", "reason": "no_ids"}
    acknowledged = list(result.get("acknowledged", []))
    failed = list(result.get("failed", []))
    if failed or acknowledged != requested:
        return {
            "status": "failure",
            "error": "feed ack 未完整提交",
            "retryable": True,
            "failed_ids": failed,
        }
    return {"status": "committed", "ids": acknowledged}
