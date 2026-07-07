"""
feed-mcp backend

最小实现：
1. 用 sqlite 管理订阅源和条目
2. 按需轮询 RSS/Atom
3. 对外提供 proactive content 事件与基础管理查询能力
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import sqlite3
import subprocess
import time
import uuid
import xml.etree.ElementTree as ET
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import requests

logger = logging.getLogger(__name__)


_MAX_CONTENT = 300
_SUMMARY_MAX_CHARS = 400
_DEFAULT_CONFIG = {
    "db_path": "./feed_mcp.sqlite3",
    "poll_ttl_seconds": 300,
    "item_retention_hours": 72,
    "max_items_per_source": 100,
    "max_content_events": 50,
    "rank_mode": "shadow",
    "rank_impression_limit": 5,
    "rank_model_learning_rate": 0.08,
}


@dataclass
class FeedMcpConfig:
    db_path: Path
    poll_ttl_seconds: int
    item_retention_hours: int
    max_items_per_source: int
    max_content_events: int
    rank_mode: str
    rank_impression_limit: int
    rank_model_learning_rate: float


def _config_path() -> Path:
    return Path(__file__).resolve().parent.parent / "feed_mcp.json"


def _runtime_root() -> Path:
    raw = os.environ.get("AKA_PLUGIN_DATA_DIR", "").strip()
    if not raw:
        return _config_path().parent
    path = Path(raw).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path

def load_config() -> FeedMcpConfig:
    raw = dict(_DEFAULT_CONFIG)
    path = _config_path()
    if path.exists():
        raw.update(json.loads(path.read_text()))
    db_path = Path(str(raw["db_path"]))
    if not db_path.is_absolute():
        db_path = (_runtime_root() / db_path).resolve()
    return FeedMcpConfig(
        db_path=db_path,
        poll_ttl_seconds=max(60, int(raw["poll_ttl_seconds"])),
        item_retention_hours=max(
            1,
            int(raw.get("item_retention_hours", int(raw.get("item_retention_days", 3)) * 24)),
        ),
        max_items_per_source=max(1, int(raw.get("max_items_per_source", 100))),
        max_content_events=max(1, int(raw["max_content_events"])),
        rank_mode=str(raw.get("rank_mode", "shadow")),
        rank_impression_limit=max(1, int(raw.get("rank_impression_limit", 5))),
        rank_model_learning_rate=max(0.001, float(raw.get("rank_model_learning_rate", 0.08))),
    )


def _connect(cfg: FeedMcpConfig) -> sqlite3.Connection:
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sources (
            id TEXT PRIMARY KEY,
            type TEXT NOT NULL,
            name TEXT NOT NULL,
            url TEXT NOT NULL,
            note TEXT,
            enabled INTEGER NOT NULL,
            poll_interval_seconds INTEGER NOT NULL,
            added_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS items (
            event_id TEXT PRIMARY KEY,
            source_id TEXT NOT NULL,
            source_name TEXT NOT NULL,
            source_type TEXT NOT NULL,
            title TEXT,
            content TEXT NOT NULL,
            url TEXT,
            author TEXT,
            published_at TEXT,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            emitted_at TEXT,
            content_hash TEXT NOT NULL
        )
        """
    )
    columns = {
        str(row["name"])
        for row in conn.execute("PRAGMA table_info(items)").fetchall()
    }
    if "author" not in columns:
        conn.execute("ALTER TABLE items ADD COLUMN author TEXT")
    if "interest_ok" not in columns:
        conn.execute("ALTER TABLE items ADD COLUMN interest_ok INTEGER")
    if "interest_scored_at" not in columns:
        conn.execute("ALTER TABLE items ADD COLUMN interest_scored_at TEXT")
    for column in ("served_count", "last_served_at", "rank_score", "is_canonical"):
        if column in columns:
            conn.execute(f"ALTER TABLE items DROP COLUMN {column}")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS acked_items (
            event_id TEXT PRIMARY KEY,
            acked_at TEXT NOT NULL,
            expires_at TEXT NOT NULL
        )
        """
    )
    conn.execute("DROP TABLE IF EXISTS pending_items")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS poll_state (
            source_id TEXT PRIMARY KEY,
            last_polled_at TEXT,
            last_success_at TEXT,
            last_error TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS rank_stats (
            key TEXT PRIMARY KEY,
            pos INTEGER NOT NULL DEFAULT 0,
            neg INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS rank_impressions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL,
            served_at TEXT NOT NULL,
            position INTEGER NOT NULL,
            rank_score REAL NOT NULL,
            features_json TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS rank_model_weights (
            key TEXT PRIMARY KEY,
            weight REAL NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS rank_model_updates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL,
            label INTEGER NOT NULL,
            prediction REAL NOT NULL,
            error REAL NOT NULL,
            feature_count INTEGER NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    _ensure_rank_stats(conn)
    _ensure_rank_model(conn, cfg)
    _normalize_existing_source_urls(conn)
    _normalize_existing_item_urls(conn)
    _normalize_existing_xcancel_items(conn)
    return conn


def _now() -> datetime:
    return datetime.now(UTC)


def _cleanup(conn: sqlite3.Connection, cfg: FeedMcpConfig) -> None:
    now = _now()
    conn.execute(
        "DELETE FROM acked_items WHERE expires_at <= ?",
        (now.isoformat(),),
    )
    _delete_expired_items(conn, cfg, now)
    _trim_items_per_source(conn, cfg.max_items_per_source)
    conn.commit()


def _delete_expired_items(conn: sqlite3.Connection, cfg: FeedMcpConfig, now: datetime) -> None:
    cutoff = (now - timedelta(hours=cfg.item_retention_hours)).isoformat()
    conn.execute(
        """
        DELETE FROM items
        WHERE coalesce(published_at, last_seen_at) <= ?
        """,
        (cutoff,),
    )


def _trim_items_per_source(conn: sqlite3.Connection, max_items_per_source: int) -> None:
    rows = conn.execute(
        """
        SELECT event_id, source_id
        FROM items
        ORDER BY coalesce(published_at, last_seen_at) DESC, last_seen_at DESC
        """
    ).fetchall()
    keep_counts: dict[str, int] = {}
    to_delete: list[str] = []
    for row in rows:
        source_id = str(row["source_id"])
        keep_counts[source_id] = keep_counts.get(source_id, 0) + 1
        if keep_counts[source_id] > max_items_per_source:
            to_delete.append(str(row["event_id"]))
    if not to_delete:
        return
    conn.executemany("DELETE FROM items WHERE event_id = ?", [(event_id,) for event_id in to_delete])


def _delete_source_history(conn: sqlite3.Connection, source_ids: list[str]) -> tuple[int, int]:
    if not source_ids:
        return 0, 0
    source_placeholders = ",".join("?" for _ in source_ids)
    event_rows = conn.execute(
        f"SELECT event_id FROM items WHERE source_id IN ({source_placeholders})",
        source_ids,
    ).fetchall()
    event_ids = [str(row["event_id"]) for row in event_rows]

    deleted_acked = 0
    if event_ids:
        event_placeholders = ",".join("?" for _ in event_ids)
        cursor = conn.execute(
            f"DELETE FROM acked_items WHERE event_id IN ({event_placeholders})",
            event_ids,
        )
        deleted_acked = max(0, cursor.rowcount)

    cursor = conn.execute(
        f"DELETE FROM items WHERE source_id IN ({source_placeholders})",
        source_ids,
    )
    deleted_items = max(0, cursor.rowcount)
    conn.execute(
        f"DELETE FROM poll_state WHERE source_id IN ({source_placeholders})",
        source_ids,
    )
    return deleted_items, deleted_acked


def _stable_event_id(source_id: str, url: str, title: str, published_at: str | None) -> str:
    item_key = _canonical_item_key(url or "")
    if item_key.startswith("xstatus:"):
        raw = "|".join([source_id, item_key])
    else:
        raw = "|".join([source_id, item_key, title or "", published_at or ""])
    return "fmcp_" + hashlib.sha1(raw.encode()).hexdigest()[:16]


def _normalize_item_url(url: str | None) -> str | None:
    raw = (url or "").strip()
    if not raw:
        return None
    normalized_x_url = _normalize_x_status_url(raw)
    if normalized_x_url:
        return normalized_x_url
    return raw


def _canonical_item_key(url: str) -> str:
    raw = (url or "").strip()
    if not raw:
        return ""
    status_id = _extract_x_status_id(raw)
    if status_id:
        return f"xstatus:{status_id}"
    return raw


def _extract_x_status_id(url: str) -> str | None:
    parsed = urlparse((url or "").strip())
    host = (parsed.netloc or "").lower()
    if host not in {
        "twitter.com",
        "www.twitter.com",
        "x.com",
        "www.x.com",
        "xcancel.com",
        "rss.xcancel.com",
        "nitter.net",
        "www.nitter.net",
    }:
        return None
    match = re.search(r"/status/(\d+)", parsed.path or "")
    if not match:
        return None
    return match.group(1)


def _normalize_x_status_url(url: str) -> str | None:
    parsed = urlparse((url or "").strip())
    host = (parsed.netloc or "").lower()
    if host not in {
        "twitter.com",
        "www.twitter.com",
        "x.com",
        "www.x.com",
        "mobile.twitter.com",
        "mobile.x.com",
        "xcancel.com",
        "rss.xcancel.com",
        "nitter.net",
        "www.nitter.net",
    }:
        return None
    match = re.search(r"/([^/]+)/status/(\d+)", parsed.path or "")
    if not match:
        return None
    username = match.group(1).lstrip("@")
    status_id = match.group(2)
    return f"https://nitter.net/{username}/status/{status_id}#m"


def _normalize_source_url(url: str) -> str:
    raw = (url or "").strip()
    if not raw:
        return raw
    username = _extract_x_username(raw)
    if username:
        return f"https://nitter.net/{username}/rss"
    return raw


def _extract_x_username(url: str) -> str | None:
    parsed = urlparse((url or "").strip())
    host = (parsed.netloc or "").lower()
    if host not in {
        "twitter.com",
        "www.twitter.com",
        "x.com",
        "www.x.com",
        "mobile.twitter.com",
        "mobile.x.com",
        "xcancel.com",
        "rss.xcancel.com",
        "nitter.net",
        "www.nitter.net",
    }:
        return None
    parts = [part for part in (parsed.path or "").split("/") if part]
    if not parts:
        return None
    first = parts[0]
    if first.lower() in {"home", "explore", "search", "i", "intent", "share", "hashtag"}:
        return None
    return first.lstrip("@") or None


def _normalize_existing_xcancel_items(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        """
        SELECT event_id, source_id, title, published_at, url
        FROM items
        WHERE url LIKE '%twitter.com/%'
           OR url LIKE '%x.com/%'
           OR url LIKE '%xcancel.com/%'
        """
    ).fetchall()
    for row in rows:
        normalized_event_id = _stable_event_id(
            str(row["source_id"]),
            str(row["url"] or ""),
            str(row["title"] or ""),
            str(row["published_at"] or "").strip() or None,
        )
        if normalized_event_id != row["event_id"]:
            exists = conn.execute(
                "SELECT 1 FROM items WHERE event_id = ? LIMIT 1",
                (normalized_event_id,),
            ).fetchone()
            if exists is not None:
                conn.execute("DELETE FROM items WHERE event_id = ?", (row["event_id"],))
                continue
        conn.execute(
            "UPDATE items SET event_id = ? WHERE event_id = ?",
            (normalized_event_id, row["event_id"]),
        )
    conn.commit()


def _normalize_existing_item_urls(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        """
        SELECT event_id, url
        FROM items
        WHERE url LIKE '%twitter.com/%'
           OR url LIKE '%x.com/%'
           OR url LIKE '%xcancel.com/%'
        """
    ).fetchall()
    for row in rows:
        normalized_url = _normalize_item_url(str(row["url"] or ""))
        if not normalized_url or normalized_url == row["url"]:
            continue
        conn.execute(
            "UPDATE items SET url = ? WHERE event_id = ?",
            (normalized_url, str(row["event_id"])),
        )
    conn.commit()


def _normalize_existing_source_urls(conn: sqlite3.Connection) -> None:
    rows = conn.execute("SELECT id, url FROM sources").fetchall()
    for row in rows:
        normalized_url = _normalize_source_url(str(row["url"] or ""))
        if normalized_url == row["url"]:
            continue
        duplicate = conn.execute(
            "SELECT id FROM sources WHERE url = ? AND id != ? LIMIT 1",
            (normalized_url, str(row["id"])),
        ).fetchone()
        if duplicate is not None:
            conn.execute("DELETE FROM sources WHERE id = ?", (str(row["id"]),))
            continue
        conn.execute(
            "UPDATE sources SET url = ?, updated_at = ? WHERE id = ?",
            (normalized_url, _now().isoformat(), str(row["id"])),
        )
    conn.commit()


def _parse_dt(value: str | None) -> str | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.isoformat()
    except Exception:
        pass
    try:
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.isoformat()
    except Exception:
        return None


def _strip(text: str | None) -> str:
    return (text or "").strip()


def _xml_local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find_child_text(el: ET.Element, *names: str) -> str:
    wanted = {name.lower() for name in names}
    for child in el:
        if _xml_local_name(child.tag).lower() in wanted:
            return _strip(child.text)
    return ""


def _parse_rss(xml_text: str) -> list[dict[str, str | None]]:
    xml_text = _normalize_xml_text(xml_text)
    if _is_xcancel_whitelist_feed(xml_text):
        return []
    root = ET.fromstring(xml_text)
    items: list[dict[str, str | None]] = []
    if root.tag.endswith("feed"):
        ns = {"a": root.tag.split("}")[0].strip("{")} if "}" in root.tag else {}
        for entry in root.findall("a:entry" if ns else "entry", ns):
            link = None
            for link_el in entry.findall("a:link" if ns else "link", ns):
                href = link_el.attrib.get("href")
                rel = link_el.attrib.get("rel", "alternate")
                if href and rel == "alternate":
                    link = href
                    break
            items.append(
                {
                    "title": _strip((entry.findtext("a:title", default="", namespaces=ns) if ns else entry.findtext("title"))),
                    "content": _strip_html(_strip(
                        (entry.findtext("a:summary", default="", namespaces=ns) if ns else entry.findtext("summary"))
                        or (entry.findtext("a:content", default="", namespaces=ns) if ns else entry.findtext("content"))
                    ))[:_MAX_CONTENT],
                    "url": link,
                    "author": _strip(
                        (entry.findtext("a:author/a:name", default="", namespaces=ns) if ns else "")
                        or (entry.findtext("author/name") if not ns else "")
                    ) or None,
                    "published_at": _parse_dt(
                        (entry.findtext("a:published", default="", namespaces=ns) if ns else entry.findtext("published"))
                        or (entry.findtext("a:updated", default="", namespaces=ns) if ns else entry.findtext("updated"))
                    ),
                }
            )
        return items
    for item in root.findall(".//item"):
        items.append(
            {
                "title": _find_child_text(item, "title"),
                "content": _strip_html(
                    _find_child_text(item, "description") or _find_child_text(item, "encoded")
                )[:_MAX_CONTENT],
                "url": _find_child_text(item, "link") or None,
                "author": _find_child_text(item, "author", "creator") or None,
                "published_at": _parse_dt(_find_child_text(item, "pubDate", "date", "published", "updated")),
            }
        )
    return items


def _normalize_xml_text(text: str) -> str:
    return (text or "").lstrip("\ufeff\r\n\t ")


def _is_xcancel_whitelist_feed(text: str) -> bool:
    return "rss reader not yet whitelisted" in (text or "").lower()


def _strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&#?\w+;", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _read_local_text(url: str) -> str:
    parsed = urlparse(url)
    local_path = unquote(parsed.path or "")
    return Path(local_path).read_text(encoding="utf-8")


def _trace_id_short() -> str:
    return uuid.uuid4().hex[:8]


def _err_text(err: Exception) -> str:
    return f"{type(err).__name__}: {err}"


def _fetch_rss_text(url: str, *, trace_id: str, source_name: str) -> str:
    if url.startswith("file://"):
        logger.info("[feed][trace=%s] source=%s fetch via local_file", trace_id, source_name)
        return _read_local_text(url)
    if "xcancel.com" in url:
        return _fetch_via_curl(url, trace_id=trace_id, source_name=source_name)
    return _fetch_via_requests(url, trace_id=trace_id, source_name=source_name)


def _fetch_via_requests(url: str, *, trace_id: str, source_name: str) -> str:
    last_err: Exception | None = None
    attempts = (0.0, 0.3)
    for idx, delay in enumerate(attempts, start=1):
        if delay > 0:
            time.sleep(delay)
        try:
            logger.info(
                "[feed][trace=%s] source=%s fetch(requests) attempt=%d/%d",
                trace_id,
                source_name,
                idx,
                len(attempts),
            )
            resp = requests.get(
                url,
                timeout=15,
                headers={
                    "User-Agent": "FreshRSS/1.24.0",
                    "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.5",
                },
            )
            resp.raise_for_status()
            logger.info(
                "[feed][trace=%s] source=%s fetch(requests) success attempt=%d bytes=%d",
                trace_id,
                source_name,
                idx,
                len(resp.text or ""),
            )
            return resp.text
        except Exception as e:
            last_err = e
            logger.warning(
                "[feed][trace=%s] source=%s fetch(requests) failed attempt=%d/%d err=%s",
                trace_id,
                source_name,
                idx,
                len(attempts),
                _err_text(e),
            )
    raise last_err or RuntimeError("rss request failed")


def _fetch_via_curl(url: str, *, trace_id: str, source_name: str) -> str:
    last_err: Exception | None = None
    attempts = (0.0, 0.3, 0.8)
    for idx, delay in enumerate(attempts, start=1):
        if delay > 0:
            time.sleep(delay)
        try:
            logger.info(
                "[feed][trace=%s] source=%s fetch(curl) attempt=%d/%d",
                trace_id,
                source_name,
                idx,
                len(attempts),
            )
            proc = subprocess.run(
                [
                    "curl",
                    "-sS",
                    "-L",
                    "--max-time",
                    "15",
                    "-A",
                    "FreshRSS/1.24.0",
                    "-H",
                    "Accept: */*",
                    url,
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            logger.info(
                "[feed][trace=%s] source=%s fetch(curl) success attempt=%d bytes=%d",
                trace_id,
                source_name,
                idx,
                len(proc.stdout or ""),
            )
            return proc.stdout
        except Exception as e:
            last_err = e
            logger.warning(
                "[feed][trace=%s] source=%s fetch(curl) failed attempt=%d/%d err=%s",
                trace_id,
                source_name,
                idx,
                len(attempts),
                _err_text(e),
            )
    raise last_err or RuntimeError("curl fetch failed")


def _resolve_kb_root(url: str) -> Path | None:
    if not url:
        return None
    if url.startswith("file://"):
        path_str = url[len("file://") :]
    else:
        path_str = url
    path = Path(path_str)
    if not path.is_absolute():
        return None
    return path


def _extract_body(text: str, max_chars: int) -> str:
    lines = text.splitlines()
    body_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if re.match(r"^#{1,2}\s", stripped):
            continue
        if re.match(r"^-\s+\w[\w\s]*:", stripped):
            continue
        body_lines.append(line)
    body = "\n".join(body_lines).strip()
    body = re.sub(r"\n{3,}", "\n\n", body)
    if len(body) <= max_chars:
        return body
    return body[:max_chars].rstrip() + "…"


def _fetch_novel_items(source: sqlite3.Row, limit: int) -> list[dict[str, str | None]]:
    kb_root = _resolve_kb_root(str(source["url"]))
    if kb_root is None:
        return []
    index_path = kb_root / "summaries" / "index.json"
    if not index_path.exists():
        return []
    index = json.loads(index_path.read_text(encoding="utf-8"))
    chunks = index.get("chunks", [])
    recent = sorted(chunks, key=lambda c: c.get("created_at", ""), reverse=True)[:limit]
    items: list[dict[str, str | None]] = []
    kb_name = kb_root.name
    for rec in recent:
        chunk_id = str(rec.get("chunk_id", "") or "").strip()
        if not chunk_id:
            continue
        summary_rel = str(rec.get("summary_file", "") or "").strip()
        summary_path = (
            kb_root / summary_rel
            if summary_rel
            else kb_root / "summaries" / "chunks" / f"{chunk_id}.summary.md"
        )
        if not summary_path.exists():
            continue
        raw = summary_path.read_text(encoding="utf-8")
        content = _extract_body(raw, _SUMMARY_MAX_CHARS)
        if not content:
            continue
        segment = rec.get("segment") or rec.get("route", "")
        items.append(
            {
                "title": f"[{source['name']}·{segment}] {chunk_id}",
                "content": content,
                "url": f"novel://{kb_name}/{chunk_id}",
                "author": None,
                "published_at": _parse_dt(str(rec.get("created_at") or "")),
            }
        )
    return items


def _fetch_source_items(source: sqlite3.Row, limit: int, *, trace_id: str) -> list[dict[str, str | None]]:
    source_name = str(source["name"])
    source_type = str(source["type"] or "rss").strip().lower()
    if source_type == "novel-kb":
        items = _fetch_novel_items(source, limit)
        logger.info(
            "[feed][trace=%s] source=%s fetch(novel-kb) done items=%d",
            trace_id,
            source_name,
            len(items),
        )
        return items
    last_err: Exception | None = None
    attempts = (0.0, 0.3)
    for idx, delay in enumerate(attempts, start=1):
        if delay > 0:
            time.sleep(delay)
        try:
            logger.info(
                "[feed][trace=%s] source=%s parse attempt=%d/%d",
                trace_id,
                source_name,
                idx,
                len(attempts),
            )
            text = _fetch_rss_text(str(source["url"]), trace_id=trace_id, source_name=source_name)
            items = _parse_rss(text)[:limit]
            logger.info(
                "[feed][trace=%s] source=%s parse success attempt=%d items=%d",
                trace_id,
                source_name,
                idx,
                len(items),
            )
            return items
        except ET.ParseError as e:
            last_err = e
            logger.warning(
                "[feed][trace=%s] source=%s parse failed attempt=%d/%d err=%s",
                trace_id,
                source_name,
                idx,
                len(attempts),
                _err_text(e),
            )
        except Exception as e:
            last_err = e
            logger.warning(
                "[feed][trace=%s] source=%s fetch_or_parse failed attempt=%d/%d err=%s",
                trace_id,
                source_name,
                idx,
                len(attempts),
                _err_text(e),
            )
    raise last_err or RuntimeError("source fetch failed")


def _poll_source(
    conn: sqlite3.Connection,
    cfg: FeedMcpConfig,
    source: sqlite3.Row,
    *,
    force: bool = False,
    trace_id: str = "",
) -> None:
    now = _now()
    source_id = str(source["id"])
    source_name = str(source["name"])
    poll_state = conn.execute(
        "SELECT last_polled_at FROM poll_state WHERE source_id = ?",
        (source_id,),
    ).fetchone()
    if not force and poll_state and poll_state["last_polled_at"]:
        last_polled = datetime.fromisoformat(str(poll_state["last_polled_at"]))
        elapsed_s = int((now - last_polled).total_seconds())
        poll_interval_s = int(source["poll_interval_seconds"])
        if elapsed_s < poll_interval_s:
            remain_s = max(0, poll_interval_s - elapsed_s)
            logger.info(
                "[feed][trace=%s] source=%s skipped by interval remain=%ss",
                trace_id,
                source_name,
                remain_s,
            )
            return

    # 1. 按 source_type 拉取原始条目（rss / novel-kb），使用静态抓取上限。
    parsed = _fetch_source_items(source, cfg.max_content_events, trace_id=trace_id)
    parsed_count = len(parsed)
    valid_count = 0
    skipped_empty_count = 0
    inserted_count = 0
    updated_count = 0
    sample_titles: list[str] = []

    # 2. 规范化条目并写入 sqlite，统计本轮新增/更新情况。
    for item in parsed:
        title = str(item.get("title") or "").strip()
        content = str(item.get("content") or "").strip()
        url = _normalize_item_url(item.get("url"))
        author = str(item.get("author") or "").strip() or None
        published_at = str(item.get("published_at") or "").strip() or None
        if not (title or content):
            skipped_empty_count += 1
            continue
        valid_count += 1
        display_title = (title or content).replace("\n", " ").strip()
        if display_title and len(sample_titles) < 3:
            sample_titles.append(display_title[:80])

        event_id = _stable_event_id(source_id, url or "", title, published_at)
        exists = conn.execute(
            "SELECT 1 FROM items WHERE event_id = ? LIMIT 1",
            (event_id,),
        ).fetchone()
        if exists:
            updated_count += 1
        else:
            inserted_count += 1

        content_hash = hashlib.sha1((title + "\n" + content).encode()).hexdigest()[:16]
        conn.execute(
            """
            INSERT INTO items (
                event_id, source_id, source_name, source_type, title, content, url,
                author, published_at, first_seen_at, last_seen_at, emitted_at, content_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(event_id) DO UPDATE SET
                source_name=excluded.source_name,
                title=excluded.title,
                content=excluded.content,
                url=excluded.url,
                author=excluded.author,
                published_at=excluded.published_at,
                last_seen_at=excluded.last_seen_at,
                content_hash=excluded.content_hash
            """,
            (
                event_id,
                source_id,
                str(source["name"]),
                str(source["type"] or "rss"),
                title or None,
                content or title,
                url,
                author,
                published_at,
                now.isoformat(),
                now.isoformat(),
                None,
                content_hash,
            ),
        )

    # 3. 更新 poll 状态并输出本轮摘要日志，便于排查“是否真的拉到内容”。
    conn.execute(
        """
        INSERT INTO poll_state (source_id, last_polled_at, last_success_at, last_error)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(source_id) DO UPDATE SET
            last_polled_at=excluded.last_polled_at,
            last_success_at=excluded.last_success_at,
            last_error=NULL
        """,
        (source_id, now.isoformat(), now.isoformat(), None),
    )
    conn.commit()
    logger.info(
        "[feed][trace=%s] source=%s poll done parsed=%d valid=%d inserted=%d updated=%d skipped_empty=%d sample_titles=%s",
        trace_id,
        source_name,
        parsed_count,
        valid_count,
        inserted_count,
        updated_count,
        skipped_empty_count,
        sample_titles,
    )


def _poll_rows(
    conn: sqlite3.Connection,
    cfg: FeedMcpConfig,
    rows: list[sqlite3.Row],
    *,
    force: bool,
    trace_id: str = "",
) -> dict[str, Any]:
    failed_sources: list[str] = []
    success_count = 0
    for row in rows:
        try:
            _poll_source(conn, cfg, row, force=force, trace_id=trace_id)
            success_count += 1
        except Exception as e:
            failed_sources.append(str(row["name"]))
            logger.warning(
                "[feed][trace=%s] poll failed source=%s err=%s",
                trace_id,
                row["name"],
                _err_text(e),
            )
            conn.execute(
                """
                INSERT INTO poll_state (source_id, last_polled_at, last_success_at, last_error)
                VALUES (?, ?, NULL, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    last_polled_at=excluded.last_polled_at,
                    last_error=excluded.last_error
                """,
                (row["id"], _now().isoformat(), repr(e)),
            )
            conn.commit()
    return {
        "total": len(rows),
        "success": success_count,
        "failed": len(failed_sources),
        "failed_sources": failed_sources,
    }


def feed_manage(action: str, name: str = "", url: str = "", source_type: str = "rss", note: str = "") -> str:
    cfg = load_config()
    conn = _connect(cfg)
    try:
        _cleanup(conn, cfg)
        if action == "list":
            rows = conn.execute(
                "SELECT name, type, url, enabled, note FROM sources ORDER BY added_at DESC"
            ).fetchall()
            if not rows:
                return "当前没有订阅任何信息源"
            lines = [f"RSS 订阅列表（共 {len(rows)} 个）："]
            for row in rows:
                status = "启用" if int(row["enabled"]) else "停用"
                note_text = f"  备注: {row['note']}" if row["note"] else ""
                lines.append(f"  [{status}] {row['name']}  {row['url']}{note_text}")
            return "\n".join(lines)

        if action == "unsubscribe":
            if not name.strip():
                return "错误：unsubscribe 需要 name"
            rows = conn.execute(
                "SELECT id, name FROM sources WHERE lower(name) LIKE ? ORDER BY added_at DESC",
                (f"%{name.strip().lower()}%",),
            ).fetchall()
            if not rows:
                return f"没有找到名称包含 {name!r} 的订阅"
            source_ids = [str(row["id"]) for row in rows]
            deleted_items, deleted_acked = _delete_source_history(conn, source_ids)
            conn.executemany("DELETE FROM sources WHERE id = ?", [(source_id,) for source_id in source_ids])
            conn.commit()
            names = "、".join(f"「{row['name']}」" for row in rows)
            return (
                f"已取消订阅：{names}"
                f"（已清理 {deleted_items} 条历史内容，{deleted_acked} 条 ack 记录）"
            )

        if action == "subscribe":
            if not name.strip() or not url.strip():
                return "错误：subscribe 需要 name 和 url"
            normalized_url = _normalize_source_url(url)
            duplicate = conn.execute(
                "SELECT id, name FROM sources WHERE url = ? LIMIT 1",
                (normalized_url,),
            ).fetchone()
            if duplicate is not None:
                return (
                    f"已经订阅过该地址：{duplicate['name']!r}"
                    f"（id: {str(duplicate['id'])[:8]}），无需重复添加"
                )
            now = _now().isoformat()
            source_id = str(uuid.uuid4())
            conn.execute(
                """
                INSERT INTO sources (id, type, name, url, note, enabled, poll_interval_seconds, added_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_id,
                    source_type or "rss",
                    name.strip(),
                    normalized_url,
                    note.strip() or None,
                    1,
                    cfg.poll_ttl_seconds,
                    now,
                    now,
                ),
            )
            conn.commit()
            return f"已订阅 {name.strip()!r}（类型={source_type or 'rss'} {normalized_url}），下次主动巡检时开始收集"
        return "错误：action 必须是 subscribe|list|unsubscribe"
    finally:
        conn.close()


def sync_legacy_subscriptions(json_path: str) -> dict[str, int]:
    cfg = load_config()
    conn = _connect(cfg)
    inserted = 0
    updated = 0
    try:
        # 1. 从旧 feeds.json 读取订阅列表，兼容历史本地 feed 配置。
        path = Path(json_path).expanduser()
        if not path.exists():
            return {"inserted": 0, "updated": 0}
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            return {"inserted": 0, "updated": 0}
        # 2. 按 URL 对齐到 feed-mcp 的 sources 表，避免迁移时重复插入。
        for item in raw:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            url = _normalize_source_url(str(item.get("url") or "").strip())
            if not name or not url:
                continue
            row = conn.execute(
                "SELECT id FROM sources WHERE url = ? LIMIT 1",
                (url,),
            ).fetchone()
            payload = (
                str(item.get("type") or "rss"),
                name,
                url,
                str(item.get("note") or "").strip() or None,
                1 if bool(item.get("enabled", True)) else 0,
                cfg.poll_ttl_seconds,
                str(item.get("added_at") or _now().isoformat()),
                _now().isoformat(),
            )
            if row is None:
                source_id = str(item.get("id") or uuid.uuid4())
                conn.execute(
                    """
                    INSERT INTO sources (id, type, name, url, note, enabled, poll_interval_seconds, added_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (source_id, *payload),
                )
                inserted += 1
                continue
            conn.execute(
                """
                UPDATE sources
                SET type = ?, name = ?, note = ?, enabled = ?, poll_interval_seconds = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    payload[0],
                    payload[1],
                    payload[3],
                    payload[4],
                    payload[5],
                    payload[7],
                    str(row["id"]),
                ),
            )
            updated += 1
        conn.commit()
        return {"inserted": inserted, "updated": updated}
    finally:
        conn.close()


