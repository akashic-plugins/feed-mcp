#!/usr/bin/env python3
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path


def _runtime_dir() -> Path:
    """返回 Core 注入的数据目录，但不创建或读取其中的文件。"""

    raw = os.environ.get("AKA_PLUGIN_DATA_DIR", "").strip()
    if not raw:
        raise RuntimeError("feed MCP 缺少 AKA_PLUGIN_DATA_DIR")
    return Path(raw).expanduser()


def _setup_logging() -> None:
    """把 MCP 日志绑定到 stderr，不创建运行日志文件。"""

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s | %(message)s"
    )
    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setLevel(logging.INFO)
    stream_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    root.addHandler(stream_handler)


def main() -> None:
    # 1. 校验 Core 注入的数据目录变量，但不创建或读取运行态文件。
    _runtime_dir()

    # 2. 切换到脚本目录，保证相对代码路径稳定。
    script_dir = Path(__file__).resolve().parent
    os.chdir(script_dir)
    if str(script_dir) not in sys.path:
        sys.path.insert(0, str(script_dir))

    # 3. 启动 MCP stdio 服务；日志只经过 stderr。
    _setup_logging()
    from src.mcp_bridge import create_mcp_server

    create_mcp_server().run(transport="stdio")


if __name__ == "__main__":
    main()
