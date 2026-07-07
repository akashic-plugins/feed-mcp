from __future__ import annotations

import json
import logging
from typing import List

from mcp.server.fastmcp import FastMCP

from src import feed_backend

logger = logging.getLogger(__name__)


def create_mcp_server() -> FastMCP:
    mcp = FastMCP("feed-mcp")

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
    def poll_feeds() -> str:
        try:
            feed_backend.poll_feeds_only()
            return "ok"
        except Exception as e:
            logger.exception("[feed] poll_feeds 系统级失败")
            return f"error: {e}"

    @mcp.tool()
    def get_proactive_events() -> str:
        return json.dumps(feed_backend.get_proactive_events(), ensure_ascii=False)

    @mcp.tool()
    def acknowledge_events(event_ids: List[str], feedback: str, ttl_hours: int = 0) -> str:
        actual_ttl = ttl_hours if ttl_hours > 0 else None
        return json.dumps(
            feed_backend.acknowledge_events(event_ids, feedback=feedback, ttl_hours=actual_ttl),
            ensure_ascii=False,
        )

    return mcp
