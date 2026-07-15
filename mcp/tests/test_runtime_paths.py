from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from plugin import FeedPlugin
from run_mcp import _runtime_dir
from src.feed_backend import _runtime_root, load_config


def test_runtime_entrypoints_reject_missing_data_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AKA_PLUGIN_DATA_DIR", "   ")
    with pytest.raises(RuntimeError, match="AKA_PLUGIN_DATA_DIR"):
        _runtime_dir()
    with pytest.raises(RuntimeError, match="AKA_PLUGIN_DATA_DIR"):
        _runtime_root()
    with pytest.raises(RuntimeError, match="AKA_PLUGIN_DATA_DIR"):
        load_config()


def test_initialize_rejects_missing_context_paths(tmp_path: Path) -> None:
    plugin = FeedPlugin()
    plugin.context = SimpleNamespace(data_dir=None, workspace=tmp_path)
    with pytest.raises(RuntimeError, match="数据目录"):
        asyncio.run(plugin.initialize())

    plugin.context = SimpleNamespace(data_dir=tmp_path, workspace=None)
    with pytest.raises(RuntimeError, match="workspace"):
        asyncio.run(plugin.initialize())
