from __future__ import annotations

import os
from typing import Any

from mcp.server.fastmcp import FastMCP


def _recording_backend() -> bool:
    return os.environ.get("FEED_BACKEND", "").strip().lower() == "recording"


def _live_backend() -> Any:
    if _recording_backend():
        raise RuntimeError("feed recording backend 不允许访问正式 Feed 后端")
    from feed_runtime import backend

    return backend


def create_mcp_server() -> FastMCP:
    """暴露用户驱动的 Feed 工具，但不拥有后台轮询。"""

    mcp = FastMCP("feed-mcp")

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
        """查询由唯一 Timer owner 维护的 RSS 缓存。"""

        return _live_backend().feed_query(
            action=action,
            source=source,
            keyword=keyword,
            limit=limit,
            page=page,
            page_size=page_size,
        )

    return mcp
