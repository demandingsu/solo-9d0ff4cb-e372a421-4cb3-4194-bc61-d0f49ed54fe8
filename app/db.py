"""SQLite 连接管理与建表脚本。

设计要点：
- WAL 模式 + busy_timeout，读写并发不互相阻塞；
- 写事务一律 BEGIN IMMEDIATE，配合条件更新保证并发确认的原子性；
- 每个请求一个连接，异常回滚，正常结束提交。
"""
from __future__ import annotations

import sqlite3
import threading
from typing import Iterator

from .config import DB_PATH

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS casts (
    cast_id            TEXT PRIMARY KEY,
    current_version_id INTEGER,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    FOREIGN KEY (current_version_id) REFERENCES raw_versions(id)
);

CREATE TABLE IF NOT EXISTS raw_versions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    cast_id        TEXT NOT NULL,
    version_no     INTEGER NOT NULL,
    status         TEXT NOT NULL CHECK (status IN ('draft','published','superseded')),
    file_sha256    TEXT NOT NULL,
    filename       TEXT,
    row_count      INTEGER NOT NULL,
    created_at     TEXT NOT NULL,
    published_at   TEXT,
    superseded_at  TEXT,
    UNIQUE (cast_id, version_no),
    FOREIGN KEY (cast_id) REFERENCES casts(cast_id)
);

CREATE TABLE IF NOT EXISTS raw_rows (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id  INTEGER NOT NULL,
    row_index   INTEGER NOT NULL,           -- 原始 CSV 数据行号（1 起）
    cast_id     TEXT NOT NULL,
    sample_no   INTEGER NOT NULL,
    depth       REAL NOT NULL,
    temperature REAL,                        -- 空值=缺测
    salinity    REAL,                        -- 空值=缺测
    UNIQUE (version_id, row_index),
    FOREIGN KEY (version_id) REFERENCES raw_versions(id)
);

CREATE TABLE IF NOT EXISTS profile_bins (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id      INTEGER NOT NULL,
    depth_bin       INTEGER NOT NULL,        -- floor(depth)，整米段
    row_count       INTEGER NOT NULL,
    temp_mean       REAL,
    salinity_mean   REAL,
    temp_count      INTEGER NOT NULL,
    salinity_count  INTEGER NOT NULL,
    UNIQUE (version_id, depth_bin),
    FOREIGN KEY (version_id) REFERENCES raw_versions(id)
);

CREATE TABLE IF NOT EXISTS excluded_rows (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id  INTEGER NOT NULL,
    raw_row_id  INTEGER NOT NULL,
    reason_code TEXT NOT NULL,               -- upcast / repeated_depth
    reason      TEXT NOT NULL,
    detail      TEXT,
    UNIQUE (version_id, raw_row_id),
    FOREIGN KEY (version_id) REFERENCES raw_versions(id),
    FOREIGN KEY (raw_row_id) REFERENCES raw_rows(id)
);

CREATE TABLE IF NOT EXISTS missing_bins (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id INTEGER NOT NULL,
    depth_bin  INTEGER NOT NULL,
    UNIQUE (version_id, depth_bin),
    FOREIGN KEY (version_id) REFERENCES raw_versions(id)
);

CREATE INDEX IF NOT EXISTS idx_raw_rows_version ON raw_rows(version_id);
CREATE INDEX IF NOT EXISTS idx_versions_cast ON raw_versions(cast_id);
CREATE INDEX IF NOT EXISTS idx_excluded_version ON excluded_rows(version_id);
"""

# 进程内写锁：同一容器单进程下串行化写事务，数据库条件更新仍是最终防线。
write_lock = threading.RLock()


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        DB_PATH, timeout=30, check_same_thread=False, isolation_level=None
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db() -> None:
    conn = _connect()
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def get_conn() -> Iterator[sqlite3.Connection]:
    """FastAPI 依赖：请求级连接，出错回滚，否则提交。"""
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
