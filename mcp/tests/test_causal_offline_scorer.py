from __future__ import annotations

import hashlib
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.causal_offline_scorer import score_databases
from src import feed_backend


def _create_db(path: Path) -> sqlite3.Connection:
    cfg = feed_backend.load_config()
    cfg.db_path = path
    return feed_backend._connect(cfg)


def _insert_item(
    conn: sqlite3.Connection,
    event_id: str,
    *,
    first_seen_at: datetime,
    last_seen_at: datetime,
    title: str,
    published_at: datetime | None,
    interest_ok: int | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO items (
            event_id, source_id, source_name, source_type, title, content,
            url, author, published_at, first_seen_at, last_seen_at,
            emitted_at, content_hash, interest_ok, interest_scored_at
        ) VALUES (?, 'source', 'Source', 'rss', ?, ?, NULL, NULL, ?, ?, ?, NULL, ?, ?, ?)
        """,
        (
            event_id,
            title,
            title,
            published_at.isoformat() if published_at else None,
            first_seen_at.isoformat(),
            last_seen_at.isoformat(),
            event_id,
            interest_ok,
            last_seen_at.isoformat() if interest_ok is not None else None,
        ),
    )


def _fingerprint(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_score_databases_is_causal_read_only_and_keeps_missing_published_items(tmp_path, monkeypatch):
    monkeypatch.setenv("AKA_PLUGIN_DATA_DIR", str(tmp_path / "runtime"))
    older = tmp_path / "older.sqlite3"
    newer = tmp_path / "newer.sqlite3"
    base = datetime(2026, 7, 1, 8, 15, tzinfo=UTC)

    conn = _create_db(older)
    _insert_item(
        conn,
        "duplicate",
        first_seen_at=base,
        last_seen_at=base,
        title="old title",
        published_at=base - timedelta(hours=2),
        interest_ok=1,
    )
    conn.execute(
        "INSERT INTO acked_items VALUES ('duplicate', ?, ?)",
        (base.isoformat(), (base + timedelta(days=1)).isoformat()),
    )
    conn.execute(
        "INSERT INTO rank_impressions(event_id, served_at, position, rank_score, features_json) VALUES ('duplicate', ?, 1, 1, '{}')",
        (base.isoformat(),),
    )
    conn.execute(
        "INSERT INTO rank_model_weights VALUES ('bias', 8, ?)",
        (base.isoformat(),),
    )
    conn.commit()
    conn.close()

    conn = _create_db(newer)
    _insert_item(
        conn,
        "duplicate",
        first_seen_at=base,
        last_seen_at=base + timedelta(minutes=10),
        title="new title",
        published_at=base - timedelta(hours=2),
        interest_ok=0,
    )
    _insert_item(
        conn,
        "no-published-at",
        first_seen_at=base + timedelta(hours=1, minutes=5),
        last_seen_at=base + timedelta(hours=1, minutes=5),
        title="available only by first seen",
        published_at=None,
    )
    conn.commit()
    conn.close()

    before = {_path: _fingerprint(_path) for _path in (older, newer)}
    result = score_databases([older, newer])

    assert set(result) == {"duplicate", "no-published-at"}
    assert result["no-published-at"]["published_at"] is None
    assert result["no-published-at"]["wake_eligible"] is False
    assert result["no-published-at"]["freshness_reason"] == "missing_published_at"
    assert result["no-published-at"]["available_at"] == (base + timedelta(hours=1, minutes=5)).isoformat()
    assert result["duplicate"]["features"]["ml_score"] == 0.5
    assert result["duplicate"]["features"]["exposure_count"] == 0.0
    assert result["duplicate"]["features"]["source_prior"] == 0.5
    assert result["duplicate"]["scored_at"] == base.isoformat()
    assert {_path: _fingerprint(_path) for _path in (older, newer)} == before


def test_latest_last_seen_wins_during_deduplication(tmp_path, monkeypatch):
    monkeypatch.setenv("AKA_PLUGIN_DATA_DIR", str(tmp_path / "runtime"))
    older = tmp_path / "older.sqlite3"
    newer = tmp_path / "newer.sqlite3"
    base = datetime(2026, 7, 1, 8, tzinfo=UTC)
    for path, title, last_seen in (
        (older, "old", base),
        (newer, "new", base + timedelta(minutes=1)),
    ):
        conn = _create_db(path)
        _insert_item(
            conn,
            "same",
            first_seen_at=base,
            last_seen_at=last_seen,
            title=title,
            published_at=base,
        )
        conn.commit()
        conn.close()

    forward = score_databases([older, newer])
    backward = score_databases([newer, older])

    assert forward["same"] == backward["same"]


def test_optional_overrides_apply_by_canonical_recovery_key(tmp_path, monkeypatch):
    monkeypatch.setenv("AKA_PLUGIN_DATA_DIR", str(tmp_path / "runtime"))
    db = tmp_path / "feed.sqlite3"
    seen = datetime(2026, 7, 7, 8, tzinfo=UTC)
    conn = _create_db(db)
    _insert_item(
        conn,
        "paper",
        first_seen_at=seen,
        last_seen_at=seen,
        title="Paper",
        published_at=None,
    )
    conn.execute(
        "UPDATE items SET url = 'https://arxiv.org/abs/2607.04010v2' WHERE event_id = 'paper'"
    )
    conn.commit()
    conn.close()

    without = score_databases([db])
    with_override = score_databases(
        [db],
        {
            "arxiv:2607.04010": {
                "published_at": "2026-07-07T07:00:00Z",
                "provenance": "arxiv_api",
                "confidence": 1.0,
            }
        },
    )

    assert without["paper"]["published_at"] is None
    assert "published_at_override" not in without["paper"]
    assert with_override["paper"]["published_at"] == "2026-07-07T07:00:00+00:00"
    assert with_override["paper"]["wake_eligible"] is True
    assert with_override["paper"]["published_at_override"] == {
        "key": "arxiv:2607.04010",
        "provenance": "arxiv_api",
        "confidence": 1.0,
    }
