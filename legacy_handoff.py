from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import closing
from pathlib import Path
from typing import Protocol, cast

from agent.migrations.proactive_island import (
    AdapterPlan,
    HandoffBlocked,
    LegacyFact,
    LegacyFactKind,
    TargetReceipt,
)
from agent.migrations.proactive_island.handoff import receipt_digest

from content_source import CONTENT_SOURCE_ID
from feed_runtime import backend


LEGACY_SOURCE_ID = "feed@github:subscriptions"


class BoundContentSource(Protocol):
    def submit(
        self, batch_id: str, items: Sequence[Mapping[str, object]]
    ) -> Mapping[str, object]: ...

    def read_submission(self, batch_id: str) -> Mapping[str, object] | None: ...

    def read_revision(
        self, item_id: str, revision: str
    ) -> Mapping[str, object] | None: ...


class FeedLegacyHandoffAdapter:
    """Move exact legacy Feed reservoir facts into the existing Content source."""

    def __init__(self, feed_data_root: Path, content: BoundContentSource) -> None:
        self._provider_db = backend.provider_database_path(feed_data_root)
        self._content = content

    def accepts(self, fact: LegacyFact) -> bool:
        return (
            fact.kind is LegacyFactKind.WAKE_SOURCE_ITEM
            and fact.source_identity == LEGACY_SOURCE_ID
        )

    def plan(self, fact: LegacyFact) -> AdapterPlan:
        """Resolve one provider-owned revision without mounting or writing Content."""

        row = _legacy_row(fact)
        provider = self._provider_item(_text(row, "source_event_id"))
        return AdapterPlan(_target_identity(provider))

    def apply(self, fact: LegacyFact, plan: AdapterPlan) -> TargetReceipt:
        """Submit the exact planned target and return its normalized durable receipt."""

        # 1. Re-read the owner row and reject a revision change after planning.
        row = _legacy_row(fact)
        provider = self._provider_item(_text(row, "source_event_id"))
        target_identity = _target_identity(provider)
        if plan.target_identity != target_identity:
            raise RuntimeError("Feed handoff target identity drift after plan")

        # 2. A fact-stable batch makes target-before-marker replay idempotent.
        batch_id = _batch_id(fact)
        item = _content_item(row, provider)
        content_receipt = self._content.submit(batch_id, (item,))
        receipt_id = _text(content_receipt, "receipt_id")
        normalized = _receipt_payload(fact, target_identity, content_receipt)
        return TargetReceipt(
            receipt_id=receipt_id,
            receipt_digest=receipt_digest(normalized),
            target_identity=target_identity,
        )

    def verify(self, fact: LegacyFact, receipt: TargetReceipt) -> bool:
        """Verify provider lineage and checkpointed Content facts without writing."""

        try:
            plan = self.plan(fact)
        except HandoffBlocked:
            return False
        if receipt.target_identity != plan.target_identity:
            return False
        row = _legacy_row(fact)
        provider = self._provider_item(_text(row, "source_event_id"))
        item = _content_item(row, provider)
        batch_id = _batch_id(fact)
        submission = self._content.read_submission(batch_id)
        revision = self._content.read_revision(
            _text(item, "item_id"), _text(item, "revision")
        )
        if submission is None or revision is None:
            return False
        normalized = _receipt_payload(fact, plan.target_identity, submission)
        return (
            receipt.receipt_id == submission.get("receipt_id")
            and receipt.receipt_digest == receipt_digest(normalized)
            and _revision_matches(revision, item)
        )

    def _provider_item(self, event_id: str) -> dict[str, object]:
        """Read one exact Feed row through a query-only SQLite connection."""

        if not self._provider_db.is_file():
            raise HandoffBlocked("feed_provider_database_missing")
        wal = self._provider_db.with_name(self._provider_db.name + "-wal")
        if wal.is_file() and wal.stat().st_size > 0:
            raise HandoffBlocked("feed_provider_checkpoint_required")
        uri = self._provider_db.resolve().as_uri() + "?mode=ro&immutable=1"
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            _ = connection.execute("PRAGMA query_only = ON")
            result = connection.execute(
                """
                SELECT event_id, source_id, source_type, source_name, title,
                       content, url, author, published_at, first_seen_at,
                       content_hash
                FROM items WHERE event_id = ?
                """,
                (event_id,),
            ).fetchone()
        if result is None:
            raise HandoffBlocked(f"feed_provider_item_missing:{event_id}")
        return {key: result[key] for key in result.keys()}


