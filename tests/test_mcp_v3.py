from __future__ import annotations

import importlib.util
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest


MCP_BRIDGE_PATH = Path(__file__).resolve().parents[1] / "mcp" / "src" / "mcp_bridge.py"
RUN_MCP_PATH = Path(__file__).resolve().parents[1] / "mcp" / "run_mcp.py"


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_recording_mcp_exposes_only_user_driven_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge = _load_module(MCP_BRIDGE_PATH, "feed_test_mcp_bridge")
    monkeypatch.setenv("FEED_BACKEND", "recording")
    server = bridge.create_mcp_server()

    assert {tool.name for tool in server._tool_manager.list_tools()} == {
        "feed_manage",
        "feed_query",
    }


def test_recording_user_tool_fails_before_backend_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge = _load_module(MCP_BRIDGE_PATH, "feed_test_mcp_recording")
    monkeypatch.setenv("FEED_BACKEND", "recording")
    with pytest.raises(RuntimeError, match="recording backend"):
        bridge._live_backend()


def test_runner_uses_three_bounded_log_rotations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_module(RUN_MCP_PATH, "feed_test_run_mcp")
    monkeypatch.setenv("AKA_PLUGIN_DATA_DIR", str(tmp_path))
    runner._setup_logging(tmp_path)
    handlers = logging.getLogger().handlers
    rotating = [handler for handler in handlers if isinstance(handler, RotatingFileHandler)]
    assert len(rotating) == 1
    assert rotating[0].backupCount == 3
    assert rotating[0].maxBytes == 5 * 1024 * 1024
    for handler in handlers:
        handler.close()
