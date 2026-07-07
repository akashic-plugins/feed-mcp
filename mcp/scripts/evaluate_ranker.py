#!/usr/bin/env python3
from __future__ import annotations

import argparse
import math
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import feed_backend


def _default_feed_db() -> Path:
    return Path.home() / ".akashic-plugin" / "data" / "feed-lab" / "feed_mcp.sqlite3"


def _precision(labels: list[int], k: int) -> float:
    top = labels[:k]
    return sum(top) / len(top) if top else 0.0


def _dcg(labels: list[int], k: int) -> float:
    return sum(label / math.log2(i + 2) for i, label in enumerate(labels[:k]))


def _ndcg(labels: list[int], k: int) -> float:
    ideal = sorted(labels, reverse=True)
    best = _dcg(ideal, k)
    return _dcg(labels, k) / best if best else 0.0


def _mrr(labels: list[int]) -> float:
    for i, label in enumerate(labels, start=1):
        if label:
            return 1.0 / i
    return 0.0


def _metrics(labels: list[int], k: int) -> dict[str, float]:
    return {
        f"precision@{k}": _precision(labels, k),
        f"ndcg@{k}": _ndcg(labels, k),
        "mrr": _mrr(labels),
        "accept_rate": sum(labels) / len(labels) if labels else 0.0,
        "count": float(len(labels)),
    }


def evaluate(feed_db: Path, k: int) -> None:
    cfg = feed_backend.load_config()
    cfg.db_path = feed_db
    conn = feed_backend._connect(cfg)
    try:
        rows = conn.execute(
            """
            SELECT
                event_id, source_id, source_type, source_name, title, content, url,
                author, published_at, first_seen_at, served_count, last_served_at,
                interest_ok
            FROM items
            WHERE interest_ok IN (0, 1)
            """
        ).fetchall()
        if not rows:
            raise SystemExit("no labeled items")
        by_time = sorted(
            rows,
            key=lambda row: str(row["published_at"] or row["first_seen_at"] or ""),
            reverse=True,
        )
        ranked = feed_backend._rank_rows(conn, cfg, list(rows), feed_backend._now())
        baseline_labels = [int(row["interest_ok"]) for row in by_time]
        ranker_labels = [int(row["interest_ok"]) for row, _score, _features in ranked]
        print("baseline_time", _metrics(baseline_labels, k))
        print("ranker", _metrics(ranker_labels, k))
        print("top_ranked")
        for row, score, features in ranked[:k]:
            print(
                f"- y={row['interest_ok']} score={score:.4f} "
                f"interest={features['interest']:.3f} novelty={features['novelty']:.3f} "
                f"fresh={features['freshness']:.3f} title={str(row['title'] or '')[:80]}"
            )
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--feed-db", type=Path, default=_default_feed_db())
    parser.add_argument("-k", type=int, default=5)
    args = parser.parse_args()
    evaluate(args.feed_db, args.k)


if __name__ == "__main__":
    main()
