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

-- 瓶样批次（整批更正：同投放重传即新批次，旧批次立即 superseded）
CREATE TABLE IF NOT EXISTS bottle_batches (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    cast_id       TEXT NOT NULL,
    batch_no      INTEGER NOT NULL,            -- 瓶样批次号（投放内自增）
    tolerance     REAL NOT NULL,               -- 本批平行瓶盐度容差
    file_sha256   TEXT NOT NULL,
    filename      TEXT,
    row_count     INTEGER NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('active','superseded')),
    created_at    TEXT NOT NULL,
    superseded_at TEXT,
    UNIQUE (cast_id, batch_no),
    FOREIGN KEY (cast_id) REFERENCES casts(cast_id)
);

CREATE TABLE IF NOT EXISTS bottle_rows (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id     INTEGER NOT NULL,
    row_index    INTEGER NOT NULL,             -- 瓶样 CSV 数据行号（1 起）
    bottle_no    TEXT NOT NULL,                -- 瓶号
    sample_no    INTEGER NOT NULL,             -- 击发采样序号
    lab_salinity REAL NOT NULL,                -- 实验室盐度（必填有限数）
    UNIQUE (batch_id, row_index),
    UNIQUE (batch_id, bottle_no),
    FOREIGN KEY (batch_id) REFERENCES bottle_batches(id)
);

-- 交接单：一次生成对应“一个已提交瓶样版 + 一个已发布原始版”
CREATE TABLE IF NOT EXISTS handover_sheets (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    cast_id          TEXT NOT NULL,
    batch_id         INTEGER NOT NULL,
    raw_version_id   INTEGER NOT NULL,
    tolerance        REAL NOT NULL,
    is_current       INTEGER NOT NULL DEFAULT 0,
    delivered_count  INTEGER NOT NULL,
    pending_count    INTEGER NOT NULL,         -- 平行组待核瓶数
    unlinked_count   INTEGER NOT NULL,         -- 无法关联瓶数
    created_at       TEXT NOT NULL,
    superseded_at    TEXT,
    invalidate_reason TEXT,                    -- raw_reupload / bottle_correction
    UNIQUE (batch_id, raw_version_id),
    FOREIGN KEY (batch_id) REFERENCES bottle_batches(id),
    FOREIGN KEY (raw_version_id) REFERENCES raw_versions(id)
);

CREATE TABLE IF NOT EXISTS handover_rows (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    sheet_id        INTEGER NOT NULL,
    row_index       INTEGER NOT NULL,
    bottle_no       TEXT NOT NULL,
    sample_no       INTEGER NOT NULL,
    lab_salinity    REAL NOT NULL,
    linked          INTEGER NOT NULL,          -- 1=已关联原始行 0=无法关联
    raw_row_id      INTEGER,
    raw_row_index   INTEGER,                   -- 原始 CSV 行号（依据）
    depth           REAL,                      -- 原始行深度（回升行也取原始行）
    ctd_temperature REAL,
    ctd_salinity    REAL,
    is_upcast       INTEGER NOT NULL DEFAULT 0,-- 对应原始行是否为回升/重复深度行
    upcast_reason   TEXT,
    parallel_size   INTEGER NOT NULL,          -- 同序号平行瓶数
    salinity_spread REAL NOT NULL,             -- 同组实验室盐度极差
    group_status    TEXT NOT NULL CHECK (group_status IN ('ok','pending')),
    deliverable     INTEGER NOT NULL,
    unlinked_reason TEXT,
    FOREIGN KEY (sheet_id) REFERENCES handover_sheets(id),
    FOREIGN KEY (raw_row_id) REFERENCES raw_rows(id)
);

CREATE INDEX IF NOT EXISTS idx_raw_rows_version ON raw_rows(version_id);
CREATE INDEX IF NOT EXISTS idx_versions_cast ON raw_versions(cast_id);
CREATE INDEX IF NOT EXISTS idx_excluded_version ON excluded_rows(version_id);
CREATE INDEX IF NOT EXISTS idx_bottle_batches_cast ON bottle_batches(cast_id);
CREATE INDEX IF NOT EXISTS idx_bottle_rows_batch ON bottle_rows(batch_id);
CREATE INDEX IF NOT EXISTS idx_handover_cast_current ON handover_sheets(cast_id, is_current);
CREATE INDEX IF NOT EXISTS idx_handover_rows_sheet ON handover_rows(sheet_id);
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