def feed_query(
    action: str,
    source: str = "",
    keyword: str = "",
    limit: int = 5,
    page: int = 1,
    page_size: int = 20,
) -> str:
    cfg = load_config()
    conn = _connect(cfg)
    try:
        trace_id = _trace_id_short()
        # 1. 查询工具尽量复刻旧逻辑：每次查询前都主动刷新启用源，而不是只看旧缓存。
        _cleanup(conn, cfg)
        enabled_rows = conn.execute(
            "SELECT * FROM sources WHERE enabled = 1 ORDER BY added_at DESC"
        ).fetchall()
        limit = max(1, min(int(limit or 5), 30))
        source_like = f"%{source.strip().lower()}%" if source else None
        if source_like:
            enabled_rows = [
                row for row in enabled_rows if source.strip().lower() in str(row["name"]).lower()
            ]
        if not enabled_rows:
            return "没有匹配的启用订阅"
        summary = _poll_rows(conn, cfg, enabled_rows, force=True, trace_id=trace_id)
        logger.info(
            "[feed][trace=%s] feed_query force_poll summary total=%d success=%d failed=%d failed_sources=%s",
            trace_id,
            int(summary["total"]),
            int(summary["success"]),
            int(summary["failed"]),
            summary["failed_sources"],
        )
        # 2. 刷新完成后再重新做查询，确保 latest/search/catalog 的时效性尽量贴近旧实现。
        if action == "summary":
            source_names = sorted({str(row["name"]) for row in enabled_rows if row["name"]})
            names = "（无）"
            if source_names:
                shown = source_names[:50]
                names = "、".join(shown)
                if len(source_names) > 50:
                    names += f" …（其余 {len(source_names) - 50} 个省略）"
            total_items = conn.execute(
                "SELECT COUNT(*) FROM items WHERE source_name IN (%s)" % ",".join("?" * len(enabled_rows)),
                [str(row["name"]) for row in enabled_rows],
            ).fetchone()[0]
            return (
                f"订阅概况：sources={len(enabled_rows)} items={total_items} 来源={names}。"
                "如需逐条订阅 URL 与启用状态，请使用 feed_manage(action=list)。"
            )
        if action == "catalog":
            page = max(1, int(page or 1))
            page_size = max(1, min(int(page_size or 20), 100))
            rows = conn.execute(
                """
                SELECT source_name, title, url, published_at, last_seen_at
                FROM items
                WHERE 1=1
                LIMIT ? OFFSET ?
                """,
                (page_size, (page - 1) * page_size),
            ).fetchall()
            base_sql = """
                SELECT source_name, title, url, published_at, last_seen_at
                FROM items
                WHERE 1=1
            """
            params: list[Any] = []
            if source_like:
                base_sql += " AND lower(source_name) LIKE ?"
                params.append(source_like)
            total = conn.execute(
                f"SELECT COUNT(*) FROM ({base_sql})",
                params,
            ).fetchone()[0]
            paged = conn.execute(
                base_sql + " ORDER BY coalesce(published_at, last_seen_at) DESC LIMIT ? OFFSET ?",
                params + [page_size, (page - 1) * page_size],
            ).fetchall()
            if (page - 1) * page_size >= total and total > 0:
                return json.dumps(
                    {
                        "action": "catalog",
                        "source": source or None,
                        "page": page,
                        "page_size": page_size,
                        "total": total,
                        "has_more": False,
                        "next_page": None,
                        "items": [],
                        "error": "page out of range",
                    },
                    ensure_ascii=False,
                )
            payload_items = [
                {
                    "source": row["source_name"],
                    "title": row["title"] or "(无标题)",
                    "url": row["url"] or "",
                    "published_at": row["published_at"],
                }
                for row in paged
            ]
            return json.dumps(
                {
                    "action": "catalog",
                    "source": source or None,
                    "page": page,
                    "page_size": page_size,
                    "total": total,
                    "has_more": (page * page_size) < total,
                    "next_page": (page + 1) if (page * page_size) < total else None,
                    "items": payload_items,
                },
                ensure_ascii=False,
            )
        query_sql = """
            SELECT source_name, title, content, url, author, published_at, last_seen_at
            FROM items
            WHERE 1=1
        """
        params: list[Any] = []
        if source_like:
            query_sql += " AND lower(source_name) LIKE ?"
            params.append(source_like)
        if action == "search" and keyword.strip():
            query_sql += " AND (lower(title) LIKE ? OR lower(content) LIKE ?)"
            like = f"%{keyword.strip().lower()}%"
            params.extend([like, like])
        if action == "search" and not keyword.strip():
            return "错误：search 需要 keyword"
        query_sql += " ORDER BY coalesce(published_at, last_seen_at) DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(query_sql, params).fetchall()
        if not rows:
            return "没有找到匹配条目"
        lines: list[str] = []
        for row in rows:
            raw_ts = row["published_at"] or row["last_seen_at"]
            ts = "未知时间"
            if raw_ts:
                try:
                    ts = datetime.fromisoformat(str(raw_ts)).astimezone().strftime("%Y-%m-%d %H:%M")
                except Exception:
                    ts = str(raw_ts)
            lines.append(f"- [{row['source_name']}] {row['title'] or '(无标题)'} ({ts})")
            if row["url"]:
                lines.append(f"  {row['url']}")
        return "\n".join(lines)
    finally:
        conn.close()


