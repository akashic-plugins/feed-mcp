from __future__ import annotations

import hashlib
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import recover_published_at
from src import feed_backend


class _Response:
    def __init__(self, content: bytes):
        self.content = content

    def raise_for_status(self) -> None:
        pass


def _create_db(path: Path) -> sqlite3.Connection:
    cfg = feed_backend.load_config()
    cfg.db_path = path
    return feed_backend._connect(cfg)


def _insert(conn: sqlite3.Connection, event_id: str, url: str, published_at: str | None = None) -> None:
    now = datetime(2026, 7, 7, 5, tzinfo=UTC).isoformat()
    conn.execute(
        """
        INSERT INTO items (
            event_id, source_id, source_name, source_type, title, content,
            url, author, published_at, first_seen_at, last_seen_at,
            emitted_at, content_hash
        ) VALUES (?, 'source', 'Source', 'rss', ?, '', ?, NULL, ?, ?, ?, NULL, ?)
        """,
        (event_id, event_id, url, published_at, now, now, event_id),
    )


def _atom(ids: list[str]) -> bytes:
    entries = "".join(
        f"""
        <entry><id>http://arxiv.org/abs/{arxiv_id}v1</id>
        <title>{arxiv_id}</title><published>2026-07-04T12:00:00Z</published>
        <updated>2026-07-04T12:00:00Z</updated></entry>
        """
        for arxiv_id in ids
    )
    return f'<feed xmlns="http://www.w3.org/2005/Atom">{entries}</feed>'.encode()


def test_recover_is_read_only_and_batches_arxiv(tmp_path, monkeypatch):
    monkeypatch.setenv("AKA_PLUGIN_DATA_DIR", str(tmp_path / "runtime"))
    db = tmp_path / "feed.sqlite3"
    conn = _create_db(db)
    _insert(conn, "x", "https://nitter.net/OpenAI/status/2074185390060110138#m")
    for index in range(51):
        _insert(conn, f"a-{index}", f"https://arxiv.org/abs/2607.{index:05d}v2")
    _insert(conn, "existing", "https://arxiv.org/abs/2607.99999", "2026-07-01T00:00:00+00:00")
    conn.commit()
    conn.close()
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    calls: list[list[str]] = []
    sleeps: list[int] = []

    def fake_get(_url, *, params, headers, timeout):
        assert headers["User-Agent"]
        assert timeout == 30
        ids = str(params["id_list"]).split(",")
        calls.append(ids)
        return _Response(_atom(ids))

    monkeypatch.setattr(recover_published_at.requests, "get", fake_get)
    monkeypatch.setattr(recover_published_at.time, "sleep", sleeps.append)

    overrides = recover_published_at.recover([db])

    assert [len(batch) for batch in calls] == [50, 1]
    assert sleeps == [3]
    assert len(overrides) == 52
    assert overrides["x:2074185390060110138"]["provenance"] == "x_snowflake"
    assert overrides["arxiv:2607.00000"] == {
        "published_at": "2026-07-04T12:00:00+00:00",
        "provenance": "arxiv_api",
        "confidence": 1.0,
    }
    assert "arxiv:2607.99999" not in overrides
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before
