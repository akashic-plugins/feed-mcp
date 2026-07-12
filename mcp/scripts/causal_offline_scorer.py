#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import feed_backend
from scripts.recover_published_at import recovery_key


_ITEM_COLUMNS = (
    "event_id",
    "source_id",
    "source_name",
    "source_type",
    "title",
    "content",
    "url",
    "author",
    "published_at",
    "first_seen_at",
    "last_seen_at",
    "emitted_at",
    "content_hash",
    "interest_ok",
    "interest_scored_at",
)
_X_STATUS_RE = re.compile(r"/status/(\d+)")
_X_EPOCH_MS = 1_288_834_974_657


def _x_published_at(url: object) -> datetime | None:
    match = _X_STATUS_RE.search(str(url or ""))
    if match is None:
        return None
    timestamp_ms = (int(match.group(1)) >> 22) + _X_EPOCH_MS
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=feed_backend.UTC)


def _item_identity(row: dict[str, Any], event_id: str) -> str:
    match = _X_STATUS_RE.search(str(row.get("url") or ""))
    if match is not None:
        return f"x:{row.get('source_id')}:{match.group(1)}"
    return f"event:{event_id}"


def _read_items(path: Path) -> list[dict[str, Any]]:
    uri = f"{path.resolve().as_uri()}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(items)").fetchall()
        }
        if not {"event_id", "first_seen_at"}.issubset(columns):
            raise ValueError(f"{path}: items 缺少 event_id 或 first_seen_at")
        expressions = [column if column in columns else f"NULL AS {column}" for column in _ITEM_COLUMNS]
        rows = conn.execute(f"SELECT {', '.join(expressions)} FROM items ORDER BY event_id").fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def _last_seen(row: dict[str, Any]) -> datetime:
    return (
        feed_backend._parse_rank_dt(row.get("last_seen_at"))
        or feed_backend._parse_rank_dt(row.get("first_seen_at"))
        or datetime.min.replace(tzinfo=feed_backend.UTC)
    )


