from __future__ import annotations

import asyncio
import importlib.util
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest


MCP_BRIDGE_PATH = Path(__file__).resolve().parents[1] / "mcp" / "src" / "mcp_bridge.py"
RUN_MCP_PATH = Path(__file__).resolve().parents[1] / "mcp" / "run_mcp.py"


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_recording_fetch_ack_and_lifespan_are_zero_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge = _load_module(MCP_BRIDGE_PATH, "feed_test_mcp_bridge")
    monkeypatch.setenv("FEED_BACKEND", "recording")
    monkeypatch.setenv("AKA_PLUGIN_DATA_DIR", str(tmp_path))

    class UnexpectedPoller:
        def __init__(self) -> None:
            raise AssertionError("recording 不得创建 FeedPoller")

    monkeypatch.setattr(bridge, "FeedPoller", UnexpectedPoller)
    monkeypatch.setattr(
        bridge,
        "_live_backend",
        lambda: (_ for _ in ()).throw(AssertionError("recording 不得加载后端")),
    )

    server = bridge.create_mcp_server()
    assert server is not None
    assert bridge._fetch_proactive_events() == {"status": "empty"}
    with pytest.raises(RuntimeError, match="不允许确认"):
        bridge._acknowledge_proactive_events(["event-1"])
    assert list(tmp_path.iterdir()) == []


def test_live_results_are_explicit_typed_payloads(monkeypatch: pytest.MonkeyPatch) -> None:
    bridge = _load_module(MCP_BRIDGE_PATH, "feed_test_mcp_bridge_live")
    monkeypatch.delenv("FEED_BACKEND", raising=False)
    backend = SimpleNamespace(
        get_proactive_events=lambda **_: [],
        acknowledge_events=lambda ids, feedback=None: {
            "acknowledged": list(ids),
            "failed": [],
        },
    )
    monkeypatch.setattr(bridge, "_live_backend", lambda: backend)

    assert bridge._fetch_proactive_events() == {"status": "empty"}
    backend.get_proactive_events = lambda **_: [{"event_id": "one", "kind": "content"}]
    assert bridge._fetch_proactive_events() == {
        "status": "items",
        "items": [{"event_id": "one", "kind": "content"}],
    }
    assert bridge._acknowledge_proactive_events(["one"]) == {
        "status": "committed",
        "ids": ["one"],
    }
    assert bridge._proactive_ack_payload(
        ["one", "two"], {"acknowledged": ["one"], "failed": ["two"]}
    )["status"] == "failure"
    assert bridge._proactive_ack_payload(
        [], {"acknowledged": [], "failed": []}
    ) == {"status": "skipped", "reason": "no_ids"}


def test_proactive_cursor_returns_every_event_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge = _load_module(MCP_BRIDGE_PATH, "feed_test_mcp_bridge_pages")
    monkeypatch.delenv("FEED_BACKEND", raising=False)
    events = [{"event_id": f"event-{index}", "kind": "content"} for index in range(51)]

    def fetch(*, offset: int, limit: int):
        return events[offset : offset + limit]

    monkeypatch.setattr(
        bridge,
        "_live_backend",
        lambda: SimpleNamespace(get_proactive_events=fetch),
    )

    first = bridge._fetch_proactive_events(limit=50)
    assert first["cursor"] == "feed-offset:50"
    second = bridge._fetch_proactive_events(limit=50, cursor=first["cursor"])
    combined = [*first["items"], *second["items"]]
    assert [item["event_id"] for item in combined] == [
        f"event-{index}" for index in range(51)
    ]
    assert "cursor" not in second


def test_runner_configures_stderr_without_runtime_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_module(RUN_MCP_PATH, "feed_test_run_mcp")
    monkeypatch.setenv("AKA_PLUGIN_DATA_DIR", str(tmp_path))
    runner._setup_logging()
    assert all(
        not isinstance(handler, logging.FileHandler)
        for handler in logging.getLogger().handlers
    )
    assert list(tmp_path.iterdir()) == []