def _legacy_row(fact: LegacyFact) -> dict[str, object]:
    if fact.kind is not LegacyFactKind.WAKE_SOURCE_ITEM:
        raise TypeError("Feed handoff received another legacy fact kind")
    if fact.source_identity != LEGACY_SOURCE_ID:
        raise TypeError("Feed handoff received another legacy source owner")
    if hashlib.sha256(fact.opaque).hexdigest() != fact.source_digest:
        raise RuntimeError("Feed legacy source digest mismatch")
    decoded = json.loads(fact.opaque)
    if not isinstance(decoded, dict):
        raise TypeError("Feed legacy reservoir row must be an object")
    row = cast(dict[str, object], decoded)
    event_id = _text(row, "source_event_id")
    if not fact.locator.endswith(f":{_text(row, 'item_id')}"):
        raise RuntimeError("Feed legacy locator does not match item_id")
    payload = _payload(row)
    if payload.get("event_id") != event_id or payload.get("kind") != "content":
        raise RuntimeError("Feed legacy payload identity mismatch")
    return row


def _content_item(
    legacy: Mapping[str, object], provider: Mapping[str, object]
) -> dict[str, object]:
    event_id = _text(provider, "event_id")
    if _text(legacy, "source_event_id") != event_id:
        raise RuntimeError("Feed provider join identity mismatch")
    source_payload = _payload(legacy)
    payload: dict[str, object] = {
        "kind": "content",
        "source_type": provider["source_type"],
        "source_id": provider["source_id"],
        "source_name": provider["source_name"],
        "title": provider["title"],
        "content": provider["content"],
        "url": provider["url"],
        "author": provider["author"],
        "published_at": provider["published_at"],
        "first_seen_at": provider["first_seen_at"],
        "preprocess_score": source_payload.get(
            "preprocess_score", legacy["preprocess_score"]
        ),
        "preprocess_features": source_payload.get("preprocess_features", {}),
    }
    return {
        "item_id": event_id,
        "revision": _text(provider, "content_hash"),
        "payload": payload,
        "not_before": str(provider["published_at"] or provider["first_seen_at"]),
        "requires_ack": True,
    }


def _target_identity(provider: Mapping[str, object]) -> str:
    return (
        f"content:{CONTENT_SOURCE_ID}:{_text(provider, 'event_id')}:"
        f"{_text(provider, 'content_hash')}"
    )


def _batch_id(fact: LegacyFact) -> str:
    encoded = f"{fact.locator}\x00{fact.source_digest}".encode("utf-8")
    return f"feed-legacy:{hashlib.sha256(encoded).hexdigest()}"


def _receipt_payload(
    fact: LegacyFact,
    target_identity: str,
    content_receipt: Mapping[str, object],
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "legacy_locator": fact.locator,
        "legacy_source_digest": fact.source_digest,
        "legacy_source_identity": fact.source_identity,
        "target_identity": target_identity,
        "content_receipt": dict(content_receipt),
    }


def _revision_matches(
    revision: Mapping[str, object], item: Mapping[str, object]
) -> bool:
    return (
        revision.get("ref")
        == {
            "source_id": CONTENT_SOURCE_ID,
            "item_id": item["item_id"],
            "revision": item["revision"],
        }
        and revision.get("payload") == item["payload"]
        and revision.get("not_before") == item["not_before"]
        and revision.get("requires_ack") is True
    )


def _payload(row: Mapping[str, object]) -> dict[str, object]:
    raw = _text(row, "payload_json")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise TypeError("Feed legacy payload_json must contain an object")
    return cast(dict[str, object], value)


def _text(row: Mapping[str, object], field: str) -> str:
    value = row[field]
    if not isinstance(value, str) or not value:
        raise TypeError(f"Feed {field} must be a non-empty string")
    return value


__all__ = ["FeedLegacyHandoffAdapter"]