def _build_display_text(row: sqlite3.Row) -> str:
    """为单条 item 生成预格式化展示文本，供 proactive LLM 直接使用。

    MCP 侧控制格式，proactive 侧无需感知内容类型细节。
    url 同时保留在独立字段，proactive 会兜底追加以保证溯源链完整。
    """
    source = (row["source_name"] or "").strip()
    title = (row["title"] or "（无标题）").strip()
    header = f"[{source}] {title}" if source else title
    content = (row["content"] or "").strip().replace("\n", " ")
    if len(content) > 300:
        content = content[:300] + "..."
    parts = [header]
    if content:
        parts.append(content)
    if row["url"]:
        parts.append(f"原文链接: {row['url']}")
    return "\n".join(parts)


_TOKEN_RE = re.compile(r"[a-z0-9_+#.-]{2,}|[\u4e00-\u9fff]", re.IGNORECASE)
_RANK_BIAS = -0.8
_FRESHNESS_HALF_LIFE_HOURS = 36.0
_EXPOSURE_RECENCY_HALF_LIFE_HOURS = 12.0
_RECENT_CONTEXT_LIMIT = 100
_MAX_SHORTLIST_SOURCE_SHARE = 0.4


def _tokenize_rank_text(*parts: object) -> list[str]:
    text = " ".join(str(part or "") for part in parts).lower()
    raw_tokens = _TOKEN_RE.findall(text)
    tokens: list[str] = []
    cjk_buffer: list[str] = []
    for token in raw_tokens:
        if re.fullmatch(r"[\u4e00-\u9fff]", token):
            cjk_buffer.append(token)
            continue
        if len(cjk_buffer) >= 2:
            tokens.extend("".join(cjk_buffer[i:i + 2]) for i in range(len(cjk_buffer) - 1))
        cjk_buffer = []
        if len(token) >= 2:
            tokens.append(token)
    if len(cjk_buffer) >= 2:
        tokens.extend("".join(cjk_buffer[i:i + 2]) for i in range(len(cjk_buffer) - 1))
    seen: set[str] = set()
    result: list[str] = []
    for token in tokens:
        if token in seen:
            continue
        seen.add(token)
        result.append(token[:64])
        if len(result) >= 80:
            break
    return result


