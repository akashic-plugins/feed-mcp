#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import feed_backend


def _default_feed_db() -> Path:
    return Path.home() / ".akashic-plugin" / "data" / "feed-lab" / "feed_mcp.sqlite3"


def _default_sessions_db() -> Path:
    return Path.home() / ".akashic" / "workspace" / "sessions.db"


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


def _normalize_event_id(raw: str) -> str | None:
    value = raw.strip()
    if value.startswith("feed-mcp:"):
        value = value.removeprefix("feed-mcp:")
    elif value.startswith("feed:"):
        value = value.removeprefix("feed:")
    if value.startswith("fmcp_"):
        return value
    return None


def _feedback_label(feedback_type: str) -> int | None:
    if feedback_type in {"explicit_quote", "topic_follow"}:
        return 1
    if feedback_type == "no_topic_follow":
        return 0
    return None


def _labels_from_proactive_feedback(feedback_db: Path, sessions_db: Path) -> dict[str, int]:
    if not feedback_db.exists():
        raise SystemExit(f"feedback db not found: {feedback_db}")
    if not sessions_db.exists():
        raise SystemExit(f"sessions db not found: {sessions_db}")

    feedback = sqlite3.connect(feedback_db)
    feedback.row_factory = sqlite3.Row
    sessions = sqlite3.connect(sessions_db)
    sessions.row_factory = sqlite3.Row
    try:
        counts: dict[str, list[int]] = {}
        rows = feedback.execute(
            """
            SELECT proactive_message_id, feedback_type
            FROM proactive_feedback_events
            WHERE proactive_message_id IS NOT NULL
            """
        ).fetchall()
        for row in rows:
            label = _feedback_label(str(row["feedback_type"]))
            if label is None:
                continue
            message = sessions.execute(
                "SELECT extra FROM messages WHERE id = ?",
                (str(row["proactive_message_id"]),),
            ).fetchone()
            if message is None:
                continue
            try:
                extra = json.loads(str(message["extra"] or "{}"))
            except json.JSONDecodeError:
                continue
            for raw_event_id in extra.get("evidence_item_ids", []):
                event_id = _normalize_event_id(str(raw_event_id))
                if event_id is None:
                    continue
                bucket = counts.setdefault(event_id, [0, 0])
                bucket[label] += 1
        return {
            event_id: 1 if pos >= neg else 0
            for event_id, (neg, pos) in counts.items()
            if neg or pos
        }
    finally:
        feedback.close()
        sessions.close()


def evaluate(
    feed_db: Path,
    k: int,
    feedback_db: Path | None,
    sessions_db: Path,
) -> None:
    cfg = feed_backend.load_config()
    cfg.db_path = feed_db
    conn = feed_backend._connect(cfg)
    try:
        labels = None
        if feedback_db is not None:
            labels = _labels_from_proactive_feedback(feedback_db, sessions_db)
            where = ""
        else:
            where = "WHERE interest_ok IN (0, 1)"
        rows = conn.execute(
            f"""
            SELECT
                event_id, source_id, source_type, source_name, title, content, url,
                author, published_at, first_seen_at, served_count, last_served_at,
                interest_ok
            FROM items
            {where}
            """
        ).fetchall()
        if labels is not None:
            rows = [row for row in rows if str(row["event_id"]) in labels]
        if not rows:
            raise SystemExit("no labeled items")
        by_time = sorted(
            rows,
            key=lambda row: str(row["published_at"] or row["first_seen_at"] or ""),
            reverse=True,
        )
        ranked = feed_backend._rank_rows(conn, cfg, list(rows), feed_backend._now())
        if labels is None:
            baseline_labels = [int(row["interest_ok"]) for row in by_time]
            ranker_labels = [int(row["interest_ok"]) for row, _score, _features in ranked]
        else:
            baseline_labels = [labels[str(row["event_id"])] for row in by_time]
            ranker_labels = [labels[str(row["event_id"])] for row, _score, _features in ranked]
        print("label_source", "proactive_feedback" if labels is not None else "interest_ok")
        print("baseline_time", _metrics(baseline_labels, k))
        print("ranker", _metrics(ranker_labels, k))
        print("top_ranked")
        for row, score, features in ranked[:k]:
            label = labels[str(row["event_id"])] if labels is not None else int(row["interest_ok"])
            print(
                f"- y={label} score={score:.4f} "
                f"ml={features.get('ml_score', score):.3f} "
                f"coarse={features.get('coarse_score', 0.0):.3f} "
                f"interest={features['interest']:.3f} novelty={features['novelty']:.3f} "
                f"fresh={features['freshness']:.3f} title={str(row['title'] or '')[:80]}"
            )
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--feed-db", type=Path, default=_default_feed_db())
    parser.add_argument("--feedback-db", type=Path, default=None)
    parser.add_argument("--sessions-db", type=Path, default=_default_sessions_db())
    parser.add_argument("-k", type=int, default=5)
    args = parser.parse_args()
    evaluate(args.feed_db, args.k, args.feedback_db, args.sessions_db)


if __name__ == "__main__":
    main()
