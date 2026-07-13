from __future__ import annotations

import asyncio
from types import SimpleNamespace

from src import mcp_bridge


def test_poller_refreshes_immediately_and_continues(monkeypatch) -> None:
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
            for _ in range(100):
                if calls:
                    break
                await asyncio.sleep(0.01)
            assert calls == [1]
            for _ in range(100):
                if len(calls) >= 2:
                    break
                await asyncio.sleep(0.01)
            assert len(calls) >= 2
        finally:
            await poller.stop()

    asyncio.run(scenario())


def test_poller_logs_refresh_failure_and_retries(monkeypatch) -> None:
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
            for _ in range(100):
                if attempts >= 2:
                    break
                await asyncio.sleep(0.01)
            assert attempts >= 2
        finally:
            await poller.stop()

    asyncio.run(scenario())