def _rank_stat_keys(row: sqlite3.Row) -> list[str]:
    keys = [f"source:{row['source_id']}"]
    author = str(row["author"] or "").strip().lower()
    if author:
        keys.append(f"author:{author[:96]}")
    for token in _tokenize_rank_text(row["source_name"], row["title"], row["content"]):
        keys.append(f"token:{token}")
    return keys


def _ensure_rank_stats(conn: sqlite3.Connection) -> None:
    count = conn.execute("SELECT COUNT(*) FROM rank_stats").fetchone()[0]
    if int(count) > 0:
        return
    rows = conn.execute(
        """
        SELECT source_id, source_name, author, title, content, interest_ok, interest_scored_at
        FROM items
        WHERE interest_ok IN (0, 1)
        """
    ).fetchall()
    now = _now().isoformat()
    for row in rows:
        _update_rank_stats_for_row(
            conn,
            row,
            int(row["interest_ok"]),
            str(row["interest_scored_at"] or now),
        )


def _update_rank_stats_for_row(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    interest_ok: int,
    updated_at: str,
) -> None:
    pos_inc = 1 if interest_ok == 1 else 0
    neg_inc = 1 if interest_ok == 0 else 0
    for key in _rank_stat_keys(row):
        conn.execute(
            """
            INSERT INTO rank_stats(key, pos, neg, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                pos = pos + excluded.pos,
                neg = neg + excluded.neg,
                updated_at = excluded.updated_at
            """,
            (key, pos_inc, neg_inc, updated_at),
        )


