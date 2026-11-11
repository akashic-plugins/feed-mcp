from __future__ import annotations

import asyncio
import json
import logging
import sys
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import cast

from plugins.timer.contract import OneShotTimer, TimerHandle, TimerStatus
from plugins.watch.contract import WatchEvents

from .feed_runtime import CONTENT_SOURCE_ID, backend, scripts
from .feed_runtime.events import ScriptOutcome, failed
from .feed_runtime.scripts import ScriptSource


ScriptExecutor = Callable[[ScriptSource], Awaitable[ScriptOutcome]]


class FeedContentRuntime:
    """轮询 Feed，把新条目逐条交给值班，交付成功即在 Feed 里结算。"""

    def __init__(
        self,
        data_root: Path,
        timers: OneShotTimer,
        watch: WatchEvents,
        *,
        now: Callable[[], datetime] | None = None,
        run_script: ScriptExecutor | None = None,
    ) -> None:
        self._data_root = data_root
        self._timers = timers
        self._watch = watch
        self._run_script = run_script
        self._now = now or (lambda: datetime.now(UTC))
        self._handle: TimerHandle | None = None
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        self._log = logging.Logger("feed-content-source", level=logging.INFO)
        self._log.propagate = False
        self._log_handler: RotatingFileHandler | None = None

    async def start(self) -> None:
        """恢复 source deadline，并只注册一个 Timer。"""

        if self._closed:
            raise RuntimeError("Feed Content runtime 已关闭")
        if self._handle is not None:
            return
        self._start_diagnostics()
        # 升级后第一次启动：旧流程积压的"未结算"条目直接标记为已处理，不重交。
        retired = await asyncio.to_thread(
            backend.retire_content_backlog,
            data_root=self._data_root,
            now=self._aware_now(),
        )
        if retired:
            self._log.info("retired legacy backlog items=%d", retired)
        deadline = await asyncio.to_thread(
            backend.content_source_deadline,
            data_root=self._data_root,
            now=self._aware_now(),
        )
        self._arm(deadline)

    async def close(self) -> None:
        """收束进行中的轮询、取消等待并释放诊断日志。"""

        if self._closed:
            return
        self._closed = True
        handle = self._handle
        task = self._task
        if handle is not None:
            _ = await handle.cancel()
        if task is not None and task is not asyncio.current_task():
            await task
        if handle is not None:
            await handle.cleanup()
        self._handle = None
        self._task = None
        self._stop_diagnostics()

    def _arm(self, deadline: datetime) -> None:
        if self._closed or self._handle is not None:
            return
        if deadline.tzinfo is None:
            raise ValueError("Feed Content deadline 必须包含时区")
        handle = self._timers.schedule(deadline)
        self._handle = handle
        self._task = asyncio.create_task(
            self._wait_poll_rearm(handle), name="feed-content-source:poll"
        )

    async def _wait_poll_rearm(self, handle: TimerHandle) -> None:
        """消费一个 Timer，完成一次 source 事务后再注册下一次。"""

        try:
            receipt = await handle.result()
            if receipt.status is TimerStatus.CANCELLED or self._closed:
                return

            # 1. 由唯一外部轮询 owner 刷新持久 Feed 缓存。
            await asyncio.to_thread(
                backend.poll_feeds_only, data_root=self._data_root
            )
            polled_scripts = await self._poll_scripts()
            items = await _prepare_content_items(
                self._data_root, self._aware_now()
            )

            # 2. 逐条交给值班，交付成功立刻结算；失败则下一轮用同一个 key 重试。
            emitted = await self._emit_items(items)

            # 3. 全部交付成功后才能推进 source deadline。
            config = await asyncio.to_thread(
                backend.load_config, self._data_root
            )
            next_due = self._aware_now() + timedelta(
                seconds=config.poll_ttl_seconds
            )
            await asyncio.to_thread(
                backend.commit_content_source_deadline,
                data_root=self._data_root,
                deadline=next_due,
            )
            self._log.info(
                "poll committed items=%d emitted=%d scripts=%d next_due=%s",
                len(items),
                emitted,
                polled_scripts,
                next_due.isoformat(),
            )
        finally:
            self._handle = None
            self._task = None
            await handle.cleanup()
        if not self._closed:
            deadline = await asyncio.to_thread(
                backend.content_source_deadline,
                data_root=self._data_root,
                now=self._aware_now(),
            )
            self._arm(deadline)

    # 串行执行到期的脚本来源：共用一个已登录浏览器的脚本不会互相抢页面；
    # 单个来源失败只记进它自己的轮询状态，不影响其他来源和本轮投递。
    async def _poll_scripts(self) -> int:
        if self._run_script is None:
            return 0
        due = await asyncio.to_thread(
            scripts.due_script_sources, data_root=self._data_root, now=self._aware_now()
        )
        for source in due:
            # 关闭时不再开始新的脚本，卸载最多等当前这一个结束。
            if self._closed:
                break
            try:
                outcome = await self._run_script(source)
            except Exception as error:
                outcome = failed("error", f"{type(error).__name__}: {error}")
            self._log.info(
                "script source=%s v%d status=%s events=%d",
                source.name, source.version, outcome.status, len(outcome.events),
            )
            await asyncio.to_thread(
                scripts.record_outcome, data_root=self._data_root, source=source,
                outcome=outcome, now=self._aware_now(),
            )
        return len(due)

    # 把每条 Feed 条目作为一次 emit 交给值班，并在成功后立刻结算。
    async def _emit_items(self, items: tuple[dict[str, object], ...]) -> int:
        emitted = 0
        for item in items:
            item_id = _string(item["item_id"], "Feed item_id")
            revision = _string(item["revision"], "Feed revision")
            payload = _mapping(item["payload"], "Feed payload")
            title = _text(payload, "title")
            text = _text(payload, "content")
            url = _text(payload, "url")
            # 标题、正文、链接都空的条目没有可交的内容，直接结算掉避免每轮重试。
            if title or text or url:
                _ = await self._watch.emit(
                    _text(payload, "source_name") or CONTENT_SOURCE_ID,
                    title=title or ("" if text else url),
                    text=text,
                    url=url,
                    at=_event_time(payload),
                    key=f"{item_id}:{revision}",
                )
                emitted += 1
            _ = await asyncio.to_thread(
                backend.settle_content_item,
                item_id,
                revision,
                data_root=self._data_root,
            )
        return emitted

    def _aware_now(self) -> datetime:
        value = self._now()
        if value.tzinfo is None:
            raise ValueError("Feed Content clock 必须包含时区")
        return value.astimezone(UTC)

    def _start_diagnostics(self) -> None:
        if self._log_handler is not None:
            return
        self._data_root.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            self._data_root / "feed_source.runtime.log",
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)-8s %(name)s | %(message)s"
            )
        )
        self._log.addHandler(handler)
        self._log_handler = handler

    def _stop_diagnostics(self) -> None:
        handler = self._log_handler
        if handler is None:
            return
        self._log.removeHandler(handler)
        handler.close()
        self._log_handler = None


