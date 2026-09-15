from __future__ import annotations

import json
import time

from agent.plugin_composition import RUNTIME_STARTED, RUNTIME_STOPPING, Context


api_version = 3
name = "feed"
version = "3.0.0"
desc = "旧轮询 owner 换班 fixture"
inject = ()


def _record(ctx: Context, event: str) -> None:
    root = ctx.data_root
    root.mkdir(parents=True, exist_ok=True)
    with (root / "legacy-owner.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"event": event, "time_ns": time.time_ns()}) + "\n")


async def apply(ctx: Context) -> None:
    """用 runtime 生命周期事件表示旧轮询 owner 的进出。"""

    _ = await ctx.on(RUNTIME_STARTED, lambda _: _record(ctx, "started"))
    _ = await ctx.on(RUNTIME_STOPPING, lambda _: _record(ctx, "stopped"))
