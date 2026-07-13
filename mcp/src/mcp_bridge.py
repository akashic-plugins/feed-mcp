from __future__ import annotations

# pyright: reportMissingImports=false

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator, List, Optional

from mcp.server.fastmcp import FastMCP

from src import feed_backend

logger = logging.getLogger(__name__)


class FeedPoller:
    """在后台维护 Feed 缓存刷新，不阻塞缓存读取。"""

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
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        self._task = None

    async def poll_now(self) -> None:
        async with self._lock:
            await asyncio.to_thread(feed_backend.poll_feeds_only)

    async def _run(self) -> None:
        """首次立即刷新，随后按缓存 TTL 持续刷新。"""

        # 1. 首次刷新在后台执行，读取方始终可以使用现有缓存
        try:
            await self.poll_now()
        except Exception:
            logger.exception("[feed] 首次缓存刷新失败")

        # 2. 后续失败显式记录，并保留下一轮重试能力
        while not self._stop.is_set():
            try:
                interval = feed_backend.load_config().poll_ttl_seconds
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
    poller = FeedPoller()

    @asynccontextmanager
    async def lifespan(_: FastMCP) -> AsyncIterator[None]:
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
        """管理 RSS 订阅源：添加、删除、列出订阅。支持 rss add / 添加订阅 / 订阅管理 / 取消订阅。

        action: list（列出所有订阅）/ add（添加新订阅）/ remove（删除订阅）
        """
        return feed_backend.feed_manage(
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
        """查询 RSS 订阅内容，获取最近新闻、最新文章、最新资讯、rss查询。

        action: latest（最近内容）/ search（关键词搜索）/ sources（列出信息来源）
        """
        return feed_backend.feed_query(
            action=action,
            source=source,
            keyword=keyword,
            limit=limit,
            page=page,
            page_size=page_size,
        )

    @mcp.tool()
    async def poll_feeds() -> str:
        try:
            await poller.poll_now()
            return "ok"
        except Exception as e:
            logger.exception("[feed] poll_feeds 系统级失败")
            return f"error: {e}"

    @mcp.tool()
    async def get_proactive_events(offset: int = 0, limit: int = 50) -> str:
        events = await asyncio.to_thread(
            feed_backend.get_proactive_events,
            offset=offset,
            limit=limit,
        )
        return json.dumps(
            events,
            ensure_ascii=False,
        )

    @mcp.tool()
    def acknowledge_events(event_ids: List[str], feedback: Optional[str] = None) -> str:
        return json.dumps(
            feed_backend.acknowledge_events(event_ids, feedback=feedback),
            ensure_ascii=False,
        )

    return mcp
