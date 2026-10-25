from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agent.plugin_composition import Context, ServiceKey
from plugins.ledger.contract import ContentPart
from plugins.tools.contract import TOOLS, CallSource, Result, ToolRef, ToolView

from .feed_runtime import backend


FEED_TOOLS = ServiceKey[ToolView]("feed.tools.v1")


class ManageArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["list", "subscribe", "unsubscribe", "pause", "resume", "update"]
    name: str = Field(default="", description="来源名；pause/resume/update 需完整名称，unsubscribe 按包含匹配")
    url: str = Field(default="", description="subscribe/update：RSS 地址")
    source_type: Literal["rss", "novel-kb"] = "rss"
    note: str = ""


class QueryArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["latest", "search", "summary", "catalog"]
    source: str = Field(default="", description="按来源名包含匹配过滤")
    keyword: str = Field(default="", description="search 必填")
    limit: int = Field(default=5, ge=1, le=30, description="latest/search 返回条数")
    page: int = Field(default=1, ge=1, description="catalog 页码")
    page_size: int = Field(default=20, ge=1, le=100, description="catalog 每页条数")


def _manage(args: ManageArgs, data_root: Path) -> str:
    return backend.feed_manage(
        args.action, args.name, args.url, args.source_type, args.note, data_root=data_root,
    )


def _query(args: QueryArgs, data_root: Path) -> str:
    return backend.feed_query(
        args.action, args.source, args.keyword, args.limit, args.page, args.page_size,
        data_root=data_root,
    )


class FeedTool[A: BaseModel]:
    """一次调用：校验参数后在线程里读写 Feed 数据库，结果文本原样返回。"""

    def __init__(self, model: type[A], run: Callable[[A, Path], str], data_root: Path, *, idempotent: bool):
        self._model = model
        self._run = run
        self._data_root = data_root
        self.idempotent = idempotent

    async def prepare(
        self, arguments: Mapping[str, object], source: CallSource | None = None,
    ) -> Mapping[str, object] | str:
        try:
            return self._model.model_validate(dict(arguments)).model_dump()
        except ValidationError as error:
            return f"参数错误：{error}"

    async def invoke(self, key: str, arguments: Mapping[str, object]) -> Result:
        text = await asyncio.to_thread(self._run, self._model.model_validate(dict(arguments)), self._data_root)
        outcome = "error" if text.startswith("错误") else "success"
        return Result(outcome, (ContentPart("text", text),))

    async def query(self, key: str) -> Result | None:
        # 不保存调用回执；写操作不能据此重放。
        return None


async def register_tools(ctx: Context, *, description: str) -> None:
    """声明 Feed 工具组并注册订阅管理与缓存查询；脚本工具由宿主执行子 Fiber 另行注册。"""
    tools = ctx.require(TOOLS)
    await tools.declare_group(ctx, description=description)
    refs = [
        await _register(
            ctx, "feed_manage", ManageArgs, _manage, idempotent=False,
            description=(
                "管理订阅：list 列出全部来源（类型、健康、最近错误）；subscribe 添加 RSS；"
                "unsubscribe 删除来源及其历史；pause/resume 暂停或恢复（保留历史）；update 更换 RSS 地址。"
                "没有 RSS 的页面用 feed_script 接入。"
            ),
        ),
        await _register(
            ctx, "feed_query", QueryArgs, _query, idempotent=True,
            description=(
                "查询已抓取的缓存，不触发抓取：latest 最近条目，search 关键词检索，"
                "summary 订阅概况，catalog 分页条目清单（JSON）。"
            ),
        ),
    ]
    await ctx.provide(FEED_TOOLS, tools.view(*refs))


async def _register[A: BaseModel](
    ctx: Context, name: str, model: type[A], run: Callable[[A, Path], str],
    *, idempotent: bool, description: str,
) -> ToolRef:
    @asynccontextmanager
    async def open_tool(_options: Mapping[str, object]) -> AsyncIterator[FeedTool[A]]:
        yield FeedTool(model, run, ctx.data_root, idempotent=idempotent)

    return await ctx.require(TOOLS).register(
        ctx,
        name=name,
        description=description,
        parameters=cast(Mapping[str, object], model.model_json_schema()),
        open=open_tool,
        idempotent=idempotent,
    )
