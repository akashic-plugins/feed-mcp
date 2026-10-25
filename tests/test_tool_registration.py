from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from plugins.tools.plugin import TOOLS, ToolRef, ToolView
from feed_test_plugin.tools import FEED_TOOLS, register_tools


@pytest.mark.asyncio
async def test_native_tools_validate_and_use_plugin_data_root(tmp_path: Path):
    registrations = {}

    class Catalog:
        async def declare_group(self, ctx, **kwargs):
            pass

        def view(self, *refs):
            return ToolView(refs)

        async def register(self, ctx, **record):
            registrations[record["name"]] = record
            return ToolRef(record["name"], record)

    services = {TOOLS: Catalog()}
    async def provide(key, value):
        services[key] = value
    ctx = SimpleNamespace(require=services.__getitem__, provide=provide, data_root=tmp_path)
    await register_tools(ctx, description="fixture tools")
    assert [ref.name for ref in services[FEED_TOOLS].refs] == ["feed_manage", "feed_query"]
    assert registrations["feed_query"]["idempotent"] is True
    assert registrations["feed_manage"]["idempotent"] is False

    # 参数在执行前按 schema 校验，非法动作不触达数据库。
    async with registrations["feed_manage"]["open"]({}) as manage:
        assert "参数错误" in str(await manage.prepare({"action": "drop"}))
        arguments = await manage.prepare({"action": "subscribe", "name": "Example", "url": "https://example.com/feed.xml"})
        assert (await manage.invoke("one", arguments)).outcome == "success"
        listed = await manage.invoke("two", await manage.prepare({"action": "list"}))
        assert "Example" in str(listed.parts[0].value)
        failed = await manage.invoke("three", await manage.prepare({"action": "subscribe"}))
        assert failed.outcome == "error"
        assert await manage.query("one") is None
    assert (tmp_path / "feed_mcp.sqlite3").exists()

    async with registrations["feed_query"]["open"]({}) as query:
        assert "参数错误" in str(await query.prepare({"action": "latest", "limit": 99}))
        result = await query.invoke("four", await query.prepare({"action": "summary"}))
        assert "sources=1" in str(result.parts[0].value)
