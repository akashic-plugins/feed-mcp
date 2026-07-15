#!/usr/bin/env python3
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


def _runtime_dir() -> Path:
    raw = os.environ.get("AKA_PLUGIN_DATA_DIR", "").strip()
    if not raw:
        raise RuntimeError("feed MCP 缺少 AKA_PLUGIN_DATA_DIR")
    path = Path(raw).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _setup_logging(runtime_dir: Path) -> None:
    runtime_log = runtime_dir / "feed_mcp.runtime.log"
    runtime_log.parent.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s | %(message)s"
    )
    file_handler = RotatingFileHandler(
        runtime_log,
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
    # 1. 切换到脚本目录，保证相对路径（sqlite/json）稳定。
    script_dir = Path(__file__).resolve().parent
    os.chdir(script_dir)
    if str(script_dir) not in sys.path:
        sys.path.insert(0, str(script_dir))

    # 2. 初始化日志（落盘 + stderr）。
    _setup_logging(_runtime_dir())

    # 3. 启动 MCP stdio 服务。
    from src.mcp_bridge import create_mcp_server

    mcp = create_mcp_server()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
