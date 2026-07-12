from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from src import mcp_bridge


def test_poller_refreshes_before_first_cache_read_and_continues(monkeypatch) -> None:
    calls: list[int] = []

    monkeypatch.setattr(
        mcp_bridge.feed_backend,
        "poll_feeds_only",
        lambda: calls.append(len(calls) + 1),
    )
    monkeypatch.setattr(
        mcp_bridge.feed_backend,
        "load_config",
        lambda: SimpleNamespace(poll_ttl_seconds=0.01),
    )

    async def scenario() -> None:
        poller = mcp_bridge.FeedPoller()
        await poller.start()
        try:
            await asyncio.wait_for(poller.require_fresh_cache(), timeout=1)
            assert calls == [1]
            for _ in range(100):
                if len(calls) >= 2:
                    break
                await asyncio.sleep(0.01)
            assert len(calls) >= 2
        finally:
            await poller.stop()

    asyncio.run(scenario())


def test_poller_exposes_refresh_failure_and_retries(monkeypatch) -> None:
    attempts = 0

    def poll() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("feed database unavailable")

    monkeypatch.setattr(mcp_bridge.feed_backend, "poll_feeds_only", poll)
    monkeypatch.setattr(
        mcp_bridge.feed_backend,
        "load_config",
        lambda: SimpleNamespace(poll_ttl_seconds=0.01),
    )

    async def scenario() -> None:
        poller = mcp_bridge.FeedPoller()
        await poller.start()
        try:
            with pytest.raises(RuntimeError, match="Feed 缓存刷新失败"):
                await asyncio.wait_for(poller.require_fresh_cache(), timeout=1)
            for _ in range(100):
                if attempts >= 2:
                    break
                await asyncio.sleep(0.01)
            await poller.require_fresh_cache()
            assert attempts >= 2
        finally:
            await poller.stop()

    asyncio.run(scenario())
