from __future__ import annotations

import shutil
from pathlib import Path
from typing import cast

from pydantic import BaseModel, Field

from agent.plugins import McpServerSpec, Plugin, ProactiveSourceSpec


class FeedProactiveConfig(BaseModel):
    enabled: bool = True
    poll_interval_seconds: int = Field(default=300, ge=1)


class FeedConfig(BaseModel):
    proactive: FeedProactiveConfig = Field(default_factory=FeedProactiveConfig)


class FeedPlugin(Plugin):
    name = "feed"
    version = "1.1.0"
    desc = "Feed MCP plugin"
    ConfigModel = FeedConfig

    @classmethod
    def skill_roots(cls) -> tuple[str, ...]:
        return ("skills",)

    @classmethod
    def mcp_servers(cls) -> list[McpServerSpec]:
        return [
            McpServerSpec(
                name="feed",
                command=("python", "mcp/run_mcp.py"),
            )
        ]

    def proactive_sources(self) -> list[ProactiveSourceSpec]:
        config = cast(FeedConfig, self.context.config)
        if not config.proactive.enabled:
            return []
        return [
            ProactiveSourceSpec(
                id="subscriptions",
                channels=("content",),
                server="feed",
                fetch_tool="get_proactive_events",
                ack_tool="acknowledge_events",
                poll_tool="poll_feeds",
                poll_interval_seconds=config.proactive.poll_interval_seconds,
                fetch_page_size=50,
            )
        ]

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
