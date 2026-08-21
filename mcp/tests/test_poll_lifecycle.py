from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

from src import mcp_bridge


def test_poller_refreshes_immediately_and_continues(monkeypatch) -> None:
    calls: list[int] = []

    backend = SimpleNamespace(
        poll_feeds_only=lambda: calls.append(len(calls) + 1),
        load_config=lambda: SimpleNamespace(poll_ttl_seconds=0.01),
    )
    monkeypatch.setattr(mcp_bridge, "_live_backend", lambda: backend)

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

    backend = SimpleNamespace(
        poll_feeds_only=poll,
        load_config=lambda: SimpleNamespace(poll_ttl_seconds=0.01),
    )
    monkeypatch.setattr(mcp_bridge, "_live_backend", lambda: backend)

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


def test_poller_stop_waits_for_inflight_thread(monkeypatch) -> None:
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def poll() -> None:
        entered.set()
        release.wait(timeout=5)
        finished.set()

    backend = SimpleNamespace(
        poll_feeds_only=poll,
        load_config=lambda: SimpleNamespace(poll_ttl_seconds=60),
    )
    monkeypatch.setattr(mcp_bridge, "_live_backend", lambda: backend)

    async def scenario() -> None:
        poller = mcp_bridge.FeedPoller()
        await poller.start()
        await asyncio.to_thread(entered.wait, 5)
        stop = asyncio.create_task(poller.stop())
        await asyncio.sleep(0)
        assert not stop.done()
        assert not finished.is_set()
        release.set()
        await stop
        assert finished.is_set()

    asyncio.run(scenario())


def test_poller_stop_finishes_worker_before_restoring_cancellation(monkeypatch) -> None:
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def poll() -> None:
        entered.set()
        release.wait(timeout=5)
        finished.set()

    backend = SimpleNamespace(
        poll_feeds_only=poll,
        load_config=lambda: SimpleNamespace(poll_ttl_seconds=60),
    )
    monkeypatch.setattr(mcp_bridge, "_live_backend", lambda: backend)

    async def scenario() -> None:
        poller = mcp_bridge.FeedPoller()
        await poller.start()
        await asyncio.to_thread(entered.wait, 5)
        stop = asyncio.create_task(poller.stop())
        await asyncio.sleep(0)
        stop.cancel()
        await asyncio.sleep(0)
        assert not stop.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await stop
        assert finished.is_set()

    asyncio.run(scenario())