def _rank_stats(conn: sqlite3.Connection, keys: list[str]) -> dict[str, tuple[int, int]]:
    if not keys:
        return {}
    placeholders = ",".join("?" for _ in keys)
    rows = conn.execute(
        f"SELECT key, pos, neg FROM rank_stats WHERE key IN ({placeholders})",
        keys,
    ).fetchall()
    return {str(row["key"]): (int(row["pos"]), int(row["neg"])) for row in rows}


def _rank_model_weights(conn: sqlite3.Connection, keys: list[str]) -> dict[str, float]:
    if not keys:
        return {}
    placeholders = ",".join("?" for _ in keys)
    rows = conn.execute(
        f"SELECT key, weight FROM rank_model_weights WHERE key IN ({placeholders})",
        keys,
    ).fetchall()
    return {str(row["key"]): float(row["weight"]) for row in rows}


def _update_rank_model_weights(
    conn: sqlite3.Connection,
    features: dict[str, float],
    label: int,
    learning_rate: float,
    updated_at: str,
    event_id: str,
) -> None:
    weights = _rank_model_weights(conn, list(features))
    prediction = _sigmoid(sum(weights.get(key, 0.0) * value for key, value in features.items()))
    error = float(label) - prediction
    for key, value in features.items():
        old_weight = weights.get(key, 0.0)
        new_weight = max(-8.0, min(8.0, old_weight + learning_rate * error * value))
        conn.execute(
            """
            INSERT INTO rank_model_weights(key, weight, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                weight = excluded.weight,
                updated_at = excluded.updated_at
            """,
            (key, new_weight, updated_at),
        )
    conn.execute(
        """
        INSERT INTO rank_model_updates(
            event_id, label, prediction, error, feature_count, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (event_id, label, prediction, error, len(features), updated_at),
    )


def _ensure_rank_model(conn: sqlite3.Connection, cfg: FeedMcpConfig) -> None:
    count = conn.execute("SELECT COUNT(*) FROM rank_model_weights").fetchone()[0]
    if int(count) > 0:
        return
    rows = conn.execute(
        """
        SELECT
            event_id, source_id, source_name, author, title, content, published_at,
            first_seen_at, interest_ok, interest_scored_at
        FROM items
        WHERE interest_ok IN (0, 1)
        ORDER BY coalesce(interest_scored_at, published_at, first_seen_at)
        """
    ).fetchall()
    for row in rows:
        _update_rank_model_for_row(
            conn,
            cfg,
            row,
            int(row["interest_ok"]),
            str(row["interest_scored_at"] or _now().isoformat()),
        )


def _accept_rate(pos: int, neg: int) -> float:
    return (pos + 1.0) / (pos + neg + 2.0)


def _sigmoid(value: float) -> float:
    if value < -40:
        return 0.0
    if value > 40:
        return 1.0
    return 1.0 / (1.0 + math.exp(-value))


def _parse_rank_dt(value: object) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _freshness_score(row: sqlite3.Row, cfg: FeedMcpConfig, now: datetime) -> tuple[float, float]:
    ts = _parse_rank_dt(row["published_at"]) or _parse_rank_dt(row["first_seen_at"]) or now
    age_hours = max(0.0, (now - ts).total_seconds() / 3600.0)
    return math.exp(-age_hours / _FRESHNESS_HALF_LIFE_HOURS), age_hours


def _recent_context_tokens(
    conn: sqlite3.Connection,
    now: datetime,
) -> list[set[str]]:
    rows = conn.execute(
        """
        SELECT i.source_name, i.title, i.content
        FROM items i
        WHERE i.interest_ok = 1
           OR i.event_id IN (
                SELECT event_id
                FROM rank_impressions
                ORDER BY served_at DESC
                LIMIT ?
           )
        ORDER BY coalesce(i.interest_scored_at, i.published_at, i.first_seen_at) DESC
        LIMIT ?
        """,
        (_RECENT_CONTEXT_LIMIT, _RECENT_CONTEXT_LIMIT),
    ).fetchall()
    return [
        set(_tokenize_rank_text(row["source_name"], row["title"], row["content"]))
        for row in rows
    ]


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _frontier_overlap(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    intersection = len(left & right)
    return max(intersection / len(left | right), intersection / min(len(left), len(right)))


def _novelty_score(tokens: set[str], contexts: list[set[str]]) -> float:
    if not tokens or not contexts:
        return 1.0
    max_overlap = max((_jaccard(tokens, ctx) for ctx in contexts), default=0.0)
    return max(0.05, 1.0 - max_overlap)


def _exposure_decay(exposure_count: int, last_exposed_at: str | None, now: datetime) -> float:
    count_decay = 1.0 / (1.0 + max(0, exposure_count))
    last_seen = _parse_rank_dt(last_exposed_at)
    if last_seen is None:
        return count_decay
    age_hours = max(0.0, (now - last_seen).total_seconds() / 3600.0)
    recency_decay = 1.0 - math.exp(-age_hours / _EXPOSURE_RECENCY_HALF_LIFE_HOURS)
    return count_decay * (0.05 + 0.95 * recency_decay)


def _impression_summary(
    conn: sqlite3.Connection,
    event_ids: list[str],
) -> dict[str, tuple[int, str | None]]:
    if not event_ids:
        return {}
    placeholders = ",".join("?" for _ in event_ids)
    rows = conn.execute(
        f"""
        SELECT event_id, COUNT(*) AS count, MAX(served_at) AS last_served_at
        FROM rank_impressions
        WHERE event_id IN ({placeholders})
        GROUP BY event_id
        """,
        event_ids,
    ).fetchall()
    return {
        str(row["event_id"]): (int(row["count"] or 0), row["last_served_at"])
        for row in rows
    }


def _row_rank_tokens(row: sqlite3.Row) -> set[str]:
    return set(_tokenize_rank_text(row["source_name"], row["title"], row["content"]))


def _row_rank_time(row: sqlite3.Row, now: datetime) -> datetime:
    return _parse_rank_dt(row["published_at"]) or _parse_rank_dt(row["first_seen_at"]) or now


def _frontier_scores(rows: list[sqlite3.Row], now: datetime) -> dict[str, float]:
    tokens_by_id = {str(row["event_id"]): _row_rank_tokens(row) for row in rows}
    time_by_id = {str(row["event_id"]): _row_rank_time(row, now) for row in rows}
    scores: dict[str, float] = {}
    for row in rows:
        event_id = str(row["event_id"])
        tokens = tokens_by_id[event_id]
        max_newer_overlap = 0.0
        for other in rows:
            other_id = str(other["event_id"])
            if other_id == event_id:
                continue
            if other["source_id"] != row["source_id"]:
                continue
            if time_by_id[other_id] <= time_by_id[event_id]:
                continue
            max_newer_overlap = max(max_newer_overlap, _frontier_overlap(tokens, tokens_by_id[other_id]))
        scores[event_id] = 1.0 if max_newer_overlap < 0.13 else max(0.08, 1.0 - 3.0 * max_newer_overlap)
    return scores


def _rank_candidate(
    conn: sqlite3.Connection,
    cfg: FeedMcpConfig,
    row: sqlite3.Row,
    contexts: list[set[str]],
    now: datetime,
    frontier: float,
    exposure_count: int,
    last_exposed_at: str | None,
) -> tuple[float, dict[str, float]]:
    tokens = _row_rank_tokens(row)
    information_density = min(1.0, len(tokens) / 16.0)
    keys = _rank_stat_keys(row)
    stats = _rank_stats(conn, keys)
    source_pos, source_neg = stats.get(f"source:{row['source_id']}", (0, 0))
    author = str(row["author"] or "").strip().lower()
    author_pos, author_neg = stats.get(f"author:{author[:96]}", (0, 0)) if author else (0, 0)
    token_values: list[float] = []
    for token in tokens:
        pos, neg = stats.get(f"token:{token}", (0, 0))
        if pos or neg:
            token_values.append(math.log((pos + 1.0) / (neg + 1.0)))
    avg_token_log_odds = sum(token_values) / len(token_values) if token_values else 0.0
    source_prior = _accept_rate(source_pos, source_neg)
    author_prior = _accept_rate(author_pos, author_neg) if author else 0.5
    freshness, age_hours = _freshness_score(row, cfg, now)
    novelty = _novelty_score(tokens, contexts)
    exposure_decay = _exposure_decay(exposure_count, last_exposed_at, now)
    z = (
        _RANK_BIAS
        + 2.2 * (source_prior - 0.5)
        + 1.4 * (author_prior - 0.5)
        + 0.8 * avg_token_log_odds
        + 0.6 * freshness
    )
    interest = _sigmoid(z)
    score = (
        interest
        * (0.25 + 0.75 * freshness)
        * (0.25 + 0.75 * novelty)
        * (0.25 + 0.75 * information_density)
        * exposure_decay
        * frontier
    )
    features = {
        "interest": interest,
        "freshness": freshness,
        "age_hours": age_hours,
        "novelty": novelty,
        "exposure_decay": exposure_decay,
        "frontier": frontier,
        "source_prior": source_prior,
        "author_prior": author_prior,
        "token_log_odds": avg_token_log_odds,
        "exposure_count": float(exposure_count),
        "information_density": information_density,
    }
    return score, features


def _age_bucket(age_hours: float) -> str:
    if age_hours <= 1:
        return "1h"
    if age_hours <= 6:
        return "6h"
    if age_hours <= 24:
        return "24h"
    if age_hours <= 72:
        return "72h"
    return "old"


def _model_feature_values(
    row: sqlite3.Row,
    coarse_features: dict[str, float],
) -> dict[str, float]:
    tokens = _tokenize_rank_text(row["source_name"], row["title"], row["content"])
    token_weight = 1.0 / math.sqrt(max(1, min(len(tokens), 32)))
    features: dict[str, float] = {
        "bias": 1.0,
        f"source:{row['source_id']}": 1.0,
        f"age:{_age_bucket(float(coarse_features.get('age_hours', 0.0)))}": 1.0,
        "freshness": float(coarse_features.get("freshness", 0.0)),
        "novelty": float(coarse_features.get("novelty", 1.0)),
        "frontier": float(coarse_features.get("frontier", 1.0)),
        "density": float(coarse_features.get("information_density", 0.0)),
        "source_prior": float(coarse_features.get("source_prior", 0.5)) - 0.5,
        "exposed": min(1.0, float(coarse_features.get("exposure_count", 0.0)) / 5.0),
    }
    author = str(row["author"] or "").strip().lower()
    if author:
        features[f"author:{author[:96]}"] = 1.0
    for token in tokens[:32]:
        features[f"token:{token}"] = token_weight
    return features


def _predict_rank_model(
    conn: sqlite3.Connection,
    features: dict[str, float],
) -> float:
    weights = _rank_model_weights(conn, list(features))
    return _sigmoid(sum(weights.get(key, 0.0) * value for key, value in features.items()))


def _update_rank_model_for_row(
    conn: sqlite3.Connection,
    cfg: FeedMcpConfig,
    row: sqlite3.Row,
    interest_ok: int,
    updated_at: str,
) -> None:
    contexts = _recent_context_tokens(conn, _now())
    exposure_count, last_exposed_at = _impression_summary(conn, [str(row["event_id"])]).get(
        str(row["event_id"]),
        (0, None),
    )
    _coarse_score, coarse_features = _rank_candidate(
        conn,
        cfg,
        row,
        contexts,
        _now(),
        1.0,
        exposure_count,
        last_exposed_at,
    )
    source_pos, source_neg = _rank_stats(conn, [f"source:{row['source_id']}"]).get(
        f"source:{row['source_id']}",
        (0, 0),
    )
    source_weight = 1.0 / math.sqrt(1.0 + (source_pos + source_neg) / 20.0)
    _update_rank_model_weights(
        conn,
        _model_feature_values(row, coarse_features),
        interest_ok,
        cfg.rank_model_learning_rate * source_weight,
        updated_at,
        str(row["event_id"]),
    )


def _source_balanced_shortlist(
    ranked: list[tuple[sqlite3.Row, float, dict[str, float]]],
    limit: int,
) -> list[tuple[sqlite3.Row, float, dict[str, float]]]:
    if len(ranked) <= limit:
        return ranked
    by_source: dict[str, list[tuple[sqlite3.Row, float, dict[str, float]]]] = {}
    for item in ranked:
        row = item[0]
        source_key = str(row["source_id"] or row["source_name"] or "")
        by_source.setdefault(source_key, []).append(item)
    per_source = max(4, math.ceil(limit * _MAX_SHORTLIST_SOURCE_SHARE))
    picked_ids: set[str] = set()
    shortlist: list[tuple[sqlite3.Row, float, dict[str, float]]] = []
    for items in by_source.values():
        for item in items[:per_source]:
            event_id = str(item[0]["event_id"])
            if event_id in picked_ids:
                continue
            picked_ids.add(event_id)
            shortlist.append(item)
    shortlist.sort(
        key=lambda item: (
            item[1],
            str(item[0]["published_at"] or item[0]["first_seen_at"] or ""),
        ),
        reverse=True,
    )
    for item in ranked:
        if len(shortlist) >= limit:
            break
        event_id = str(item[0]["event_id"])
        if event_id in picked_ids:
            continue
        picked_ids.add(event_id)
        shortlist.append(item)
    return shortlist[:limit]


def _information_gain_score(ml_score: float, features: dict[str, float]) -> float:
    freshness = float(features.get("freshness", 0.0))
    novelty = float(features.get("novelty", 1.0))
    uncertainty = ml_score * (1.0 - ml_score)
    useful_learning = uncertainty * freshness * novelty
    stale_redundancy = (1.0 - freshness) * (1.0 - novelty)
    return (
        ml_score
        * (0.82 + 0.18 * freshness)
        + 0.04 * useful_learning
        - 0.04 * ml_score * stale_redundancy
    )


def _rank_rows(
    conn: sqlite3.Connection,
    cfg: FeedMcpConfig,
    rows: list[sqlite3.Row],
    now: datetime,
) -> list[tuple[sqlite3.Row, float, dict[str, float]]]:
    contexts = _recent_context_tokens(conn, now)
    frontier_by_id = _frontier_scores(rows, now)
    impressions_by_id = _impression_summary(conn, [str(row["event_id"]) for row in rows])
    coarse_ranked = [
        (
            row,
            *_rank_candidate(
                conn,
                cfg,
                row,
                contexts,
                now,
                frontier_by_id.get(str(row["event_id"]), 1.0),
                impressions_by_id.get(str(row["event_id"]), (0, None))[0],
                impressions_by_id.get(str(row["event_id"]), (0, None))[1],
            ),
        )
        for row in rows
    ]
    coarse_ranked.sort(
        key=lambda item: (
            item[1],
            str(item[0]["published_at"] or item[0]["first_seen_at"] or ""),
        ),
        reverse=True,
    )
    coarse_limit = max(cfg.max_content_events, cfg.rank_impression_limit * 10)
    shortlist = _source_balanced_shortlist(coarse_ranked, coarse_limit)
    refined: list[tuple[sqlite3.Row, float, dict[str, float]]] = []
    for row, coarse_score, features in shortlist:
        ml_features = _model_feature_values(row, features)
        ml_score = _predict_rank_model(conn, ml_features)
        merged = dict(features)
        merged["coarse_score"] = coarse_score
        merged["ml_score"] = ml_score
        final_score = _information_gain_score(ml_score, merged)
        merged["uncertainty"] = ml_score * (1.0 - ml_score)
        merged["information_gain_score"] = final_score
        refined.append((row, final_score, merged))
    refined.sort(
        key=lambda item: (
            item[1],
            item[2].get("coarse_score", 0.0),
            str(item[0]["published_at"] or item[0]["first_seen_at"] or ""),
        ),
        reverse=True,
    )
    head_limit = min(cfg.rank_impression_limit, len(refined))
    if head_limit <= 1:
        return refined

    max_per_source = max(1, head_limit // 2)
    source_counts: dict[str, int] = {}
    head: list[tuple[sqlite3.Row, float, dict[str, float]]] = []
    tail: list[tuple[sqlite3.Row, float, dict[str, float]]] = []
    for item in refined:
        row = item[0]
        source_key = str(row["source_id"] or row["source_name"] or "")
        if len(head) < head_limit and source_counts.get(source_key, 0) < max_per_source:
            source_counts[source_key] = source_counts.get(source_key, 0) + 1
            head.append(item)
        else:
            tail.append(item)
    if len(head) < head_limit:
        missing = head_limit - len(head)
        head.extend(tail[:missing])
        tail = tail[missing:]
    return head + tail


def _record_rank_impressions(
    conn: sqlite3.Connection,
    ranked: list[tuple[sqlite3.Row, float, dict[str, float]]],
    now: datetime,
    limit: int,
) -> None:
    served_at = now.isoformat()
    for position, (row, score, features) in enumerate(ranked[:limit], start=1):
        event_id = str(row["event_id"])
        conn.execute(
            """
            INSERT INTO rank_impressions(event_id, served_at, position, rank_score, features_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (event_id, served_at, position, score, json.dumps(features, ensure_ascii=False)),
        )


def poll_feeds_only() -> None:
    """按需轮询所有启用源（尊重 poll_ttl_seconds），不返回内容。
    由 proactive loop 按固定周期调用，与 get_proactive_events 完全解耦。
    单源失败已在 _poll_rows 内部隔离；系统级异常（DB 不可用、配置损坏等）直接上抛，
    由调用方决定如何处理，避免故障被静默吞掉。
    """
    cfg = load_config()
    conn = _connect(cfg)
    try:
        trace_id = _trace_id_short()
        _cleanup(conn, cfg)
        sources = conn.execute(
            "SELECT * FROM sources WHERE enabled = 1 ORDER BY added_at DESC"
        ).fetchall()
        logger.info("[feed][trace=%s] poll_feeds_only: %d sources", trace_id, len(sources))
        summary = _poll_rows(conn, cfg, sources, force=False, trace_id=trace_id)
        conn.commit()
        logger.info(
            "[feed][trace=%s] poll_feeds_only done total=%d success=%d failed=%d failed_sources=%s",
            trace_id,
            int(summary["total"]),
            int(summary["success"]),
            int(summary["failed"]),
            summary["failed_sources"],
        )
    finally:
        conn.close()


def get_proactive_events() -> list[dict[str, Any]]:
    cfg = load_config()
    conn = _connect(cfg)
    try:
        # 纯 DB 查询，不触发轮询。轮询由 proactive loop 通过 poll_feeds 工具独立驱动。
        _cleanup(conn, cfg)
        now = _now()
        published_after = (now - timedelta(hours=cfg.item_retention_hours)).isoformat()
        candidate_limit = max(1000, cfg.max_content_events * 20)
        rows = conn.execute(
            """
            SELECT
                i.event_id,
                i.source_id,
                i.source_type,
                i.source_name,
                i.title,
                i.content,
                i.url,
                i.author,
                i.published_at,
                i.first_seen_at
            FROM items i
            LEFT JOIN acked_items a ON a.event_id = i.event_id
            WHERE a.event_id IS NULL
              AND (i.published_at IS NOT NULL OR i.source_type != 'rss')
              AND coalesce(i.published_at, i.first_seen_at) >= ?
            ORDER BY coalesce(i.published_at, i.first_seen_at) DESC
            LIMIT ?
            """,
            (published_after, candidate_limit),
        ).fetchall()
        ranked = _rank_rows(conn, cfg, list(rows), now)
        ranked_by_id = {str(row["event_id"]): (score, features) for row, score, features in ranked}
        if cfg.rank_mode == "ranked":
            selected = ranked[:cfg.max_content_events]
        else:
            selected = [
                (
                    row,
                    ranked_by_id.get(str(row["event_id"]), (0.0, {}))[0],
                    ranked_by_id.get(str(row["event_id"]), (0.0, {}))[1],
                )
                for row in rows[:cfg.max_content_events]
            ]
        _record_rank_impressions(
            conn,
            selected,
            now,
            min(cfg.rank_impression_limit, cfg.max_content_events),
        )
        conn.commit()
        return [
            {
                "event_id": row["event_id"],
                "kind": "content",
                "source_type": row["source_type"],
                "source_name": row["source_name"],
                "title": row["title"],
                "content": row["content"],
                "url": row["url"],
                "published_at": row["published_at"],
                "display_text": _build_display_text(row),
                "rank_score": round(score, 6),
            }
            for row, score, _features in selected
        ]
    finally:
        conn.close()


def _interest_ok_from_feedback(feedback: str) -> int:
    if feedback == "interesting":
        return 1
    if feedback == "not_interesting":
        return 0
    raise ValueError(f"invalid feedback: {feedback}")


def acknowledge_events(
    event_ids: list[str],
    feedback: str,
) -> dict[str, list[str]]:
    cfg = load_config()
    conn = _connect(cfg)
    now = _now()
    acked: list[str] = []
    failed: list[str] = []
    try:
        interest_ok = _interest_ok_from_feedback(feedback)
        _cleanup(conn, cfg)
        for event_id in event_ids:
            try:
                conn.execute(
                    """
                    INSERT INTO acked_items (event_id, acked_at, expires_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(event_id) DO UPDATE SET
                        acked_at=excluded.acked_at,
                        expires_at=excluded.expires_at
                    """,
                    (
                        event_id,
                        now.isoformat(),
                        (now + timedelta(hours=cfg.item_retention_hours)).isoformat(),
                    ),
                )
                conn.execute(
                    """
                    UPDATE items
                    SET interest_ok = ?, interest_scored_at = ?
                    WHERE event_id = ?
                    """,
                    (interest_ok, now.isoformat(), event_id),
                )
                row = conn.execute(
                    """
                    SELECT
                        event_id, source_id, source_name, author, title, content, published_at,
                        first_seen_at, interest_ok, interest_scored_at
                    FROM items
                    WHERE event_id = ?
                    """,
                    (event_id,),
                ).fetchone()
                if row is not None:
                    _update_rank_stats_for_row(conn, row, interest_ok, now.isoformat())
                    _update_rank_model_for_row(conn, cfg, row, interest_ok, now.isoformat())
                acked.append(event_id)
            except Exception:
                logger.exception("feed ack failed: %s", event_id)
                failed.append(event_id)
        conn.commit()
        return {"acknowledged": acked, "failed": failed}
    finally:
        conn.close()


def startup_force_poll() -> None:
    """MCP 服务启动时主动拉一次所有启用源，忽略 poll_ttl 限制。"""
    cfg = load_config()
    conn = _connect(cfg)
    try:
        trace_id = _trace_id_short()
        _cleanup(conn, cfg)
        sources = conn.execute(
            "SELECT * FROM sources WHERE enabled = 1 ORDER BY added_at DESC"
        ).fetchall()
        logger.info("[feed][trace=%s] startup force poll: %d sources", trace_id, len(sources))
        summary = _poll_rows(conn, cfg, sources, force=True, trace_id=trace_id)
        conn.commit()
        logger.info(
            "[feed][trace=%s] startup force poll done total=%d success=%d failed=%d failed_sources=%s",
            trace_id,
            int(summary["total"]),
            int(summary["success"]),
            int(summary["failed"]),
            summary["failed_sources"],
        )
    except Exception as e:
        logger.warning("[feed] startup force poll failed: %s", _err_text(e))
    finally:
        conn.close()
