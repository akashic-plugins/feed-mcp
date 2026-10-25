"""脚本来源的输出契约：一次运行 = 一个状态 + 一组格式固定的事件。

脚本只回答"这一页上现在有哪些事件"，不判断新旧、不保存状态；
去重、排序和投递都由 Feed 负责。契约写死在这里，脚本必须收敛到它。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, cast

ScriptStatus = Literal["ok", "empty", "blocked", "login_required", "stale", "error"]
OutcomeStatus = ScriptStatus | Literal["invalid", "timeout", "unavailable"]

SCRIPT_STATUSES: tuple[ScriptStatus, ...] = (
    "ok", "empty", "blocked", "login_required", "stale", "error",
)
EVENT_FIELDS = frozenset({"id", "url", "title", "content", "author", "published_at"})
MAX_EVENTS = 100
_MAX_TITLE = 200
_MAX_ID = 512


@dataclass(frozen=True, slots=True)
class FeedEvent:
    id: str
    url: str
    title: str
    content: str
    author: str | None
    published_at: str | None


@dataclass(frozen=True, slots=True)
class ScriptOutcome:
    status: OutcomeStatus
    events: tuple[FeedEvent, ...]
    note: str
    issues: tuple[str, ...]

    @property
    def succeeded(self) -> bool:
        return self.status in {"ok", "empty"}


def failed(status: OutcomeStatus, note: str) -> ScriptOutcome:
    return ScriptOutcome(status, (), note[-2000:], ())


# 解析一次运行的输出：最后一个非空行是结果 JSON，之前的行都当作诊断日志。
def parse_output(output: str) -> ScriptOutcome:
    # 1. 取结果行并解析外层信封
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        return ScriptOutcome("invalid", (), "", ("没有输出；最后一行必须是结果 JSON",))
    try:
        raw = json.loads(lines[-1])
    except json.JSONDecodeError as error:
        return ScriptOutcome("invalid", (), lines[-1][:300], (f"最后一行不是 JSON：{error.msg}",))
    if not isinstance(raw, dict):
        return ScriptOutcome("invalid", (), "", ("结果必须是 JSON 对象",))
    envelope = cast(dict[str, object], raw)
    issues = [f"未知字段 {key!r}" for key in envelope if key not in {"status", "events", "note"}]
    status = envelope.get("status")
    note = envelope.get("note", "")
    if status not in SCRIPT_STATUSES:
        issues.append(f"status 必须是 {'|'.join(SCRIPT_STATUSES)}")
    if not isinstance(note, str):
        issues.append("note 必须是字符串")
        note = ""
    raw_events = envelope.get("events", [])
    if not isinstance(raw_events, list):
        issues.append("events 必须是数组")
        raw_events = []
    elif len(cast(list[object], raw_events)) > MAX_EVENTS:
        issues.append(f"events 最多 {MAX_EVENTS} 条，实际 {len(cast(list[object], raw_events))} 条；只输出页面当前最新的部分")

    # 2. 逐条校验事件，并检查同一次输出里的 id 唯一
    events: list[FeedEvent] = []
    seen: set[str] = set()
    for index, item in enumerate(cast(list[object], raw_events)[:MAX_EVENTS]):
        event = _event(item, index, issues)
        if event is None:
            continue
        if event.id in seen:
            issues.append(f"events[{index}].id 重复：{event.id}")
            continue
        seen.add(event.id)
        events.append(event)

    # 3. 状态与事件数量必须一致，"没更新"和"抓不到"不能混为一谈
    if status == "ok" and not events and not issues:
        issues.append("status=ok 至少要有一条事件；页面确实没有内容时用 empty")
    if status == "empty" and events:
        issues.append("status=empty 不能携带事件")
    if issues:
        return ScriptOutcome("invalid", (), note, tuple(issues))
    if status not in {"ok", "empty"}:
        return ScriptOutcome(cast(ScriptStatus, status), (), note, ())
    return ScriptOutcome(cast(ScriptStatus, status), tuple(events), note, ())


def _event(item: object, index: int, issues: list[str]) -> FeedEvent | None:
    where = f"events[{index}]"
    if not isinstance(item, dict):
        issues.append(f"{where} 必须是对象")
        return None
    value = cast(dict[str, object], item)
    before = len(issues)
    issues.extend(f"{where} 未知字段 {key!r}" for key in value if key not in EVENT_FIELDS)
    texts: dict[str, str] = {}
    for key in EVENT_FIELDS:
        field = value.get(key)
        if field is None:
            continue
        if not isinstance(field, str):
            issues.append(f"{where}.{key} 必须是字符串")
            continue
        texts[key] = field.strip()
    event_id, url = texts.get("id", ""), texts.get("url", "")
    if not event_id or len(event_id) > _MAX_ID:
        issues.append(f"{where}.id 必须是 1–{_MAX_ID} 字符的稳定标识")
    if not url.startswith(("https://", "http://")):
        issues.append(f"{where}.url 必须是 http(s) 地址")
    if not (texts.get("title") or texts.get("content")):
        issues.append(f"{where} title 与 content 至少要有一个")
    published_at = _published_at(texts.get("published_at"), where, issues)
    if len(issues) > before:
        return None
    return FeedEvent(
        id=event_id,
        url=url,
        title=texts.get("title", "")[:_MAX_TITLE],
        content=texts.get("content", ""),
        author=texts.get("author") or None,
        published_at=published_at,
    )


def _published_at(raw: str | None, where: str, issues: list[str]) -> str | None:
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        issues.append(f"{where}.published_at 必须是 ISO-8601 时间")
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()
