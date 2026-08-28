from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from agent.migrations.proactive_island import (
    HandoffBlocked,
    HandoffStatus,
    Inventory,
    LegacyFact,
    LegacyFactKind,
    apply_handoff,
)
from plugins.eventmail.store import EventMailIdentityConflict, EventMailStore

from feed_runtime import backend
from legacy_handoff import FeedLegacyHandoffAdapter, LEGACY_SOURCE_ID


ROOT = Path(__file__).resolve().parent
TARGET_SOURCE = "feed-subscriptions"


class _BoundContent:
    def __init__(self, store: EventMailStore, source_id: str = TARGET_SOURCE) -> None:
        self.store = store
        self.source_id = source_id

    def submit(
        self, batch_id: str, items: Sequence[Mapping[str, object]]
    ) -> Mapping[str, object]:
        return self.store.submit(self.source_id, batch_id, items)

    def read_submission(self, batch_id: str) -> Mapping[str, object] | None:
        return self.store.read_submission(self.source_id, batch_id)

    def read_revision(
        self, item_id: str, revision: str
    ) -> Mapping[str, object] | None:
        return self.store.read_revision(self.source_id, item_id, revision)

    def unsettled(self, limit: int = 100) -> tuple[Mapping[str, object], ...]:
        return self.store.unsettled(self.source_id, limit)

    def ack(self, settlement_ref: str) -> Mapping[str, object]:
        return self.store.ack(self.source_id, settlement_ref)


def _legacy_rows() -> list[dict[str, object]]:
    value = json.loads(
        (ROOT / "fixtures" / "legacy_feed_reservoir_rows.json").read_text()
    )
    assert isinstance(value, list) and len(value) == 15
    return cast(list[dict[str, object]], value)


