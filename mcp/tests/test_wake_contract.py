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


def test_wake_fetch_excludes_missing_and_stale_publication_even_when_just_seen(
    tmp_path, monkeypatch
):
    now = datetime(2026, 7, 12, 12, tzinfo=UTC)
    monkeypatch.setenv("AKA_PLUGIN_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(feed_backend, "_now", lambda: now)
    cfg = feed_backend.load_config()
    conn = feed_backend._connect(cfg)
    try:
        conn.executemany(
            """
            INSERT INTO items (
                event_id, source_id, source_name, source_type, title, content,
                url, author, published_at, first_seen_at, last_seen_at,
                emitted_at, content_hash
            ) VALUES (?, 'source', 'Source', 'twitter', ?, '', '', '', ?, ?, ?, NULL, ?)
            """,
            [
                ("missing", "missing", None, now.isoformat(), now.isoformat(), "m"),
                (
                    "stale", "stale", (now - timedelta(days=10)).isoformat(),
                    now.isoformat(), now.isoformat(), "s",
                ),
                (
                    "fresh", "fresh", (now - timedelta(minutes=5)).isoformat(),
                    now.isoformat(), now.isoformat(), "f",
                ),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    events = feed_backend.get_proactive_events()

    assert [event["event_id"] for event in events] == ["fresh"]


def test_feedparser_reads_rfc2822_namespace_date_and_stable_entry_id():
    rss = """
    <rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/">
      <channel>
        <item>
          <guid>stable-guid</guid><title>Title</title>
          <link>https://example.com/item</link>
          <description>Body</description>
          <pubDate>Mon, 06 Jul 2026 15:46:43 +0000</pubDate>
        </item>
        <item>
          <guid>arxiv-id</guid><title>Paper</title>
          <link>https://arxiv.org/abs/2607.00001</link>
          <dc:date>2026-07-06T12:00:00Z</dc:date>
        </item>
      </channel>
    </rss>
    """

    items = feed_backend._parse_rss(rss)

    assert [item["entry_id"] for item in items] == ["stable-guid", "arxiv-id"]
    assert items[0]["published_at"] == "2026-07-06T15:46:43+00:00"
    assert items[1]["published_at"] == "2026-07-06T12:00:00+00:00"


def test_stable_identity_does_not_include_title_or_publication_time():
    first = feed_backend._stable_event_id(
        "source", "https://example.com/post", "old title", "2026-07-01T00:00:00Z",
        "guid-1",
    )
    updated = feed_backend._stable_event_id(
        "source", "https://example.com/post", "new title", "2026-07-02T00:00:00Z",
        "guid-1",
    )

    assert first == updated
