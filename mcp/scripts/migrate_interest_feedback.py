#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from path_config import resolve_feed_db, resolve_workspace_path


def _event_id(compound_key: str) -> str:
    server, _, item_id = compound_key.partition(":")
    if server not in {"feed", "feed-mcp"}:
        return ""
    return item_id


def _load_ids(raw: str) -> list[str]:
    try:
        data = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return [str(item) for item in data if isinstance(item, str)]


def _iter_feedback(proactive_db: Path):
    conn = sqlite3.connect(proactive_db)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT started_at, interesting_ids, discarded_ids, cited_ids
            FROM tick_log
            ORDER BY started_at ASC, id ASC
            """
        ).fetchall()
    finally:
        conn.close()

    for row in rows:
        scored_at = str(row["started_at"] or "")
        for key in _load_ids(str(row["discarded_ids"] or "")):
            event_id = _event_id(key)
            if event_id:
                yield event_id, 0, scored_at
        positive = set(_load_ids(str(row["interesting_ids"] or "")))
        positive.update(_load_ids(str(row["cited_ids"] or "")))
        for key in sorted(positive):
            event_id = _event_id(key)
            if event_id:
                yield event_id, 1, scored_at


def migrate(feed_db: Path, proactive_db: Path) -> tuple[int, int]:
    if not feed_db.exists():
        raise FileNotFoundError(f"feed db not found: {feed_db}")
    if not proactive_db.exists():
        raise FileNotFoundError(f"proactive db not found: {proactive_db}")
    conn = sqlite3.connect(feed_db)
    try:
        has_items = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'items'"
        ).fetchone()
        if has_items is None:
            raise RuntimeError(f"feed db missing items table: {feed_db}")
        columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(items)").fetchall()
        }
        if "interest_ok" not in columns:
            conn.execute("ALTER TABLE items ADD COLUMN interest_ok INTEGER")
        if "interest_scored_at" not in columns:
            conn.execute("ALTER TABLE items ADD COLUMN interest_scored_at TEXT")

        updated = 0
        seen = 0
        fallback_time = datetime.now(UTC).isoformat()
        for event_id, interest_ok, scored_at in _iter_feedback(proactive_db):
            seen += 1
            cur = conn.execute(
                """
                UPDATE items
                SET interest_ok = ?, interest_scored_at = ?
                WHERE event_id = ?
                """,
                (interest_ok, scored_at or fallback_time, event_id),
            )
            updated += cur.rowcount
        has_rank_stats = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'rank_stats'"
        ).fetchone()
        if has_rank_stats is not None:
            conn.execute("DELETE FROM rank_stats")
        conn.commit()
        return seen, updated
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--feed-db", type=Path)
    parser.add_argument("--proactive-db", type=Path)
    args = parser.parse_args()
    feed_db = resolve_feed_db(args.feed_db)
    proactive_db = resolve_workspace_path(args.proactive_db, "proactive.db")
    seen, updated = migrate(feed_db, proactive_db)
    print(f"seen={seen} updated={updated}")


if __name__ == "__main__":
    main()
