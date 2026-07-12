from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import feed_backend


def _insert_item(conn, *, index: int, source: str, published_at: datetime) -> str:
    event_id = f"event-{index:03d}"
    timestamp = published_at.isoformat()
    conn.execute(
        """
        INSERT INTO items (
            event_id, source_id, source_name, source_type, title, content,
            url, author, published_at, first_seen_at, last_seen_at,
            emitted_at, content_hash
        ) VALUES (?, ?, ?, 'rss', ?, ?, ?, '', ?, ?, ?, NULL, ?)
        """,
        (
            event_id,
            source.lower(),
            source,
            f"title-{index}",
            f"content-{index}",
            f"https://example.com/{index}",
            timestamp,
            timestamp,
            timestamp,
            f"hash-{index}",
        ),
    )
    return event_id


def test_wake_fetch_returns_all_unread_grouped_by_source_and_consumption_is_not_feedback(
    tmp_path, monkeypatch
):
    now = datetime(2026, 7, 12, 12, tzinfo=UTC)
    monkeypatch.setenv("AKA_PLUGIN_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(feed_backend, "_now", lambda: now)
    cfg = feed_backend.load_config()
    conn = feed_backend._connect(cfg)
    expected = []
    try:
        for index in range(60):
            source = "Alpha" if index % 2 == 0 else "Beta"
            expected.append(
                _insert_item(
                    conn,
                    index=index,
                    source=source,
                    published_at=now - timedelta(minutes=index),
                )
            )
        conn.commit()
    finally:
        conn.close()

    events = feed_backend.get_proactive_events()

    assert len(events) == 60
    assert [event["source_name"] for event in events] == ["Alpha"] * 30 + ["Beta"] * 30
    for source in ("Alpha", "Beta"):
        timestamps = [
            event["published_at"] for event in events if event["source_name"] == source
        ]
        assert timestamps == sorted(timestamps, reverse=True)
    assert all("preprocess_score" in event for event in events)

    consumed = expected[0]
    feed_backend.acknowledge_events([consumed])
    conn = feed_backend._connect(cfg)
    try:
        row = conn.execute(
            "SELECT interest_ok, interest_scored_at FROM items WHERE event_id = ?",
            (consumed,),
        ).fetchone()
        updates = conn.execute(
            "SELECT COUNT(*) FROM rank_model_updates WHERE event_id = ?",
            (consumed,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert row["interest_ok"] is None
    assert row["interest_scored_at"] is None
    assert updates == 0
