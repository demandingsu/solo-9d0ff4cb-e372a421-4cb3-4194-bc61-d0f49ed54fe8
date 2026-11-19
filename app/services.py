"""业务服务层：版本化入库、发布、追溯与导出。

版本生命周期
    draft --(确认发布)-----------> published --(新版重传)--> superseded
      |                              ^
      +--(新版重传，未发布也被替换)--> superseded

关键一致性规则
- 上传在单个 BEGIN IMMEDIATE 事务内完成：原始行 + 剖面 + 排除行 + 缺测段
  要么全部落库，要么整体回滚，不存在半版；
- 重传新版即把旧现行版置为 superseded、清空 casts.current_version_id，
  旧剖面数据保留可追溯，但“现行下载”立即停止，直到新版确认发布；
- 发布只允许确认该投放最新的原始版（max version id），旧预览确认报 409；
- 发布用条件 UPDATE 兜底，并发下也不会产生错误现行结果；
- 整批校验在开事务之前完成，拒收不改动任何既有数据。
"""
from __future__ import annotations

import csv
import hashlib
import io
import math
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

from .db import write_lock
from .parsing import BatchRejected, ParsedBatch, parse_csv
from .profiling import ProfileResult, compute_profile


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- 入库

def upload_raw(conn: sqlite3.Connection, raw: bytes, filename: str) -> dict[str, Any]:
    # 事务外解析：坏行/冲突直接拒收，库内数据不受影响
    batch: ParsedBatch = parse_csv(raw)
    digest = hashlib.sha256(raw).hexdigest()
    now = _now()

    with write_lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            # 同内容重复上传直接拒绝，避免制造无意义版本
            dup = conn.execute(
                "SELECT id, version_no FROM raw_versions WHERE cast_id=? AND file_sha256=?",
                (batch.cast_id, digest),
            ).fetchone()
            if dup is not None:
                conn.rollback()
                raise _http(
                    409,
                    f"该文件与第 {dup['version_no']} 版内容完全相同（sha256 一致），无需重传",
                )

            cast = conn.execute(
                "SELECT * FROM casts WHERE cast_id=?", (batch.cast_id,)
            ).fetchone()
            if cast is None:
                conn.execute(
                    "INSERT INTO casts(cast_id, current_version_id, created_at, updated_at)"
                    " VALUES (?, NULL, ?, ?)",
                    (batch.cast_id, now, now),
                )
                version_no = 1
                previous_version_id: Optional[int] = None
            else:
                row = conn.execute(
                    "SELECT COALESCE(MAX(version_no), 0) AS m FROM raw_versions WHERE cast_id=?",
                    (batch.cast_id,),
                ).fetchone()
                version_no = row["m"] + 1
                previous_version_id = cast["current_version_id"]

            cur = conn.execute(
                "INSERT INTO raw_versions(cast_id, version_no, status, file_sha256,"
                " filename, row_count, created_at) VALUES (?, ?, 'draft', ?, ?, ?, ?)",
                (batch.cast_id, version_no, digest, filename, len(batch.rows), now),
            )
            version_id = cur.lastrowid

            # 原始行完整保留（含缺测 NULL），并记录原始 CSV 行号
            row_ids: dict[int, int] = {}
            for r in batch.rows:
                rid = conn.execute(
                    "INSERT INTO raw_rows(version_id, row_index, cast_id, sample_no,"
                    " depth, temperature, salinity) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (version_id, r.row_index, r.cast_id, r.sample_no, r.depth,
                     r.temperature, r.salinity),
                ).lastrowid
                row_ids[r.row_index] = rid

            profile = compute_profile(batch.rows)
            _persist_profile(conn, version_id, row_ids, profile)

            # 整版更正：旧版（无论 draft 还是 published）立即 superseded，
            # 清空现行指针——旧剖面可追溯，但停止现行下载
            old = conn.execute(
                "SELECT id, status FROM raw_versions WHERE cast_id=? AND id != ?",
                (batch.cast_id, version_id),
            ).fetchall()
            for v in old:
                if v["status"] != "superseded":
                    conn.execute(
                        "UPDATE raw_versions SET status='superseded', superseded_at=? WHERE id=?",
                        (now, v["id"]),
                    )
            conn.execute(
                "UPDATE casts SET current_version_id=NULL, updated_at=? WHERE cast_id=?",
                (now, batch.cast_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    return {
        "cast_id": batch.cast_id,
        "version_id": version_id,
        "version_no": version_no,
        "status": "draft",
        "filename": filename,
        "file_sha256": digest,
        "row_count": len(batch.rows),
        "retained_count": profile.retained_count,
        "excluded_count": profile.excluded_count,
        "previous_version_id": previous_version_id,
        "created_at": now,
    }


def _persist_profile(
    conn: sqlite3.Connection,
    version_id: int,
    row_ids: dict[int, int],
    profile: ProfileResult,
) -> None:
    for b in profile.bins:
        conn.execute(
            "INSERT INTO profile_bins(version_id, depth_bin, row_count, temp_mean,"
            " salinity_mean, temp_count, salinity_count) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (version_id, b.depth_bin, b.row_count, b.temp_mean, b.salinity_mean,
             b.temp_count, b.salinity_count),
        )
    for b in profile.missing_bins:
        conn.execute(
            "INSERT INTO missing_bins(version_id, depth_bin) VALUES (?, ?)",
            (version_id, b),
        )
    for ex in profile.exclusions:
        conn.execute(
            "INSERT INTO excluded_rows(version_id, raw_row_id, reason_code, reason, detail)"
            " VALUES (?, ?, ?, ?, ?)",
            (version_id, row_ids[ex.row_index], ex.reason_code, ex.reason, ex.detail),
        )


# ---------------------------------------------------------------- 发布

class StalePreview(Exception):
    pass


def publish_version(conn: sqlite3.Connection, version_id: int) -> dict[str, Any]:
    with write_lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            version = conn.execute(
                "SELECT * FROM raw_versions WHERE id=?", (version_id,)
            ).fetchone()
            if version is None:
                conn.rollback()
                raise _http(404, f"原始版 {version_id} 不存在")

            # 只允许确认该投放最新的原始版：预览后原始版被换则拒绝确认
            latest = conn.execute(
                "SELECT MAX(id) AS m FROM raw_versions WHERE cast_id=?",
                (version["cast_id"],),
            ).fetchone()["m"]
            if latest != version_id:
                conn.rollback()
                raise StalePreview(
                    f"预览的第 {version['version_no']} 版已不是 {version['cast_id']} "
                    f"的最新原始版（最新版本 id={latest}），请重新预览最新版后再确认"
                )

            if version["status"] == "published":
                # 幂等：已发布且仍为最新，直接返回
                conn.rollback()
                return _version_summary(conn, version_id)

            # 条件更新兜底：并发情况下只有一个事务能把 draft 改成 published
            cur = conn.execute(
                "UPDATE raw_versions SET status='published', published_at=? "
                "WHERE id=? AND status='draft'",
                (_now(), version_id),
            )
            if cur.rowcount != 1:
                conn.rollback()
                raise _http(409, "发布冲突：该版本状态已被其他操作改变，请刷新后重试")

            now = _now()
            conn.execute(
                "UPDATE casts SET current_version_id=?, updated_at=? WHERE cast_id=?",
                (version_id, now, version["cast_id"]),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    return _version_summary(conn, version_id)


# ---------------------------------------------------------------- 查询

def list_casts(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT c.cast_id, c.current_version_id, c.updated_at,
               (SELECT COUNT(*) FROM raw_versions v WHERE v.cast_id=c.cast_id) AS version_count,
               v.version_no AS current_version_no, v.status AS current_status
        FROM casts c
        LEFT JOIN raw_versions v ON v.id = c.current_version_id
        ORDER BY c.updated_at DESC
        """
    ).fetchall()
    return [dict(r) for r in rows]


def list_versions(conn: sqlite3.Connection, cast_id: str) -> list[dict[str, Any]]:
    if conn.execute("SELECT 1 FROM casts WHERE cast_id=?", (cast_id,)).fetchone() is None:
        raise _http(404, f"投放 {cast_id} 不存在")
    rows = conn.execute(
        "SELECT * FROM raw_versions WHERE cast_id=? ORDER BY version_no", (cast_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def _version_summary(conn: sqlite3.Connection, version_id: int) -> dict[str, Any]:
    v = conn.execute("SELECT * FROM raw_versions WHERE id=?", (version_id,)).fetchone()
    if v is None:
        raise _http(404, f"原始版 {version_id} 不存在")
    d = dict(v)
    d["is_current"] = (
        conn.execute(
            "SELECT current_version_id FROM casts WHERE cast_id=?", (v["cast_id"],)
        ).fetchone()["current_version_id"]
        == version_id
    )
    return d


def get_version_detail(conn: sqlite3.Connection, version_id: int) -> dict[str, Any]:
    d = _version_summary(conn, version_id)
    d["bins"] = [
        {**dict(r), "all_missing": r["temp_count"] == 0 and r["salinity_count"] == 0}
        for r in conn.execute(
            "SELECT * FROM profile_bins WHERE version_id=? ORDER BY depth_bin", (version_id,)
        )
    ]
    d["missing_bins"] = [
        r["depth_bin"]
        for r in conn.execute(
            "SELECT depth_bin FROM missing_bins WHERE version_id=? ORDER BY depth_bin",
            (version_id,),
        )
    ]
    d["excluded_rows"] = [
        dict(r)
        for r in conn.execute(
            """
            SELECT e.id, e.raw_row_id, e.reason_code, e.reason, e.detail,
                   r.row_index, r.sample_no, r.depth, r.temperature, r.salinity
            FROM excluded_rows e JOIN raw_rows r ON r.id = e.raw_row_id
            WHERE e.version_id=? ORDER BY r.row_index
            """,
            (version_id,),
        )
    ]
    d["raw_rows"] = [
        dict(r)
        for r in conn.execute(
            "SELECT row_index, sample_no, depth, temperature, salinity"
            " FROM raw_rows WHERE version_id=? ORDER BY row_index",
            (version_id,),
        )
    ]
    # 保留行（下行剖面读数）
    excluded_ids = {r["raw_row_id"] for r in d["excluded_rows"]}
    d["retained_rows"] = [
        r for r in d["raw_rows"] if r["row_index"] not in excluded_ids
    ]
    return d


def get_current_version(conn: sqlite3.Connection, cast_id: str) -> Optional[dict[str, Any]]:
    cast = conn.execute(
        "SELECT current_version_id FROM casts WHERE cast_id=?", (cast_id,)
    ).fetchone()
    if cast is None:
        raise _http(404, f"投放 {cast_id} 不存在")
    if cast["current_version_id"] is None:
        return None
    return get_version_detail(conn, cast["current_version_id"])


# ---------------------------------------------------------------- 导出

def export_profile_csv(conn: sqlite3.Connection, cast_id: str) -> tuple[str, str]:
    """现行剖面导出。返回 (文件名, csv 文本)。无现行版时抛 409。"""
    detail = get_current_version(conn, cast_id)
    if detail is None:
        raise _http(
            409,
            f"投放 {cast_id} 暂无现行剖面：原始版更正后尚未确认发布，"
            "可在版本历史中追溯旧版，但不提供现行下载",
        )

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["记录类型", "投放号", "版本号", "版本id", "文件sha256", "原始行号",
                "采样序号", "深度段(米)", "深度", "水温", "盐度",
                "段内读数计数", "水温有效数", "盐度有效数",
                "水温均值", "盐度均值", "排除原因码", "排除说明", "排除细节"])
    w.writerow(["META", cast_id, f"v{detail['version_no']}", detail["id"],
                detail["file_sha256"], "", "", "", "", "", "", "", "", "", "", "",
                "", f"现行发布时间 {detail['published_at']}", ""])

    # 剖面段（含缺测段）
    for b in detail["bins"]:
        w.writerow([
            "缺测段" if b["depth_bin"] in detail["missing_bins"] else "剖面段",
            cast_id, f"v{detail['version_no']}", detail["id"], detail["file_sha256"],
            "", "", b["depth_bin"], "", "", "",
            b["row_count"], b["temp_count"], b["salinity_count"],
            _fmt(b["temp_mean"]), _fmt(b["salinity_mean"]), "", "", "",
        ])

    # 保留读数：带原始版依据（原始行号 + 版本 sha256）
    for r in detail["retained_rows"]:
        w.writerow([
            "保留读数", cast_id, f"v{detail['version_no']}", detail["id"], detail["file_sha256"],
            r["row_index"], r["sample_no"], math.floor(r["depth"]),
            r["depth"], _fmt(r["temperature"]), _fmt(r["salinity"]),
            "", "", "", "", "", "", "",
        ])

    # 被排除的回升/重复深度行
    for e in detail["excluded_rows"]:
        w.writerow([
            "排除行", cast_id, f"v{detail['version_no']}", detail["id"], detail["file_sha256"],
            e["row_index"], e["sample_no"], "",
            e["depth"], _fmt(e["temperature"]), _fmt(e["salinity"]),
            "", "", "", "", "", e["reason_code"], e["reason"], e["detail"],
        ])

    fname = f"profile_{cast_id}_v{detail['version_no']}.csv"
    return fname, buf.getvalue()


def export_raw_csv(conn: sqlite3.Connection, version_id: int) -> tuple[str, str]:
    """重建指定版本的原始 CSV（原始行序、原始五列）。"""
    v = conn.execute("SELECT * FROM raw_versions WHERE id=?", (version_id,)).fetchone()
    if v is None:
        raise _http(404, f"原始版 {version_id} 不存在")
    rows = conn.execute(
        "SELECT * FROM raw_rows WHERE version_id=? ORDER BY row_index", (version_id,)
    ).fetchall()

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["投放号", "采样序号", "深度", "水温", "盐度"])
    for r in rows:
        w.writerow([
            r["cast_id"], r["sample_no"], r["depth"],
            "" if r["temperature"] is None else r["temperature"],
            "" if r["salinity"] is None else r["salinity"],
        ])
    return f"raw_{v['cast_id']}_v{v['version_no']}.csv", buf.getvalue()


# ---------------------------------------------------------------- 工具

def _fmt(x: Optional[float]) -> str:
    return "" if x is None else f"{x:.6g}"


def _http(status: int, message: str, errors: Optional[list[str]] = None) -> Exception:
    from fastapi import HTTPException

    detail: dict[str, Any] = {"message": message}
    if errors:
        detail["errors"] = errors
    return HTTPException(status_code=status, detail=detail)
