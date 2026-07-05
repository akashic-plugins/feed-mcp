from __future__ import annotations

import shutil
from pathlib import Path

from agent.plugins import Plugin


class FeedPlugin(Plugin):
    name = "feed"
    version = "0.1.0"
    desc = "Feed MCP plugin"

    async def initialize(self) -> None:
        data_dir = self.context.data_dir
        workspace = self.context.workspace
        if data_dir is None or workspace is None:
            return
        data_dir.mkdir(parents=True, exist_ok=True)
        if (data_dir / "feed_mcp.sqlite3").exists():
            return
        for source_dir in _legacy_feed_dirs(workspace):
            copied = _copy_legacy_state(source_dir, data_dir)
            if copied:
                return


def _legacy_feed_dirs(workspace: Path) -> list[Path]:
    result: list[Path] = []
    primary = workspace / "mcp" / "feed-mcp"
    if primary.exists():
        result.append(primary)
    backups_root = workspace / "backups"
    if backups_root.exists():
        result.extend(
            sorted(
                backups_root.glob("feed-plugin-migration-*/feed-mcp"),
                reverse=True,
            )
        )
    return result


def _copy_legacy_state(source_dir: Path, data_dir: Path) -> bool:
    copied = False
    for name in ("feed_mcp.sqlite3", "source_scores.json", "feed_cache.db"):
        source = source_dir / name
        target = data_dir / name
        if not source.exists() or target.exists():
            continue
        shutil.copy2(source, target)
        copied = True
    return copied
