"""脚本来源：每个来源一份带版本的抓取脚本，由插件运行时在宿主执行。

这里只拥有脚本的存储、到期判断和结果入库；执行方式不在这里，
入库与 RSS 共用 backend 的同一条路径，去重和排序不感知来源类型。
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import backend
from .events import ScriptOutcome

SOURCE_TYPE = "script"
MIN_INTERVAL_SECONDS = 300


@dataclass(frozen=True, slots=True)
class ScriptSource:
    source_id: str
    name: str
    url: str
    version: int
    code: str


def due_script_sources(*, data_root: Path, now: datetime) -> list[ScriptSource]:
    """返回已启用、已有生效脚本且到了轮询间隔的脚本来源。"""
    conn = backend._connect(backend.load_config(data_root))
    try:
        rows = conn.execute(
            """
            SELECT s.id, s.name, s.url, s.poll_interval_seconds, s.script_version,
                   c.code, p.last_polled_at
            FROM sources s
            JOIN source_scripts c ON c.source_id = s.id AND c.version = s.script_version
            LEFT JOIN poll_state p ON p.source_id = s.id
            WHERE s.enabled = 1 AND s.type = ?
            ORDER BY s.added_at ASC
            """,
            (SOURCE_TYPE,),
        ).fetchall()
    finally:
        conn.close()
    due: list[ScriptSource] = []
    for row in rows:
        last = row["last_polled_at"]
        if last and (now - datetime.fromisoformat(str(last))).total_seconds() < int(row["poll_interval_seconds"]):
            continue
        due.append(ScriptSource(str(row["id"]), str(row["name"]), str(row["url"]),
                                int(row["script_version"]), str(row["code"])))
    return due


def record_outcome(*, data_root: Path, source: ScriptSource, outcome: ScriptOutcome, now: datetime) -> None:
    """成功的事件走共用入库路径；失败只记录状态，不动已有条目。"""
    conn = backend._connect(backend.load_config(data_root))
    try:
        # 1. 写锁内核对来源仍启用且仍是这一版脚本；运行期间被暂停或换版的结果直接丢弃
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM sources WHERE id = ? AND enabled = 1 AND script_version = ?",
            (source.source_id, source.version),
        ).fetchone()
        if row is None:
            conn.rollback()
            return

        # 2. 入库并记录健康，与核对在同一事务中提交
        if outcome.succeeded:
            items: list[dict[str, str | None]] = [
                {
                    "entry_id": event.id,
                    "title": event.title,
                    "content": event.content[: backend._MAX_CONTENT],
                    "url": event.url,
                    "author": event.author,
                    "published_at": event.published_at,
                }
                for event in outcome.events
            ]
            _ = backend._ingest_items(conn, row, items, now)
            backend._record_poll_success(conn, source.source_id, now, outcome.status, len(items))
        else:
            detail = "；".join(outcome.issues) or outcome.note or outcome.status
            backend._record_poll_failure(conn, source.source_id, now, outcome.status, f"v{source.version}: {detail}")
        conn.commit()
    finally:
        conn.close()


def describe(outcome: ScriptOutcome) -> str:
    """一次运行的可读报告：试跑结果与入库时保存的都是这一份。"""
    lines = [f"status={outcome.status} events={len(outcome.events)}"]
    if outcome.note:
        lines.append(f"note: {outcome.note[:300]}")
    lines.extend(f"问题: {issue}" for issue in outcome.issues[:20])
    for event in outcome.events[:5]:
        title = (event.title or event.content).replace("\n", " ")[:80]
        lines.append(f"- {event.published_at or '无时间'}  {event.id}  {title}")
    if outcome.events:
        dated = sum(1 for event in outcome.events if event.published_at)
        lines.append(f"带时间 {dated}/{len(outcome.events)}")
    return "\n".join(lines)


def save_script(
    *, data_root: Path, name: str, url: str, code: str, note: str,
    interval_seconds: int | None, report: str,
) -> str:
    """把已试跑通过的脚本存为新版本并立即生效；来源不存在时一并创建。"""
    cfg = backend.load_config(data_root)
    conn = backend._connect(cfg)
    try:
        # 1. 写锁内定位或创建脚本来源；同名的其他类型来源不能被改写。
        #    来源检查、版本分配和生效切换在同一事务里，并发保存不会拿到同一版本号
        conn.execute("BEGIN IMMEDIATE")
        row = _source_by_name(conn, name)
        now = backend._now().isoformat()
        if interval_seconds is not None:
            interval = max(MIN_INTERVAL_SECONDS, interval_seconds)
        elif row is not None:
            interval = int(row["poll_interval_seconds"])
        else:
            interval = max(MIN_INTERVAL_SECONDS, cfg.poll_ttl_seconds)
        if row is not None and str(row["type"]) != SOURCE_TYPE:
            conn.rollback()
            return f"错误：{name!r} 是 {row['type']} 来源，不能保存脚本"
        target_url = url.strip() or (str(row["url"]) if row is not None else "")
        if not target_url:
            conn.rollback()
            return "错误：新建脚本来源需要 url"
        duplicate = conn.execute(
            "SELECT name FROM sources WHERE url = ? AND id != ? LIMIT 1",
            (target_url, "" if row is None else str(row["id"])),
        ).fetchone()
        if duplicate is not None:
            conn.rollback()
            return f"错误：该地址已被来源 {str(duplicate['name'])!r} 使用"
        if row is None:
            source_id = str(uuid.uuid4())
            conn.execute(
                """
                INSERT INTO sources (id, type, name, url, note, enabled, poll_interval_seconds, added_at, updated_at)
                VALUES (?, ?, ?, ?, NULL, 1, ?, ?, ?)
                """,
                (source_id, SOURCE_TYPE, name.strip(), target_url, interval, now, now),
            )
        else:
            source_id = str(row["id"])

        # 2. 写入新版本并切换生效版本；清空轮询时间，下一轮立即用新脚本抓取
        version = 1 + int(conn.execute(
            "SELECT coalesce(max(version), 0) FROM source_scripts WHERE source_id = ?", (source_id,)
        ).fetchone()[0])
        conn.execute(
            "INSERT INTO source_scripts (source_id, version, code, note, test_report, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (source_id, version, code, note.strip() or None, report, now),
        )
        conn.execute(
            "UPDATE sources SET url = ?, script_version = ?, poll_interval_seconds = ?, updated_at = ? WHERE id = ?",
            (target_url, version, interval, now, source_id),
        )
        conn.execute("UPDATE poll_state SET last_polled_at = NULL WHERE source_id = ?", (source_id,))
        conn.commit()
        return f"已保存 {name.strip()!r} 脚本 v{version} 并生效，下一轮轮询开始抓取"
    finally:
        conn.close()


def script_history(*, data_root: Path, name: str) -> str:
    conn = backend._connect(backend.load_config(data_root))
    try:
        row = _source_by_name(conn, name)
        if row is None or str(row["type"]) != SOURCE_TYPE:
            return f"没有名为 {name!r} 的脚本来源"
        versions = conn.execute(
            "SELECT version, note, created_at, test_report FROM source_scripts WHERE source_id = ? ORDER BY version DESC",
            (str(row["id"]),),
        ).fetchall()
        lines = [f"{row['name']}  {row['url']}  生效 v{row['script_version']}"]
        for item in versions:
            mark = "*" if item["version"] == row["script_version"] else " "
            first = str(item["test_report"]).splitlines()[0]
            lines.append(f"{mark} v{item['version']}  {item['created_at'][:16]}  {first}  {item['note'] or ''}".rstrip())
        return "\n".join(lines)
    finally:
        conn.close()


def script_code(*, data_root: Path, name: str, version: int | None) -> str | None:
    conn = backend._connect(backend.load_config(data_root))
    try:
        row = _source_by_name(conn, name)
        if row is None or str(row["type"]) != SOURCE_TYPE:
            return None
        found = conn.execute(
            "SELECT code FROM source_scripts WHERE source_id = ? AND version = ?",
            (str(row["id"]), version or row["script_version"]),
        ).fetchone()
        return None if found is None else str(found["code"])
    finally:
        conn.close()


def use_version(*, data_root: Path, name: str, version: int) -> str:
    conn = backend._connect(backend.load_config(data_root))
    try:
        row = _source_by_name(conn, name)
        if row is None or str(row["type"]) != SOURCE_TYPE:
            return f"没有名为 {name!r} 的脚本来源"
        exists = conn.execute(
            "SELECT 1 FROM source_scripts WHERE source_id = ? AND version = ?", (str(row["id"]), version)
        ).fetchone()
        if exists is None:
            return f"错误：{name!r} 没有 v{version}"
        conn.execute(
            "UPDATE sources SET script_version = ?, updated_at = ? WHERE id = ?",
            (version, backend._now().isoformat(), str(row["id"])),
        )
        conn.execute("UPDATE poll_state SET last_polled_at = NULL WHERE source_id = ?", (str(row["id"]),))
        conn.commit()
        return f"已切换 {str(row['name'])!r} 到脚本 v{version}，下一轮轮询生效"
    finally:
        conn.close()


def _source_by_name(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    rows = conn.execute("SELECT * FROM sources WHERE lower(name) = lower(?)", (name.strip(),)).fetchall()
    return rows[0] if len(rows) == 1 else None
