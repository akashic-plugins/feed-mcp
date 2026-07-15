from __future__ import annotations

import os
from pathlib import Path


def resolve_feed_db(explicit: Path | None) -> Path:
    if explicit is not None:
        raw_explicit = str(explicit).strip()
        if not raw_explicit:
            raise RuntimeError("--feed-db 不能为空")
        return Path(raw_explicit).expanduser()
    data_dir = os.environ.get("AKA_PLUGIN_DATA_DIR", "").strip()
    if not data_dir:
        raise RuntimeError("未提供 --feed-db，且缺少 AKA_PLUGIN_DATA_DIR")
    return Path(data_dir).expanduser() / "feed_mcp.sqlite3"


def resolve_workspace_path(explicit: Path | None, *parts: str) -> Path:
    if explicit is not None:
        raw_explicit = str(explicit).strip()
        if not raw_explicit:
            raise RuntimeError("数据库路径不能为空")
        return Path(raw_explicit).expanduser()
    workspace = os.environ.get("AKASHIC_WORKSPACE", "").strip()
    if not workspace:
        raise RuntimeError("未提供数据库路径，且缺少 AKASHIC_WORKSPACE")
    return Path(workspace).expanduser().joinpath(*parts)
