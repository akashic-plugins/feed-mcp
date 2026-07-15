from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.path_config import resolve_feed_db, resolve_workspace_path


def test_explicit_paths_have_priority(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("AKA_PLUGIN_DATA_DIR", raising=False)
    monkeypatch.delenv("AKASHIC_WORKSPACE", raising=False)

    assert resolve_feed_db(tmp_path / "feed.db") == tmp_path / "feed.db"
    assert resolve_workspace_path(tmp_path / "sessions.db", "ignored") == tmp_path / "sessions.db"


def test_environment_paths_are_isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    data_dir = tmp_path / "plugin-data"
    workspace = tmp_path / "workspace"
    monkeypatch.setenv("AKA_PLUGIN_DATA_DIR", str(data_dir))
    monkeypatch.setenv("AKASHIC_WORKSPACE", str(workspace))

    assert resolve_feed_db(None) == data_dir / "feed_mcp.sqlite3"
    assert resolve_workspace_path(None, "sessions.db") == workspace / "sessions.db"


def test_missing_environment_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AKA_PLUGIN_DATA_DIR", "   ")
    monkeypatch.setenv("AKASHIC_WORKSPACE", "   ")

    with pytest.raises(RuntimeError, match="AKA_PLUGIN_DATA_DIR"):
        resolve_feed_db(None)
    with pytest.raises(RuntimeError, match="AKASHIC_WORKSPACE"):
        resolve_workspace_path(None, "sessions.db")


def test_blank_explicit_paths_fail_loudly() -> None:
    with pytest.raises(RuntimeError, match="feed-db"):
        resolve_feed_db(Path("   "))
    with pytest.raises(RuntimeError, match="不能为空"):
        resolve_workspace_path(Path("   "), "sessions.db")
