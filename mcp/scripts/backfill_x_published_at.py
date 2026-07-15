#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.path_config import resolve_feed_db
from src import feed_backend

_TWITTER_EPOCH_MS = 1288834974657

def _published_at_from_status_id(status_id: str) -> str | None:
    if not status_id.isdigit():
        return None
    value = int(status_id)
    if value <= 0:
        return None
    timestamp_ms = (value >> 22) + _TWITTER_EPOCH_MS
    if timestamp_ms < _TWITTER_EPOCH_MS:
        return None
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC).replace(microsecond=0).isoformat()


def _merge_event(conn: sqlite3.Connection, old_event_id: str, new_event_id: str, published_at: str) -> None:
    target = conn.execute(
        "SELECT interest_ok FROM items WHERE event_id = ?",
        (new_event_id,),
    ).fetchone()
    source = conn.execute(
        "SELECT interest_ok FROM items WHERE event_id = ?",
        (old_event_id,),
    ).fetchone()
    if target is None or source is None:
        return

    interest_ok = target["interest_ok"] if target["interest_ok"] is not None else source["interest_ok"]
    conn.execute(
        """
        UPDATE items
        SET published_at = ?,
            interest_ok = ?
        WHERE event_id = ?
        """,
        (published_at, interest_ok, new_event_id),
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO acked_items(event_id, acked_at, expires_at)
        SELECT ?, acked_at, expires_at FROM acked_items WHERE event_id = ?
        """,
        (new_event_id, old_event_id),
    )
    conn.execute("UPDATE rank_impressions SET event_id = ? WHERE event_id = ?", (new_event_id, old_event_id))
    conn.execute("UPDATE rank_model_updates SET event_id = ? WHERE event_id = ?", (new_event_id, old_event_id))
    conn.execute("DELETE FROM acked_items WHERE event_id = ?", (old_event_id,))
    conn.execute("DELETE FROM items WHERE event_id = ?", (old_event_id,))


def _move_event_refs(conn: sqlite3.Connection, old_event_id: str, new_event_id: str) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO acked_items(event_id, acked_at, expires_at)
        SELECT ?, acked_at, expires_at FROM acked_items WHERE event_id = ?
        """,
        (new_event_id, old_event_id),
    )
    conn.execute("UPDATE rank_impressions SET event_id = ? WHERE event_id = ?", (new_event_id, old_event_id))
    conn.execute("UPDATE rank_model_updates SET event_id = ? WHERE event_id = ?", (new_event_id, old_event_id))
    conn.execute("DELETE FROM acked_items WHERE event_id = ?", (old_event_id,))


def backfill(feed_db: Path, dry_run: bool) -> None:
    conn = sqlite3.connect(feed_db)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT event_id, source_id, title, url, published_at
            FROM items
            WHERE (
                lower(url) LIKE '%nitter.net/%/status/%'
                OR lower(url) LIKE '%twitter.com/%/status/%'
                OR lower(url) LIKE '%x.com/%/status/%'
                OR lower(url) LIKE '%xcancel.com/%/status/%'
              )
            """
        ).fetchall()
        updated = 0
        merged = 0
        skipped = 0
        for row in rows:
            old_event_id = str(row["event_id"])
            status_id = feed_backend._extract_x_status_id(str(row["url"] or ""))
            published_at = _published_at_from_status_id(status_id or "")
            if published_at is None:
                skipped += 1
                continue
            new_event_id = feed_backend._stable_event_id(
                str(row["source_id"]),
                str(row["url"] or ""),
                str(row["title"] or ""),
                published_at,
            )
            if dry_run:
                if new_event_id == old_event_id:
                    if row["published_at"] != published_at:
                        updated += 1
                elif conn.execute("SELECT 1 FROM items WHERE event_id = ?", (new_event_id,)).fetchone():
                    merged += 1
                else:
                    updated += 1
                continue
            if new_event_id == old_event_id:
                conn.execute("UPDATE items SET published_at = ? WHERE event_id = ?", (published_at, old_event_id))
                updated += 1
            elif conn.execute("SELECT 1 FROM items WHERE event_id = ?", (new_event_id,)).fetchone():
                _merge_event(conn, old_event_id, new_event_id, published_at)
                merged += 1
            else:
                conn.execute("UPDATE items SET event_id = ?, published_at = ? WHERE event_id = ?", (new_event_id, published_at, old_event_id))
                _move_event_refs(conn, old_event_id, new_event_id)
                updated += 1
        if dry_run:
            conn.rollback()
        else:
            conn.commit()
        print(f"scanned={len(rows)} updated={updated} merged={merged} skipped={skipped} dry_run={dry_run}")
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--feed-db", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    backfill(resolve_feed_db(args.feed_db), args.dry_run)


if __name__ == "__main__":
    main()