def _deduplicate(
    paths: list[Path],
    overrides: dict[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    items: dict[str, dict[str, Any]] = {}
    for path in paths:
        for row in _read_items(path):
            event_id = str(row.get("event_id") or "").strip()
            if not event_id:
                continue
            available_at = feed_backend._parse_rank_dt(row.get("first_seen_at"))
            if available_at is None:
                raise ValueError(f"{path}: {event_id} 的 first_seen_at 无效")
            row = dict(row)
            if not row.get("published_at"):
                key = recovery_key(row)
                override = overrides.get(key) if overrides and key else None
                if override is not None:
                    published_at = feed_backend._parse_rank_dt(override.get("published_at"))
                    if published_at is None:
                        raise ValueError(f"{key}: override 的 published_at 无效")
                    row["published_at"] = published_at.isoformat()
                    row["published_at_override"] = {
                        "key": key,
                        "provenance": override.get("provenance"),
                        "confidence": override.get("confidence"),
                    }
            if not row.get("published_at"):
                recovered = _x_published_at(row.get("url"))
                if recovered is not None:
                    row["published_at"] = recovered.isoformat()
            identity = _item_identity(row, event_id)
            previous = items.get(identity)
            if previous is None or _last_seen(row) > _last_seen(previous):
                if previous is not None:
                    row["first_seen_at"] = min(
                        str(row["first_seen_at"]), str(previous["first_seen_at"])
                    )
                items[identity] = row
            elif str(row["first_seen_at"]) < str(previous["first_seen_at"]):
                previous["first_seen_at"] = row["first_seen_at"]
    return items


def _insert_items(conn: sqlite3.Connection, items: dict[str, dict[str, Any]]) -> None:
    for identity in sorted(items):
        row = items[identity]
        event_id = str(row["event_id"])
        title = str(row.get("title") or "").strip() or None
        content = str(row.get("content") or title or "")
        conn.execute(
            """
            INSERT INTO items (
                event_id, source_id, source_name, source_type, title, content,
                url, author, published_at, first_seen_at, last_seen_at,
                emitted_at, content_hash, interest_ok, interest_scored_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                str(row.get("source_id") or "unknown"),
                str(row.get("source_name") or row.get("source_id") or "unknown"),
                str(row.get("source_type") or "rss"),
                title,
                content,
                row.get("url"),
                row.get("author"),
                row.get("published_at"),
                row.get("first_seen_at"),
                row.get("last_seen_at") or row.get("first_seen_at"),
                row.get("emitted_at"),
                str(row.get("content_hash") or ""),
                row.get("interest_ok"),
                row.get("interest_scored_at"),
            ),
        )


def _reset_learning_state(conn: sqlite3.Connection, cfg: feed_backend.FeedMcpConfig) -> None:
    conn.execute("DELETE FROM acked_items")
    conn.execute("UPDATE items SET interest_ok = NULL, interest_scored_at = NULL")
    conn.execute("DELETE FROM rank_stats")
    conn.execute("DELETE FROM rank_impressions")
    conn.execute("DELETE FROM rank_model_updates")
    conn.execute("DELETE FROM rank_model_weights")
    feed_backend._ensure_rank_model(conn, cfg)
    conn.commit()


def score_databases(
    paths: list[Path],
    overrides: dict[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    if not paths:
        raise ValueError("至少需要一个 Feed DB")
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        raise FileNotFoundError(", ".join(missing))

    items = _deduplicate(paths, overrides)
    cfg = feed_backend.load_config()
    with tempfile.TemporaryDirectory(prefix="feed-causal-score-") as temp_dir:
        cfg.db_path = Path(temp_dir) / "feed.sqlite3"
        conn = feed_backend._connect(cfg)
        try:
            _insert_items(conn, items)
            _reset_learning_state(conn, cfg)

            by_hour: dict[datetime, list[str]] = defaultdict(list)
            available_by_id: dict[str, datetime] = {}
            for item in items.values():
                event_id = str(item["event_id"])
                available_at = feed_backend._parse_rank_dt(item["first_seen_at"])
                assert available_at is not None
                available_by_id[event_id] = available_at
                by_hour[available_at.replace(minute=0, second=0, microsecond=0)].append(event_id)

            output: dict[str, dict[str, Any]] = {}
            for hour in sorted(by_hour):
                event_ids = sorted(by_hour[hour])
                placeholders = ",".join("?" for _ in event_ids)
                rows = conn.execute(
                    f"SELECT * FROM items WHERE event_id IN ({placeholders})",
                    event_ids,
                ).fetchall()
                scored_at = max(available_by_id[event_id] for event_id in event_ids)
                for row, score, features in feed_backend._rank_rows(conn, cfg, list(rows), scored_at):
                    event_id = str(row["event_id"])
                    published_at = feed_backend._parse_rank_dt(row["published_at"])
                    age = scored_at - published_at if published_at is not None else None
                    wake_eligible = bool(
                        age is not None
                        and timedelta(0) <= age <= timedelta(hours=cfg.item_retention_hours)
                    )
                    output[event_id] = {
                        "score": score,
                        "features": features,
                        "available_at": available_by_id[event_id].isoformat(),
                        "scored_at": scored_at.isoformat(),
                        "published_at": row["published_at"],
                        "wake_eligible": wake_eligible,
                        "freshness_reason": (
                            "fresh"
                            if wake_eligible
                            else "missing_published_at"
                            if published_at is None
                            else "outside_retention_window"
                        ),
                        "source_id": row["source_id"],
                        "source_name": row["source_name"],
                    }
                    override = items[_item_identity(dict(row), event_id)].get(
                        "published_at_override"
                    )
                    if override is not None:
                        output[event_id]["published_at_override"] = override
            return output
        finally:
            conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="按 first_seen 小时重放 Feed 预处理评分")
    parser.add_argument("feed_db", type=Path, nargs="+")
    parser.add_argument("--overrides", type=Path)
    parser.add_argument("-o", "--output", type=Path)
    args = parser.parse_args()

    overrides = None
    if args.overrides:
        raw = json.loads(args.overrides.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("overrides 必须是 JSON 对象")
        overrides = raw
    result = score_databases(args.feed_db, overrides)
    payload = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(payload, encoding="utf-8")
    else:
        sys.stdout.write(payload)
    print(f"scored_events={len(result)}", file=sys.stderr)


if __name__ == "__main__":
    main()