def _fact(row: Mapping[str, object]) -> LegacyFact:
    opaque = json.dumps(
        row, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    item_id = cast(str, row["item_id"])
    return LegacyFact(
        kind=LegacyFactKind.WAKE_SOURCE_ITEM,
        locator=f"wake:reservoir_events:{item_id}",
        source_digest=hashlib.sha256(opaque).hexdigest(),
        source_identity="feed@github:subscriptions",
        opaque=opaque,
    )


def _ack_fact(row: Mapping[str, object], action: str = "consume") -> LegacyFact:
    event_id = cast(str, row["source_event_id"])
    item_id = cast(str, row["item_id"])
    acknowledgement = {
        "source_id": LEGACY_SOURCE_ID,
        "source_event_id": event_id,
        "item_id": item_id,
        "action": action,
        "queued_at": "2026-08-23T10:00:00+00:00",
    }
    opaque = json.dumps(
        acknowledgement, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return LegacyFact(
        kind=LegacyFactKind.WAKE_ACK,
        locator=(
            "wake:pending_acknowledgements:"
            f"{LEGACY_SOURCE_ID}:{event_id}:{item_id}"
        ),
        source_digest=hashlib.sha256(opaque).hexdigest(),
        source_identity=LEGACY_SOURCE_ID,
        opaque=opaque,
    )


def _seed_provider(data_root: Path, rows: Sequence[Mapping[str, object]]) -> None:
    config = backend.load_config(data_root)
    connection = backend._connect(config)
    try:
        for index, row in enumerate(rows, start=1):
            event_id = cast(str, row["source_event_id"])
            connection.execute(
                """
                INSERT INTO items(
                    event_id, source_id, source_name, source_type, title,
                    content, url, author, published_at, first_seen_at,
                    last_seen_at, emitted_at, content_hash
                ) VALUES(?, ?, ?, 'rss', ?, ?, ?, ?, ?, ?, ?, NULL, ?)
                """,
                (
                    event_id,
                    f"source-{index:02d}",
                    f"Source {index:02d}",
                    f"Title {index:02d}",
                    f"Body {index:02d}",
                    f"https://example.com/{index:02d}",
                    None if index % 3 == 0 else f"Author {index:02d}",
                    row["published_at"],
                    row["first_seen_at"],
                    row["first_seen_at"],
                    f"revision-{index:02d}",
                ),
            )
        connection.commit()
    finally:
        connection.close()


def _fixture(
    tmp_path: Path,
) -> tuple[Path, EventMailStore, _BoundContent, FeedLegacyHandoffAdapter]:
    data_root = tmp_path / "feed-data"
    _seed_provider(data_root, _legacy_rows())
    store = EventMailStore(tmp_path / "content.sqlite3")
    store.initialize()
    bound = _BoundContent(store)
    return data_root, store, bound, FeedLegacyHandoffAdapter(data_root, bound)


def _tree_state(root: Path) -> tuple[tuple[str, int, int, str], ...]:
    return tuple(
        (
            str(path.relative_to(root)),
            path.stat().st_size,
            path.stat().st_mtime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in sorted(root.rglob("*"))
        if path.is_file()
    )


def test_real_shape_fifteen_rows_plan_apply_and_verify(tmp_path: Path) -> None:
    data_root, store, _bound, adapter = _fixture(tmp_path)
    facts = tuple(_fact(row) for row in _legacy_rows())
    before = _tree_state(tmp_path)

    plans = tuple(adapter.plan(fact) for fact in facts)

    assert _tree_state(tmp_path) == before
    assert len({plan.target_identity for plan in plans}) == 15
    receipts = tuple(
        adapter.apply(fact, plan) for fact, plan in zip(facts, plans, strict=True)
    )
    after_apply = _tree_state(tmp_path)
    assert all(
        adapter.verify(fact, receipt)
        for fact, receipt in zip(facts, receipts, strict=True)
    )
    assert _tree_state(tmp_path) == after_apply
    assert len({receipt.receipt_id for receipt in receipts}) == 15
    assert store.state_counts() == {"pending": 15}
    with closing(sqlite3.connect(store.path)) as connection:
        assert connection.execute(
            "SELECT DISTINCT source_id FROM items"
        ).fetchall() == [(TARGET_SOURCE,)]
    assert not (data_root / "legacy_handoff.log").exists()


def test_adapter_accepts_only_legacy_feed_content_fact(tmp_path: Path) -> None:
    _data_root, _store, _bound, adapter = _fixture(tmp_path)
    fact = _fact(_legacy_rows()[0])

    assert adapter.accepts(fact) is True
    assert fact.source_identity == LEGACY_SOURCE_ID
    assert adapter.accepts(
        LegacyFact(
            fact.kind,
            fact.locator,
            fact.source_digest,
            "calendar@github:upcoming",
            fact.opaque,
        )
    ) is False


def test_missing_provider_item_blocks_without_writes(tmp_path: Path) -> None:
    data_root, store, _bound, adapter = _fixture(tmp_path)
    config = backend.load_config(data_root)
    connection = backend._connect(config)
    connection.execute("DELETE FROM items WHERE event_id='event-15'")
    connection.commit()
    connection.close()
    before = _tree_state(tmp_path)

    with pytest.raises(HandoffBlocked, match="feed_provider_item_missing:event-15"):
        adapter.plan(_fact(_legacy_rows()[-1]))

    assert _tree_state(tmp_path) == before
    assert store.state_counts() == {}


def test_target_before_marker_replay_returns_same_receipt(tmp_path: Path) -> None:
    _data_root, store, _bound, adapter = _fixture(tmp_path)
    fact = _fact(_legacy_rows()[0])
    plan = adapter.plan(fact)

    first = adapter.apply(fact, plan)
    repeated = adapter.apply(fact, plan)

    assert repeated == first
    assert adapter.verify(fact, repeated) is True
    assert store.state_counts() == {"pending": 1}


def test_cutover_supersedes_backlog_without_submitting_content(tmp_path: Path) -> None:
    data_root, store, _bound, _adapter = _fixture(tmp_path)
    adapter = FeedLegacyHandoffAdapter.for_cutover(data_root)
    facts = tuple(_fact(row) for row in _legacy_rows())

    receipts = tuple(adapter.apply(fact, adapter.plan(fact)) for fact in facts)

    assert all(
        adapter.verify(fact, receipt)
        for fact, receipt in zip(facts, receipts, strict=True)
    )
    assert store.state_counts() == {}
    assert backend.prepare_content_items(data_root=data_root) == ()
    config = backend.load_config(data_root)
    with closing(backend._connect(config)) as connection:
        assert connection.execute("SELECT count(*) FROM acked_items").fetchone()[0] == 15
        assert connection.execute(
            "SELECT count(*) FROM legacy_ack_handoff_receipts "
            "WHERE action='cutover_superseded'"
        ).fetchone()[0] == 15


def test_cutover_replay_uses_same_receipt_and_requires_provider_ack(
    tmp_path: Path,
) -> None:
    data_root, store, _bound, _adapter = _fixture(tmp_path)
    adapter = FeedLegacyHandoffAdapter.for_cutover(data_root)
    fact = _fact(_legacy_rows()[0])
    plan = adapter.plan(fact)

    first = adapter.apply(fact, plan)
    repeated = adapter.apply(fact, plan)

    assert repeated == first
    assert adapter.verify(fact, repeated) is True
    assert store.state_counts() == {}
    config = backend.load_config(data_root)
    with closing(backend._connect(config)) as connection:
        connection.execute("DELETE FROM acked_items")
        connection.commit()
    assert adapter.verify(fact, repeated) is False


def test_provider_backlog_plan_is_read_only_and_batch_supersession_is_exact(
    tmp_path: Path,
) -> None:
    data_root, store, _bound, _adapter = _fixture(tmp_path)
    config = backend.load_config(data_root)
    with closing(backend._connect(config)) as connection:
        now = datetime.now(UTC).isoformat()
        connection.execute(
            "UPDATE items SET published_at = ?, first_seen_at = ?", (now, now)
        )
        connection.commit()
    before = _tree_state(tmp_path)

    planned = backend.plan_content_backlog(data_root=data_root)

    assert len(planned) == 15
    assert _tree_state(tmp_path) == before
    receipt = backend.supersede_content_backlog(
        "cutover:fixture",
        planned,
        data_root=data_root,
    )
    repeated = backend.supersede_content_backlog(
        "cutover:fixture",
        planned,
        data_root=data_root,
    )
    assert repeated == receipt
    assert receipt["item_count"] == 15
    assert backend.verify_content_backlog_supersession(
        "cutover:fixture", planned, data_root=data_root
    )
    assert backend.plan_content_backlog(data_root=data_root) == ()
    assert store.state_counts() == {}


def test_provider_backlog_supersession_rejects_revision_drift(tmp_path: Path) -> None:
    data_root, _store, _bound, _adapter = _fixture(tmp_path)
    config = backend.load_config(data_root)
    with closing(backend._connect(config)) as connection:
        now = datetime.now(UTC).isoformat()
        connection.execute(
            "UPDATE items SET published_at = ?, first_seen_at = ?", (now, now)
        )
        connection.commit()
    planned = backend.plan_content_backlog(data_root=data_root)
    with closing(backend._connect(config)) as connection:
        connection.execute(
            "UPDATE items SET content_hash='changed' WHERE event_id='event-01'"
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="provider revision changed"):
        backend.supersede_content_backlog(
            "cutover:fixture", planned, data_root=data_root
        )


def test_core_replays_after_target_receipt_before_lineage_marker(
    tmp_path: Path,
) -> None:
    _data_root, store, _bound, adapter = _fixture(tmp_path)
    fact = _fact(_legacy_rows()[0])
    inventory = Inventory((fact,), ())
    workspace = tmp_path / "workspace"

    def crash_after_target(_fact: LegacyFact, _receipt: object) -> None:
        raise RuntimeError("crash before central marker")

    with pytest.raises(RuntimeError, match="crash before central marker"):
        apply_handoff(
            workspace,
            inventory,
            (adapter,),
            after_target=crash_after_target,
        )
    assert store.state_counts() == {"pending": 1}

    recovered = apply_handoff(workspace, inventory, (adapter,))

    assert recovered.status is HandoffStatus.APPLIED
    assert recovered.items[0].state == "applied"
    assert store.state_counts() == {"pending": 1}


def test_revision_change_after_target_is_a_batch_conflict(tmp_path: Path) -> None:
    data_root, store, _bound, adapter = _fixture(tmp_path)
    fact = _fact(_legacy_rows()[0])
    original = adapter.plan(fact)
    _ = adapter.apply(fact, original)
    config = backend.load_config(data_root)
    connection = backend._connect(config)
    connection.execute(
        "UPDATE items SET content_hash='revision-changed' WHERE event_id='event-01'"
    )
    connection.commit()
    connection.close()
    changed = adapter.plan(fact)

    with pytest.raises(EventMailIdentityConflict, match="batch identity conflict"):
        adapter.apply(fact, changed)

    assert store.state_counts() == {"pending": 1}


def test_plan_revision_change_before_apply_fails_before_submit(tmp_path: Path) -> None:
    data_root, store, _bound, adapter = _fixture(tmp_path)
    fact = _fact(_legacy_rows()[0])
    plan = adapter.plan(fact)
    config = backend.load_config(data_root)
    connection = backend._connect(config)
    connection.execute(
        "UPDATE items SET content_hash='revision-new' WHERE event_id='event-01'"
    )
    connection.commit()
    connection.close()

    with pytest.raises(RuntimeError, match="target identity drift"):
        adapter.apply(fact, plan)

    assert store.state_counts() == {}


def test_already_acked_target_replays_and_source_ack_is_bound_once(
    tmp_path: Path,
) -> None:
    data_root, store, bound, adapter = _fixture(tmp_path)
    now = datetime(2026, 8, 23, 10, tzinfo=UTC)
    fact = _fact(_legacy_rows()[0])
    plan = adapter.plan(fact)
    receipt = adapter.apply(fact, plan)
    snapshot = store.snapshot(now)
    candidate = cast(tuple[dict[str, object], ...], snapshot["items"])[0]
    selected = store.select(
        cast(Mapping[str, object], candidate["ref"]),
        cast(int, snapshot["snapshot_seq"]),
        {"session_id": "wake:fixture", "turn_id": "turn:feed-legacy"},
        now,
    )
    token = cast(str, selected["selection_token"])
    _ = store.transition(token, "ready_for_delivery")
    _ = store.transition(token, "delivered", settlement_ref="delivery:feed-legacy")

    assert _BoundContent(store, "another-source").unsettled() == ()
    assert len(bound.unsettled()) == 1
    assert backend.settle_content_item(
        "event-01", "revision-01", data_root=data_root
    )["disposition"] == "acknowledged"
    assert bound.ack("delivery:feed-legacy") == {
        "settled": True,
        "duplicate": False,
    }
    assert bound.ack("delivery:feed-legacy") == {
        "settled": True,
        "duplicate": True,
    }
    assert adapter.apply(fact, plan) == receipt
    assert adapter.verify(fact, receipt) is True
    assert store.state_counts() == {"settled": 1}


@pytest.mark.parametrize("action", ["consume", "expire"])
def test_core_hands_off_pending_ack_once_after_target_replay(
    tmp_path: Path,
    action: str,
) -> None:
    data_root, store, _bound, adapter = _fixture(tmp_path)
    fact = _ack_fact(_legacy_rows()[0], action)
    inventory = Inventory((fact,), ())
    workspace = tmp_path / "workspace"

    def crash_after_target(_fact: LegacyFact, _receipt: object) -> None:
        raise RuntimeError("crash before central ACK marker")

    with pytest.raises(RuntimeError, match="crash before central ACK marker"):
        apply_handoff(
            workspace,
            inventory,
            (adapter,),
            after_target=crash_after_target,
        )

    config = backend.load_config(data_root)
    with closing(backend._connect(config)) as connection:
        assert connection.execute("SELECT count(*) FROM acked_items").fetchone()[0] == 1
        assert connection.execute(
            "SELECT count(*) FROM legacy_ack_handoff_receipts"
        ).fetchone()[0] == 1
        connection.execute("DELETE FROM acked_items")
        connection.commit()

    recovered = apply_handoff(workspace, inventory, (adapter,))

    assert recovered.status is HandoffStatus.APPLIED
    assert recovered.items[0].state == "applied"
    assert store.state_counts() == {}
    with closing(backend._connect(config)) as connection:
        assert connection.execute("SELECT count(*) FROM acked_items").fetchone()[0] == 0
        assert [
            tuple(row)
            for row in connection.execute(
                "SELECT event_id, revision, action FROM legacy_ack_handoff_receipts"
            ).fetchall()
        ] == [("event-01", "revision-01", action)]


def test_provider_plan_requires_checkpoint_and_creates_no_files(
    tmp_path: Path,
) -> None:
    data_root, store, _bound, adapter = _fixture(tmp_path)
    wal = data_root / "feed_mcp.sqlite3-wal"
    wal.write_bytes(b"not-checkpointed")
    before = _tree_state(tmp_path)

    with pytest.raises(HandoffBlocked, match="feed_provider_checkpoint_required"):
        adapter.plan(_fact(_legacy_rows()[0]))

    assert _tree_state(tmp_path) == before
    assert store.state_counts() == {}


def test_pending_ack_uses_original_root_for_nested_provider_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = backend._config_values()
    config["db_path"] = "state/feed.sqlite3"
    monkeypatch.setattr(backend, "_config_values", lambda: config)
    data_root, _store, _bound, adapter = _fixture(tmp_path)
    fact = _ack_fact(_legacy_rows()[0])

    receipt = adapter.apply(fact, adapter.plan(fact))

    assert adapter.verify(fact, receipt) is True
    assert (data_root / "state" / "feed.sqlite3").is_file()
    assert not (data_root / "state" / "state" / "feed.sqlite3").exists()
