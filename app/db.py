"""SQLite 持久化层：版本元数据、网格 WKB 与乐观锁发布。

设计要点：
- WAL 模式 + BEGIN IMMEDIATE，保证两个并发发布只有一个能拿到写锁；
- versions.expected_version 为 NULL 表示首次发布（当前无版本）；
- 发布失败（校验冲突、版号冲突、锁超时）时事务回滚，当前版不变；
- 旧版网格整版保留，重启进程后仍可查询。
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from shapely.geometry.base import BaseGeometry
from shapely import wkb

SCHEMA = """
CREATE TABLE IF NOT EXISTS versions (
    version        INTEGER PRIMARY KEY,
    expected_version INTEGER,
    published_at   TEXT NOT NULL,
    watershed_wkb  BLOB NOT NULL,
    grid_count     INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS grids (
    version   INTEGER NOT NULL,
    grid_id   TEXT NOT NULL,
    geom_wkb  BLOB NOT NULL,
    PRIMARY KEY (version, grid_id),
    FOREIGN KEY (version) REFERENCES versions(version)
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class VersionConflict(Exception):
    """携带的所见版号与当前版不一致，发布被拒绝。"""


class VersionNotFound(Exception):
    """请求的版本不存在或尚无已发布版本。"""


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: str, busy_timeout_ms: int = 10_000) -> None:
        self.path = path
        self.busy_timeout_ms = busy_timeout_ms
        # 多线程下串行化写事务，配合 BEGIN IMMEDIATE 保证并发发布只成功一个
        self._write_lock = threading.Lock()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.path, timeout=self.busy_timeout_ms / 1000.0
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        return conn

    def init_schema(self) -> None:
        conn = self._connect()
        try:
            conn.executescript(SCHEMA)
            conn.commit()
        finally:
            conn.close()

    # ---- 读路径 -------------------------------------------------------

    def current_version(self) -> int | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = 'current_version'"
            ).fetchone()
            return int(row["value"]) if row else None
        finally:
            conn.close()

    def list_versions(self) -> list[dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT version, expected_version, published_at, grid_count "
                "FROM versions ORDER BY version"
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def load_snapshot(self, version: int) -> tuple[BaseGeometry, list[tuple[str, BaseGeometry]]]:
        """加载某一版的流域与网格（WKB 反序列化）。"""
        conn = self._connect()
        try:
            vrow = conn.execute(
                "SELECT watershed_wkb FROM versions WHERE version = ?", (version,)
            ).fetchone()
            if vrow is None:
                raise VersionNotFound(f"版本 {version} 不存在")
            watershed = wkb.loads(vrow["watershed_wkb"])
            grows = conn.execute(
                "SELECT grid_id, geom_wkb FROM grids WHERE version = ? ORDER BY grid_id",
                (version,),
            ).fetchall()
            grids = [(row["grid_id"], wkb.loads(row["geom_wkb"])) for row in grows]
            return watershed, grids
        finally:
            conn.close()

    # ---- 写路径 -------------------------------------------------------

    def publish(
        self,
        expected_version: int | None,
        watershed: BaseGeometry,
        grids: list[tuple[str, BaseGeometry]],
    ) -> int:
        """整版发布；版号冲突抛 VersionConflict，任何失败均回滚。"""
        with self._write_lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT value FROM meta WHERE key = 'current_version'"
                ).fetchone()
                current = int(row["value"]) if row else None
                if expected_version != current:
                    conn.rollback()
                    raise VersionConflict(
                        f"所见版号 {expected_version} 与当前版 {current} 不一致"
                    )
                next_version = 1 if current is None else current + 1
                conn.execute(
                    "INSERT INTO versions "
                    "(version, expected_version, published_at, watershed_wkb, grid_count) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        next_version,
                        expected_version,
                        _utcnow_iso(),
                        watershed.wkb,
                        len(grids),
                    ),
                )
                conn.executemany(
                    "INSERT INTO grids (version, grid_id, geom_wkb) VALUES (?, ?, ?)",
                    [
                        (next_version, grid_id, geom.wkb)
                        for grid_id, geom in grids
                    ],
                )
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES('current_version', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(next_version),),
                )
                conn.commit()
                return next_version
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
