from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from bootstrap.workspace_lock import WorkspaceInstanceLock
from scripts import migrate_v2_data as migration


def _legacy_data(workspace: Path, relative: str = "mcp/feed-mcp") -> Path:
    source = workspace / relative
    source.mkdir(parents=True)
    (source / "source_scores.json").write_text('{"source": "kept"}\n', encoding="utf-8")
    with sqlite3.connect(source / "feed_cache.db") as database:
        database.execute("CREATE TABLE cache (value TEXT NOT NULL)")
        database.execute("INSERT INTO cache VALUES ('kept')")
        database.commit()
    with sqlite3.connect(source / "feed_mcp.sqlite3") as database:
        database.execute("CREATE TABLE receipts (value TEXT NOT NULL)")
        database.execute("INSERT INTO receipts VALUES ('kept')")
        database.commit()
    return source


def test_primary_precedes_latest_backup_and_source_is_retained(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    primary = _legacy_data(workspace)
    backup = _legacy_data(
        workspace,
        "backups/feed-plugin-migration-20260817-120000/feed-mcp",
    )
    (backup / "source_scores.json").write_text("backup\n", encoding="utf-8")

    receipt_path = migration.migrate_v2_data(workspace=workspace, marketplace="github")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    target = workspace / "plugin-data" / "feed-github"

    assert receipt["source"] == "mcp/feed-mcp"
    assert receipt["recovery"] == {
        "kind": "retained_source",
        "path": "mcp/feed-mcp",
    }
    assert [item["status"] for item in receipt["files"]] == [
        "copied",
        "copied",
        "copied",
    ]
    assert receipt["files"][0]["integrity"] == "ok"
    assert (primary / "feed_mcp.sqlite3").is_file()
    assert (target / "source_scores.json").read_text(encoding="utf-8") == '{"source": "kept"}\n'
    with sqlite3.connect(target / "feed_mcp.sqlite3") as database:
        assert database.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_latest_backup_is_selected_when_primary_has_no_data(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    older = _legacy_data(
        workspace,
        "backups/feed-plugin-migration-20260816-120000/feed-mcp",
    )
    latest = _legacy_data(
        workspace,
        "backups/feed-plugin-migration-20260817-120000/feed-mcp",
    )
    (older / "source_scores.json").write_text("older\n", encoding="utf-8")
    (latest / "source_scores.json").write_text("latest\n", encoding="utf-8")

    receipt_path = migration.migrate_v2_data(workspace=workspace, marketplace="github")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["source"] == (
        "backups/feed-plugin-migration-20260817-120000/feed-mcp"
    )
    assert (
        receipt_path.parent / "source_scores.json"
    ).read_text(encoding="utf-8") == "latest\n"


def test_conflict_fails_without_changing_source_or_target(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    source = _legacy_data(workspace)
    target = workspace / "plugin-data" / "feed-github"
    target.mkdir(parents=True)
    (target / "source_scores.json").write_text("current\n", encoding="utf-8")

    with pytest.raises(FileExistsError, match="内容不同"):
        migration.migrate_v2_data(workspace=workspace, marketplace="github")

    assert (source / "source_scores.json").read_text(encoding="utf-8") == '{"source": "kept"}\n'
    assert not (target / migration._RECEIPT).exists()
    assert list((workspace / "plugin-data").glob(".feed-v2-migrate-*")) == []


def test_source_missing_target_only_is_recorded_and_verified(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    source = _legacy_data(workspace)
    (source / "source_scores.json").unlink()
    target = workspace / "plugin-data" / "feed-github"
    target.mkdir(parents=True)
    (target / "source_scores.json").write_text("target-only\n", encoding="utf-8")

    receipt_path = migration.migrate_v2_data(workspace=workspace, marketplace="github")
    entries = {
        item["name"]: item
        for item in json.loads(receipt_path.read_text(encoding="utf-8"))["files"]
    }
    assert entries["source_scores.json"]["status"] == "target_only"
    assert entries["source_scores.json"]["size"] == len("target-only\n")
    assert entries["feed_mcp.sqlite3"]["status"] == "copied"


def test_in_process_publish_failure_rolls_back_new_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    _legacy_data(workspace)
    original_replace = os.replace
    calls = 0

    def fail_second_publish(source: Path, destination: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected publish failure")
        original_replace(source, destination)

    monkeypatch.setattr(migration.os, "replace", fail_second_publish)
    with pytest.raises(OSError, match="injected publish failure"):
        migration.migrate_v2_data(workspace=workspace, marketplace="github")

    assert not (workspace / "plugin-data" / "feed-github").exists()
    assert list((workspace / "plugin-data").glob(".feed-v2-migrate-*")) == []


def test_post_replace_cancellation_rolls_back_published_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    source = _legacy_data(workspace)
    original_replace = os.replace
    calls = 0

    def cancel_after_first_replace(source_path: Path, destination: Path) -> None:
        nonlocal calls
        original_replace(source_path, destination)
        calls += 1
        if calls == 1:
            raise KeyboardInterrupt("injected post-replace cancellation")

    monkeypatch.setattr(migration.os, "replace", cancel_after_first_replace)
    with pytest.raises(KeyboardInterrupt, match="post-replace"):
        migration.migrate_v2_data(workspace=workspace, marketplace="github")

    target = workspace / "plugin-data" / "feed-github"
    assert not target.exists()
    assert (source / "feed_mcp.sqlite3").is_file()
    assert list((workspace / "plugin-data").glob(".feed-v2-migrate-*")) == []


def test_crash_partial_publish_is_reconciled_on_rerun(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    source = _legacy_data(workspace)
    plugin_data = workspace / "plugin-data"
    target = plugin_data / "feed-github"
    stale = plugin_data / ".feed-v2-migrate-crashed"
    target.mkdir(parents=True)
    stale.mkdir(parents=True)
    (target / "source_scores.json").write_bytes(
        (source / "source_scores.json").read_bytes()
    )
    (stale / "orphan").write_text("partial", encoding="utf-8")

    receipt_path = migration.migrate_v2_data(workspace=workspace, marketplace="github")
    statuses = {
        item["name"]: item["status"]
        for item in json.loads(receipt_path.read_text(encoding="utf-8"))["files"]
    }
    assert statuses["source_scores.json"] == "verified"
    assert statuses["feed_mcp.sqlite3"] == "copied"
    assert not stale.exists()


def test_process_crash_after_replace_is_reconciled_on_restart(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _legacy_data(workspace)
    script = f"""
from pathlib import Path
import os
from scripts import migrate_v2_data as migration

original_replace = migration.os.replace

def crash_after_replace(source, destination):
    original_replace(source, destination)
    os._exit(137)

migration.os.replace = crash_after_replace
migration.migrate_v2_data(workspace=Path({str(workspace)!r}), marketplace="github")
"""

    crashed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).parents[1],
        check=False,
    )
    assert crashed.returncode == 137

    receipt = migration.migrate_v2_data(
        workspace=workspace,
        marketplace="github",
    )
    statuses = {
        item["name"]: item["status"]
        for item in json.loads(receipt.read_text(encoding="utf-8"))["files"]
    }
    assert sorted(statuses.values()) == ["copied", "copied", "verified"]
    assert list((workspace / "plugin-data").glob(".feed-v2-migrate-*")) == []


def test_symlink_and_receipt_drift_are_rejected(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    _legacy_data(outside)
    workspace.mkdir()
    (workspace / "mcp").symlink_to(outside / "mcp", target_is_directory=True)
    with pytest.raises(FileNotFoundError, match="不安全"):
        migration.migrate_v2_data(workspace=workspace, marketplace="github")

    workspace = tmp_path / "workspace-2"
    _legacy_data(workspace)
    receipt_path = migration.migrate_v2_data(workspace=workspace, marketplace="github")
    target_file = receipt_path.parent / "source_scores.json"
    target_file.write_text("drifted\n", encoding="utf-8")
    with pytest.raises(ValueError, match="内容漂移"):
        migration.migrate_v2_data(workspace=workspace, marketplace="github")


def test_migration_requires_idle_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _legacy_data(workspace)
    lock = WorkspaceInstanceLock(workspace)
    lock.acquire()
    try:
        with pytest.raises(RuntimeError, match="其他 runtime 占用"):
            migration.migrate_v2_data(workspace=workspace, marketplace="github")
    finally:
        lock.release()
