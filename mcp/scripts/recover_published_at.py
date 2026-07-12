#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

import feedparser
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import feed_backend


_ARXIV_ID_RE = re.compile(r"^(?:[a-z0-9._-]+/\d{7}|\d{4}\.\d{4,5})(?:v\d+)?$", re.I)
_ARXIV_VERSION_RE = re.compile(r"v\d+$", re.I)
_X_EPOCH_MS = 1_288_834_974_657
_ARXIV_API = "https://export.arxiv.org/api/query"


def canonical_arxiv_id(url: object) -> str | None:
    parsed = urlparse(str(url or "").strip())
    if parsed.netloc.lower() not in {"arxiv.org", "www.arxiv.org", "export.arxiv.org"}:
        return None
    match = re.match(r"^/(?:abs|pdf|html)/(.+?)(?:\.pdf)?$", parsed.path, re.I)
    if match is None or _ARXIV_ID_RE.fullmatch(match.group(1)) is None:
        return None
    return _ARXIV_VERSION_RE.sub("", match.group(1))


def recovery_key(row: Mapping[str, Any]) -> str | None:
    status_id = feed_backend._extract_x_status_id(str(row.get("url") or ""))
    if status_id:
        return f"x:{status_id}"
    arxiv_id = canonical_arxiv_id(row.get("url"))
    if arxiv_id:
        return f"arxiv:{arxiv_id}"
    return None


def _read_missing(paths: list[Path]) -> dict[str, dict[str, Any]]:
    rows_by_key: dict[str, dict[str, Any]] = {}
    for path in paths:
        uri = f"{path.resolve().as_uri()}?mode=ro&immutable=1"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        try:
            columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(items)").fetchall()
            }
            if not {"event_id", "url", "published_at"}.issubset(columns):
                raise ValueError(f"{path}: items 缺少 event_id、url 或 published_at")
            rows = conn.execute(
                "SELECT event_id, url FROM items WHERE published_at IS NULL ORDER BY event_id"
            ).fetchall()
            for row in rows:
                item = dict(row)
                key = recovery_key(item)
                if key:
                    rows_by_key.setdefault(key, item)
        finally:
            conn.close()
    return rows_by_key


def _x_published_at(status_id: str) -> str:
    timestamp_ms = (int(status_id) >> 22) + _X_EPOCH_MS
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC).isoformat()


def _fetch_arxiv_batch(arxiv_ids: list[str]) -> dict[str, str]:
    response = requests.get(
        _ARXIV_API,
        params={"id_list": ",".join(arxiv_ids), "max_results": len(arxiv_ids)},
        headers={"User-Agent": "feed-mcp published-at recovery"},
        timeout=30,
    )
    response.raise_for_status()
    feed = feedparser.parse(response.content)
    recovered: dict[str, str] = {}
    for entry in feed.entries:
        arxiv_id = canonical_arxiv_id(entry.get("id") or entry.get("link"))
        parsed = entry.get("published_parsed")
        if arxiv_id and isinstance(parsed, time.struct_time):
            recovered[arxiv_id] = datetime(*parsed[:6], tzinfo=UTC).isoformat()
    return recovered


def recover(paths: list[Path]) -> dict[str, dict[str, Any]]:
    if not paths:
        raise ValueError("至少需要一个 Feed DB")
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        raise FileNotFoundError(", ".join(missing))

    rows_by_key = _read_missing(paths)
    overrides: dict[str, dict[str, Any]] = {}
    for key in sorted(rows_by_key):
        if key.startswith("x:"):
            overrides[key] = {
                "published_at": _x_published_at(key.removeprefix("x:")),
                "provenance": "x_snowflake",
                "confidence": 1.0,
            }

    arxiv_ids = sorted(key.removeprefix("arxiv:") for key in rows_by_key if key.startswith("arxiv:"))
    for offset in range(0, len(arxiv_ids), 50):
        batch = arxiv_ids[offset : offset + 50]
        for arxiv_id, published_at in _fetch_arxiv_batch(batch).items():
            overrides[f"arxiv:{arxiv_id}"] = {
                "published_at": published_at,
                "provenance": "arxiv_api",
                "confidence": 1.0,
            }
        if offset + 50 < len(arxiv_ids):
            time.sleep(3)
    return overrides


def main() -> None:
    parser = argparse.ArgumentParser(description="为缺失发布时间的 Feed 条目生成只读 recovery overrides")
    parser.add_argument("feed_db", type=Path, nargs="+")
    parser.add_argument("-o", "--output", type=Path)
    args = parser.parse_args()

    overrides = recover(args.feed_db)
    payload = json.dumps(overrides, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(payload, encoding="utf-8")
    else:
        sys.stdout.write(payload)
    print(f"recovered={len(overrides)}", file=sys.stderr)


if __name__ == "__main__":
    main()
