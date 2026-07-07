#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.evaluate_ranker import _labels_from_proactive_feedback
from src import feed_backend


def _default_feed_db() -> Path:
    return Path.home() / ".akashic-plugin" / "data" / "feed-lab" / "feed_mcp.sqlite3"


def _default_feedback_db() -> Path:
    return Path.home() / ".akashic" / "workspace" / "proactive_feedback" / "proactive_feedback.db"


def _default_sessions_db() -> Path:
    return Path.home() / ".akashic" / "workspace" / "sessions.db"


def _label(event_id: str, fallback_labels: dict[str, int], feedback_labels: dict[str, int] | None) -> int | None:
    if feedback_labels is not None and event_id in feedback_labels:
        return feedback_labels[event_id]
    return fallback_labels.get(event_id)


def _metrics(labels: list[int | None], k: int) -> dict[str, float]:
    top = labels[:k]
    known = [label for label in top if label is not None]
    positives = sum(1 for label in top if label == 1)
    utility = sum(1 if label == 1 else -1 if label == 0 else 0 for label in top)
    return {
        "positive_at_k": float(positives),
        "utility_at_k": float(utility),
        "accept_rate_at_k": positives / k if k else 0.0,
        "known_accept_rate": positives / len(known) if known else 0.0,
        "known": float(len(known)),
        "unknown": float(len(top) - len(known)),
    }


def _old_rows(conn: sqlite3.Connection, tick: str, k: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT i.*
        FROM rank_impressions r
        JOIN items i ON i.event_id = r.event_id
        WHERE r.served_at = ?
        ORDER BY r.position
        LIMIT ?
        """,
        (tick, k),
    ).fetchall()


def _candidate_rows(conn: sqlite3.Connection, cfg: feed_backend.FeedMcpConfig, tick: str) -> list[sqlite3.Row]:
    tick_dt = feed_backend._parse_rank_dt(tick) or feed_backend._now()
    cutoff = (tick_dt - timedelta(hours=cfg.item_retention_hours)).isoformat()
    return conn.execute(
        """
        SELECT
            event_id, source_id, source_type, source_name, title, content, url,
            author, published_at, first_seen_at, interest_ok, interest_scored_at
        FROM items
        WHERE (published_at IS NOT NULL OR source_type != 'rss')
          AND coalesce(published_at, first_seen_at) <= ?
          AND coalesce(published_at, first_seen_at) >= ?
          AND (interest_scored_at IS NULL OR interest_scored_at > ?)
        ORDER BY coalesce(published_at, first_seen_at) DESC
        LIMIT 200
        """,
        (tick, cutoff, tick),
    ).fetchall()


def _snapshot_at(conn: sqlite3.Connection, tick: str) -> sqlite3.Connection:
    snap = sqlite3.connect(":memory:")
    snap.row_factory = sqlite3.Row
    conn.backup(snap)
    snap.execute("DELETE FROM rank_impressions WHERE served_at > ?", (tick,))
    snap.execute(
        """
        UPDATE items
        SET interest_ok = NULL,
            interest_scored_at = NULL
        WHERE interest_scored_at > ?
        """,
        (tick,),
    )
    snap.commit()
    return snap


def replay(
    feed_db: Path,
    feedback_db: Path | None,
    sessions_db: Path,
    ticks: int,
    k: int,
) -> None:
    cfg = feed_backend.load_config()
    cfg.db_path = feed_db
    conn = feed_backend._connect(cfg)
    try:
        feedback_labels = None
        if feedback_db is not None and feedback_db.exists():
            feedback_labels = _labels_from_proactive_feedback(feedback_db, sessions_db)
        fallback_labels = {
            str(row["event_id"]): int(row["interest_ok"])
            for row in conn.execute("SELECT event_id, interest_ok FROM items WHERE interest_ok IN (0, 1)")
        }
        tick_rows = conn.execute(
            """
            SELECT served_at
            FROM rank_impressions
            GROUP BY served_at
            ORDER BY served_at DESC
            LIMIT ?
            """,
            (ticks,),
        ).fetchall()
        served_ticks = [str(row["served_at"]) for row in reversed(tick_rows)]
        old_labels: list[int | None] = []
        new_labels: list[int | None] = []
        changed = 0
        print("tick_count", len(served_ticks))
        for idx, tick in enumerate(served_ticks, start=1):
            now = feed_backend._parse_rank_dt(tick) or feed_backend._now()
            old = _old_rows(conn, tick, k)
            snap = _snapshot_at(conn, tick)
            try:
                candidates = _candidate_rows(snap, cfg, tick)
                new = [row for row, _score, _features in feed_backend._rank_rows(snap, cfg, list(candidates), now)[:k]]
            finally:
                snap.close()
            old_ids = [str(row["event_id"]) for row in old]
            new_ids = [str(row["event_id"]) for row in new]
            changed += old_ids != new_ids
            old_tick_labels = [_label(str(row["event_id"]), fallback_labels, feedback_labels) for row in old]
            new_tick_labels = [_label(str(row["event_id"]), fallback_labels, feedback_labels) for row in new]
            old_labels.extend(old_tick_labels)
            new_labels.extend(new_tick_labels)
            print(f"tick {idx} {tick}")
            print("  old", _metrics(old_tick_labels, k), " | ", " ; ".join(str(row["title"] or "")[:48] for row in old))
            print("  new", _metrics(new_tick_labels, k), " | ", " ; ".join(str(row["title"] or "")[:48] for row in new))
        print("summary_old", _metrics(old_labels, k * max(1, len(served_ticks))))
        print("summary_new", _metrics(new_labels, k * max(1, len(served_ticks))))
        print("changed_ticks", changed)
        print("label_source", "proactive_feedback" if feedback_labels is not None else "interest_ok")
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--feed-db", type=Path, default=_default_feed_db())
    parser.add_argument("--feedback-db", type=Path, default=_default_feedback_db())
    parser.add_argument("--sessions-db", type=Path, default=_default_sessions_db())
    parser.add_argument("--ticks", type=int, default=20)
    parser.add_argument("-k", type=int, default=5)
    args = parser.parse_args()
    replay(args.feed_db, args.feedback_db, args.sessions_db, args.ticks, args.k)


if __name__ == "__main__":
    main()
