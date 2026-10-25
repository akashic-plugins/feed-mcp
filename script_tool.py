from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agent.plugin_composition import Context
from plugins.ledger.contract import ContentPart
from plugins.tools.contract import TOOLS, CallSource, Result, ToolRef

from .feed_runtime import scripts
from .script_runner import ScriptRunner

_DESCRIPTION = (
    "管理脚本来源：没有 RSS 的页面由一段宿主 shell 脚本抓取，最后一行输出事件 JSON。"
    "test 试跑并校验格式；save 重新试跑，通过才保存为新版本并开始定时抓取；"
    "show/history/use 查看代码、版本历史和回滚。格式与流程见 feed-manage skill。"
)


class ScriptArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["test", "save", "show", "history", "use"]
    name: str = Field(default="", description="来源名；save/show/history/use 必填")
    url: str = Field(default="", description="来源页面地址；新建来源时必填")
    code: str = Field(default="", description="test/save：完整 shell 脚本")
    note: str = Field(default="", description="save：这一版改了什么")
    interval_seconds: int | None = Field(default=None, ge=scripts.MIN_INTERVAL_SECONDS, description="save：轮询间隔")
    version: int | None = Field(default=None, ge=1, description="show/use：版本号；show 缺省为生效版本")


class FeedScriptTool:
    """一次调用只做一个动作；试跑与保存都在宿主真实执行脚本。"""

    idempotent = False

    def __init__(self, ctx: Context, runner: ScriptRunner) -> None:
        self._ctx = ctx
        self._runner = runner

    async def prepare(
        self, arguments: Mapping[str, object], source: CallSource | None = None,
    ) -> Mapping[str, object] | str:
        try:
            return ScriptArgs.model_validate(dict(arguments)).model_dump()
        except ValidationError as error:
            return f"参数错误：{error}"

    async def invoke(self, key: str, arguments: Mapping[str, object]) -> Result:
        args = ScriptArgs.model_validate(dict(arguments))
        text, ok = await self._dispatch(args)
        return Result("success" if ok else "error", (ContentPart("text", text),))

    async def query(self, key: str) -> Result | None:
        return None

    async def _dispatch(self, args: ScriptArgs) -> tuple[str, bool]:
        root = self._ctx.data_root
        if args.action in {"test", "save"} and not args.code.strip():
            return "错误：test/save 需要 code", False
        if args.action != "test" and not args.name.strip():
            return f"错误：{args.action} 需要 name", False
        if args.action == "test":
            outcome = await self._runner.run(owner=f"feed-script-test:{uuid.uuid4().hex}", code=args.code)
            return scripts.describe(outcome), outcome.succeeded
        if args.action == "save":
            return await self._save(args)
        if args.action == "show":
            code = await asyncio.to_thread(scripts.script_code, data_root=root, name=args.name, version=args.version)
            return (code, True) if code is not None else (f"没有找到 {args.name!r} 的脚本", False)
        if args.action == "history":
            text = await asyncio.to_thread(scripts.script_history, data_root=root, name=args.name)
            return text, not text.startswith("没有")
        if args.version is None:
            return "错误：use 需要 version", False
        text = await asyncio.to_thread(scripts.use_version, data_root=root, name=args.name, version=args.version)
        return text, text.startswith("已切换")

    # 保存前重新试跑：只有本次真实输出 status=ok 的脚本才能成为生效版本。
    async def _save(self, args: ScriptArgs) -> tuple[str, bool]:
        outcome = await self._runner.run(owner=f"feed-script-test:{uuid.uuid4().hex}", code=args.code)
        report = scripts.describe(outcome)
        if outcome.status != "ok":
            return f"未保存：试跑必须是 status=ok 且至少一条事件\n{report}", False
        text = await asyncio.to_thread(
            scripts.save_script, data_root=self._ctx.data_root, name=args.name, url=args.url,
            code=args.code, note=args.note, interval_seconds=args.interval_seconds, report=report,
        )
        return f"{text}\n{report}", text.startswith("已保存")


async def register_script_tool(ctx: Context, runner: ScriptRunner) -> ToolRef:
    @asynccontextmanager
    async def open_tool(_options: Mapping[str, object]) -> AsyncIterator[FeedScriptTool]:
        yield FeedScriptTool(ctx, runner)

    return await ctx.require(TOOLS).register(
        ctx,
        name="feed_script",
        description=_DESCRIPTION,
        parameters=cast(Mapping[str, object], ScriptArgs.model_json_schema()),
        open=open_tool,
    )
