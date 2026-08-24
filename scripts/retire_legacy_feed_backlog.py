#!/usr/bin/env python3
"""Retire an exact legacy Feed backlog without submitting it to Content."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from agent.migrations.proactive_island.cli import report_payload, retire
from agent.migrations.proactive_island.inventory import (
    inventory_digest,
    inventory_workspace,
)
from legacy_handoff import FeedLegacyHandoffAdapter
from feed_runtime import backend


def _backlog_digest(items: tuple[dict[str, str], ...]) -> str:
    payload = json.dumps(items, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--workspace", required=True, type=Path)
    _ = parser.add_argument("--feed-data-root", required=True, type=Path)
    mode = parser.add_mutually_exclusive_group()
    _ = mode.add_argument("--apply", action="store_true")
    _ = mode.add_argument("--plan", action="store_true")
    _ = parser.add_argument("--backup-root", type=Path)
    _ = parser.add_argument("--expected-inventory-sha256")
    _ = parser.add_argument("--expected-provider-backlog-sha256")
    _ = parser.add_argument("--expected-provider-backlog-count", type=int)
    args = parser.parse_args()

    inventory = inventory_workspace(args.workspace)
    inventory_sha256 = inventory_digest(inventory)
    backlog = backend.plan_content_backlog(data_root=args.feed_data_root)
    backlog_sha256 = _backlog_digest(backlog)
    if not args.apply:
        print(
            json.dumps(
                {
                    "status": "plan",
                    "inventory_sha256": inventory_sha256,
                    "provider_backlog_sha256": backlog_sha256,
                    "provider_backlog_count": len(backlog),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if any(
        value is None
        for value in (
            args.backup_root,
            args.expected_inventory_sha256,
            args.expected_provider_backlog_sha256,
            args.expected_provider_backlog_count,
        )
    ):
        parser.error(
            "--apply requires --backup-root and all expected inventory/backlog fields"
        )
    expected_inventory = str(args.expected_inventory_sha256)
    expected_backlog = str(args.expected_provider_backlog_sha256)
    expected_count = int(args.expected_provider_backlog_count)
    batch_id = f"proactive-island-cutover:{expected_inventory}"
    completed = backend.read_content_backlog_supersession(
        batch_id,
        data_root=args.feed_data_root,
    )
    if completed is not None:
        completed_receipt, completed_items = completed
        if (
            completed_receipt["items_digest"] != expected_backlog
            or completed_receipt["item_count"] != expected_count
            or not backend.verify_content_backlog_supersession(
                batch_id, completed_items, data_root=args.feed_data_root
            )
        ):
            raise RuntimeError(
                "Feed completed cutover receipt conflicts with expected plan"
            )
        backlog = completed_items
        backlog_sha256 = _backlog_digest(backlog)

    if (
        inventory_sha256 != expected_inventory
        or backlog_sha256 != expected_backlog
        or len(backlog) != expected_count
    ):
        print(
            json.dumps(
                {
                    "status": "block",
                    "reason": "cutover_plan_drift",
                    "inventory_sha256": inventory_sha256,
                    "provider_backlog_sha256": backlog_sha256,
                    "provider_backlog_count": len(backlog),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2

    provider_receipt = backend.supersede_content_backlog(
        batch_id,
        backlog,
        data_root=args.feed_data_root,
    )
    adapter = FeedLegacyHandoffAdapter.for_cutover(args.feed_data_root)
    report = retire(
        args.workspace,
        args.backup_root,
        inventory_sha256,
        (adapter,),
    )
    payload = report_payload(report)
    payload["inventory_sha256"] = inventory_sha256
    payload["provider_backlog"] = provider_receipt
    payload["provider_backlog_remaining"] = len(
        backend.plan_content_backlog(data_root=args.feed_data_root)
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if report.status.value != "block" else 2


if __name__ == "__main__":
    raise SystemExit(main())
