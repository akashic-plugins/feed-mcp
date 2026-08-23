#!/usr/bin/env python3
from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


def _runtime_dir() -> Path:
    """返回 Core 注入的数据目录，但不创建或读取其中的文件。"""

    raw = os.environ.get("AKA_PLUGIN_DATA_DIR", "").strip()
    if not raw:
        raise RuntimeError("feed MCP 缺少 AKA_PLUGIN_DATA_DIR")
    return Path(raw).expanduser()


def _setup_logging(runtime_dir: Path) -> None:
    """将诊断写入 stderr 和三个有界本地轮转文件。"""

    runtime_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s | %(message)s"
    )
    file_handler = RotatingFileHandler(
        runtime_dir / "feed_mcp.runtime.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setLevel(logging.INFO)
    stream_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    root.addHandler(file_handler)
    root.addHandler(stream_handler)


def main() -> None:
    # 1. 暴露插件根目录中的共享 Feed domain package。
    script_dir = Path(__file__).resolve().parent
    os.chdir(script_dir)
    for path in (script_dir.parent, script_dir):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))

    # 2. 用有界诊断日志启动用户驱动的 MCP adapter。
    _setup_logging(_runtime_dir())
    from src.mcp_bridge import create_mcp_server

    create_mcp_server().run(transport="stdio")


if __name__ == "__main__":
    main()