# 排序是长时间纯 CPU 计算，放进独立进程，宿主事件循环不再与它争 GIL。
async def _prepare_content_items(
    data_root: Path, now: datetime
) -> tuple[dict[str, object], ...]:
    # 1. 子进程按文件路径执行部署版本 backend，-I 不读取宿主的 PYTHON* 环境。
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        str(Path(backend.__file__)),
        "prepare-content-items",
        str(data_root),
        now.isoformat(),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await process.communicate()
    except BaseException:
        # 2. 取消或读取失败时先回收子进程，下一轮不会与残留进程同时写 Feed 数据库。
        await _reap_process(process)
        raise
    if process.returncode != 0:
        detail = stderr.decode("utf-8", "replace").strip()[-2000:]
        raise RuntimeError(
            f"Feed Content 准备进程失败（退出码 {process.returncode}）：{detail}"
        )
    return _prepared_items(json.loads(stdout))


# 被取消的 communicate 已带走管道读取者；kill 后必须继续排空管道，wait 才能返回。
async def _reap_process(process: asyncio.subprocess.Process) -> None:
    # 1. 结束子进程；已退出时只需排空。
    if process.returncode is None:
        process.kill()

    # 2. 由一个受 shield 保护的 communicate 排空 stdout/stderr 并等进程退出。
    drain = asyncio.ensure_future(process.communicate())

    # 3. 清理期间再次取消也继续等待，直到排空任务真正结束。
    while not drain.done():
        try:
            _ = await asyncio.shield(drain)
        except asyncio.CancelledError:
            continue
    _ = drain.result()


def _prepared_items(value: object) -> tuple[dict[str, object], ...]:
    if not isinstance(value, list):
        raise TypeError("Feed Content 准备结果必须是 list")
    return tuple(
        dict(_mapping(item, "Feed Content item"))
        for item in cast(list[object], value)
    )


def _text(payload: Mapping[str, object], field: str) -> str:
    value = payload.get(field)
    return value.strip() if isinstance(value, str) else ""


# 条目的发生时间：优先发布时间，缺失或无法解析时用首次看到的时间。
def _event_time(payload: Mapping[str, object]) -> datetime | None:
    for field in ("published_at", "first_seen_at"):
        raw = _text(payload, field)
        if not raw:
            continue
        try:
            value = datetime.fromisoformat(raw)
        except ValueError:
            continue
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return None


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} 必须是 Mapping")
    return cast(Mapping[str, object], value)


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise TypeError(f"{label} 必须是非空字符串")
    return value
