from __future__ import annotations

import json
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from mcp.server.fastmcp import FastMCP


def _record(event: str) -> None:
    root = Path(os.environ["AKA_PLUGIN_DATA_DIR"])
    root.mkdir(parents=True, exist_ok=True)
    with (root / "legacy-owner.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"event": event, "time_ns": time.time_ns()}) + "\n")


@asynccontextmanager
async def _lifespan(_: FastMCP) -> AsyncIterator[None]:
    _record("started")
    try:
        yield None
    finally:
        _record("stopped")


server = FastMCP("legacy-feed-owner", lifespan=_lifespan)


@server.tool()
def legacy_status() -> str:
    return "ready"


if __name__ == "__main__":
    server.run(transport="stdio")
