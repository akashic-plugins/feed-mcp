"""Feed 自己拥有的旧 Wake source 事实清单。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class LegacyFactKind(StrEnum):
    WAKE_SOURCE_ITEM = "wake_source_item"
    WAKE_ACK = "wake_ack"


@dataclass(frozen=True, slots=True)
class LegacyFact:
    """在 lineage 只保存摘要的前提下携带一条精确 Feed 事实。"""

    kind: LegacyFactKind
    locator: str
    source_digest: str
    source_identity: str
    opaque: bytes


@dataclass(frozen=True, slots=True)
class InventoryBlock:
    locator: str
    reason: str
    source_digest: str = ""


@dataclass(frozen=True, slots=True)
class Inventory:
    facts: tuple[LegacyFact, ...]
    blocks: tuple[InventoryBlock, ...]


_SOURCE_ID = "feed@github:subscriptions"


def inventory_workspace(workspace: Path) -> Inventory:
    """只读取 Feed 声明拥有的 Wake rows，不窥探其他插件的 workspace 状态。"""

    path = workspace.expanduser().resolve(strict=False) / "wake_proactive.db"
    if not path.is_file():
        return Inventory((), ())
    wal_path = path.with_name(path.name + "-wal")
    if wal_path.is_file() and wal_path.stat().st_size > 0:
        raise RuntimeError(f"Feed Wake SQLite has uncheckpointed WAL: {path}")
    uri = path.resolve().as_uri() + "?mode=ro&immutable=1"
    facts: list[LegacyFact] = []
    blocks: list[InventoryBlock] = []
    with sqlite3.connect(uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise RuntimeError(f"Feed Wake SQLite quick_check failed: {path}")
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        if "reservoir_events" not in tables:
            blocks.append(InventoryBlock("wake:reservoir_events", "schema_missing"))
            return Inventory((), tuple(blocks))
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(reservoir_events)")
        }
        required = {
            "item_id",
            "kind",
            "source_id",
            "ack_source_id",
            "source_event_id",
            "status",
        }
        if not required.issubset(columns):
            blocks.append(
                InventoryBlock("wake:reservoir_events", "schema_columns_missing")
            )
            return Inventory((), tuple(blocks))
        rows = connection.execute(
            "SELECT * FROM reservoir_events ORDER BY item_id"
        ).fetchall()
        pending = (
            connection.execute(
                "SELECT * FROM pending_acknowledgements "
                "ORDER BY source_id, source_event_id, item_id"
            ).fetchall()
            if "pending_acknowledgements" in tables
            else ()
        )

    owners: dict[tuple[str, str], tuple[str, str]] = {}
    by_item = {str(row["item_id"]): row for row in rows}
    for row in rows:
        source_id = _identity(row["ack_source_id"]) or _identity(row["source_id"])
        event_id = _identity(row["source_event_id"])
        if source_id != _SOURCE_ID or event_id is None:
            continue
        payload = _row_bytes(row)
        item_id = str(row["item_id"])
        digest = _digest(payload)
        previous = owners.get((source_id, event_id))
        if previous is not None and previous != (item_id, digest):
            blocks.append(
                InventoryBlock(
                    f"wake:reservoir_events:{item_id}",
                    "source_identity_conflict",
                    digest,
                )
            )
            continue
        owners[(source_id, event_id)] = (item_id, digest)
        status = str(row["status"])
        kind = str(row["kind"])
        if status == "unread":
            if kind != "content":
                blocks.append(
                    InventoryBlock(
                        f"wake:reservoir_events:{item_id}",
                        f"unknown_wake_kind:{kind}",
                        digest,
                    )
                )
                continue
            facts.append(
                LegacyFact(
                    LegacyFactKind.WAKE_SOURCE_ITEM,
                    f"wake:reservoir_events:{item_id}",
                    digest,
                    source_id,
                    payload,
                )
            )
        elif status not in {"consumed", "pending_expiry", "expired", "quarantined"}:
            blocks.append(
                InventoryBlock(
                    f"wake:reservoir_events:{item_id}",
                    f"unknown_wake_status:{status}",
                    digest,
                )
            )

    for row in pending:
        source_id = _identity(row["source_id"])
        event_id = _identity(row["source_event_id"])
        item_id = str(row["item_id"])
        if source_id != _SOURCE_ID or event_id is None:
            continue
        payload = _row_bytes(row)
        digest = _digest(payload)
        reservoir = by_item.get(item_id)
        action = str(row["action"])
        locator = f"wake:pending_acknowledgements:{source_id}:{event_id}:{item_id}"
        if reservoir is None:
            blocks.append(InventoryBlock(locator, "orphan_pending_ack", digest))
            continue
        reservoir_source = _identity(reservoir["ack_source_id"]) or _identity(
            reservoir["source_id"]
        )
        reservoir_event = _identity(reservoir["source_event_id"])
        if action not in {"consume", "expire"}:
            blocks.append(InventoryBlock(locator, f"unknown_ack_action:{action}", digest))
            continue
        if reservoir_source != source_id or reservoir_event != event_id:
            blocks.append(
                InventoryBlock(locator, "ack_source_identity_conflict", digest)
            )
            continue
        facts.append(
            LegacyFact(
                LegacyFactKind.WAKE_ACK,
                locator,
                digest,
                source_id,
                payload,
            )
        )

    facts.sort(key=lambda item: (item.kind.value, item.locator))
    blocks.sort(key=lambda item: (item.locator, item.reason))
    return Inventory(tuple(facts), tuple(blocks))


def inventory_digest(inventory: Inventory) -> str:
    """只摘要事实身份，不把旧 payload 放入 operator 计划。"""

    payload = {
        "facts": [
            {
                "kind": fact.kind.value,
                "locator": fact.locator,
                "source_digest": fact.source_digest,
                "source_identity": fact.source_identity,
            }
            for fact in inventory.facts
        ],
        "blocks": [
            {
                "locator": block.locator,
                "reason": block.reason,
                "source_digest": block.source_digest,
            }
            for block in inventory.blocks
        ],
    }
    return _digest(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())


def _row_bytes(row: sqlite3.Row) -> bytes:
    payload = {str(key): _json_value(row[key]) for key in row.keys()}
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _json_value(value: object) -> object:
    if isinstance(value, bytes):
        return {"sqlite_blob_hex": value.hex()}
    return value


def _identity(value: object) -> str | None:
    return value if isinstance(value, str) and value and value.strip() == value else None


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


__all__ = [
    "Inventory",
    "InventoryBlock",
    "LegacyFact",
    "LegacyFactKind",
    "inventory_digest",
    "inventory_workspace",
]
