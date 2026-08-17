#!/usr/bin/env python3
"""把 Feed v2 workspace 数据非破坏迁移到 v3 plugin-data。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any

from agent.plugins.manifest import (
    ensure_workspace_plugin_data_dir,
    validate_workspace_plugin_data_path,
)
from bootstrap.workspace_lock import WorkspaceInstanceLock


_DATA_FILES = (
    "feed_mcp.sqlite3",
    "source_scores.json",
    "feed_cache.db",
)
_RECEIPT = ".feed-v2-migration.json"
_BACKUP_GLOB = "feed-plugin-migration-*/feed-mcp"
_TARGET_STATUSES = {"target_only", "verified", "copied"}


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _sqlite_integrity(path: Path) -> str:
    """读取 SQLite integrity receipt，不修改数据库。"""

    uri = f"{path.resolve().as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as database:
        result = database.execute("PRAGMA integrity_check").fetchone()
    if result != ("ok",):
        raise sqlite3.DatabaseError(f"Feed SQLite 完整性检查失败: {path} ({result})")
    return "ok"


def _is_sqlite_file(path: Path) -> bool:
    """按文件名和 SQLite magic 判断是否必须执行数据库迁移。"""

    if path.name == "feed_mcp.sqlite3":
        return True
    with path.open("rb") as stream:
        return stream.read(16) == b"SQLite format 3\x00"


def _copy_sqlite(source: Path, destination: Path) -> str:
    """用 SQLite 在线备份生成一致副本，并校验源和目标。"""

    source_integrity = _sqlite_integrity(source)
    uri = f"{source.resolve().as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as source_db:
        with closing(sqlite3.connect(destination)) as destination_db:
            source_db.backup(destination_db, pages=256, sleep=0.1)
            destination_db.commit()
    target_integrity = _sqlite_integrity(destination)
    if source_integrity != target_integrity:
        raise sqlite3.DatabaseError(
            f"Feed SQLite integrity receipt 不一致: {source} -> {destination}"
        )
    return target_integrity


def _source_candidates(workspace: Path) -> tuple[tuple[Path, str], ...]:
    """按 primary、最新 backup 顺序返回安全的 v2 数据目录候选。"""

    root = workspace.resolve()
    primary_root = workspace / "mcp"
    backups_root = workspace / "backups"
    candidates: list[tuple[Path, str]] = []

    # 1. primary 或 backup 根穿过符号链接都属于不可信迁移输入。
    if primary_root.is_symlink():
        raise FileNotFoundError(f"Feed v2 数据目录不安全: {primary_root}")
    primary = primary_root / "feed-mcp"
    if primary.exists() or primary.is_symlink():
        if primary.is_symlink() or not primary.is_dir():
            raise FileNotFoundError(f"Feed v2 数据目录不存在或不安全: {primary}")
        if not primary.resolve().is_relative_to(root):
            raise FileNotFoundError(f"Feed v2 数据目录越界: {primary}")
        candidates.append((primary, "mcp/feed-mcp"))

    if backups_root.is_symlink():
        raise FileNotFoundError(f"Feed v2 备份目录不安全: {backups_root}")
    if backups_root.is_dir():
        backups = sorted(
            backups_root.glob(_BACKUP_GLOB),
            key=lambda item: item.parent.name,
            reverse=True,
        )
        for backup in backups:
            if backup.is_symlink() or not backup.is_dir():
                raise FileNotFoundError(f"Feed v2 备份目录不存在或不安全: {backup}")
            if not backup.resolve().is_relative_to(root):
                raise FileNotFoundError(f"Feed v2 备份目录越界: {backup}")
            relative = backup.relative_to(root).as_posix()
            candidates.append((backup, relative))
    return tuple(candidates)


def _has_data_file(source: Path) -> bool:
    return any(
        (source / name).exists() or (source / name).is_symlink()
        for name in _DATA_FILES
    )


def _select_source(workspace: Path) -> tuple[Path, str]:
    """选择第一个含有 v2 文件的 primary 或最新 backup。"""

    for source, relative in _source_candidates(workspace):
        if _has_data_file(source):
            return source, relative
    raise FileNotFoundError(
        "Feed v2 primary 与 backups 都没有可迁移文件"
    )


def _stage_files(
    source: Path,
    source_relative: str,
    staging: Path,
) -> tuple[dict[str, object], ...]:
    """复制选定 v2 源的全部数据文件并返回内容 receipt。"""

    entries: list[dict[str, object]] = []
    for name in _DATA_FILES:
        source_file = source / name
        source_name = f"{source_relative}/{name}"
        if not source_file.exists() and not source_file.is_symlink():
            entries.append(
                {
                    "name": name,
                    "status": "source_missing",
                    "source": source_name,
                }
            )
            continue
        if source_file.is_symlink() or not source_file.is_file():
            raise ValueError(f"Feed v2 数据不是普通文件: {source_file}")
        integrity: str | None = None
        if _is_sqlite_file(source_file):
            integrity = _copy_sqlite(source_file, staging / name)
        else:
            shutil.copy2(source_file, staging / name)
        entry: dict[str, object] = {
            "name": name,
            "status": "staged",
            "source": source_name,
            "sha256": _digest(staging / name),
            "size": (staging / name).stat().st_size,
        }
        if integrity is not None:
            entry["integrity"] = integrity
            entry["sqlite_integrity"] = integrity
        entries.append(entry)
    return tuple(entries)


def _record_target_file(entry: dict[str, object], destination: Path) -> None:
    entry["sha256"] = _digest(destination)
    entry["size"] = destination.stat().st_size
    if _is_sqlite_file(destination):
        integrity = _sqlite_integrity(destination)
        entry["integrity"] = integrity
        entry["sqlite_integrity"] = integrity


def _validate_targets(target: Path, entries: tuple[dict[str, object], ...]) -> None:
    """发布前拒绝覆盖不同内容，并收束崩溃留下的同内容文件。"""

    for entry in entries:
        name = str(entry["name"])
        destination = target / name
        if destination.is_symlink():
            raise ValueError(f"Feed v3 目标不得是符号链接: {destination}")
        if entry["status"] == "source_missing":
            if not destination.exists():
                continue
            if not destination.is_file():
                raise FileExistsError(f"Feed v3 目标不是普通文件: {destination}")
            entry["status"] = "target_only"
            _record_target_file(entry, destination)
            continue
        if not destination.exists():
            entry["status"] = "copied"
            continue
        if not destination.is_file():
            raise FileExistsError(f"Feed v3 目标不是普通文件: {destination}")
        raw_size = entry.get("size")
        raw_digest = entry.get("sha256")
        if not isinstance(raw_size, int) or isinstance(raw_size, bool):
            raise ValueError(f"Feed migration entry 缺少文件大小: {destination}")
        if not isinstance(raw_digest, str):
            raise ValueError(f"Feed migration entry 缺少文件 hash: {destination}")
        expected_size = raw_size
        expected_digest = raw_digest
        if (
            destination.stat().st_size != expected_size
            or _digest(destination) != expected_digest
        ):
            raise FileExistsError(f"Feed v3 目标已存在且内容不同: {destination}")
        entry["status"] = "verified"
        if _is_sqlite_file(destination):
            integrity = _sqlite_integrity(destination)
            if entry.get("integrity") != integrity:
                raise sqlite3.DatabaseError(
                    f"Feed SQLite integrity receipt 不一致: {destination}"
                )

    if all(entry["status"] == "source_missing" for entry in entries):
        raise FileNotFoundError("Feed v2 与 v3 数据目录都没有可迁移文件")


def _publish(
    staging: Path,
    target: Path,
    entries: tuple[dict[str, object], ...],
    receipt: dict[str, object],
) -> None:
    """发布本事务创建的文件，失败时只回滚本事务新增文件。"""

    published: list[Path] = []
    receipt_path = target / _RECEIPT
    try:
        for entry in entries:
            name = str(entry["name"])
            destination = target / name
            if entry["status"] != "copied":
                continue
            published.append(destination)
            os.replace(staging / name, destination)
        staged_receipt = staging / _RECEIPT
        staged_receipt.write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        published.append(receipt_path)
        os.replace(staged_receipt, receipt_path)
    except BaseException:
        for path in reversed(published):
            path.unlink(missing_ok=True)
        raise


def _remove_stale_staging(workspace: Path) -> None:
    """清理上次进程崩溃遗留且未发布的 Feed staging。"""

    root = workspace / "plugin-data"
    if root.is_symlink():
        raise ValueError(f"Feed plugin-data 目录不得是符号链接: {root}")
    if not root.is_dir():
        return
    for path in root.glob(".feed-v2-migrate-*"):
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)


def _validate_receipt_file(
    item: object,
    *,
    target: Path,
    source_relative: str,
) -> None:
    if not isinstance(item, dict):
        raise ValueError("Feed migration receipt 文件记录无效")
    name = item.get("name")
    status = item.get("status")
    if name not in _DATA_FILES or status not in {"source_missing", *_TARGET_STATUSES}:
        raise ValueError("Feed migration receipt 文件记录无效")
    if item.get("source") != f"{source_relative}/{name}":
        raise ValueError("Feed migration receipt recovery source 无效")
    destination = target / str(name)
    if status == "source_missing":
        if destination.exists() or destination.is_symlink():
            raise ValueError(f"Feed migration receipt 目标漂移: {destination}")
        return
    digest = item.get("sha256")
    size = item.get("size")
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
        or not isinstance(size, int)
        or isinstance(size, bool)
        or size < 0
    ):
        raise ValueError("Feed migration receipt 文件证据无效")
    if destination.is_symlink() or not destination.is_file():
        raise ValueError(f"Feed migration receipt 目标缺失: {destination}")
    if destination.stat().st_size != size or _digest(destination) != digest:
        raise ValueError(f"Feed migration receipt 目标内容漂移: {destination}")
    if _is_sqlite_file(destination):
        if item.get("integrity") != "ok" or item.get("sqlite_integrity") != "ok":
            raise ValueError("Feed migration receipt 缺少 SQLite integrity")
        _sqlite_integrity(destination)


def _has_valid_receipt(
    path: Path,
    *,
    target: Path,
    marketplace: str,
    source_relative: str,
) -> bool:
    """严格复核已有迁移 receipt 与正式 target 的内容。"""

    if not path.exists() and not path.is_symlink():
        return False
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Feed migration receipt 不是普通文件: {path}")
    value: Any = json.loads(path.read_text(encoding="utf-8"))
    files = value.get("files") if isinstance(value, dict) else None
    expected_target = f"plugin-data/feed-{marketplace}"
    expected_recovery = {"kind": "retained_source", "path": source_relative}
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or value.get("source") != source_relative
        or value.get("target") != expected_target
        or value.get("recovery") != expected_recovery
        or not isinstance(files, list)
        or [item.get("name") for item in files if isinstance(item, dict)]
        != list(_DATA_FILES)
    ):
        raise ValueError(f"Feed migration receipt 无效: {path}")
    for item in files:
        _validate_receipt_file(
            item,
            target=target,
            source_relative=source_relative,
        )
    return True


def migrate_v2_data(*, workspace: Path, marketplace: str) -> Path:
    """持有 workspace 独占锁迁移 Feed 数据并写最终 receipt。"""

    workspace = workspace.expanduser().resolve()
    lock = WorkspaceInstanceLock(workspace)
    lock.acquire()
    try:
        return _migrate_locked(workspace=workspace, marketplace=marketplace)
    finally:
        lock.release()


def _migrate_locked(*, workspace: Path, marketplace: str) -> Path:
    """在 workspace 独占区间准备、校验并发布一次迁移。"""

    if not marketplace or not marketplace.replace("-", "").isalnum():
        raise ValueError(f"Feed marketplace 无效: {marketplace!r}")
    source, source_relative = _select_source(workspace)
    target = workspace / "plugin-data" / f"feed-{marketplace}"
    validate_workspace_plugin_data_path(target, workspace)
    _remove_stale_staging(workspace)
    receipt_path = target / _RECEIPT
    if _has_valid_receipt(
        receipt_path,
        target=target,
        marketplace=marketplace,
        source_relative=source_relative,
    ):
        return receipt_path

    staging = workspace / "plugin-data" / f".feed-v2-migrate-{uuid.uuid4().hex}"
    created_target = not target.exists()
    ensure_workspace_plugin_data_dir(staging, workspace)
    try:
        entries = _stage_files(source, source_relative, staging)
        ensure_workspace_plugin_data_dir(target, workspace)
        _validate_targets(target, entries)
        receipt: dict[str, object] = {
            "schema_version": 1,
            "source": source_relative,
            "target": f"plugin-data/feed-{marketplace}",
            "recovery": {
                "kind": "retained_source",
                "path": source_relative,
            },
            "files": entries,
        }
        _publish(staging, target, entries, receipt)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
        if created_target and target.is_dir() and not any(target.iterdir()):
            target.rmdir()
    return receipt_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--marketplace", default="github")
    args = parser.parse_args()
    print(migrate_v2_data(workspace=args.workspace, marketplace=args.marketplace))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
